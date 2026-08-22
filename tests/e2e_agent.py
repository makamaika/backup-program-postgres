"""HTTPS バックアップエージェントの Python/Node 相互運用 E2E テスト。

前提: tests/setup_clusters.ps1 のクラスタ A (既定 localhost:55432) が稼働中。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

from pgbackup.agent.certs import generate_self_signed_certificate
from pgbackup.agent.client import AgentClient
from pgbackup.agent.config import (
    AgentProfile,
    AgentProfileStore,
    AgentServerConfig,
    initialize_server,
)
from pgbackup.backup import backup_agent_database_to_file, backup_to_agent
from pgbackup.catalog import Catalog
from pgbackup.config import Profile, ProfileStore
from pgbackup.errors import AgentError
from pgbackup.restore import restore_version

REPO = Path(__file__).resolve().parent.parent
NODE_BIN = REPO / "nodejs" / "bin" / "pgbackup.js"
PYTHON = sys.executable
PG_PORT = int(os.environ.get("E2E_PORT_A", "55432"))
PG_USER = os.environ.get("E2E_USER", "postgres")

passed = 0
failed = 0


def check(name: str, fn) -> None:
    global passed, failed
    try:
        fn()
        passed += 1
        print(f"  PASS  {name}")
    except Exception:
        failed += 1
        print(f"  FAIL  {name}")
        traceback.print_exc(limit=5)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def drop_db(name: str) -> None:
    with psycopg.connect(
        host="localhost", port=PG_PORT, user=PG_USER, dbname="postgres", autocommit=True
    ) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{name.replace(chr(34), chr(34) * 2)}"')


def count(db: str, table: str) -> int:
    with psycopg.connect(
        host="localhost", port=PG_PORT, user=PG_USER, dbname=db
    ) as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def run_node(home: Path, *args: str) -> str:
    env = os.environ.copy()
    env["PGBACKUP_HOME"] = str(home)
    result = subprocess.run(
        ["node", str(NODE_BIN), *args],
        cwd=REPO / "nodejs",
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def web_json(base: str, endpoint: str, body: dict | None = None) -> dict | list:
    request = urllib.request.Request(
        base + endpoint,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_web(base: str, process: subprocess.Popen) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Web サーバーが起動前に終了しました")
        try:
            web_json(base, "/api/agents")
            return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    raise RuntimeError("Web サーバーが起動しませんでした")


def wait_job(base: str, job_id: str) -> dict:
    for _ in range(240):
        job = web_json(base, f"/api/jobs/{job_id}")
        if job["status"] != "running":
            assert job["status"] == "done", job
            return job
        time.sleep(0.25)
    raise AssertionError("Web ジョブがタイムアウトしました")


def main() -> int:
    global passed, failed
    with tempfile.TemporaryDirectory(prefix="pgbackup-agent-e2e-") as tmp:
        home = Path(tmp)
        os.environ["PGBACKUP_HOME"] = str(home)
        port = free_port()
        public_url = f"https://localhost:{port}"
        server, pairing_path, _ = initialize_server(
            name="remote-agent",
            public_url=public_url,
            max_upload_bytes=1024 * 1024,
            allow_db_read=False,
            allow_db_restore=False,
            overwrite=True,
        )
        agent_store = AgentProfileStore()
        agent = agent_store.add_pairing_file(pairing_path)
        db_profile = Profile(
            name="local", host="localhost", port=PG_PORT, user=PG_USER, dbname="postgres"
        )
        ProfileStore().add(db_profile, overwrite=True)
        catalog = Catalog()
        log = open(home / "agent.log", "w", encoding="utf-8")
        process: subprocess.Popen | None = None

        def start() -> None:
            nonlocal process
            env = os.environ.copy()
            process = subprocess.Popen(
                [
                    PYTHON,
                    "-m",
                    "pgbackup",
                    "agent",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=REPO,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            client = AgentClient(agent, timeout=1)
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError((home / "agent.log").read_text(encoding="utf-8"))
                try:
                    client.health()
                    return
                except AgentError:
                    time.sleep(0.1)
            raise RuntimeError("エージェントが起動しませんでした")

        def stop() -> None:
            nonlocal process
            if process is None:
                return
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process = None

        try:
            start()
            client = AgentClient(agent)
            check("TLS 証明書固定 + トークン認証で health 成功", lambda: (
                client.health()["protocol"] == 1 or (_ for _ in ()).throw(AssertionError())
            ))

            def wrong_token():
                bad = AgentProfile(agent.name, agent.url, "x" * 48, agent.ca_cert)
                try:
                    AgentClient(bad).health()
                except AgentError as exc:
                    assert "401" in str(exc)
                    return
                raise AssertionError("不正トークンが受理された")

            check("不正トークンを 401 で拒否", wrong_token)

            def wrong_cert():
                cert = home / "wrong-cert.pem"
                key = home / "wrong-key.pem"
                generate_self_signed_certificate(
                    cert, key, common_name="wrong", public_url=public_url
                )
                bad = AgentProfile(agent.name, agent.url, agent.token, cert.read_text())
                try:
                    AgentClient(bad).health()
                except AgentError as exc:
                    assert "CERTIFICATE_VERIFY_FAILED" in str(exc)
                    return
                raise AssertionError("別証明書が受理された")

            check("証明書が異なるサーバーを拒否", wrong_cert)

            def oversized():
                path = home / "oversized.dump"
                path.write_bytes(b"x" * (1024 * 1024 + 1))
                try:
                    client.upload_backup(
                        path,
                        version_id="oversized/db@v1",
                        source_profile="oversized",
                        source_db="db",
                        fmt="custom",
                        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
                except AgentError as exc:
                    assert "上限" in str(exc)
                    assert not client.list_backups()
                    return
                raise AssertionError("容量超過が受理された")

            check("最大アップロード容量を超えるデータを 413 で拒否", oversized)

            def capabilities_off():
                assert client.health()["capabilities"] == ["storage"]
                try:
                    client.databases("local")
                except AgentError as exc:
                    assert "403" in str(exc)
                    return
                raise AssertionError("無効な DB 読み取りが許可された")

            check("DB 読取は既定で無効", capabilities_off)
            stop()

            server.allow_db_read = True
            server.allow_db_restore = True
            server.save()
            start()
            client = AgentClient(agent)

            check("エージェント経由で DB 一覧・構造を取得", lambda: (
                any(row["name"] == "shop_db" for row in client.databases("local"))
                and len(client.structure("local", "shop_db")["schemas"]) > 0
                or (_ for _ in ()).throw(AssertionError())
            ))

            records: dict[str, dict] = {}

            def push_backup():
                rec = backup_to_agent(catalog, db_profile, "shop_db", agent, note="agent e2e")
                records["push"] = rec
                assert rec["mode"] == "agent"
                assert client.get_backup(rec["agent_backup_id"])["sha256"] == rec["sha256"]

            check("ローカル DB を HTTPS エージェントへ保存", push_backup)

            def restore_local():
                drop_db("shop_agent_restore")
                restore_version(
                    catalog,
                    ProfileStore(),
                    records["push"]["version_id"],
                    db_profile,
                    dest_db="shop_agent_restore",
                    drop=True,
                    agents=agent_store,
                )
                assert count("shop_agent_restore", "public.customers") == 500

            check("エージェント保管バックアップを手元へ復元", restore_local)

            def pull_remote():
                rec = backup_agent_database_to_file(
                    catalog, agent, "local", "inventory_db", note="pull e2e"
                )
                records["pull"] = rec
                assert Path(rec["location"]).is_file()
                assert rec["size"] > 0

            check("エージェント端末の DB を手元へ pull", pull_remote)

            def reject_plain_remote_restore():
                plain = client.create_snapshot("local", "inventory_db", fmt="plain")
                try:
                    client.restore_remote_backup(
                        plain["id"], "local", dest_db="must_not_restore", drop=True
                    )
                except AgentError as exc:
                    assert "custom" in str(exc)
                    return
                raise AssertionError("plain SQL のリモート復元が許可された")

            check("plain SQL のリモート復元を拒否 (psqlメタコマンド対策)", reject_plain_remote_restore)

            def snapshot_and_remote_restore():
                snap = client.create_snapshot("local", "shop_db", note="remote snapshot")
                drop_db("shop_agent_remote_restore")
                result = client.restore_remote_backup(
                    snap["id"], "local", dest_db="shop_agent_remote_restore", drop=True
                )
                assert result["dest_db"] == "shop_agent_remote_restore"
                assert count("shop_agent_remote_restore", "sales.orders") == 2000

            check("エージェント内 snapshot → 同端末 PostgreSQL へ復元", snapshot_and_remote_restore)

            check("Node クライアントが同じ pairing/保管庫を利用", lambda: (
                "capabilities=" in run_node(home, "agent", "test", "remote-agent")
                and "shop_db" in run_node(home, "agent", "backups", "remote-agent")
                or (_ for _ in ()).throw(AssertionError())
            ))

            def node_push_restore():
                out = run_node(
                    home,
                    "backup",
                    "local",
                    "--db",
                    "inventory_db",
                    "--mode",
                    "agent",
                    "--agent",
                    "remote-agent",
                )
                version_id = next(
                    token for token in out.split() if token.startswith("local/inventory_db@v")
                )
                drop_db("inventory_agent_node_restore")
                run_node(
                    home,
                    "restore",
                    version_id,
                    "--dest",
                    "local",
                    "--db",
                    "inventory_agent_node_restore",
                    "--drop",
                )
                assert count("inventory_agent_node_restore", "public.items") == 100

            check("Node版 agent backup → agent restore", node_push_restore)

            def python_web_agent():
                web_port = free_port()
                base = f"http://127.0.0.1:{web_port}"
                env = os.environ.copy()
                web = subprocess.Popen(
                    [PYTHON, "-m", "pgbackup", "web", "--port", str(web_port)],
                    cwd=REPO,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    wait_web(base, web)
                    response = web_json(
                        base,
                        "/api/backup",
                        {
                            "profile": "local",
                            "dbs": ["inventory_db"],
                            "mode": "agent",
                            "fmt": "custom",
                            "agent": "remote-agent",
                            "note": "python web agent e2e",
                        },
                    )
                    job = wait_job(base, response["job_id"])
                    assert job["result"][0]["mode"] == "agent"
                    assert web_json(base, "/api/agent-backups?agent=remote-agent")
                finally:
                    web.terminate()
                    web.wait(timeout=10)

            check("Python Web: agentモード backup + 保管庫API", python_web_agent)

            def node_web_agent():
                web_port = free_port()
                base = f"http://127.0.0.1:{web_port}"
                env = os.environ.copy()
                web = subprocess.Popen(
                    ["node", str(NODE_BIN), "web", "--port", str(web_port)],
                    cwd=REPO / "nodejs",
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    wait_web(base, web)
                    agents_json = web_json(base, "/api/agents")
                    assert agents_json[0]["token"] == "********"
                    assert "ca_cert" not in agents_json[0]
                    dbs = web_json(
                        base,
                        "/api/agent-databases?agent=remote-agent&remote_profile=local",
                    )
                    assert any(row["name"] == "shop_db" for row in dbs)
                    assert web_json(base, "/api/agent-backups?agent=remote-agent")
                finally:
                    web.terminate()
                    web.wait(timeout=10)

            check("Node Web: pairing秘匿 + DB/保管庫プロキシAPI", node_web_agent)
        finally:
            stop()
            log.close()
            for name in (
                "shop_agent_restore",
                "shop_agent_remote_restore",
                "inventory_agent_node_restore",
                "must_not_restore",
            ):
                try:
                    drop_db(name)
                except Exception:
                    pass

    print(f"\n結果: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
