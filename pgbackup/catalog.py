"""バージョンカタログ (SQLite)。

すべてのバックアップ (file / archive) にバージョン ID を付与して記録する。
バージョン ID の形式: "<プロファイル名>/<DB名>@v<連番>"  例: local/shop_db@v3
また backup / restore / clone の操作履歴も保持する。
"""

from __future__ import annotations

import json
import sqlite3
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
    size INTEGER,
    sha256 TEXT,
    pg_dump_version TEXT,
    note TEXT,
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

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- バージョン ----

    def next_version_no(self, profile: str, db: str, at_least: int = 1) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM backups WHERE profile = ? AND db = ?",
                (profile, db),
            ).fetchone()
            return max(row[0], at_least)

    def add_backup(
        self,
        *,
        profile: str,
        db: str,
        version_no: int,
        mode: str,
        fmt: str,
        location: str | None = None,
        archive_profile: str | None = None,
        archive_db: str | None = None,
        archive_row_id: int | None = None,
        size: int | None = None,
        sha256: str | None = None,
        pg_dump_version: str | None = None,
        note: str | None = None,
    ) -> dict:
        version_id = f"{profile}/{db}@v{version_no}"
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO backups
                   (version_id, profile, db, version_no, mode, fmt, location,
                    archive_profile, archive_db, archive_row_id,
                    size, sha256, pg_dump_version, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id, profile, db, version_no, mode, fmt, location,
                    archive_profile, archive_db, archive_row_id,
                    size, sha256, pg_dump_version, note, _now(),
                ),
            )
        return self.get_backup(version_id)

    def get_backup(self, version_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM backups WHERE version_id = ?", (version_id,)).fetchone()
        if row is None:
            raise PgBackupError(
                f"バージョン '{version_id}' がカタログに見つかりません。"
                "'pgbackup versions' で一覧を確認してください。"
            )
        return dict(row)

    def list_backups(self, profile: str | None = None, db: str | None = None) -> list[dict]:
        q = "SELECT * FROM backups"
        cond, args = [], []
        if profile:
            cond.append("profile = ?")
            args.append(profile)
        if db:
            cond.append("db = ?")
            args.append(db)
        if cond:
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
