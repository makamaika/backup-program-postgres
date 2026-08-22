// ローカル Web UI (Express)。
//
// API は Python 版 (FastAPI) と互換で、フロントエンド (static/index.html) は共通。
// バックアップ / リストアは時間がかかるためバックグラウンドジョブとして実行し、
// /api/jobs/{id} のポーリングで進捗と結果を返す。

import crypto from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";

import express from "express";

import * as archiveMod from "../archive.js";
import { AgentClient } from "../agent.js";
import * as backupMod from "../backup.js";
import * as dataeditMod from "../dataedit.js";
import * as inspectMod from "../inspect.js";
import * as restoreMod from "../restore.js";
import { Catalog } from "../catalog.js";
import {
  AgentProfileStore, DEFAULT_ARCHIVE_DB, ProfileStore, agentMasked, cleanupTmp,
  makeAgentProfile, makeProfile, profileMasked,
} from "../config.js";
import { serverVersion } from "../db.js";
import { PgBackupError } from "../errors.js";

const STATIC_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "static");

function now() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

// メモリ上の簡易ジョブ管理 (Web プロセス内のみ)
class JobManager {
  constructor() {
    this.jobs = new Map();
  }

  submit(kind, title, fn) {
    const id = crypto.randomBytes(6).toString("hex");
    const job = {
      id, kind, title, status: "running",
      log: [], result: null, error: null,
      started_at: now(), finished_at: null,
    };
    this.jobs.set(id, job);
    const log = (msg) => job.log.push(`[${now()}] ${msg}`);
    (async () => {
      try {
        job.result = await fn(log);
        job.status = "done";
      } catch (e) {
        job.status = "error";
        job.error = e.message;
      } finally {
        job.finished_at = now();
      }
    })();
    return id;
  }

  get(id) {
    const job = this.jobs.get(id);
    if (!job) {
      const err = new PgBackupError(`ジョブ ${id} が見つかりません`);
      err.httpStatus = 404;
      throw err;
    }
    return job;
  }

  list() {
    return [...this.jobs.values()].sort((a, b) => (a.started_at < b.started_at ? 1 : -1));
  }
}

function requireQ(req, name) {
  const raw = req.query[name];
  const v = Array.isArray(raw) ? raw[0] : raw; // 重複パラメータは先頭を採用
  if (v === undefined || v === "") throw new PgBackupError(`クエリパラメータ '${name}' は必須です`);
  return String(v);
}

function optQ(req, name) {
  const raw = req.query[name];
  const v = Array.isArray(raw) ? raw[0] : raw;
  return v === undefined || v === "" ? null : String(v);
}

function limitQ(req, fallback, max) {
  const n = Number(Array.isArray(req.query.limit) ? req.query.limit[0] : req.query.limit);
  return Number.isInteger(n) && n >= 1 ? Math.min(n, max) : fallback;
}

