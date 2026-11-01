"""バックアップエンジン。

4 つのモードを提供する:
  - file    : pg_dump でファイルに出力 (SQL またはカスタム形式) + バージョン登録
  - archive : 格納先サーバーの BACK_UP_DB にダンプを保存 + バージョン登録
  - clone   : pg_dump | pg_restore でそのまま別 DB に複製 (ローカル/リモートの任意の組合せ)
  - agent   : pg_dump の結果を HTTPS バックアップエージェントへ保存
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import archive as archive_mod
from . import pgtools
from .agent.client import AgentClient
from .agent.config import AgentProfile
from .catalog import Catalog
from .config import DEFAULT_ARCHIVE_DB, Profile, backups_dir, tmp_dir
from .db import database_encoding, replace_database, same_cluster, server_version
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


def _major(version_text: str) -> int:
    m = re.search(r"(\d+)", version_text.rsplit(")", 1)[-1])
    return int(m.group(1)) if m else 0


def check_dump_compat(source: Profile) -> None:
    """pg_dump がソースサーバーより古いメジャーバージョンなら実行前に止める。

    clone は失敗時の影響が大きい (複製先を作り直す) ため、事前に検出する。
    """
    dump_major = _major(pgtools.pg_dump_version())
    server_major = _major(server_version(source))
    if 0 < dump_major < server_major:
        raise BackupError(
            f"pg_dump (メジャー {dump_major}) がサーバー (メジャー {server_major}) より古いため"
            "実行できません。新しい PostgreSQL クライアントツールを PGBACKUP_PG_BIN に指定してください。"
        )


def check_mode_options(
    mode: str,
    *,
    fmt: str | None = None,
    out: str | None = None,
    dest: str | None = None,
    dest_db: str | None = None,
    agent: str | None = None,
    drop: bool = False,
    keep_owner: bool = False,
    n_dbs: int = 1,
) -> str:
    """モードごとに無効なオプションの組合せを検出する (CLI / Web 共通)。

    戻り値は解決済みの fmt。無効な指定は黙って無視せずエラーにする。
    """
    if mode not in ("file", "archive", "clone", "agent"):
        raise BackupError(f"不明なモード: {mode}")
    if mode not in ("file", "agent"):
        if fmt is not None:
            raise BackupError(f"--fmt は file / agent モード専用です (mode={mode})")
    if mode != "file":
        if out:
            raise BackupError(f"--out は file モード専用です (mode={mode})")
    if mode != "clone":
        if dest_db:
            raise BackupError(f"--dest-db は clone モード専用です (mode={mode})")
        if drop:
            raise BackupError(f"--drop は clone モード専用です (mode={mode})")
    if mode == "archive" and keep_owner:
        raise BackupError("--keep-owner は archive モードでは指定できません "
                          "(復元時 'restore --keep-owner' で指定します)")
    if mode in ("file", "agent") and dest:
        raise BackupError("--dest は archive / clone モード専用です")
    if mode == "clone" and not dest:
        raise BackupError("clone モードでは --dest (複製先プロファイル) が必須です")
    if mode == "agent" and not agent:
        raise BackupError("agent モードでは --agent (保存先エージェント) が必須です")
    if mode != "agent" and agent:
        raise BackupError("--agent は agent モード専用です")
    if dest_db and n_dbs > 1:
        raise BackupError("--dest-db は対象 DB が 1 つのときのみ指定できます")
    if mode == "file" and out and n_dbs > 1:
        p = Path(out)
        if not (p.is_dir() or str(out).endswith(("/", "\\"))):
            raise BackupError(
                "--out にファイルパスを指定した場合、対象 DB は 1 つだけにしてください "
                "(複数 DB はディレクトリを指定)"
            )
    resolved_fmt = fmt or ("custom" if mode == "agent" else "plain")
    if resolved_fmt not in ("plain", "custom"):
        raise BackupError(f"不明なフォーマット: {resolved_fmt} (plain または custom)")
    return resolved_fmt


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


def _resolve_out_path(
    out: Path | None, profile: Profile, db: str, version_no: int, fmt: str
) -> Path:
    ext = "sql" if fmt == "plain" else "dump"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    auto_name = f"{_safe_name(db)}_v{version_no}_{stamp}_{uuid.uuid4().hex[:12]}.{ext}"
    if out is None:
        return backups_dir() / _safe_name(profile.name) / _safe_name(db) / auto_name
    if out.is_dir() or str(out).endswith(("/", "\\")):
        return Path(out) / auto_name
    if out.exists():
        raise BackupError(
            f"出力先 {out} は既に存在します。上書きを避けるため別のパスを指定してください。"
        )
    return out


def _reserve_out_path(out: Path | None, profile: Profile, db: str,
                      version_no: int, fmt: str) -> Path:
    """出力先を排他的に確保する。失敗時は呼び出し元に所有権を渡さない。"""
    path = _resolve_out_path(out, profile, db, version_no, fmt)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise BackupError(
            f"出力先 {path} は既に存在します。上書きを避けるため別のパスを指定してください。"
        ) from exc
    os.close(fd)
    return path


def backup_to_file(
    catalog: Catalog,
    profile: Profile,
    db: str,
    fmt: str = "plain",
    out: Path | None = None,
    note: str | None = None,
    keep_owner: bool = False,
) -> dict:
    """pg_dump でファイルへバックアップし、バージョンをカタログに登録する。"""
    if fmt not in ("plain", "custom"):
        raise BackupError(f"不明なフォーマット: {fmt} (plain または custom)")
    rec = catalog.reserve_version(profile.name, db, mode="file", fmt=fmt)
    out_path: Path | None = None
    try:
        out_path = _reserve_out_path(out, profile, db, rec["version_no"], fmt)
        pgtools.dump_to_file(profile, db, out_path, fmt=fmt, keep_owner=keep_owner)
        rec = catalog.finalize_backup(
            rec["id"],
            location=str(out_path.resolve()),
            size=out_path.stat().st_size,
            sha256=sha256_of_file(out_path),
            pg_dump_version=pgtools.pg_dump_version(),
            note=note,
        )
    except Exception as e:
        # 作りかけのダンプファイルを残さない (自分で作ったパスのみ削除)
        if out_path is not None:
            out_path.unlink(missing_ok=True)
        catalog.abort_backup(rec["id"])
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
    if db == archive_db and same_cluster(source, dest):
        raise BackupError("アーカイブ DB 自身をアーカイブへバックアップすることはできません")

    archive_mod.ensure_archive_db(dest, archive_db)
    rec = catalog.reserve_version(
        source.name, db, mode="archive", fmt="custom",
        at_least=archive_mod.next_archive_version_no(dest, archive_db, db),
    )
    tmp_path = tmp_dir() / f"archive_{uuid.uuid4().hex}.dump"
    try:
        pgtools.dump_to_file(source, db, tmp_path, fmt="custom")
        digest = sha256_of_file(tmp_path)
        row_id = archive_mod.store_dump(
            dest,
            archive_db,
            version_id=rec["version_id"],
            source_profile=source.name,
            source_host=source.host,
            source_port=source.port,
            source_db=db,
            version_no=rec["version_no"],
            fmt="custom",
            dump_path=tmp_path,
            sha256=digest,
            pg_dump_version=pgtools.pg_dump_version(),
            note=note,
        )
        rec = catalog.finalize_backup(
            rec["id"],
            archive_profile=dest.name,
            archive_db=archive_db,
            archive_row_id=row_id,
            size=tmp_path.stat().st_size,
            sha256=digest,
            pg_dump_version=pgtools.pg_dump_version(),
            note=note,
        )
    except Exception as e:
        catalog.abort_backup(rec["id"])
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


def backup_to_agent(
    catalog: Catalog,
    source: Profile,
    db: str,
    agent: AgentProfile,
    *,
    fmt: str = "custom",
    note: str | None = None,
    keep_owner: bool = False,
) -> dict:
    """ローカル接続できる DB をダンプし、HTTPS エージェントへ安全に保存する。"""
    if fmt not in ("plain", "custom"):
        raise BackupError(f"不明なフォーマット: {fmt} (plain または custom)")
    rec = catalog.reserve_version(source.name, db, mode="agent", fmt=fmt)
    suffix = ".sql" if fmt == "plain" else ".dump"
    temp_path = tmp_dir() / f"agent_upload_{uuid.uuid4().hex}{suffix}"
    remote_id: str | None = None
    client = AgentClient(agent)
    try:
        pgtools.dump_to_file(source, db, temp_path, fmt=fmt, keep_owner=keep_owner)
        digest = sha256_of_file(temp_path)
        remote = client.upload_backup(
            temp_path,
            version_id=rec["version_id"],
            source_profile=source.name,
            source_db=db,
            fmt=fmt,
            sha256=digest,
            note=note,
        )
        remote_id = remote["id"]
        rec = catalog.finalize_backup(
            rec["id"],
            agent_profile=agent.name,
            agent_backup_id=remote_id,
            size=temp_path.stat().st_size,
            sha256=digest,
            pg_dump_version=pgtools.pg_dump_version(),
            note=note,
        )
    except Exception as exc:
        if remote_id is not None:
            try:
                client.delete_backup(remote_id)
            except Exception:
                pass
        catalog.abort_backup(rec["id"])
        catalog.log_operation(
            "backup",
            False,
            f"agent バックアップ失敗: {source.name}/{db}: {exc}",
            {"profile": source.name, "db": db, "mode": "agent", "agent": agent.name},
        )
        raise
    finally:
        temp_path.unlink(missing_ok=True)
    catalog.log_operation(
        "backup",
        True,
        f"agent バックアップ: {rec['version_id']} -> {agent.name}:{remote_id}",
        {
            "version_id": rec["version_id"],
            "mode": "agent",
            "agent": agent.name,
            "agent_backup_id": remote_id,
        },
    )
    return rec


def backup_agent_database_to_file(
    catalog: Catalog,
    agent: AgentProfile,
    remote_profile: str,
    db: str,
    *,
    fmt: str = "custom",
    out: Path | None = None,
    note: str | None = None,
    keep_owner: bool = False,
) -> dict:
    """エージェント端末のローカル PostgreSQL をダンプし、手元へ保存する。"""
    if fmt not in ("plain", "custom"):
        raise BackupError(f"不明なフォーマット: {fmt} (plain または custom)")
    rec = catalog.reserve_version(agent.name, db, mode="file", fmt=fmt)
    out_path: Path | None = None
    try:
        out_path = _reserve_out_path(out, agent, db, rec["version_no"], fmt)
        meta = AgentClient(agent).download_database(
            remote_profile,
            db,
            out_path,
            fmt=fmt,
            keep_owner=keep_owner,
            replace_reserved=True,
        )
        rec = catalog.finalize_backup(
            rec["id"],
            location=str(out_path.resolve()),
            size=meta["size"],
            sha256=meta["sha256"],
            note=note,
        )
    except Exception as exc:
        if out_path is not None:
            out_path.unlink(missing_ok=True)
        catalog.abort_backup(rec["id"])
        catalog.log_operation(
            "backup",
            False,
            f"agent pull 失敗: {agent.name}/{remote_profile}/{db}: {exc}",
            {
                "agent": agent.name,
                "remote_profile": remote_profile,
                "db": db,
                "mode": "agent-pull",
            },
        )
        raise
    catalog.log_operation(
        "backup",
        True,
        f"agent pull: {rec['version_id']} <- {agent.name}:{remote_profile}/{db}",
        {
            "version_id": rec["version_id"],
            "agent": agent.name,
            "remote_profile": remote_profile,
            "location": str(out_path),
        },
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
    note: str | None = None,
) -> dict:
    """DB をそのまま複製する。ローカル/リモートの任意の組合せに対応。

    複製先 DB は自動作成される (既存の場合は drop=True が必要)。
    同一クラスタ判定はホスト名の表記ゆれ (localhost / 127.0.0.1 等) に騙されないよう
    サーバーの system_identifier で行い、複製元自身の破壊を防ぐ。
    複製に失敗した場合、既存の複製先 DB は元の状態に戻る。
    """
    if source_db == dest_db and same_cluster(source, dest):
        raise BackupError(
            "複製元と複製先が同一です (同じサーバーの同じ DB)。別の DB 名を指定してください。"
        )

    detail = {
        "source": f"{source.name}:{source_db}",
        "dest": f"{dest.name}:{dest_db}",
        "drop": drop,
        "note": note,
    }
    try:
        # 破壊的操作 (複製先の入替) の前に、失敗が予見できるものは検出しておく
        check_dump_compat(source)
        encoding = database_encoding(source, source_db)
        if encoding is None:
            raise BackupError(f"複製元 DB '{source_db}' が {source.display()} に存在しません")
        with replace_database(dest, dest_db, drop=drop, encoding=encoding):
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
