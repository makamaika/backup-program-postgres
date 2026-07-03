"""CLI の E2E テスト。

前提: 2 つの PostgreSQL クラスタが稼働していること (trust 認証)。
  - クラスタ A (ローカル役):  localhost:$E2E_PORT_A (既定 55432)  shop_db / inventory_db
  - クラスタ B (リモート役): localhost:$E2E_PORT_B (既定 55433)  analytics_db
PGBACKUP_HOME を一時ディレクトリに向けて実行すること。

実行例:
  $env:PGBACKUP_HOME = "C:\\tmp\\pghome"; python tests\\e2e_cli.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path

import psycopg

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT_A = int(os.environ.get("E2E_PORT_A", "55432"))
PORT_B = int(os.environ.get("E2E_PORT_B", "55433"))
USER = os.environ.get("E2E_USER", "postgres")

passed, failed = 0, 0


def run(*args: str, expect_fail: bool = False) -> str:
    proc = subprocess.run(
        [PY, "-m", "pgbackup", *args],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if expect_fail:
        assert proc.returncode != 0, (
            f"失敗を期待したが成功した: pgbackup {' '.join(args)}\n{proc.stdout}"
        )
    else:
        assert proc.returncode == 0, (
            f"pgbackup {' '.join(args)} (rc={proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc.stdout + proc.stderr


def q(port: int, db: str, sql: str):
    with psycopg.connect(host="localhost", port=port, user=USER, dbname=db) as c:
        return c.execute(sql).fetchall()


def drop_db(port: int, name: str) -> None:
    with psycopg.connect(host="localhost", port=port, user=USER, dbname="postgres",
                         autocommit=True) as c:
        c.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        c.execute(f'DROP DATABASE IF EXISTS "{name}"')


def shop_counts(port: int, db: str) -> tuple[int, int]:
    customers = q(port, db, "SELECT count(*) FROM public.customers")[0][0]
    orders = q(port, db, "SELECT count(*) FROM sales.orders")[0][0]
    return customers, orders


def check(name: str):
    def deco(fn):
        global passed, failed
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc(limit=4)
    return deco


def main() -> int:
    home = os.environ.get("PGBACKUP_HOME")
    assert home, "PGBACKUP_HOME を一時ディレクトリに設定してから実行してください"
    print(f"PGBACKUP_HOME = {home}")

    # 再実行に備えたクリーンアップ (バージョン番号を決定的にするためカタログも消す)
    (Path(home) / "catalog.db").unlink(missing_ok=True)
    shutil.rmtree(Path(home) / "backups", ignore_errors=True)
    (Path(home) / "dup_out.sql").unlink(missing_ok=True)
    (Path(home) / "multi.sql").unlink(missing_ok=True)
    for port, dbs in (
        (PORT_A, ["shop_db_copy", "shop_db_arch1", "shop_db_arch2", "shop_db_file",
                  "analytics_db", "BACK_UP_DB", "inventory_web_restore"]),
        (PORT_B, ["shop_db", "shop_db_restored", "analytics_copy", "BACK_UP_DB"]),
    ):
        for db in dbs:
            drop_db(port, db)

    # ---- 接続プロファイル ----

    @check("conn add local/remote")
    def _():
        run("conn", "add", "local", "--host", "localhost", "--port", str(PORT_A),
            "--user", USER, "--overwrite")
        run("conn", "add", "remote", "--host", "localhost", "--port", str(PORT_B),
            "--user", USER, "--overwrite")

    @check("conn test")
    def _():
        assert "OK" in run("conn", "test", "local")
        assert "OK" in run("conn", "test", "remote")

    @check("conn list")
    def _():
        out = run("conn", "list")
        assert "local" in out and "remote" in out

    # ---- DB 一覧 / 構造検査 ----

    @check("dbs: DB 一覧の表示")
    def _():
        out = run("dbs", "local")
        assert "shop_db" in out and "inventory_db" in out
        assert "analytics_db" in run("dbs", "remote")

    @check("inspect: テーブル一覧とデータ型内訳")
    def _():
        out = run("inspect", "local", "shop_db")
        assert "customers" in out and "orders" in out
        assert "jsonb" in out and "bytea" in out  # データ型の内訳

    @check("inspect --table --sample: カラム定義とサンプル行")
    def _():
        out = run("inspect", "local", "shop_db", "--table", "customers", "--sample", "3")
        assert "email" in out and "PK" in out
        assert "総行数: 500" in out

    # ---- バックアップ (file / archive) ----

    @check("backup file plain (SQL ファイル出力) -> v1")
    def _():
        out = run("backup", "local", "--db", "shop_db", "--mode", "file",
                  "--fmt", "plain", "--note", "初回SQLバックアップ")
        assert "local/shop_db@v1" in out
        loc = _catalog_location("local/shop_db@v1")
        assert loc.endswith(".sql") and Path(loc).exists()

    @check("backup file custom -> v2")
    def _():
        out = run("backup", "local", "--db", "shop_db", "--mode", "file", "--fmt", "custom")
        assert "local/shop_db@v2" in out
        loc = _catalog_location("local/shop_db@v2")
        assert loc.endswith(".dump")
        with open(loc, "rb") as f:
            assert f.read(5) == b"PGDMP"

    @check("backup archive (同一サーバーの BACK_UP_DB) -> v3")
    def _():
        out = run("backup", "local", "--db", "shop_db", "--mode", "archive")
        assert "local/shop_db@v3" in out
        rows = q(PORT_A, "BACK_UP_DB", "SELECT version_id FROM pgbackup_archive")
        assert ("local/shop_db@v3",) in rows
        chunks = q(PORT_A, "BACK_UP_DB", "SELECT count(*) FROM pgbackup_archive_chunk")
        assert chunks[0][0] >= 1

    @check("backup archive --dest remote (リモートの BACK_UP_DB) -> v4")
    def _():
        out = run("backup", "local", "--db", "shop_db", "--mode", "archive",
                  "--dest", "remote", "--note", "リモート保管")
        assert "local/shop_db@v4" in out
        rows = q(PORT_B, "BACK_UP_DB", "SELECT version_id FROM pgbackup_archive")
        assert ("local/shop_db@v4",) in rows

    # ---- 複製 (clone) 4 方向 ----

    @check("clone ローカル -> ローカル (別 DB 名)")
    def _():
        run("backup", "local", "--db", "shop_db", "--mode", "clone",
            "--dest", "local", "--dest-db", "shop_db_copy")
        assert shop_counts(PORT_A, "shop_db_copy") == (500, 2000)

    @check("clone ローカル -> リモート")
    def _():
        run("backup", "local", "--db", "shop_db", "--mode", "clone", "--dest", "remote")
        assert shop_counts(PORT_B, "shop_db") == (500, 2000)

    @check("clone リモート -> ローカル")
    def _():
        run("backup", "remote", "--db", "analytics_db", "--mode", "clone", "--dest", "local")
        assert q(PORT_A, "analytics_db", "SELECT count(*) FROM events")[0][0] == 300

    @check("clone リモート -> リモート (別 DB 名)")
    def _():
        run("backup", "remote", "--db", "analytics_db", "--mode", "clone",
            "--dest", "remote", "--dest-db", "analytics_copy")
        assert q(PORT_B, "analytics_copy", "SELECT count(*) FROM events")[0][0] == 300

    # ---- バージョン一覧 ----

    @check("versions: 4 バージョンが登録されている")
    def _():
        out = run("versions", "--profile", "local", "--db", "shop_db")
        for v in ("@v1", "@v2", "@v3", "@v4"):
            assert v in out, f"{v} が見つからない"
        assert "初回SQLバックアップ" in out

    # ---- リストア ----

    @check("restore v1 (plain SQL) -> リモートに別名で復元")
    def _():
        run("restore", "local/shop_db@v1", "--dest", "remote", "--db", "shop_db_restored")
        assert shop_counts(PORT_B, "shop_db_restored") == (500, 2000)

    @check("restore v3 (ローカルアーカイブ) -> ローカルに復元")
    def _():
        run("restore", "local/shop_db@v3", "--dest", "local", "--db", "shop_db_arch1")
        assert shop_counts(PORT_A, "shop_db_arch1") == (500, 2000)

    @check("restore --drop で既存 DB を上書き")
    def _():
        run("restore", "local/shop_db@v2", "--dest", "local", "--db", "shop_db_arch1",
            "--drop")
        assert shop_counts(PORT_A, "shop_db_arch1") == (500, 2000)

    @check("archive list / archive restore (リモートのアーカイブから直接)")
    def _():
        out = run("archive", "list", "remote")
        assert "local/shop_db@v4" in out
        run("archive", "restore", "remote", "local/shop_db@v4",
            "--dest", "local", "--db", "shop_db_arch2")
        assert shop_counts(PORT_A, "shop_db_arch2") == (500, 2000)

    @check("restore-file: SQL ファイルを直接リストア")
    def _():
        loc = _catalog_location("local/shop_db@v1")
        run("restore-file", loc, "--dest", "local", "--db", "shop_db_file")
        assert shop_counts(PORT_A, "shop_db_file") == (500, 2000)

    # ---- 複製後のスキーマ・データ型の同一性 ----

    @check("複製先でビュー・コメント・インデックスも保持される")
    def _():
        v = q(PORT_B, "shop_db",
              "SELECT count(*) FROM information_schema.views WHERE table_name='active_customers'")
        assert v[0][0] == 1
        cmt = q(PORT_B, "shop_db",
                "SELECT obj_description('public.customers'::regclass, 'pg_class')")
        assert cmt[0][0] == "顧客マスタ"
        idx = q(PORT_B, "shop_db",
                "SELECT count(*) FROM pg_indexes WHERE indexname='idx_orders_customer'")
        assert idx[0][0] == 1

    # ---- 全 DB バックアップ ----

    @check("backup --all: 全 DB を一括バックアップ (BACK_UP_DB は除外)")
    def _():
        out = run("backup", "remote", "--all", "--mode", "file", "--fmt", "custom")
        assert "analytics_db" in out and "analytics_copy" in out
        assert "BACK_UP_DB" not in out

    # ---- 操作履歴 ----

    @check("history: 操作履歴が記録されている")
    def _():
        out = run("history", "--limit", "100")
        assert "backup" in out and "restore" in out and "clone" in out and "OK" in out

    # ---- アーカイブ削除とカタログ同期 ----

    @check("archive delete: アーカイブとローカルカタログの両方から消える")
    def _():
        out = run("archive", "delete", "local", "local/shop_db@v3")
        assert "カタログからも削除" in out
        assert "@v3" not in run("versions", "--profile", "local", "--db", "shop_db")
        rows = q(PORT_A, "BACK_UP_DB", "SELECT version_id FROM pgbackup_archive")
        assert ("local/shop_db@v3",) not in rows

    # ---- 異常系 ----

    @check("異常系: 複製元と複製先が同一ならエラー")
    def _():
        out = run("backup", "local", "--db", "shop_db", "--mode", "clone",
                  "--dest", "local", expect_fail=True)
        assert "複製元と複製先が同一" in out

    @check("異常系: ホスト表記ゆれ (localhost vs 127.0.0.1) でも同一クラスタを検出")
    def _():
        run("conn", "add", "local_alias", "--host", "127.0.0.1", "--port", str(PORT_A),
            "--user", USER, "--overwrite")
        out = run("backup", "local", "--db", "shop_db", "--mode", "clone",
                  "--dest", "local_alias", "--drop", expect_fail=True)
        assert "複製元と複製先が同一" in out
        # 複製元が無事なことを確認
        assert shop_counts(PORT_A, "shop_db") == (500, 2000)

    @check("異常系: 既存 DB へ --drop なしでリストアするとエラー")
    def _():
        out = run("restore", "local/shop_db@v1", "--dest", "local",
                  "--db", "shop_db_file", expect_fail=True)
        assert "既に存在" in out

    @check("異常系: 存在しない DB のバックアップはエラー")
    def _():
        out = run("backup", "local", "--db", "no_such_db", "--mode", "file",
                  expect_fail=True)
        assert "存在しない" in out

    @check("異常系: 存在しないバージョンのリストアはエラー")
    def _():
        out = run("restore", "local/shop_db@v999", "--dest", "local", expect_fail=True)
        assert "見つかりません" in out

    @check("異常系: --out ファイル指定 + 複数 DB はエラー")
    def _():
        out = run("backup", "local", "--db", "shop_db", "--db", "inventory_db",
                  "--mode", "file", "--out", str(Path(home) / "multi.sql"),
                  expect_fail=True)
        assert "1 つだけ" in out

    @check("異常系: 既存ファイルへの --out 上書きはエラー")
    def _():
        target = Path(home) / "dup_out.sql"
        run("backup", "local", "--db", "inventory_db", "--mode", "file",
            "--out", str(target))
        out = run("backup", "local", "--db", "inventory_db", "--mode", "file",
                  "--out", str(target), expect_fail=True)
        assert "既に存在" in out

    @check("異常系: モードに無関係なフラグはエラー (--drop + archive)")
    def _():
        out = run("backup", "local", "--db", "shop_db", "--mode", "archive",
                  "--drop", expect_fail=True)
        assert "clone モード専用" in out

    @check("異常系: 不正なプロファイル名は登録できない")
    def _():
        out = run("conn", "add", "bad/name", "--host", "localhost",
                  "--port", str(PORT_A), expect_fail=True)
        assert "使用できません" in out

    print(f"\n結果: {passed} passed, {failed} failed")
    return 1 if failed else 0


def _catalog_location(version_id: str) -> str:
    db_path = Path(os.environ["PGBACKUP_HOME"]) / "catalog.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT location FROM backups WHERE version_id = ?", (version_id,)
        ).fetchone()
    assert row, f"カタログに {version_id} がない"
    return row[0]


if __name__ == "__main__":
    sys.exit(main())
