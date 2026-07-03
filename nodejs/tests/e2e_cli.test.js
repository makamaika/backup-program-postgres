// Node.js 版 CLI の E2E テスト。
//
// 前提: 2 つの PostgreSQL クラスタが稼働していること (trust 認証)。
//   - クラスタ A (ローカル役):  localhost:$E2E_PORT_A (既定 55432)  shop_db / inventory_db
//   - クラスタ B (リモート役): localhost:$E2E_PORT_B (既定 55433)  analytics_db
// PGBACKUP_HOME を一時ディレクトリに向けて実行すること。
// クラスタの作成は ../../tests/setup_clusters.ps1 を参照。

import assert from "node:assert";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import pg from "pg";

import { DatabaseSync } from "node:sqlite";

const { Client } = pg;

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const BIN = path.join(ROOT, "bin", "pgbackup.js");
const PORT_A = Number(process.env.E2E_PORT_A ?? 55432);
const PORT_B = Number(process.env.E2E_PORT_B ?? 55433);
const USER = process.env.E2E_USER ?? "postgres";
const HOME = process.env.PGBACKUP_HOME;

let passed = 0, failed = 0;

function run(args, { expectFail = false } = {}) {
  const r = spawnSync(process.execPath, [BIN, ...args], {
    cwd: ROOT, encoding: "utf-8", env: process.env,
  });
  const out = (r.stdout || "") + (r.stderr || "");
  if (expectFail) {
    assert.notStrictEqual(r.status, 0, `失敗を期待したが成功した: pgbackup ${args.join(" ")}\n${out}`);
  } else {
    assert.strictEqual(r.status, 0, `pgbackup ${args.join(" ")} (rc=${r.status})\n${out}`);
  }
  return out;
}

async function q(port, db, sql) {
  const c = new Client({ host: "localhost", port, user: USER, database: db });
  await c.connect();
  try {
    return (await c.query(sql)).rows;
  } finally {
    await c.end();
  }
}

async function dropDb(port, name) {
  const c = new Client({ host: "localhost", port, user: USER, database: "postgres" });
  await c.connect();
  try {
    await c.query(
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
      [name]
    );
    await c.query(`DROP DATABASE IF EXISTS "${name.replaceAll('"', '""')}"`);
  } finally {
    await c.end();
  }
}

async function shopCounts(port, db) {
  const customers = Number((await q(port, db, "SELECT count(*) AS n FROM public.customers"))[0].n);
  const orders = Number((await q(port, db, "SELECT count(*) AS n FROM sales.orders"))[0].n);
  return [customers, orders];
}

