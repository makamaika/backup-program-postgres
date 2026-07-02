"""アーカイブモード: 格納先サーバー上の BACK_UP_DB (既定) にダンプを保存する。

pgbackup_archive       : バックアップのメタデータ (1 行 = 1 バックアップ)
pgbackup_archive_chunk : ダンプ本体 (bytea を 16MB チャンクに分割して格納)

チャンク分割により bytea の 1GB 制限と巨大メモリ確保を回避する。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from psycopg import sql

from .config import Profile
from .db import connect, create_database, database_exists
from .errors import BackupError, RestoreError

CHUNK_SIZE = 16 * 1024 * 1024  # 16MB

_ARCHIVE_DDL = """
CREATE TABLE IF NOT EXISTS pgbackup_archive (
    id bigserial PRIMARY KEY,
    version_id text UNIQUE NOT NULL,
    source_profile text,
    source_host text,
    source_port integer,
    source_db text NOT NULL,
    version_no integer NOT NULL,
    fmt text NOT NULL,
    size bigint,
    sha256 text,
    pg_dump_version text,
    note text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pgbackup_archive_chunk (
    backup_id bigint NOT NULL REFERENCES pgbackup_archive(id) ON DELETE CASCADE,
    seq integer NOT NULL,
    data bytea NOT NULL,
    PRIMARY KEY (backup_id, seq)
);
"""


def ensure_archive_db(profile: Profile, archive_db: str) -> None:
    """アーカイブ用 DB とテーブルがなければ作成する。"""
    if not database_exists(profile, archive_db):
        create_database(profile, archive_db, encoding="UTF8")
    with connect(profile, archive_db) as conn:
        conn.execute(_ARCHIVE_DDL)
        conn.commit()


def next_archive_version_no(profile: Profile, archive_db: str, source_db: str) -> int:
    """アーカイブ内での次バージョン番号 (他クライアントとの重複を避けるため参照する)。"""
    with connect(profile, archive_db) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM pgbackup_archive WHERE source_db = %s",
            (source_db,),
        ).fetchone()
        return row[0]


def store_dump(
    profile: Profile,
    archive_db: str,
    *,
    version_id: str,
    source_profile: str,
    source_host: str,
    source_port: int,
    source_db: str,
    version_no: int,
    fmt: str,
    dump_path: Path,
    sha256: str,
    pg_dump_version: str | None = None,
    note: str | None = None,
) -> int:
    """ダンプファイルをチャンク分割してアーカイブ DB に格納し、行 id を返す。"""
    size = dump_path.stat().st_size
    with connect(profile, archive_db) as conn:
        with conn.transaction():
            row = conn.execute(
                """INSERT INTO pgbackup_archive
                   (version_id, source_profile, source_host, source_port, source_db,
                    version_no, fmt, size, sha256, pg_dump_version, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (
                    version_id, source_profile, source_host, source_port, source_db,
                    version_no, fmt, size, sha256, pg_dump_version, note,
                ),
            ).fetchone()
            backup_id = row[0]
            with open(dump_path, "rb") as f, conn.cursor() as cur:
                seq = 0
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    cur.execute(
                        "INSERT INTO pgbackup_archive_chunk (backup_id, seq, data) VALUES (%s,%s,%s)",
                        (backup_id, seq, chunk),
                    )
                    seq += 1
    return backup_id


def list_archive(profile: Profile, archive_db: str) -> list[dict]:
    """アーカイブ DB 内のバックアップ一覧 (ダンプ本体は含まない)。"""
    if not database_exists(profile, archive_db):
        return []
    with connect(profile, archive_db) as conn:
        exists = conn.execute("SELECT to_regclass('public.pgbackup_archive')").fetchone()[0]
        if exists is None:
            return []
        cur = conn.execute(
            """SELECT id, version_id, source_profile, source_host, source_port, source_db,
                      version_no, fmt, size, sha256, pg_dump_version, note, created_at
               FROM pgbackup_archive ORDER BY source_db, version_no"""
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _find_row(conn, version_id: str | None, row_id: int | None) -> dict:
    if row_id is not None:
        cur = conn.execute("SELECT * FROM pgbackup_archive WHERE id = %s", (row_id,))
    elif version_id is not None:
        cur = conn.execute("SELECT * FROM pgbackup_archive WHERE version_id = %s", (version_id,))
    else:
        raise RestoreError("version_id か row_id のいずれかを指定してください")
    row = cur.fetchone()
    if row is None:
        key = version_id if version_id is not None else f"id={row_id}"
        raise RestoreError(f"アーカイブに '{key}' が見つかりません")
    cols = [c.name for c in cur.description]
    return dict(zip(cols, row))


def fetch_dump(
    profile: Profile,
    archive_db: str,
    out_path: Path,
    *,
    version_id: str | None = None,
    row_id: int | None = None,
) -> dict:
    """アーカイブからダンプを取り出してファイルに書き出し、メタデータを返す。

    取り出し後に sha256 を照合し、破損があれば例外を送出する。
    """
    if not database_exists(profile, archive_db):
        raise RestoreError(f"アーカイブ DB '{archive_db}' が {profile.display()} に存在しません")
    with connect(profile, archive_db) as conn:
        meta = _find_row(conn, version_id, row_id)
        h = hashlib.sha256()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f, conn.cursor(name="pgbackup_fetch") as cur:
            # 既定の itersize=100 だと 16MB チャンク x 100 行 = 約 1.6GB を
            # 一度にメモリへ読むため、必ず 1 行ずつフェッチする
            cur.itersize = 1
            cur.execute(
                "SELECT data FROM pgbackup_archive_chunk WHERE backup_id = %s ORDER BY seq",
                (meta["id"],),
            )
            for (chunk,) in cur:
                data = bytes(chunk)
                f.write(data)
                h.update(data)
    if meta.get("sha256") and h.hexdigest() != meta["sha256"]:
        raise RestoreError(
            f"アーカイブ '{meta['version_id']}' の整合性チェックに失敗しました "
            f"(sha256 不一致)。データが破損している可能性があります。"
        )
    return meta


def delete_archive_row(profile: Profile, archive_db: str, version_id: str) -> None:
    with connect(profile, archive_db) as conn:
        cur = conn.execute("DELETE FROM pgbackup_archive WHERE version_id = %s", (version_id,))
        if cur.rowcount == 0:
            raise BackupError(f"アーカイブに '{version_id}' が見つかりません")
        conn.commit()
