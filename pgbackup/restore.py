"""リストアエンジン。

- カタログのバージョン ID を指定して復元 (file / archive どちらのバックアップも可)
- 任意のダンプファイル (SQL / カスタム形式) を直接復元
- 復元先はローカル / リモートの任意のサーバー・任意の DB 名を指定可能
"""

from __future__ import annotations

import uuid
from pathlib import Path

from . import archive as archive_mod
from . import pgtools
from .agent.client import AgentClient
from .agent.config import AgentProfileStore
from .backup import sha256_of_file
from .catalog import Catalog
from .config import Profile, ProfileStore, tmp_dir
from .db import replace_database
from .errors import RestoreError


def _detect_format(path: Path) -> str:
    """ダンプファイルの形式を判定する。カスタム形式は先頭が 'PGDMP'。"""
    with open(path, "rb") as f:
        magic = f.read(5)
    return "custom" if magic == b"PGDMP" else "plain"


def _restore_dump(
    dest: Profile, dest_db: str, dump_path: Path, fmt: str, keep_owner: bool = False
) -> None:
    if fmt == "custom":
        pgtools.restore_custom(dest, dest_db, dump_path, keep_owner=keep_owner)
    else:
        pgtools.restore_plain(dest, dest_db, dump_path)


def restore_version(
    catalog: Catalog,
    profiles: ProfileStore,
    version_id: str,
    dest: Profile,
    dest_db: str | None = None,
    drop: bool = False,
    keep_owner: bool = False,
    agents: AgentProfileStore | None = None,
) -> dict:
    """カタログに登録されたバージョンを指定環境へ復元する。"""
    rec = catalog.get_backup(version_id)
    dest_db = dest_db or rec["db"]
    detail = {"version_id": version_id, "dest": f"{dest.name}:{dest_db}", "drop": drop}

    fetched_tmp: Path | None = None
    try:
        if rec["mode"] == "file":
            dump_path = Path(rec["location"])
            if not dump_path.exists():
                raise RestoreError(
                    f"バックアップファイルが見つかりません: {dump_path}\n"
                    "移動・削除された場合は 'pgbackup versions' の他のバージョンを利用してください。"
                )
            if rec.get("sha256") and sha256_of_file(dump_path) != rec["sha256"]:
                raise RestoreError(
                    f"バックアップファイルの整合性チェックに失敗しました (sha256 不一致): {dump_path}"
                )
        elif rec["mode"] == "archive":
            src_profile = profiles.get(rec["archive_profile"])
            fetched_tmp = tmp_dir() / f"restore_{uuid.uuid4().hex}.dump"
            # 行 id で特定する (行が削除→同じ version_id で再作成されていても別物を掴まない)
            meta = archive_mod.fetch_dump(
                src_profile, rec["archive_db"], fetched_tmp,
                version_id=version_id, row_id=rec.get("archive_row_id"),
            )
            if rec.get("sha256") and meta.get("sha256") != rec["sha256"]:
                raise RestoreError(
                    f"アーカイブ '{version_id}' の内容がカタログの記録と一致しません "
                    "(sha256 不一致)。アーカイブが差し替えられた可能性があります。"
                )
            dump_path = fetched_tmp
        elif rec["mode"] == "agent":
            agent_store = agents or AgentProfileStore()
            agent = agent_store.get(rec["agent_profile"])
            suffix = ".sql" if rec["fmt"] == "plain" else ".dump"
            fetched_tmp = tmp_dir() / f"restore_{uuid.uuid4().hex}{suffix}"
            meta = AgentClient(agent).download_backup(rec["agent_backup_id"], fetched_tmp)
            if rec.get("sha256") and meta.get("sha256") != rec["sha256"]:
                raise RestoreError(
                    f"エージェントバックアップ '{version_id}' の SHA-256 が"
                    "カタログの記録と一致しません"
                )
            dump_path = fetched_tmp
        else:
            raise RestoreError(f"復元できないモードです: {rec['mode']}")

        with replace_database(dest, dest_db, drop=drop):
            _restore_dump(dest, dest_db, dump_path, rec["fmt"], keep_owner=keep_owner)
    except Exception as e:
        catalog.log_operation("restore", False, f"リストア失敗: {version_id} -> {detail['dest']}: {e}", detail)
        raise
    finally:
        if fetched_tmp is not None:
            fetched_tmp.unlink(missing_ok=True)

    catalog.log_operation("restore", True, f"リストア: {version_id} -> {detail['dest']}", detail)
    return {"version_id": version_id, "dest_profile": dest.name, "dest_db": dest_db, "mode": rec["mode"]}


def restore_dump_file(
    catalog: Catalog,
    path: Path,
    dest: Profile,
    dest_db: str,
    drop: bool = False,
    keep_owner: bool = False,
) -> dict:
    """任意のダンプファイル (SQL / カスタム形式を自動判定) を復元する。"""
    path = Path(path)
    if not path.exists():
        raise RestoreError(f"ダンプファイルが見つかりません: {path}")
    fmt = _detect_format(path)
    detail = {"file": str(path), "fmt": fmt, "dest": f"{dest.name}:{dest_db}", "drop": drop}
    try:
        with replace_database(dest, dest_db, drop=drop):
            _restore_dump(dest, dest_db, path, fmt, keep_owner=keep_owner)
    except Exception as e:
        catalog.log_operation("restore", False, f"ファイルからのリストア失敗: {path} -> {detail['dest']}: {e}", detail)
        raise
    catalog.log_operation("restore", True, f"ファイルからのリストア: {path} -> {detail['dest']}", detail)
    return {"file": str(path), "fmt": fmt, "dest_profile": dest.name, "dest_db": dest_db}


def restore_from_archive(
    catalog: Catalog,
    archive_host: Profile,
    archive_db: str,
    version_id: str,
    dest: Profile,
    dest_db: str | None = None,
    drop: bool = False,
    keep_owner: bool = False,
) -> dict:
    """ローカルカタログを介さず、アーカイブ DB から直接復元する。

    別マシンで作成されたアーカイブバックアップの復元に使う。
    """
    fetched = tmp_dir() / f"restore_{uuid.uuid4().hex}.dump"
    detail = {"version_id": version_id, "archive": f"{archive_host.name}:{archive_db}",
              "dest": None, "drop": drop}
    try:
        meta = archive_mod.fetch_dump(archive_host, archive_db, fetched, version_id=version_id)
        dest_db = dest_db or meta["source_db"]
        detail["dest"] = f"{dest.name}:{dest_db}"
        with replace_database(dest, dest_db, drop=drop):
            _restore_dump(dest, dest_db, fetched, meta["fmt"], keep_owner=keep_owner)
    except Exception as e:
        catalog.log_operation("restore", False, f"アーカイブからのリストア失敗: {version_id}: {e}", detail)
        raise
    finally:
        fetched.unlink(missing_ok=True)
    catalog.log_operation("restore", True, f"アーカイブからのリストア: {version_id} -> {detail['dest']}", detail)
    return {"version_id": version_id, "dest_profile": dest.name, "dest_db": dest_db, "mode": "archive"}
