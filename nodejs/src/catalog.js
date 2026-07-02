// バージョンカタログ (SQLite / node:sqlite 組み込みモジュール)。
//
// スキーマは Python 版と完全互換。同じ PGBACKUP_HOME を使えば
// 両方の版から同一のバージョン履歴が見える。
// バージョン ID の形式: "<プロファイル名>/<DB名>@v<連番>"  例: local/shop_db@v3

import { DatabaseSync } from "node:sqlite";

import { catalogPath } from "./config.js";
import { PgBackupError } from "./errors.js";

const SCHEMA = `
CREATE TABLE IF NOT EXISTS backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT UNIQUE NOT NULL,
    profile TEXT NOT NULL,
    db TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    mode TEXT NOT NULL,             -- 'file' | 'archive'
    fmt TEXT NOT NULL,              -- 'plain' | 'custom'
    location TEXT,                  -- file: ダンプファイルパス
    archive_profile TEXT,           -- archive: 格納先プロファイル名
    archive_db TEXT,                -- archive: 格納先 DB 名
    archive_row_id INTEGER,         -- archive: pgbackup_archive.id
    size INTEGER,
    sha256 TEXT,
    pg_dump_version TEXT,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'done',  -- 'pending' | 'done'
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backups_profile_db ON backups(profile, db);

CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,             -- 'backup' | 'restore' | 'clone'
    ok INTEGER NOT NULL,
    message TEXT,
    detail TEXT,                    -- JSON
    created_at TEXT NOT NULL
);
`;

function now() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

const FINALIZE_FIELDS = new Set([
  "location", "archive_profile", "archive_db", "archive_row_id",
  "size", "sha256", "pg_dump_version", "note",
]);

export class Catalog {
  constructor(dbPath = null) {
    this.db = new DatabaseSync(dbPath || catalogPath());
    this.db.exec("PRAGMA busy_timeout = 30000");
    this.db.exec(SCHEMA);
    // 旧スキーマからの移行: status 列がなければ追加する
    const cols = this.db.prepare("PRAGMA table_info(backups)").all().map((r) => r.name);
    if (!cols.includes("status")) {
      this.db.exec("ALTER TABLE backups ADD COLUMN status TEXT NOT NULL DEFAULT 'done'");
    }
  }

  close() {
    this.db.close();
  }

  // ---- バージョン ----

  // 次のバージョン番号を採番して 'pending' 行を登録する。
  // MAX+1 の計算と INSERT を単一文で行うため、並行実行しても番号は衝突しない
  // (SQLite の書き込みロックで直列化される)。完了時に finalizeBackup、
  // 失敗時に abortBackup を呼ぶこと。
  reserveVersion(profile, db, mode, fmt, atLeast = 1) {
    const stmt = this.db.prepare(
      `INSERT INTO backups
         (version_id, profile, db, version_no, mode, fmt, status, created_at)
       SELECT ? || '/' || ? || '@v' || vn, ?, ?, vn, ?, ?, 'pending', ?
       FROM (SELECT MAX(COALESCE(MAX(version_no), 0) + 1, ?) AS vn
             FROM backups WHERE profile = ? AND db = ?)`
    );
    const r = stmt.run(profile, db, profile, db, mode, fmt, now(), atLeast, profile, db);
    return this.db.prepare("SELECT * FROM backups WHERE id = ?").get(Number(r.lastInsertRowid));
  }

  // pending 行にバックアップ結果を書き込み status='done' にする
  finalizeBackup(backupId, fields) {
    const keys = Object.keys(fields);
    const bad = keys.filter((k) => !FINALIZE_FIELDS.has(k));
    if (bad.length) throw new Error(`不明なフィールド: ${bad.join(", ")}`);
    const sets = keys.map((k) => `${k} = ?`).join(", ");
    this.db.prepare(
      `UPDATE backups SET ${sets}${sets ? ", " : ""}status = 'done' WHERE id = ?`
    ).run(...keys.map((k) => fields[k]), backupId);
    return this.db.prepare("SELECT * FROM backups WHERE id = ?").get(backupId);
  }

  // 失敗したバックアップの pending 行を削除する
  abortBackup(backupId) {
    this.db.prepare("DELETE FROM backups WHERE id = ? AND status = 'pending'").run(backupId);
  }

  getBackup(versionId) {
    const row = this.db.prepare(
      "SELECT * FROM backups WHERE version_id = ? AND status = 'done'"
    ).get(versionId);
    if (row === undefined) {
      throw new PgBackupError(
        `バージョン '${versionId}' がカタログに見つかりません。` +
        "'pgbackup versions' で一覧を確認してください。"
      );
    }
    return row;
  }

  listBackups(profile = null, db = null) {
    const cond = ["status = 'done'"];
    const args = [];
    if (profile) { cond.push("profile = ?"); args.push(profile); }
    if (db) { cond.push("db = ?"); args.push(db); }
    return this.db.prepare(
      `SELECT * FROM backups WHERE ${cond.join(" AND ")} ORDER BY profile, db, version_no`
    ).all(...args);
  }

  deleteBackup(versionId) {
    const rec = this.getBackup(versionId);
    this.db.prepare("DELETE FROM backups WHERE version_id = ?").run(versionId);
    return rec;
  }

  // ---- 操作履歴 ----

  logOperation(kind, ok, message, detail = {}) {
    this.db.prepare(
      "INSERT INTO operations (kind, ok, message, detail, created_at) VALUES (?,?,?,?,?)"
    ).run(kind, ok ? 1 : 0, message, JSON.stringify(detail), now());
  }

  listOperations(limit = 100) {
    const rows = this.db.prepare(
      "SELECT * FROM operations ORDER BY id DESC LIMIT ?"
    ).all(limit);
    return rows.map((r) => {
      let detail = r.detail;
      try { detail = JSON.parse(r.detail || "{}"); } catch { /* 生のまま返す */ }
      return { ...r, detail };
    });
  }
}
