// Node.js 版 Web API の E2E テスト。
//
// 前提: tests/e2e_cli.test.js を先に実行していること (プロファイル・テスト DB を再利用する)。
// 実サーバー (express) をサブプロセスで起動して HTTP 経由で検証する。

import assert from "node:assert";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import pg from "pg";

const { Client } = pg;

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const BIN = path.join(ROOT, "bin", "pgbackup.js");
const PORT = Number(process.env.E2E_WEB_PORT ?? 8561);
const PORT_A = Number(process.env.E2E_PORT_A ?? 55432);
const USER = process.env.E2E_USER ?? "postgres";
const BASE = `http://127.0.0.1:${PORT}`;

let passed = 0, failed = 0;

async function api(pathname, { body = null, method = null, raw = false } = {}) {
  const res = await fetch(BASE + pathname, {
    method: method ?? (body !== null ? "POST" : "GET"),
    headers: body !== null ? { "Content-Type": "application/json" } : {},
    body: body !== null ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { detail = (await res.json()).detail ?? detail; } catch { /* 非JSON */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return raw ? res.text() : res.json();
}

async function waitJob(jobId, timeoutMs = 120_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const job = await api(`/api/jobs/${jobId}`);
    if (job.status !== "running") {
      assert.strictEqual(job.status, "done", `ジョブ失敗: ${job.error}\n${(job.log || []).join("\n")}`);
      return job;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`ジョブ ${jobId} がタイムアウト`);
}

async function dropDb(name) {
  const c = new Client({ host: "localhost", port: PORT_A, user: USER, database: "postgres" });
  await c.connect();
  try {
    await c.query(
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
      [name]
    );
    await c.query(`DROP DATABASE IF EXISTS "${name}"`);
  } finally {
    await c.end();
  }
}

async function check(name, fn) {
  try {
    await fn();
    passed++;
    console.log(`  PASS  ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL  ${name}`);
    console.log(String(e.stack || e).split("\n").slice(0, 6).join("\n"));
  }
}

async function main() {
  assert.ok(process.env.PGBACKUP_HOME, "PGBACKUP_HOME を設定してください");
  await dropDb("inventory_web_restore");

  const server = spawn(process.execPath, [BIN, "web", "--port", String(PORT)], {
    cwd: ROOT, stdio: "ignore", env: process.env,
  });
  try {
    let up = false;
    for (let i = 0; i < 60; i++) {
      try {
        await api("/api/profiles");
        up = true;
        break;
      } catch {
        await new Promise((r) => setTimeout(r, 500));
      }
    }
    assert.ok(up, "Web サーバーが起動しませんでした");

    await check("GET /: UI が返る", async () => {
      const html = await api("/", { raw: true });
      assert.ok(html.includes("pgbackup") && html.includes("バックアップ"));
    });

    await check("GET /api/profiles (パスワードは伏せられる)", async () => {
      const profiles = await api("/api/profiles");
      const names = new Set(profiles.map((p) => p.name));
      assert.ok(names.has("local") && names.has("remote"));
      for (const p of profiles) assert.ok(p.password === null || p.password === "********");
    });

    await check("POST /api/profiles/{name}/test", async () => {
      const r = await api("/api/profiles/local/test", { body: {} });
      assert.ok(r.ok && r.server_version);
    });

    await check("GET /api/databases?profile=local", async () => {
      const names = new Set((await api("/api/databases?profile=local")).map((d) => d.name));
      assert.ok(names.has("shop_db") && names.has("inventory_db"));
    });

    await check("GET structure: スキーマ・テーブル・データ型内訳", async () => {
      const s = await api("/api/structure?profile=local&db=shop_db");
      const schemas = new Set(s.schemas.map((sc) => sc.name));
      assert.ok(schemas.has("public") && schemas.has("sales"));
      const types = new Set(s.datatype_summary.map((d) => d.data_type));
      assert.ok(types.has("jsonb") && types.has("bytea") && types.has("numeric(12,2)"));
    });

    await check("GET table detail: カラム・PK・インデックス", async () => {
      const d = await api("/api/table?profile=local&db=shop_db&schema=public&table=customers");
      const cols = Object.fromEntries(d.columns.map((c) => [c.name, c]));
      assert.strictEqual(cols.id.is_primary_key, true);
      assert.strictEqual(cols.profile.data_type, "jsonb");
      assert.ok(d.indexes.some((i) => i.name.includes("customers_email_key")));
    });

    await check("GET sample: データのサンプルと総行数", async () => {
      const s = await api("/api/sample?profile=local&db=shop_db&schema=public&table=customers&limit=5");
      assert.strictEqual(s.total_rows, 500);
      assert.strictEqual(s.rows.length, 5);
      assert.ok(s.columns.includes("name"));
      assert.strictEqual(s.approx, false);
    });

    await check("POST /api/backup (file) + ジョブ完了", async () => {
      const r = await api("/api/backup", {
        body: { profile: "local", dbs: ["inventory_db"], mode: "file", fmt: "custom", note: "web経由" },
      });
      const job = await waitJob(r.job_id);
      assert.ok(job.result[0].version_id.startsWith("local/inventory_db@v"));
    });

    await check("GET /api/versions に web バックアップが載る", async () => {
      const rows = await api("/api/versions?profile=local&db=inventory_db");
      assert.ok(rows.some((row) => row.note === "web経由"));
    });

    await check("POST /api/restore + データ検証", async () => {
      const rows = await api("/api/versions?profile=local&db=inventory_db");
      const vid = rows.filter((row) => row.note === "web経由").at(-1).version_id;
      const r = await api("/api/restore", {
        body: { version_id: vid, dest: "local", db: "inventory_web_restore" },
      });
      await waitJob(r.job_id);
      const c = new Client({ host: "localhost", port: PORT_A, user: USER, database: "inventory_web_restore" });
      await c.connect();
      try {
        assert.strictEqual(Number((await c.query("SELECT count(*) AS n FROM items")).rows[0].n), 100);
      } finally {
        await c.end();
      }
    });

    await check("GET /api/archive にアーカイブが載る", async () => {
      const rows = await api("/api/archive?profile=remote");
      assert.ok(rows.some((r) => r.source_db === "shop_db"));
    });

    await check("GET /api/history", async () => {
      const rows = await api("/api/history?limit=50");
      assert.ok(rows.length && rows.every((r) => "kind" in r));
    });

    await check("異常系: 存在しないプロファイルは 400", async () => {
      await assert.rejects(api("/api/databases?profile=no_such"), (e) => {
        assert.strictEqual(e.status, 400);
        assert.ok(e.message.includes("no_such"));
        return true;
      });
    });

    await check("異常系: 対象 DB 未指定の backup は 400", async () => {
      await assert.rejects(
        api("/api/backup", { body: { profile: "local", dbs: [], mode: "file" } }),
        (e) => e.status === 400
      );
    });

    await check("異常系: 存在しないジョブは 404", async () => {
      await assert.rejects(api("/api/jobs/nonexistent"), (e) => e.status === 404);
    });
  } finally {
    server.kill();
  }

  console.log(`\n結果: ${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
