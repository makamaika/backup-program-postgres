"""バックアップエンジン。

3 つのモードを提供する:
  - file    : pg_dump でファイルに出力 (SQL またはカスタム形式) + バージョン登録
  - archive : 格納先サーバーの BACK_UP_DB にダンプを保存 + バージョン登録
  - clone   : pg_dump | pg_restore でそのまま別 DB に複製 (ローカル/リモートの任意の組合せ)
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import archive as archive_mod
from . import pgtools
from .catalog import Catalog
from .config import DEFAULT_ARCHIVE_DB, Profile, backups_dir, tmp_dir
from .db import database_encoding, ensure_database
from .errors import BackupError
from .inspect import list_databases

# バックアップ対象から常に除外する DB
SYSTEM_DBS = {"template0", "template1"}


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(name: str) -> str:
    """DB 名をファイル/ディレクトリ名に使える形に変換する。"""
    return re.sub(r"[^\w.-]", "_", name)


def resolve_target_dbs(
    profile: Profile,
    dbs: list[str] | None,
    all_dbs: bool = False,
    archive_db: str = DEFAULT_ARCHIVE_DB,
) -> list[str]:
    """バックアップ対象 DB を決定する。--all の場合はテンプレートとアーカイブ DB を除く全 DB。"""
    existing = {d["name"] for d in list_databases(profile, include_templates=True)}
    if all_dbs:
        return sorted(existing - SYSTEM_DBS - {archive_db})
    if not dbs:
        raise BackupError("バックアップ対象の DB を指定してください (--db または --all)")
    missing = [d for d in dbs if d not in existing]
    if missing:
        raise BackupError(f"{profile.display()} に存在しない DB: {', '.join(missing)}")
    return dbs


def backup_to_file(
    catalog: Catalog,
    profile: Profile,
    db: str,
    fmt: str = "plain",
    out: Path | None = None,
    note: str | None = None,
) -> dict:
    """pg_dump でファイルへバックアップし、バージョンをカタログに登録する。"""
    if fmt not in ("plain", "custom"):
        raise BackupError(f"不明なフォーマット: {fmt} (plain または custom)")
    version_no = catalog.next_version_no(profile.name, db)
    ext = "sql" if fmt == "plain" else "dump"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if out is None:
        out_path = (
            backups_dir()
            / _safe_name(profile.name)
            / _safe_name(db)
            / f"{_safe_name(db)}_v{version_no}_{stamp}.{ext}"
        )
    else:
        out = Path(out)
        out_path = out / f"{_safe_name(db)}_v{version_no}_{stamp}.{ext}" if out.is_dir() else out

    try:
        pgtools.dump_to_file(profile, db, out_path, fmt=fmt)
        rec = catalog.add_backup(
            profile=profile.name,
            db=db,
            version_no=version_no,
            mode="file",
            fmt=fmt,
            location=str(out_path.resolve()),
            size=out_path.stat().st_size,
            sha256=sha256_of_file(out_path),
            pg_dump_version=pgtools.pg_dump_version(),
            note=note,
        )
    except Exception as e:
        catalog.log_operation(
            "backup", False, f"file バックアップ失敗: {profile.name}/{db}: {e}",
            {"profile": profile.name, "db": db, "mode": "file"},
        )
        raise
    catalog.log_operation(
        "backup", True, f"file バックアップ: {rec['version_id']} -> {out_path}",
        {"version_id": rec["version_id"], "mode": "file", "location": str(out_path)},
    )
    return rec


def backup_to_archive(
    catalog: Catalog,
    source: Profile,
    db: str,
    dest: Profile,
    archive_db: str = DEFAULT_ARCHIVE_DB,
    note: str | None = None,
) -> dict:
    """ダンプを取得し、格納先サーバーのアーカイブ DB (BACK_UP_DB) に保存する。"""
    if source.host == dest.host and source.port == dest.port and db == archive_db:
        raise BackupError("アーカイブ DB 自身をアーカイブへバックアップすることはできません")

    archive_mod.ensure_archive_db(dest, archive_db)
    version_no = max(
        catalog.next_version_no(source.name, db),
        archive_mod.next_archive_version_no(dest, archive_db, db),
    )
    version_id = f"{source.name}/{db}@v{version_no}"
    tmp_path = tmp_dir() / f"archive_{uuid.uuid4().hex}.dump"
    try:
        pgtools.dump_to_file(source, db, tmp_path, fmt="custom")
        digest = sha256_of_file(tmp_path)
        row_id = archive_mod.store_dump(
            dest,
            archive_db,
            version_id=version_id,
            source_profile=source.name,
            source_host=source.host,
            source_port=source.port,
            source_db=db,
            version_no=version_no,
            fmt="custom",
            dump_path=tmp_path,
            sha256=digest,
            pg_dump_version=pgtools.pg_dump_version(),
            note=note,
        )
        rec = catalog.add_backup(
            profile=source.name,
            db=db,
            version_no=version_no,
            mode="archive",
            fmt="custom",
            archive_profile=dest.name,
            archive_db=archive_db,
            archive_row_id=row_id,
            size=tmp_path.stat().st_size,
            sha256=digest,
            pg_dump_version=pgtools.pg_dump_version(),
            note=note,
        )
    except Exception as e:
        catalog.log_operation(
            "backup", False, f"archive バックアップ失敗: {source.name}/{db}: {e}",
            {"profile": source.name, "db": db, "mode": "archive", "dest": dest.name},
        )
        raise
    finally:
        tmp_path.unlink(missing_ok=True)
    catalog.log_operation(
        "backup", True,
        f"archive バックアップ: {rec['version_id']} -> {dest.name}:{archive_db}",
        {"version_id": rec["version_id"], "mode": "archive",
         "dest": dest.name, "archive_db": archive_db},
    )
    return rec


def clone_database(
    catalog: Catalog,
    source: Profile,
    source_db: str,
    dest: Profile,
    dest_db: str,
    drop: bool = False,
    keep_owner: bool = False,
) -> dict:
    """DB をそのまま複製する。ローカル/リモートの任意の組合せに対応。

    複製先 DB は自動作成される (既存の場合は drop=True が必要)。
    """
    same_server = source.host == dest.host and source.port == dest.port
    if same_server and source_db == dest_db:
        raise BackupError("複製元と複製先が同一です。別の DB 名を指定してください。")

    detail = {
        "source": f"{source.name}:{source_db}",
        "dest": f"{dest.name}:{dest_db}",
        "drop": drop,
    }
    try:
        encoding = database_encoding(source, source_db)
        ensure_database(dest, dest_db, drop=drop, encoding=encoding)
        pgtools.pipe_dump_restore(source, source_db, dest, dest_db, keep_owner=keep_owner)
    except Exception as e:
        catalog.log_operation("clone", False, f"複製失敗: {detail['source']} -> {detail['dest']}: {e}", detail)
        raise
    catalog.log_operation("clone", True, f"複製: {detail['source']} -> {detail['dest']}", detail)
    return {
        "mode": "clone",
        "source_profile": source.name,
        "source_db": source_db,
        "dest_profile": dest.name,
        "dest_db": dest_db,
    }