export function createApp() {
  const app = express();
  const allowedHosts = new Set(
    (process.env.PGBACKUP_WEB_ALLOWED_HOSTS || "127.0.0.1,localhost,::1")
      .split(",").map((value) => value.trim().toLowerCase()).filter(Boolean)
  );
  app.disable("x-powered-by");
  app.use((req, res, next) => {
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("X-Frame-Options", "DENY");
    res.setHeader("Referrer-Policy", "no-referrer");
    if (req.path.startsWith("/api/")) res.setHeader("Cache-Control", "no-store");
    if (!allowedHosts.has(String(req.hostname || "").toLowerCase())) {
      res.status(400).json({ detail: "許可されていない Host ヘッダーです" });
      return;
    }
    if (!["GET", "HEAD", "OPTIONS"].includes(req.method) && req.headers.origin) {
      let originHost = "";
      try { originHost = new URL(req.headers.origin).host.toLowerCase(); } catch { /* reject below */ }
      if (!originHost || originHost !== String(req.headers.host || "").toLowerCase()) {
        res.status(403).json({ detail: "異なる Origin からの操作は拒否されました" });
        return;
      }
    }
    next();
  });
  app.use(express.json());
  const store = new ProfileStore();
  const agents = new AgentProfileStore();
  const catalog = new Catalog();
  const jobs = new JobManager();
  cleanupTmp();

  // Express 5 は async ハンドラの reject をエラーミドルウェアへ渡す

  // ---- 静的ファイル ----

  app.get("/", (req, res) => res.sendFile(path.join(STATIC_DIR, "index.html")));

  // ---- 接続プロファイル ----

  app.get("/api/profiles", (req, res) => {
    res.json(store.list().map(profileMasked));
  });

  app.post("/api/profiles", (req, res) => {
    const b = req.body ?? {};
    const profile = makeProfile(String(b.name ?? ""), {
      host: b.host, port: b.port, user: b.user,
      password: b.password ?? null, dbname: b.dbname, sslmode: b.sslmode ?? null,
    });
    store.add(profile, Boolean(b.overwrite));
    res.json({ ok: true });
  });

  app.delete("/api/profiles/:name", (req, res) => {
    store.remove(req.params.name);
    res.json({ ok: true });
  });

  app.post("/api/profiles/:name/test", async (req, res) => {
    const profile = store.get(req.params.name);
    try {
      res.json({ ok: true, server_version: await serverVersion(profile) });
    } catch (e) {
      if (!(e instanceof PgBackupError)) throw e;
      res.json({ ok: false, error: e.message });
    }
  });

  // ---- HTTPS バックアップエージェント ----

  app.get("/api/agents", (req, res) => res.json(agents.list().map(agentMasked)));
  app.post("/api/agents", (req, res) => {
    const b = req.body ?? {};
    agents.add(makeAgentProfile(String(b.name ?? ""), b), Boolean(b.overwrite));
    res.json({ ok: true });
  });
  app.delete("/api/agents/:name", (req, res) => {
    agents.remove(req.params.name);
    res.json({ ok: true });
  });
  app.post("/api/agents/:name/test", async (req, res) => {
    res.json(await new AgentClient(agents.get(req.params.name)).health());
  });
  app.get("/api/agent-backups", async (req, res) => {
    res.json(await new AgentClient(agents.get(requireQ(req, "agent"))).listBackups(limitQ(req, 200, 1000)));
  });
  app.delete("/api/agent-backups/:id", async (req, res) => {
    const agent = requireQ(req, "agent");
    const result = await new AgentClient(agents.get(agent)).deleteBackup(req.params.id);
    for (const rec of catalog.listBackups()) {
      if (rec.agent_profile === agent && rec.agent_backup_id === req.params.id) {
        catalog.deleteBackup(rec.version_id);
      }
    }
    res.json(result);
  });
  app.get("/api/agent-databases", async (req, res) => {
    res.json(await new AgentClient(agents.get(requireQ(req, "agent"))).databases(
      requireQ(req, "remote_profile"), req.query.templates === "true"
    ));
  });
  app.get("/api/agent-structure", async (req, res) => {
    res.json(await new AgentClient(agents.get(requireQ(req, "agent"))).structure(
      requireQ(req, "remote_profile"), requireQ(req, "db")
    ));
  });
  app.get("/api/agent-table", async (req, res) => {
    res.json(await new AgentClient(agents.get(requireQ(req, "agent"))).table(
      requireQ(req, "remote_profile"), requireQ(req, "db"),
      requireQ(req, "schema"), requireQ(req, "table")
    ));
  });
  app.get("/api/agent-sample", async (req, res) => {
    res.json(await new AgentClient(agents.get(requireQ(req, "agent"))).sample(
      requireQ(req, "remote_profile"), requireQ(req, "db"),
      requireQ(req, "schema"), requireQ(req, "table"), limitQ(req, 10, 100)
    ));
  });

  // ---- データベース検査 ----
  // DB / スキーマ / テーブル名は '/' 等を含み得るためパスではなくクエリで受ける

  app.get("/api/databases", async (req, res) => {
    const profile = store.get(requireQ(req, "profile"));
    res.json(await inspectMod.listDatabases(profile, req.query.templates === "true"));
  });

  app.get("/api/structure", async (req, res) => {
    const profile = store.get(requireQ(req, "profile"));
    res.json(await inspectMod.databaseStructure(profile, requireQ(req, "db")));
  });

  app.get("/api/table", async (req, res) => {
    const profile = store.get(requireQ(req, "profile"));
    const [db, schema, table] = [requireQ(req, "db"), requireQ(req, "schema"), requireQ(req, "table")];
    res.json({
      columns: await inspectMod.tableColumns(profile, db, schema, table),
      indexes: await inspectMod.tableIndexes(profile, db, schema, table),
    });
  });

  app.get("/api/sample", async (req, res) => {
    const profile = store.get(requireQ(req, "profile"));
    res.json(await inspectMod.tableSample(
      profile, requireQ(req, "db"), requireQ(req, "schema"), requireQ(req, "table"),
      limitQ(req, 10, 200)
    ));
  });

  // ---- 行編集・テーブル比較 / merge / sync ----

  app.get("/api/data/rows", async (req, res) => {
    res.json(await dataeditMod.readRows(
      store.get(requireQ(req, "profile")), requireQ(req, "db"),
      requireQ(req, "schema"), requireQ(req, "table"),
      {
        limit: limitQ(req, dataeditMod.DEFAULT_PAGE_SIZE, dataeditMod.MAX_PAGE_SIZE),
        offset: Number(optQ(req, "offset") ?? 0),
        orderBy: optQ(req, "order_by"),
        direction: optQ(req, "direction") ?? "asc",
      }
    ));
  });

  app.post("/api/data/insert", async (req, res) => {
    const b = req.body ?? {};
    const result = await dataeditMod.insertRow(
      store.get(String(b.profile ?? "")), String(b.db ?? ""),
      String(b.schema_name ?? "public"), String(b.table ?? ""), b.values ?? {}
    );
    catalog.logOperation("data-insert", true,
      `行追加: ${b.profile}:${b.db}/${b.schema_name || "public"}.${b.table}`, { key: result.key });
    res.json(result);
  });

  app.post("/api/data/update", async (req, res) => {
    const b = req.body ?? {};
    const result = await dataeditMod.updateRow(
      store.get(String(b.profile ?? "")), String(b.db ?? ""),
      String(b.schema_name ?? "public"), String(b.table ?? ""), b.key ?? {}, b.values ?? {},
      { etag: b.etag ?? null, force: Boolean(b.force) }
    );
    catalog.logOperation("data-update", true,
      `行更新: ${b.profile}:${b.db}/${b.schema_name || "public"}.${b.table}`, { key: result.key });
    res.json(result);
  });

  app.post("/api/data/delete", async (req, res) => {
    const b = req.body ?? {};
    const result = await dataeditMod.deleteRow(
      store.get(String(b.profile ?? "")), String(b.db ?? ""),
      String(b.schema_name ?? "public"), String(b.table ?? ""), b.key ?? {},
      { etag: b.etag ?? null, force: Boolean(b.force) }
    );
    catalog.logOperation("data-delete", true,
      `行削除: ${b.profile}:${b.db}/${b.schema_name || "public"}.${b.table}`, { key: result.key });
    res.json(result);
  });

  app.post("/api/data/compare", async (req, res) => {
    const b = req.body ?? {};
    res.json(await dataeditMod.compareTables(
      store.get(String(b.source_profile ?? "")), String(b.source_db ?? ""),
      String(b.source_schema ?? "public"), String(b.source_table ?? ""),
      store.get(String(b.target_profile ?? "")), String(b.target_db ?? ""),
      String(b.target_schema ?? "public"), String(b.target_table ?? ""),
      {
        keyColumns: Array.isArray(b.key_columns) && b.key_columns.length ? b.key_columns : null,
        maxRows: Number(b.max_rows ?? dataeditMod.DEFAULT_COMPARE_ROWS),
        previewRows: Number(b.preview_rows ?? dataeditMod.DEFAULT_PREVIEW_ROWS),
      }
    ));
  });

  app.post("/api/data/apply", (req, res) => {
    const b = req.body ?? {};
    const source = store.get(String(b.source_profile ?? ""));
    const target = store.get(String(b.target_profile ?? ""));
    const run = async (log) => {
      log("比較元・比較先を再検証し、対象テーブルをロックしています");
      const result = await dataeditMod.applyTablePlan(
        source, String(b.source_db ?? ""), String(b.source_schema ?? "public"), String(b.source_table ?? ""),
        target, String(b.target_db ?? ""), String(b.target_schema ?? "public"), String(b.target_table ?? ""),
        {
          strategy: String(b.strategy ?? ""), expectedPlanHash: String(b.plan_hash ?? ""),
          confirmation: String(b.confirmation ?? ""),
          keyColumns: Array.isArray(b.key_columns) && b.key_columns.length ? b.key_columns : null,
          maxRows: Number(b.max_rows ?? dataeditMod.DEFAULT_COMPARE_ROWS),
        }
      );
      catalog.logOperation(`data-${b.strategy}`, true,
        `${b.strategy}: ${b.source_profile}:${b.source_db}/${b.source_schema}.${b.source_table} -> ` +
        `${b.target_profile}:${b.target_db}/${b.target_schema}.${b.target_table}`, result);
      log(`完了: insert=${result.inserted}, update=${result.updated}, delete=${result.deleted}`);
      return result;
    };
    res.json({
      job_id: jobs.submit(`data-${b.strategy}`, `${b.strategy}: ${b.source_table} -> ${b.target_table}`, run),
    });
  });

  // ---- バックアップ / リストア (ジョブ) ----

  app.post("/api/backup", async (req, res) => {
    const b = req.body ?? {};
    const source = store.get(String(b.profile ?? ""));
    const archiveDb = b.archive_db || DEFAULT_ARCHIVE_DB;
    const dbs = await backupMod.resolveTargetDbs(source, b.dbs?.length ? b.dbs : null, Boolean(b.all), archiveDb);
    const mode = b.mode || "file";
    const fmt = backupMod.checkModeOptions(mode, {
      fmt: b.fmt ?? null, dest: b.dest ?? null, destDb: b.dest_db ?? null,
      agent: b.agent ?? null, drop: Boolean(b.drop), keepOwner: Boolean(b.keep_owner), nDbs: dbs.length,
    });
    let dest = null;
    if (mode === "clone") dest = store.get(b.dest);
    else if (mode === "archive") dest = b.dest ? store.get(b.dest) : source;
    const agentProfile = mode === "agent" ? agents.get(b.agent) : null;

    const run = async (log) => {
      const results = [];
      for (const db of dbs) {
        log(`${db} のバックアップを開始 (${mode})`);
        let r;
        if (mode === "clone") {
          r = await backupMod.cloneDatabase(catalog, source, db, dest, b.dest_db || db, {
            drop: Boolean(b.drop), keepOwner: Boolean(b.keep_owner), note: b.note ?? null,
          });
        } else if (mode === "archive") {
          r = await backupMod.backupToArchive(catalog, source, db, dest, {
            archiveDb, note: b.note ?? null,
          });
        } else if (mode === "agent") {
          r = await backupMod.backupToAgent(catalog, source, db, agentProfile, {
            fmt, note: b.note ?? null, keepOwner: Boolean(b.keep_owner),
          });
        } else {
          r = await backupMod.backupToFile(catalog, source, db, {
            fmt, note: b.note ?? null, keepOwner: Boolean(b.keep_owner),
          });
        }
        results.push(r);
        log(`${db} 完了`);
      }
      return results;
    };

    const title = `バックアップ (${mode}): ${b.profile} / ${dbs.join(", ")}`;
    res.json({ job_id: jobs.submit("backup", title, run) });
  });

  app.post("/api/restore", (req, res) => {
    const b = req.body ?? {};
    const dest = store.get(String(b.dest ?? ""));
    const rec = catalog.getBackup(String(b.version_id ?? "")); // 存在チェックを先に行う

    const run = (log) => {
      log(`${b.version_id} を ${dest.name} に復元中`);
      return restoreMod.restoreVersion(catalog, store, b.version_id, dest, {
        destDb: b.db ?? null, drop: Boolean(b.drop), keepOwner: Boolean(b.keep_owner), agents,
      });
    };
    const title = `リストア: ${b.version_id} -> ${b.dest}:${b.db || rec.db}`;
    res.json({ job_id: jobs.submit("restore", title, run) });
  });

  app.post("/api/restore-file", (req, res) => {
    const b = req.body ?? {};
    const dest = store.get(String(b.dest ?? ""));
    const run = (log) => {
      log(`${b.path} を ${dest.name}:${b.db} に復元中`);
      return restoreMod.restoreDumpFile(catalog, String(b.path ?? ""), dest, String(b.db ?? ""), {
        drop: Boolean(b.drop), keepOwner: Boolean(b.keep_owner),
      });
    };
    const title = `ファイルからリストア: ${b.path} -> ${b.dest}:${b.db}`;
    res.json({ job_id: jobs.submit("restore", title, run) });
  });

  app.post("/api/restore-archive", (req, res) => {
    const b = req.body ?? {};
    const host = store.get(String(b.profile ?? ""));
    const dest = store.get(String(b.dest ?? ""));
    const archiveDb = b.archive_db || DEFAULT_ARCHIVE_DB;
    const run = (log) => {
      log(`アーカイブ ${b.profile}:${archiveDb} から ${b.version_id} を復元中`);
      return restoreMod.restoreFromArchive(catalog, host, archiveDb, String(b.version_id ?? ""), dest, {
        destDb: b.db ?? null, drop: Boolean(b.drop), keepOwner: Boolean(b.keep_owner),
      });
    };
    const title = `アーカイブからリストア: ${b.version_id} -> ${b.dest}`;
    res.json({ job_id: jobs.submit("restore", title, run) });
  });

  app.post("/api/agent-pull", (req, res) => {
    const b = req.body ?? {};
    const agent = agents.get(String(b.agent ?? ""));
    const run = (log) => {
      log(`${b.agent}:${b.remote_profile}/${b.db} を手元へ取得中`);
      return backupMod.backupAgentDatabaseToFile(catalog, agent, String(b.remote_profile ?? ""),
        String(b.db ?? ""), {
          fmt: b.fmt || "custom", note: b.note ?? null, keepOwner: Boolean(b.keep_owner),
        });
    };
    res.json({ job_id: jobs.submit("backup", `エージェントから取得: ${b.agent}/${b.db}`, run) });
  });

  app.post("/api/agent-snapshot", (req, res) => {
    const b = req.body ?? {};
    const client = new AgentClient(agents.get(String(b.agent ?? "")));
    const run = (log) => {
      log(`${b.agent} 内で ${b.remote_profile}/${b.db} をスナップショット中`);
      return client.createSnapshot(String(b.remote_profile ?? ""), String(b.db ?? ""), {
        versionId: b.version_id ?? null, fmt: b.fmt || "custom",
        keepOwner: Boolean(b.keep_owner), note: b.note ?? null,
      });
    };
    res.json({ job_id: jobs.submit("backup", `エージェント内スナップショット: ${b.agent}/${b.db}`, run) });
  });

  app.post("/api/agent-remote-restore", (req, res) => {
    const b = req.body ?? {};
    const client = new AgentClient(agents.get(String(b.agent ?? "")));
    const run = (log) => {
      log(`${b.backup_id} を ${b.remote_profile}:${b.db || "(元DB名)"} に復元中`);
      return client.restoreRemoteBackup(String(b.backup_id ?? ""), String(b.remote_profile ?? ""), {
        destDb: b.db ?? null, drop: Boolean(b.drop), keepOwner: Boolean(b.keep_owner),
      });
    };
    res.json({ job_id: jobs.submit("restore", `エージェント側リストア: ${b.agent}:${b.backup_id}`, run) });
  });

  // ---- バージョン / アーカイブ / 履歴 ----

  app.get("/api/versions", (req, res) => {
    res.json(catalog.listBackups(optQ(req, "profile"), optQ(req, "db")));
  });

  app.get("/api/archive", async (req, res) => {
    const profile = store.get(requireQ(req, "profile"));
    res.json(await archiveMod.listArchive(profile, String(req.query.archive_db || DEFAULT_ARCHIVE_DB)));
  });

  app.get("/api/history", (req, res) => {
    res.json(catalog.listOperations(limitQ(req, 100, 10000)));
  });

  // ---- ジョブ ----

  app.get("/api/jobs", (req, res) => res.json(jobs.list()));
  app.get("/api/jobs/:id", (req, res) => res.json(jobs.get(req.params.id)));

  // ---- エラーハンドリング ----

  app.use((err, req, res, next) => { // eslint-disable-line no-unused-vars
    // express.json() のパースエラー等は err.status を持つ (例: 400) — 500 に潰さない
    const status = err instanceof PgBackupError
      ? (err.httpStatus ?? 400)
      : (err.status ?? err.statusCode ?? 500);
    res.status(status).json({ detail: err.message });
  });

  return app;
}
