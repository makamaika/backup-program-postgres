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
import * as backupMod from "../backup.js";
import * as inspectMod from "../inspect.js";
import * as restoreMod from "../restore.js";
import { Catalog } from "../catalog.js";
import {
  DEFAULT_ARCHIVE_DB, ProfileStore, cleanupTmp, makeProfile, profileMasked,
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
  const v = req.query[name];
  if (v === undefined || v === "") throw new PgBackupError(`クエリパラメータ '${name}' は必須です`);
  return String(v);
}

export function createApp() {
  const app = express();
  app.use(express.json());
  const store = new ProfileStore();
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
      Number(req.query.limit ?? 10)
    ));
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
      drop: Boolean(b.drop), keepOwner: Boolean(b.keep_owner), nDbs: dbs.length,
    });
    let dest = null;
    if (mode === "clone") dest = store.get(b.dest);
    else if (mode === "archive") dest = b.dest ? store.get(b.dest) : source;

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
        destDb: b.db ?? null, drop: Boolean(b.drop), keepOwner: Boolean(b.keep_owner),
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

  // ---- バージョン / アーカイブ / 履歴 ----

  app.get("/api/versions", (req, res) => {
    res.json(catalog.listBackups(req.query.profile ?? null, req.query.db ?? null));
  });

  app.get("/api/archive", async (req, res) => {
    const profile = store.get(requireQ(req, "profile"));
    res.json(await archiveMod.listArchive(profile, String(req.query.archive_db || DEFAULT_ARCHIVE_DB)));
  });

  app.get("/api/history", (req, res) => {
    res.json(catalog.listOperations(Number(req.query.limit ?? 100)));
  });

  // ---- ジョブ ----

  app.get("/api/jobs", (req, res) => res.json(jobs.list()));
  app.get("/api/jobs/:id", (req, res) => res.json(jobs.get(req.params.id)));

  // ---- エラーハンドリング ----

  app.use((err, req, res, next) => { // eslint-disable-line no-unused-vars
    if (err instanceof PgBackupError) {
      res.status(err.httpStatus ?? 400).json({ detail: err.message });
    } else {
      res.status(500).json({ detail: err.message });
    }
  });

  return app;
}