function catalogLocation(versionId) {
  const db = new DatabaseSync(path.join(HOME, "catalog.db"));
  try {
    const row = db.prepare("SELECT location FROM backups WHERE version_id = ?").get(versionId);
    assert.ok(row, `カタログに ${versionId} がない`);
    return row.location;
  } finally {
    db.close();
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
  assert.ok(HOME, "PGBACKUP_HOME を一時ディレクトリに設定してから実行してください");
  console.log(`PGBACKUP_HOME = ${HOME}`);

  // 再実行に備えたクリーンアップ (バージョン番号を決定的にするためカタログも消す)
  fs.rmSync(path.join(HOME, "catalog.db"), { force: true });
  fs.rmSync(path.join(HOME, "backups"), { recursive: true, force: true });
  fs.rmSync(path.join(HOME, "dup_out.sql"), { force: true });
  fs.rmSync(path.join(HOME, "multi.sql"), { force: true });
  const cleanup = [
    [PORT_A, ["shop_db_copy", "shop_db_arch1", "shop_db_arch2", "shop_db_file",
              "analytics_db", "BACK_UP_DB", "inventory_web_restore"]],
    [PORT_B, ["shop_db", "shop_db_restored", "analytics_copy", "BACK_UP_DB"]],
  ];
  for (const [port, dbs] of cleanup) {
    for (const db of dbs) await dropDb(port, db);
  }

  await check("conn add local/remote", () => {
    run(["conn", "add", "local", "--host", "localhost", "--port", String(PORT_A), "--user", USER, "--overwrite"]);
    run(["conn", "add", "remote", "--host", "localhost", "--port", String(PORT_B), "--user", USER, "--overwrite"]);
  });

  await check("conn test / list", () => {
    assert.match(run(["conn", "test", "local"]), /OK/);
    assert.match(run(["conn", "test", "remote"]), /OK/);
    const out = run(["conn", "list"]);
    assert.ok(out.includes("local") && out.includes("remote"));
  });

  await check("dbs: DB 一覧の表示", () => {
    const out = run(["dbs", "local"]);
    assert.ok(out.includes("shop_db") && out.includes("inventory_db"));
    assert.ok(run(["dbs", "remote"]).includes("analytics_db"));
  });

  await check("inspect: テーブル一覧とデータ型内訳", () => {
    const out = run(["inspect", "local", "shop_db"]);
    assert.ok(out.includes("customers") && out.includes("orders"));
    assert.ok(out.includes("jsonb") && out.includes("bytea"));
  });

  await check("inspect --table --sample: カラム定義とサンプル行", () => {
    const out = run(["inspect", "local", "shop_db", "--table", "customers", "--sample", "3"]);
    assert.ok(out.includes("email") && out.includes("PK"));
    assert.ok(out.includes("総行数: 500"));
  });

  await check("backup file plain (SQL ファイル出力) -> v1", () => {
    const out = run(["backup", "local", "--db", "shop_db", "--mode", "file",
                     "--fmt", "plain", "--note", "初回SQLバックアップ"]);
    assert.ok(out.includes("local/shop_db@v1"));
    const loc = catalogLocation("local/shop_db@v1");
    assert.ok(loc.endsWith(".sql") && fs.existsSync(loc));
  });

  await check("backup file custom -> v2", () => {
    const out = run(["backup", "local", "--db", "shop_db", "--mode", "file", "--fmt", "custom"]);
    assert.ok(out.includes("local/shop_db@v2"));
    const loc = catalogLocation("local/shop_db@v2");
    const head = Buffer.alloc(5);
    const fh = fs.openSync(loc, "r");
    fs.readSync(fh, head, 0, 5, 0);
    fs.closeSync(fh);
    assert.strictEqual(head.toString("latin1"), "PGDMP");
  });

  await check("backup archive (同一サーバーの BACK_UP_DB) -> v3", async () => {
    const out = run(["backup", "local", "--db", "shop_db", "--mode", "archive"]);
    assert.ok(out.includes("local/shop_db@v3"));
    const rows = await q(PORT_A, "BACK_UP_DB", "SELECT version_id FROM pgbackup_archive");
    assert.ok(rows.some((r) => r.version_id === "local/shop_db@v3"));
    const chunks = await q(PORT_A, "BACK_UP_DB", "SELECT count(*) AS n FROM pgbackup_archive_chunk");
    assert.ok(Number(chunks[0].n) >= 1);
  });

  await check("backup archive --dest remote (リモートの BACK_UP_DB) -> v4", async () => {
    const out = run(["backup", "local", "--db", "shop_db", "--mode", "archive",
                     "--dest", "remote", "--note", "リモート保管"]);
    assert.ok(out.includes("local/shop_db@v4"));
    const rows = await q(PORT_B, "BACK_UP_DB", "SELECT version_id FROM pgbackup_archive");
    assert.ok(rows.some((r) => r.version_id === "local/shop_db@v4"));
  });

  await check("clone ローカル -> ローカル (別 DB 名)", async () => {
    run(["backup", "local", "--db", "shop_db", "--mode", "clone",
         "--dest", "local", "--dest-db", "shop_db_copy"]);
    assert.deepStrictEqual(await shopCounts(PORT_A, "shop_db_copy"), [500, 2000]);
  });

  await check("clone ローカル -> リモート", async () => {
    run(["backup", "local", "--db", "shop_db", "--mode", "clone", "--dest", "remote"]);
    assert.deepStrictEqual(await shopCounts(PORT_B, "shop_db"), [500, 2000]);
  });

  await check("clone リモート -> ローカル", async () => {
    run(["backup", "remote", "--db", "analytics_db", "--mode", "clone", "--dest", "local"]);
    const n = Number((await q(PORT_A, "analytics_db", "SELECT count(*) AS n FROM events"))[0].n);
    assert.strictEqual(n, 300);
  });

  await check("clone リモート -> リモート (別 DB 名)", async () => {
    run(["backup", "remote", "--db", "analytics_db", "--mode", "clone",
         "--dest", "remote", "--dest-db", "analytics_copy"]);
    const n = Number((await q(PORT_B, "analytics_copy", "SELECT count(*) AS n FROM events"))[0].n);
    assert.strictEqual(n, 300);
  });

  await check("versions: 4 バージョンが登録されている", () => {
    const out = run(["versions", "--profile", "local", "--db", "shop_db"]);
    for (const v of ["@v1", "@v2", "@v3", "@v4"]) assert.ok(out.includes(v), `${v} が見つからない`);
    assert.ok(out.includes("初回SQLバックアップ"));
  });

  await check("restore v1 (plain SQL) -> リモートに別名で復元", async () => {
    run(["restore", "local/shop_db@v1", "--dest", "remote", "--db", "shop_db_restored"]);
    assert.deepStrictEqual(await shopCounts(PORT_B, "shop_db_restored"), [500, 2000]);
  });

  await check("restore v3 (ローカルアーカイブ) -> ローカルに復元", async () => {
    run(["restore", "local/shop_db@v3", "--dest", "local", "--db", "shop_db_arch1"]);
    assert.deepStrictEqual(await shopCounts(PORT_A, "shop_db_arch1"), [500, 2000]);
  });

  await check("restore --drop で既存 DB を上書き", async () => {
    run(["restore", "local/shop_db@v2", "--dest", "local", "--db", "shop_db_arch1", "--drop"]);
    assert.deepStrictEqual(await shopCounts(PORT_A, "shop_db_arch1"), [500, 2000]);
  });

  await check("archive list / archive restore (リモートのアーカイブから直接)", async () => {
    assert.ok(run(["archive", "list", "remote"]).includes("local/shop_db@v4"));
    run(["archive", "restore", "remote", "local/shop_db@v4",
         "--dest", "local", "--db", "shop_db_arch2"]);
    assert.deepStrictEqual(await shopCounts(PORT_A, "shop_db_arch2"), [500, 2000]);
  });

  await check("restore-file: SQL ファイルを直接リストア", async () => {
    const loc = catalogLocation("local/shop_db@v1");
    run(["restore-file", loc, "--dest", "local", "--db", "shop_db_file"]);
    assert.deepStrictEqual(await shopCounts(PORT_A, "shop_db_file"), [500, 2000]);
  });

  await check("複製先でビュー・コメント・インデックスも保持される", async () => {
    const v = await q(PORT_B, "shop_db",
      "SELECT count(*) AS n FROM information_schema.views WHERE table_name='active_customers'");
    assert.strictEqual(Number(v[0].n), 1);
    const cmt = await q(PORT_B, "shop_db",
      "SELECT obj_description('public.customers'::regclass, 'pg_class') AS c");
    assert.strictEqual(cmt[0].c, "顧客マスタ");
  });

  await check("backup --all: 全 DB を一括バックアップ (BACK_UP_DB は除外)", () => {
    const out = run(["backup", "remote", "--all", "--mode", "file", "--fmt", "custom"]);
    assert.ok(out.includes("analytics_db") && out.includes("analytics_copy"));
    assert.ok(!out.includes("BACK_UP_DB"));
  });

  await check("history: 操作履歴が記録されている", () => {
    const out = run(["history", "--limit", "100"]);
    assert.ok(out.includes("backup") && out.includes("restore") && out.includes("clone") && out.includes("OK"));
  });

  await check("archive delete: アーカイブとローカルカタログの両方から消える", async () => {
    const out = run(["archive", "delete", "local", "local/shop_db@v3"]);
    assert.ok(out.includes("カタログからも削除"));
    assert.ok(!run(["versions", "--profile", "local", "--db", "shop_db"]).includes("@v3"));
    const rows = await q(PORT_A, "BACK_UP_DB", "SELECT version_id FROM pgbackup_archive");
    assert.ok(!rows.some((r) => r.version_id === "local/shop_db@v3"));
  });

  await check("異常系: 複製元と複製先が同一ならエラー", () => {
    const out = run(["backup", "local", "--db", "shop_db", "--mode", "clone", "--dest", "local"],
                    { expectFail: true });
    assert.ok(out.includes("複製元と複製先が同一"));
  });

  await check("異常系: ホスト表記ゆれ (localhost vs 127.0.0.1) でも同一クラスタを検出", async () => {
    run(["conn", "add", "local_alias", "--host", "127.0.0.1", "--port", String(PORT_A),
         "--user", USER, "--overwrite"]);
    const out = run(["backup", "local", "--db", "shop_db", "--mode", "clone",
                     "--dest", "local_alias", "--drop"], { expectFail: true });
    assert.ok(out.includes("複製元と複製先が同一"));
    assert.deepStrictEqual(await shopCounts(PORT_A, "shop_db"), [500, 2000]);
  });

  await check("異常系: 既存 DB へ --drop なしでリストアするとエラー", () => {
    const out = run(["restore", "local/shop_db@v1", "--dest", "local", "--db", "shop_db_file"],
                    { expectFail: true });
    assert.ok(out.includes("既に存在"));
  });

  await check("異常系: 存在しない DB のバックアップはエラー", () => {
    const out = run(["backup", "local", "--db", "no_such_db", "--mode", "file"], { expectFail: true });
    assert.ok(out.includes("存在しない"));
  });

  await check("異常系: 存在しないバージョンのリストアはエラー", () => {
    const out = run(["restore", "local/shop_db@v999", "--dest", "local"], { expectFail: true });
    assert.ok(out.includes("見つかりません"));
  });

  await check("異常系: --out ファイル指定 + 複数 DB はエラー", () => {
    const out = run(["backup", "local", "--db", "shop_db", "--db", "inventory_db",
                     "--mode", "file", "--out", path.join(HOME, "multi.sql")], { expectFail: true });
    assert.ok(out.includes("1 つだけ"));
  });

  await check("異常系: 既存ファイルへの --out 上書きはエラー", () => {
    const target = path.join(HOME, "dup_out.sql");
    run(["backup", "local", "--db", "inventory_db", "--mode", "file", "--out", target]);
    const out = run(["backup", "local", "--db", "inventory_db", "--mode", "file", "--out", target],
                    { expectFail: true });
    assert.ok(out.includes("既に存在"));
  });

  await check("異常系: モードに無関係なフラグはエラー (--drop + archive)", () => {
    const out = run(["backup", "local", "--db", "shop_db", "--mode", "archive", "--drop"],
                    { expectFail: true });
    assert.ok(out.includes("clone モード専用"));
  });

  await check("異常系: 不正なプロファイル名は登録できない", () => {
    const out = run(["conn", "add", "bad/name", "--host", "localhost", "--port", String(PORT_A)],
                    { expectFail: true });
    assert.ok(out.includes("使用できません"));
  });

  await check("異常系: 非数値の --port / --limit は明示的に拒否される", () => {
    const out1 = run(["conn", "add", "badport", "--host", "localhost", "--port", "55x32"],
                     { expectFail: true });
    assert.ok(out1.includes("整数"));
    const out2 = run(["history", "--limit", "abc"], { expectFail: true });
    assert.ok(out2.includes("整数"));
  });

  await check("異常系: 到達不能な複製先はハングせずエラーになる", () => {
    run(["conn", "add", "deadport", "--host", "localhost", "--port", "59999",
         "--user", USER, "--overwrite"]);
    const out = run(["backup", "local", "--db", "shop_db", "--mode", "clone",
                     "--dest", "deadport"], { expectFail: true });
    assert.ok(out.includes("接続失敗") || out.includes("失敗"));
  });

  await check("プロトタイプ名 (__proto__) のプロファイルも正しく登録・削除できる", () => {
    run(["conn", "add", "__proto__", "--host", "localhost", "--port", String(PORT_A),
         "--user", USER, "--overwrite"]);
    assert.ok(run(["conn", "list"]).includes("__proto__"));
    run(["conn", "remove", "__proto__"]);
    assert.ok(!run(["conn", "list"]).includes("__proto__"));
  });

  console.log(`\n結果: ${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
