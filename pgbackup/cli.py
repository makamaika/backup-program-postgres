"""pgbackup CLI。

使用例:
  pgbackup conn add local --host localhost --port 5432 --user postgres --ask-password
  pgbackup dbs local
  pgbackup inspect local shop_db
  pgbackup backup local --db shop_db --mode file --fmt plain
  pgbackup backup local --db shop_db --mode archive --dest remote
  pgbackup backup local --db shop_db --mode clone --dest remote --dest-db shop_db_copy
  pgbackup versions
  pgbackup restore local/shop_db@v1 --dest remote --db shop_db_restored
  pgbackup web
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import __version__, backup as backup_mod, inspect as inspect_mod, restore as restore_mod
from . import archive as archive_mod
from .catalog import Catalog
from .config import DEFAULT_ARCHIVE_DB, Profile, ProfileStore, home_dir
from .db import server_version
from .errors import PgBackupError


# ---------------------------------------------------------------- 表示ヘルパー

def _print_table(rows: list[dict], columns: list[tuple[str, str]]) -> None:
    """rows を列定義 [(キー, 見出し), ...] に従って整形表示する。"""
    if not rows:
        print("(なし)")
        return
    data = [[("" if r.get(k) is None else str(r.get(k))) for k, _ in columns] for r in rows]
    headers = [h for _, h in columns]
    widths = [max(len(h), *(len(row[i]) for row in data)) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for row in data:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)))


def _size_fmt(n) -> str:
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return ""


# ---------------------------------------------------------------- サブコマンド

def cmd_conn(args, store: ProfileStore, catalog: Catalog) -> int:
    if args.conn_cmd == "add":
        password = args.password
        if args.ask_password:
            password = getpass.getpass("パスワード: ")
        profile = Profile(
            name=args.name, host=args.host, port=args.port, user=args.user,
            password=password, dbname=args.dbname, sslmode=args.sslmode,
        )
        store.add(profile, overwrite=args.overwrite)
        print(f"接続プロファイル '{args.name}' を保存しました ({profile.display()})")
        print(f"  保存先: {store.path} (パスワードを含むため取り扱いに注意)")
    elif args.conn_cmd == "list":
        rows = [p.to_dict(mask_password=True) for p in store.list()]
        _print_table(rows, [("name", "名前"), ("host", "ホスト"), ("port", "ポート"),
                            ("user", "ユーザー"), ("dbname", "メンテDB"), ("password", "パスワード")])
    elif args.conn_cmd == "remove":
        store.remove(args.name)
        print(f"接続プロファイル '{args.name}' を削除しました")
    elif args.conn_cmd == "test":
        profile = store.get(args.name)
        ver = server_version(profile)
        print(f"OK: {profile.display()} (PostgreSQL {ver})")
    return 0


def cmd_dbs(args, store: ProfileStore, catalog: Catalog) -> int:
    profile = store.get(args.profile)
    rows = inspect_mod.list_databases(profile, include_templates=args.templates)
    _print_table(rows, [("name", "データベース"), ("owner", "所有者"),
                        ("encoding", "エンコーディング"), ("size", "サイズ")])
    return 0


def cmd_inspect(args, store: ProfileStore, catalog: Catalog) -> int:
    profile = store.get(args.profile)
    if args.table:
        schema = args.schema or "public"
        print(f"== {args.db}.{schema}.{args.table} のカラム ==")
        cols = inspect_mod.table_columns(profile, args.db, schema, args.table)
        _print_table(cols, [("name", "カラム"), ("data_type", "型"), ("nullable", "NULL可"),
                            ("default", "デフォルト"), ("is_primary_key", "PK")])
        print("\n== インデックス ==")
        _print_table(inspect_mod.table_indexes(profile, args.db, schema, args.table),
                     [("name", "名前"), ("definition", "定義")])
        if args.sample:
            print(f"\n== 先頭 {args.sample} 行 ==")
            sample = inspect_mod.table_sample(profile, args.db, schema, args.table, args.sample)
            rows = [dict(zip(sample["columns"], r)) for r in sample["rows"]]
            _print_table(rows, [(c, c) for c in sample["columns"]])
            print(f"(総行数: {sample['total_rows']})")
        return 0

    print(f"== {args.db} のテーブル一覧 ==")
    tables = inspect_mod.list_tables(profile, args.db, schema=args.schema)
    _print_table(tables, [("schema", "スキーマ"), ("name", "名前"), ("kind", "種別"),
                          ("est_rows", "推定行数"), ("size", "サイズ")])
    print("\n== 保存されているデータ型の内訳 ==")
    _print_table(inspect_mod.datatype_summary(profile, args.db),
                 [("data_type", "型"), ("column_count", "カラム数"), ("table_count", "テーブル数")])
    return 0


def cmd_backup(args, store: ProfileStore, catalog: Catalog) -> int:
    source = store.get(args.profile)
    archive_db = args.archive_db
    dbs = backup_mod.resolve_target_dbs(source, args.db, all_dbs=args.all, archive_db=archive_db)

    if args.mode == "clone":
        if not args.dest:
            raise PgBackupError("clone モードでは --dest (複製先プロファイル) が必須です")
        if args.dest_db and len(dbs) > 1:
            raise PgBackupError("--dest-db は対象 DB が 1 つのときのみ指定できます")
        dest = store.get(args.dest)
        for db in dbs:
            dest_db = args.dest_db or db
            r = backup_mod.clone_database(
                catalog, source, db, dest, dest_db, drop=args.drop, keep_owner=args.keep_owner
            )
            print(f"複製完了: {source.name}:{db} -> {dest.name}:{r['dest_db']}")
    elif args.mode == "archive":
        dest = store.get(args.dest) if args.dest else source
        for db in dbs:
            rec = backup_mod.backup_to_archive(
                catalog, source, db, dest, archive_db=archive_db, note=args.note
            )
            print(f"アーカイブ保存: {rec['version_id']} -> {dest.name}:{archive_db} "
                  f"({_size_fmt(rec['size'])})")
    else:  # file
        for db in dbs:
            rec = backup_mod.backup_to_file(
                catalog, source, db, fmt=args.fmt,
                out=Path(args.out) if args.out else None, note=args.note,
            )
            print(f"バックアップ完了: {rec['version_id']}")
            print(f"  ファイル: {rec['location']} ({_size_fmt(rec['size'])})")
    return 0


def cmd_versions(args, store: ProfileStore, catalog: Catalog) -> int:
    rows = catalog.list_backups(profile=args.profile, db=args.db)
    for r in rows:
        r["size_h"] = _size_fmt(r["size"])
        r["place"] = r["location"] if r["mode"] == "file" else f"{r['archive_profile']}:{r['archive_db']}"
    _print_table(rows, [("version_id", "バージョン"), ("mode", "モード"), ("fmt", "形式"),
                        ("size_h", "サイズ"), ("created_at", "作成日時 (UTC)"), ("place", "保存先"),
                        ("note", "メモ")])
    return 0


def cmd_restore(args, store: ProfileStore, catalog: Catalog) -> int:
    dest = store.get(args.dest)
    r = restore_mod.restore_version(
        catalog, store, args.version_id, dest,
        dest_db=args.db, drop=args.drop, keep_owner=args.keep_owner,
    )
    print(f"リストア完了: {args.version_id} -> {dest.name}:{r['dest_db']}")
    return 0


def cmd_restore_file(args, store: ProfileStore, catalog: Catalog) -> int:
    dest = store.get(args.dest)
    r = restore_mod.restore_dump_file(
        catalog, Path(args.path), dest, args.db, drop=args.drop, keep_owner=args.keep_owner
    )
    print(f"リストア完了: {args.path} ({r['fmt']}) -> {dest.name}:{args.db}")
    return 0


def cmd_archive(args, store: ProfileStore, catalog: Catalog) -> int:
    profile = store.get(args.profile)
    if args.archive_cmd == "list":
        rows = archive_mod.list_archive(profile, args.archive_db)
        for r in rows:
            r["size_h"] = _size_fmt(r["size"])
        _print_table(rows, [("version_id", "バージョン"), ("source_db", "元DB"),
                            ("size_h", "サイズ"), ("created_at", "作成日時"), ("note", "メモ")])
    elif args.archive_cmd == "restore":
        dest = store.get(args.dest)
        r = restore_mod.restore_from_archive(
            catalog, profile, args.archive_db, args.version_id, dest,
            dest_db=args.db, drop=args.drop, keep_owner=args.keep_owner,
        )
        print(f"リストア完了: {args.version_id} -> {dest.name}:{r['dest_db']}")
    elif args.archive_cmd == "delete":
        archive_mod.delete_archive_row(profile, args.archive_db, args.version_id)
        print(f"アーカイブから削除しました: {args.version_id}")
    return 0


def cmd_history(args, store: ProfileStore, catalog: Catalog) -> int:
    rows = catalog.list_operations(limit=args.limit)
    for r in rows:
        r["ok_h"] = "OK" if r["ok"] else "失敗"
    _print_table(rows, [("created_at", "日時 (UTC)"), ("kind", "操作"), ("ok_h", "結果"),
                        ("message", "内容")])
    return 0


def cmd_web(args, store: ProfileStore, catalog: Catalog) -> int:
    import uvicorn

    from .web.server import create_app

    print(f"Web UI: http://{args.host}:{args.port}/  (Ctrl+C で終了)")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


# ---------------------------------------------------------------- パーサー定義

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pgbackup",
        description="PostgreSQL バックアップ / リストア / バージョン管理ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version=f"pgbackup {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    # conn
    pc = sub.add_parser("conn", help="接続プロファイルの管理")
    pcs = pc.add_subparsers(dest="conn_cmd", required=True)
    pca = pcs.add_parser("add", help="プロファイルを追加")
    pca.add_argument("name")
    pca.add_argument("--host", default="localhost")
    pca.add_argument("--port", type=int, default=5432)
    pca.add_argument("--user", default="postgres")
    pca.add_argument("--password", help="パスワード (履歴に残るため --ask-password 推奨)")
    pca.add_argument("--ask-password", action="store_true", help="対話的にパスワードを入力")
    pca.add_argument("--dbname", default="postgres", help="メンテナンス接続用 DB (既定: postgres)")
    pca.add_argument("--sslmode", help="require / verify-full など")
    pca.add_argument("--overwrite", action="store_true", help="既存プロファイルを上書き")
    pca.set_defaults(func=cmd_conn)
    for name, help_ in (("list", "一覧"), ("remove", "削除"), ("test", "接続テスト")):
        sp = pcs.add_parser(name, help=help_)
        if name != "list":
            sp.add_argument("name")
        sp.set_defaults(func=cmd_conn)

    # dbs
    pd = sub.add_parser("dbs", help="サーバー上のデータベース一覧")
    pd.add_argument("profile")
    pd.add_argument("--templates", action="store_true", help="テンプレート DB も表示")
    pd.set_defaults(func=cmd_dbs)

    # inspect
    pi = sub.add_parser("inspect", help="DB の構造・データ型の確認")
    pi.add_argument("profile")
    pi.add_argument("db")
    pi.add_argument("--schema", help="スキーマで絞り込み")
    pi.add_argument("--table", help="テーブルの詳細 (カラム・インデックス) を表示")
    pi.add_argument("--sample", type=int, default=0, help="--table 指定時にデータの先頭 N 行を表示")
    pi.set_defaults(func=cmd_inspect)

    # backup
    pb = sub.add_parser("backup", help="バックアップの実行 (file / archive / clone)")
    pb.add_argument("profile", help="バックアップ元プロファイル")
    pb.add_argument("--db", action="append", help="対象 DB (複数指定可)")
    pb.add_argument("--all", action="store_true", help="テンプレート以外の全 DB を対象にする")
    pb.add_argument("--mode", choices=["file", "archive", "clone"], default="file")
    pb.add_argument("--fmt", choices=["plain", "custom"], default="plain",
                    help="file モードの形式 (plain=SQL ファイル, custom=pg_restore 用)")
    pb.add_argument("--out", help="file モードの出力先 (ファイルまたはディレクトリ)")
    pb.add_argument("--dest", help="archive/clone モードの保存先プロファイル (archive の既定は元と同じ)")
    pb.add_argument("--dest-db", help="clone モードの複製先 DB 名 (既定: 元と同名)")
    pb.add_argument("--archive-db", default=DEFAULT_ARCHIVE_DB,
                    help=f"archive モードの格納 DB 名 (既定: {DEFAULT_ARCHIVE_DB})")
    pb.add_argument("--drop", action="store_true", help="clone: 複製先に同名 DB があれば削除して作り直す")
    pb.add_argument("--keep-owner", action="store_true", help="所有者・権限情報も復元する")
    pb.add_argument("--note", help="バージョンに付けるメモ")
    pb.set_defaults(func=cmd_backup)

    # versions
    pv = sub.add_parser("versions", help="バックアップのバージョン一覧")
    pv.add_argument("--profile")
    pv.add_argument("--db")
    pv.set_defaults(func=cmd_versions)

    # restore
    pr = sub.add_parser("restore", help="バージョン ID を指定してリストア")
    pr.add_argument("version_id", help="例: local/shop_db@v3")
    pr.add_argument("--dest", required=True, help="復元先プロファイル")
    pr.add_argument("--db", help="復元先 DB 名 (既定: バックアップ元と同名)")
    pr.add_argument("--drop", action="store_true", help="復元先に同名 DB があれば削除して作り直す")
    pr.add_argument("--keep-owner", action="store_true")
    pr.set_defaults(func=cmd_restore)

    # restore-file
    prf = sub.add_parser("restore-file", help="ダンプファイルを直接リストア")
    prf.add_argument("path")
    prf.add_argument("--dest", required=True)
    prf.add_argument("--db", required=True)
    prf.add_argument("--drop", action="store_true")
    prf.add_argument("--keep-owner", action="store_true")
    prf.set_defaults(func=cmd_restore_file)

    # archive
    pa = sub.add_parser("archive", help="アーカイブ DB (BACK_UP_DB) の直接操作")
    pas = pa.add_subparsers(dest="archive_cmd", required=True)
    pal = pas.add_parser("list", help="アーカイブ内のバックアップ一覧")
    pal.add_argument("profile")
    pal.add_argument("--archive-db", default=DEFAULT_ARCHIVE_DB)
    pal.set_defaults(func=cmd_archive)
    par = pas.add_parser("restore", help="アーカイブから直接リストア (別マシンで作成した分も可)")
    par.add_argument("profile", help="アーカイブ DB があるプロファイル")
    par.add_argument("version_id")
    par.add_argument("--archive-db", default=DEFAULT_ARCHIVE_DB)
    par.add_argument("--dest", required=True)
    par.add_argument("--db")
    par.add_argument("--drop", action="store_true")
    par.add_argument("--keep-owner", action="store_true")
    par.set_defaults(func=cmd_archive)
    pad = pas.add_parser("delete", help="アーカイブからバックアップを削除")
    pad.add_argument("profile")
    pad.add_argument("version_id")
    pad.add_argument("--archive-db", default=DEFAULT_ARCHIVE_DB)
    pad.set_defaults(func=cmd_archive)

    # history
    ph = sub.add_parser("history", help="操作履歴の表示")
    ph.add_argument("--limit", type=int, default=50)
    ph.set_defaults(func=cmd_history)

    # web
    pw = sub.add_parser("web", help="ローカル Web UI を起動")
    pw.add_argument("--host", default="127.0.0.1")
    pw.add_argument("--port", type=int, default=8500)
    pw.set_defaults(func=cmd_web)

    return p


def main(argv: list[str] | None = None) -> int:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    store = ProfileStore()
    catalog = Catalog()
    try:
        return args.func(args, store, catalog)
    except PgBackupError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("中断しました", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
