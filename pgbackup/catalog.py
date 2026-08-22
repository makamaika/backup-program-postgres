"""バージョンカタログ (SQLite)。

すべてのバックアップ (file / archive) にバージョン ID を付与して記録する。
バージョン ID の形式: "<プロファイル名>/<DB名>@v<連番>"  例: local/shop_db@v3
また backup / restore / clone の操作履歴も保持する。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import catalog_path
from .errors import PgBackupError

_SCHEMA = """
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
    agent_profile TEXT,             -- agent: 格納先エージェント名
    agent_backup_id TEXT,           -- agent: リモート保管庫の ID
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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Catalog:
    def __init__(self, path: Path | None = None):
        self.path = path or catalog_path()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            # 旧スキーマからの移行: status 列がなければ追加する
            cols = {r[1] for r in conn.execute("PRAGMA table_info(backups)").fetchall()}
            if "status" not in cols:
                conn.execute("ALTER TABLE backups ADD COLUMN status TEXT NOT NULL DEFAULT 'done'")
            if "agent_profile" not in cols:
                conn.execute("ALTER TABLE backups ADD COLUMN agent_profile TEXT")
            if "agent_backup_id" not in cols:
                conn.execute("ALTER TABLE backups ADD COLUMN agent_backup_id TEXT")

    @contextmanager
    def _conn(self):
        """接続を開き、正常時はコミット、終了時は必ずクローズする。"""
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn

    # ---- バージョン ----

    def reserve_version(
        self, profile: str, db: str, mode: str, fmt: str, at_least: int = 1
    ) -> dict:
        """次のバージョン番号を採番して 'pending' 行を登録する。

        MAX+1 の計算と INSERT を単一文で行うため、並行実行しても番号は衝突しない
        (SQLite の書き込みロックで直列化される)。バックアップ完了時に
        finalize_backup、失敗時に abort_backup を呼ぶこと。
        """
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO backups
                     (version_id, profile, db, version_no, mode, fmt, status, created_at)
                   SELECT ?1 || '/' || ?2 || '@v' || vn, ?1, ?2, vn, ?3, ?4, 'pending', ?5
                   FROM (SELECT MAX(COALESCE(MAX(version_no), 0) + 1, ?6) AS vn
                         FROM backups WHERE profile = ?1 AND db = ?2)""",
                (profile, db, mode, fmt, _now(), at_least),
            )
            row = conn.execute("SELECT * FROM backups WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def finalize_backup(self, backup_id: int, **fields) -> dict:
        """pending 行にバックアップ結果を書き込み status='done' にする。"""
        allowed = {
            "location", "archive_profile", "archive_db", "archive_row_id",
            "agent_profile", "agent_backup_id",
            "size", "sha256", "pg_dump_version", "note",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"不明なフィールド: {bad}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE backups SET {sets}{', ' if sets else ''}status = 'done' WHERE id = ?",
                (*fields.values(), backup_id),
            )
            row = conn.execute("SELECT * FROM backups WHERE id = ?", (backup_id,)).fetchone()
            return dict(row)

    def abort_backup(self, backup_id: int) -> None:
        """失敗したバックアップの pending 行を削除する。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM backups WHERE id = ? AND status = 'pending'", (backup_id,))

    def get_backup(self, version_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM backups WHERE version_id = ? AND status = 'done'", (version_id,)
            ).fetchone()
        if row is None:
            raise PgBackupError(
                f"バージョン '{version_id}' がカタログに見つかりません。"
                "'pgbackup versions' で一覧を確認してください。"
            )
        return dict(row)

    def list_backups(self, profile: str | None = None, db: str | None = None) -> list[dict]:
        q = "SELECT * FROM backups"
        cond, args = ["status = 'done'"], []
        if profile:
            cond.append("profile = ?")
            args.append(profile)
        if db:
            cond.append("db = ?")
            args.append(db)
        q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY profile, db, version_no"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, args).fetchall()]

    def delete_backup(self, version_id: str) -> dict:
        rec = self.get_backup(version_id)
        with self._conn() as conn:
            conn.execute("DELETE FROM backups WHERE version_id = ?", (version_id,))
        return rec

    # ---- 操作履歴 ----

    def log_operation(self, kind: str, ok: bool, message: str, detail: dict | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO operations (kind, ok, message, detail, created_at) VALUES (?,?,?,?,?)",
                (kind, 1 if ok else 0, message, json.dumps(detail or {}, ensure_ascii=False), _now()),
            )

    def list_operations(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
            except json.JSONDecodeError:
                pass
            out.append(d)
        return out
