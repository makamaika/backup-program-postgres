"""Web API の E2E テスト。

前提: tests/e2e_cli.py を先に実行していること (プロファイル・テスト DB を再利用する)。
実サーバー (uvicorn) をサブプロセスで起動して HTTP 経由で検証する。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = int(os.environ.get("E2E_WEB_PORT", "8560"))
PORT_A = int(os.environ.get("E2E_PORT_A", "55432"))
USER = os.environ.get("E2E_USER", "postgres")
BASE = f"http://127.0.0.1:{PORT}"

passed, failed = 0, 0


def api(path: str, body: dict | None = None, method: str | None = None, raw: bool = False):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        data = res.read()
    return data.decode("utf-8") if raw else json.loads(data)


def wait_job(job_id: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = api(f"/api/jobs/{job_id}")
        if job["status"] != "running":
            assert job["status"] == "done", f"ジョブ失敗: {job.get('error')}\n{job.get('log')}"
            return job
        time.sleep(0.5)
    raise AssertionError(f"ジョブ {job_id} がタイムアウト")


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


def drop_db(name: str) -> None:
    with psycopg.connect(host="localhost", port=PORT_A, user=USER, dbname="postgres",
                         autocommit=True) as c:
        c.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        c.execute(f'DROP DATABASE IF EXISTS "{name}"')


def main() -> int:
    assert os.environ.get("PGBACKUP_HOME"), "PGBACKUP_HOME を設定してください"
    drop_db("inventory_web_restore")

    server = subprocess.Popen(
        [PY, "-m", "pgbackup", "web", "--port", str(PORT)],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            try:
                api("/api/profiles")
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.5)
        else:
            raise RuntimeError("Web サーバーが起動しませんでした")

        @check("GET /: UI が返る")
        def _():
            html = api("/", raw=True)
            assert "pgbackup" in html and "バックアップ" in html

        @check("GET /api/profiles")
        def _():
            names = {p["name"] for p in api("/api/profiles")}
            assert {"local", "remote"} <= names
            # パスワードが平文で返らないこと
            for p in api("/api/profiles"):
                assert p.get("password") in (None, "********")

        @check("POST /api/profiles/{name}/test")
        def _():
            r = api("/api/profiles/local/test", body={})
            assert r["ok"] and r["server_version"]

        @check("GET /api/databases?profile=local")
        def _():
            names = {d["name"] for d in api("/api/databases?profile=local")}
            assert "shop_db" in names and "inventory_db" in names

        @check("GET structure: スキーマ・テーブル・データ型内訳")
        def _():
            s = api("/api/structure?profile=local&db=shop_db")
            schemas = {sc["name"] for sc in s["schemas"]}
            assert {"public", "sales"} <= schemas
            types = {d["data_type"] for d in s["datatype_summary"]}
            assert "jsonb" in types and "bytea" in types and "numeric(12,2)" in types

        @check("GET table detail: カラム・PK・インデックス")
        def _():
            d = api("/api/table?profile=local&db=shop_db&schema=public&table=customers")
            cols = {c["name"]: c for c in d["columns"]}
            assert cols["id"]["is_primary_key"] is True
            assert cols["profile"]["data_type"] == "jsonb"
            assert any("customers_email_key" in i["name"] for i in d["indexes"])

        @check("GET sample: データのサンプルと総行数")
        def _():
            s = api("/api/sample?profile=local&db=shop_db&schema=public&table=customers&limit=5")
            assert s["total_rows"] == 500 and len(s["rows"]) == 5
            assert "name" in s["columns"] and s["approx"] is False

        @check("POST /api/backup (file) + ジョブ完了")
        def _():
            r = api("/api/backup", body={
                "profile": "local", "dbs": ["inventory_db"], "mode": "file",
                "fmt": "custom", "note": "web経由",
            })
            job = wait_job(r["job_id"])
            assert job["result"][0]["version_id"].startswith("local/inventory_db@v")

        @check("GET /api/versions に web バックアップが載る")
        def _():
            rows = api("/api/versions?profile=local&db=inventory_db")
            assert any(r["note"] == "web経由" for r in rows)

        @check("POST /api/restore + データ検証")
        def _():
            rows = api("/api/versions?profile=local&db=inventory_db")
            vid = [r for r in rows if r["note"] == "web経由"][-1]["version_id"]
            r = api("/api/restore", body={
                "version_id": vid, "dest": "local", "db": "inventory_web_restore",
            })
            wait_job(r["job_id"])
            with psycopg.connect(host="localhost", port=PORT_A, user=USER,
                                 dbname="inventory_web_restore") as c:
                assert c.execute("SELECT count(*) FROM items").fetchone()[0] == 100

        @check("GET /api/archive にアーカイブが載る")
        def _():
            # CLI テストで remote の BACK_UP_DB に shop_db@v4 を保存済み
            rows = api("/api/archive?profile=remote")
            assert any(r["source_db"] == "shop_db" for r in rows)

        @check("GET /api/history")
        def _():
            rows = api("/api/history?limit=50")
            assert rows and all("kind" in r for r in rows)

        @check("異常系: 存在しないプロファイルは 400")
        def _():
            try:
                api("/api/databases?profile=no_such")
                raise AssertionError("400 を期待したが成功した")
            except urllib.error.HTTPError as e:
                assert e.code == 400
                detail = json.loads(e.read())["detail"]
                assert "no_such" in detail

        @check("異常系: 対象 DB 未指定の backup は 400")
        def _():
            try:
                api("/api/backup", body={"profile": "local", "dbs": [], "mode": "file"})
                raise AssertionError("400 を期待したが成功した")
            except urllib.error.HTTPError as e:
                assert e.code == 400

    finally:
        server.terminate()
        server.wait(timeout=10)

    print(f"\n結果: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
