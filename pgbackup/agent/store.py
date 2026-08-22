"""エージェント側のバックアップ保管庫。

SQLite にはメタデータだけを保存し、ダンプ本体は content-addressed ファイルとして
保存する。巨大な BLOB を SQLite に入れず、ストリーミング転送と原子的な配置を行う。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..errors import AgentError

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS agent_backups (
    id TEXT PRIMARY KEY,
    version_id TEXT UNIQUE NOT NULL,
    source_profile TEXT NOT NULL,
    source_db TEXT NOT NULL,
    fmt TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    object_name TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_backups_created ON agent_backups(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_backups_source ON agent_backups(source_profile, source_db);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(
    value: str | None,
    field: str,
    max_length: int,
    *,
    required: bool = True,
    allow_newlines: bool = False,
) -> str:
    text = (value or "").strip()
    if required and not text:
        raise AgentError(f"{field} は必須です")
    allowed_controls = {"\t", "\n", "\r"} if allow_newlines else set()
    if len(text) > max_length or any(ord(ch) < 32 and ch not in allowed_controls for ch in text):
        raise AgentError(f"{field} が長すぎるか、不正な文字を含んでいます")
    return text


class AgentRepository:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.objects = self.root / "objects"
        self.temp = self.root / "tmp"
        self.db_path = self.root / "agent.db"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.temp.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
        self.cleanup_temp()

    @contextmanager
    def _conn(self):
        with closing(sqlite3.connect(self.db_path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn

    def cleanup_temp(self) -> None:
        for path in self.temp.glob("*.part"):
            try:
                path.unlink()
            except OSError:
                pass

    def new_temp_path(self) -> Path:
        return self.temp / f"{uuid.uuid4().hex}.part"

    def commit_temp(
        self,
        temp_path: Path,
        *,
        version_id: str,
        source_profile: str,
        source_db: str,
        fmt: str,
        size: int,
        sha256: str,
        note: str | None = None,
        max_storage_bytes: int | None = None,
    ) -> dict:
        version_id = _clean_text(version_id, "version_id", 256)
        source_profile = _clean_text(source_profile, "source_profile", 128)
        source_db = _clean_text(source_db, "source_db", 256)
        note = _clean_text(note, "note", 2000, required=False, allow_newlines=True) or None
        if fmt not in ("plain", "custom"):
            raise AgentError("fmt は plain または custom を指定してください")
        if size < 0 or not temp_path.is_file() or temp_path.stat().st_size != size:
            raise AgentError("アップロードされたファイルサイズが一致しません")
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise AgentError("SHA-256 値が不正です")
        if fmt == "custom":
            with open(temp_path, "rb") as file:
                if file.read(5) != b"PGDMP":
                    raise AgentError("custom 形式の内容が PostgreSQL ダンプ (PGDMP) ではありません")

        object_name = f"{sha256}.dump"
        object_path = self.objects / object_name
        backup_id = uuid.uuid4().hex
        created_at = _now()

        with self._lock:
            with self._conn() as conn:
                existing = conn.execute(
                    "SELECT * FROM agent_backups WHERE version_id = ?", (version_id,)
                ).fetchone()
                if existing is not None:
                    if existing["sha256"] == sha256:
                        temp_path.unlink(missing_ok=True)
                        return dict(existing)
                    raise AgentError(
                        f"version_id '{version_id}' は別内容のバックアップとして既に存在します"
                    )

                created_object = False
                try:
                    if object_path.exists():
                        temp_path.unlink(missing_ok=True)
                    else:
                        if max_storage_bytes is not None:
                            used = self.storage_size()
                            if used + size > max_storage_bytes:
                                raise AgentError(
                                    f"保管庫の容量上限を超えます: "
                                    f"used={used}, upload={size}, limit={max_storage_bytes}"
                                )
                        os.replace(temp_path, object_path)
                        created_object = True
                    conn.execute(
                        """INSERT INTO agent_backups
                           (id, version_id, source_profile, source_db, fmt, size,
                            sha256, object_name, note, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (
                            backup_id,
                            version_id,
                            source_profile,
                            source_db,
                            fmt,
                            size,
                            sha256,
                            object_name,
                            note,
                            created_at,
                        ),
                    )
                except Exception:
                    if created_object:
                        referenced = conn.execute(
                            "SELECT 1 FROM agent_backups WHERE object_name = ? LIMIT 1",
                            (object_name,),
                        ).fetchone()
                        if referenced is None:
                            object_path.unlink(missing_ok=True)
                    raise

        return self.get(backup_id)

    def storage_size(self) -> int:
        total = 0
        for path in self.objects.glob("*.dump"):
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def list(self, limit: int = 200) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise AgentError("limit は 1〜1000 の範囲で指定してください")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_backups ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, backup_id: str) -> dict:
        backup_id = _clean_text(backup_id, "backup_id", 64)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM agent_backups WHERE id = ?", (backup_id,)
            ).fetchone()
        if row is None:
            raise AgentError(f"バックアップ '{backup_id}' が見つかりません")
        return dict(row)

    def get_by_version(self, version_id: str) -> dict:
        version_id = _clean_text(version_id, "version_id", 256)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM agent_backups WHERE version_id = ?", (version_id,)
            ).fetchone()
        if row is None:
            raise AgentError(f"バックアップ '{version_id}' が見つかりません")
        return dict(row)

    def object_path(self, record: dict) -> Path:
        name = record.get("object_name", "")
        if not name or Path(name).name != name:
            raise AgentError("保管庫メタデータの object_name が不正です")
        path = (self.objects / name).resolve()
        if path.parent != self.objects.resolve() or not path.is_file():
            raise AgentError(f"バックアップ本体が見つかりません: {record.get('id')}")
        return path

    def delete(self, backup_id: str) -> dict:
        with self._lock:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM agent_backups WHERE id = ?", (backup_id,)
                ).fetchone()
                if row is None:
                    raise AgentError(f"バックアップ '{backup_id}' が見つかりません")
                record = dict(row)
                conn.execute("DELETE FROM agent_backups WHERE id = ?", (backup_id,))
                remaining = conn.execute(
                    "SELECT 1 FROM agent_backups WHERE object_name = ? LIMIT 1",
                    (record["object_name"],),
                ).fetchone()
            if remaining is None:
                (self.objects / record["object_name"]).unlink(missing_ok=True)
        return record
