"""行編集・比較・merge/sync の Python/Node/CLI/Web 共通 E2E。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

from pgbackup import dataedit
from pgbackup.config import Profile, ProfileStore
from pgbackup.errors import DataEditError

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
NODE_BIN = REPO / "nodejs" / "bin" / "pgbackup.js"
SEED = REPO / "tests" / "seed_data_editor.sql"
PORT = int(os.environ.get("E2E_PORT_A", "55432"))
USER = os.environ.get("E2E_USER", "postgres")
PG_BIN = Path(os.environ.get("PGBACKUP_PG_BIN", r"C:\Program Files\PostgreSQL\18\bin"))

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
        traceback.print_exc(limit=6)


def seed() -> None:
    result = subprocess.run(
        [
            str(PG_BIN / "psql.exe"), "-h", "localhost", "-p", str(PORT),
            "-U", USER, "-d", "shop_db", "-v", "ON_ERROR_STOP=1", "-q", "-f", str(SEED),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def query(sql_text: str, params=()):
    with psycopg.connect(
        host="localhost", port=PORT, user=USER, dbname="shop_db"
    ) as conn:
        cursor = conn.execute(sql_text, params)
        return cursor.fetchall() if cursor.description else []


def run_cli(kind: str, *args: str, expect_fail: bool = False) -> str:
    command = [PYTHON, "-m", "pgbackup"] if kind == "python" else ["node", str(NODE_BIN)]
    result = subprocess.run(
        [*command, *args],
        cwd=REPO if kind == "python" else REPO / "nodejs",
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if expect_fail:
        assert result.returncode != 0, "失敗を期待しましたが成功しました"
        return result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def web_request(base: str, path: str, body: dict | None = None, raw: bool = False):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    return data.decode("utf-8") if raw else json.loads(data)


def wait_web(base: str, process: subprocess.Popen) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise AssertionError("Web サーバーが起動前に終了しました")
        try:
            web_request(base, "/api/profiles")
            return
        except urllib.error.URLError:
            time.sleep(0.1)
    raise AssertionError("Web サーバーが起動しませんでした")


def wait_job(base: str, job_id: str, expect_error: bool = False) -> dict:
    for _ in range(400):
        job = web_request(base, f"/api/jobs/{job_id}")
        if job["status"] != "running":
            if expect_error:
                assert job["status"] == "error", job
            else:
                assert job["status"] == "done", job
            return job
        time.sleep(0.25)
    raise AssertionError("ジョブがタイムアウトしました")


def compare_body() -> dict:
    return {
        "source_profile": "local",
        "source_db": "shop_db",
        "source_schema": "public",
        "source_table": "edit_source",
        "target_profile": "local",
        "target_db": "shop_db",
        "target_schema": "public",
        "target_table": "edit_target",
        "key_columns": ["id"],
        "max_rows": 10000,
        "preview_rows": 100,
    }


def exercise_web(kind: str) -> None:
    seed()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    command = (
        [PYTHON, "-m", "pgbackup", "web", "--port", str(port)]
        if kind == "python"
        else ["node", str(NODE_BIN), "web", "--port", str(port)]
    )
    process = subprocess.Popen(
        command,
        cwd=REPO if kind == "python" else REPO / "nodejs",
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_web(base, process)
        html = web_request(base, "/", raw=True)
        assert "データ編集・比較" in html
        page = web_request(
            base,
            "/api/data/rows?profile=local&db=shop_db&schema=public&table=edit_target&limit=20&offset=0",
        )
        assert page["metadata"]["row_key"] == ["id"] and len(page["rows"]) == 3
        inserted = web_request(base, "/api/data/insert", {
            "profile": "local", "db": "shop_db", "schema_name": "public", "table": "edit_target",
            "values": {"id": 8, "name": "web", "amount": "8.25", "meta": {"web": True}},
        })
        updated = web_request(base, "/api/data/update", {
            "profile": "local", "db": "shop_db", "schema_name": "public", "table": "edit_target",
            "key": {"id": 8}, "values": {"name": "WEB"}, "etag": inserted["etag"],
        })
        assert updated["values"]["name"] == "WEB"
        web_request(base, "/api/data/delete", {
            "profile": "local", "db": "shop_db", "schema_name": "public", "table": "edit_target",
            "key": {"id": 8}, "etag": updated["etag"],
        })
        plan = web_request(base, "/api/data/compare", compare_body())
        assert plan["summary"]["changed"] == 1
        merge = web_request(base, "/api/data/apply", {
            **compare_body(), "strategy": "merge", "plan_hash": plan["plan_hash"],
            "confirmation": plan["confirmation"],
        })
        assert wait_job(base, merge["job_id"])["result"]["updated"] == 1
        plan2 = web_request(base, "/api/data/compare", compare_body())
        sync = web_request(base, "/api/data/apply", {
            **compare_body(), "strategy": "sync", "plan_hash": plan2["plan_hash"],
            "confirmation": plan2["confirmation"],
        })
        assert wait_job(base, sync["job_id"])["result"]["deleted"] == 1
        final = web_request(base, "/api/data/compare", compare_body())
        assert final["summary"] == {
            "source_rows": 3, "target_rows": 3, "only_source": 0,
            "only_target": 0, "changed": 0, "same": 3,
        }
    finally:
        process.terminate()
        process.wait(timeout=10)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pgbackup-data-e2e-") as tmp:
        os.environ["PGBACKUP_HOME"] = tmp
        profile = Profile(
            name="local", host="localhost", port=PORT, user=USER, dbname="postgres"
        )
        ProfileStore().add(profile, overwrite=True)

        seed()

        def metadata_test():
            meta = dataedit.table_metadata(profile, "shop_db", "public", "edit_target")
            assert meta["row_key"] == ["id"] and meta["can_update"] and meta["can_delete"]
            families = {column["name"]: column["type_family"] for column in meta["columns"]}
            assert families["amount"] == "numeric" and families["meta"] == "json"
            assert families["tags"] == "array" and families["payload"] == "binary"
            assert dataedit.table_metadata(profile, "shop_db", "public", "edit_unique")["row_key"] == ["code"]
            assert not dataedit.table_metadata(profile, "shop_db", "public", "edit_no_key")["row_key"]

        check("メタデータ: 主キー・一意キーfallback・型ファミリー", metadata_test)

        def page_and_cross_etag():
            page = dataedit.read_rows(profile, "shop_db", "public", "edit_target", limit=2)
            assert len(page["rows"]) == 2 and page["total_rows"] == 3
            python_row = next(row for row in page["rows"] if row["key"] == {"id": 1})
            node_page = json.loads(run_cli(
                "node", "data", "rows", "local", "shop_db", "edit_target", "--limit", "2", "--json"
            ))
            node_row = next(row for row in node_page["rows"] if row["key"] == {"id": 1})
            assert python_row["etag"] == node_row["etag"]
            assert python_row["values"]["payload"] == "\\x0102"

        check("ページ表示とPython/Node共通ETag (複合データ型)", page_and_cross_etag)

        def crud_and_conflict():
            inserted = dataedit.insert_row(profile, "shop_db", "public", "edit_target", {
                "id": 5, "name": "x'); DROP TABLE edit_source; --", "amount": "5.50",
                "meta": {"safe": True}, "tags": ["x"], "payload": "\\x0506",
            })
            updated = dataedit.update_row(
                profile, "shop_db", "public", "edit_target", {"id": 5},
                {"name": "safe", "amount": "6.00"}, etag=inserted["etag"],
            )
            try:
                dataedit.update_row(
                    profile, "shop_db", "public", "edit_target", {"id": 5},
                    {"name": "stale"}, etag=inserted["etag"],
                )
                raise AssertionError("古いETagが受理された")
            except DataEditError as exc:
                assert "変更" in str(exc)
            dataedit.delete_row(
                profile, "shop_db", "public", "edit_target", {"id": 5}, etag=updated["etag"]
            )
            assert query("SELECT count(*) FROM edit_source")[0][0] == 3

        check("CRUD・値SQL注入防止・ETag競合拒否", crud_and_conflict)

        def identifier_injection():
            try:
                dataedit.read_rows(
                    profile, "shop_db", "public", "edit_target; DROP TABLE edit_source", limit=10
                )
                raise AssertionError("不正テーブル名が通った")
            except DataEditError:
                pass
            assert query("SELECT count(*) FROM edit_source")[0][0] == 3

        check("識別子SQL注入を拒否", identifier_injection)

        plans: dict[str, dict] = {}

        def compare_parity():
            python_plan = dataedit.compare_tables(
                profile, "shop_db", "public", "edit_source",
                profile, "shop_db", "public", "edit_target",
            )
            node_plan = json.loads(run_cli(
                "node", "data", "compare", "local", "shop_db", "edit_source",
                "--target-profile", "local", "--target-db", "shop_db",
                "--target-table", "edit_target", "--json",
            ))
            expected = {
                "source_rows": 3, "target_rows": 3, "only_source": 1,
                "only_target": 1, "changed": 1, "same": 1,
            }
            assert python_plan["summary"] == expected and node_plan["summary"] == expected
            assert python_plan["plan_hash"] == node_plan["plan_hash"]
            plans["initial"] = python_plan

        check("比較結果・plan_hashがPython/Nodeで一致", compare_parity)

        def stale_plan_rejected():
            plan = plans["initial"]
            query("UPDATE edit_target SET name = 'changed-after-preview' WHERE id = 2")
            try:
                dataedit.apply_table_plan(
                    profile, "shop_db", "public", "edit_source",
                    profile, "shop_db", "public", "edit_target",
                    strategy="merge", expected_plan_hash=plan["plan_hash"],
                    confirmation=plan["confirmation"],
                )
                raise AssertionError("古い計画が適用された")
            except DataEditError as exc:
                assert "再比較" in str(exc)
            assert query("SELECT count(*) FROM edit_target WHERE id = 3")[0][0] == 0

        check("比較後の変更を検知し古い計画を全ロールバック", stale_plan_rejected)

        def guards():
            seed()
            try:
                dataedit.compare_tables(
                    profile, "shop_db", "public", "edit_source",
                    profile, "shop_db", "public", "edit_incompatible",
                )
                raise AssertionError("型不一致が許可された")
            except DataEditError as exc:
                assert "互換性" in str(exc)
            try:
                dataedit.compare_tables(
                    profile, "shop_db", "public", "edit_source",
                    profile, "shop_db", "public", "edit_target", max_rows=2,
                )
                raise AssertionError("比較上限超過が許可された")
            except DataEditError as exc:
                assert "比較上限" in str(exc)
            plan = dataedit.compare_tables(
                profile, "shop_db", "public", "edit_source",
                profile, "shop_db", "public", "edit_target",
            )
            try:
                dataedit.apply_table_plan(
                    profile, "shop_db", "public", "edit_source",
                    profile, "shop_db", "public", "edit_target",
                    strategy="sync", expected_plan_hash=plan["plan_hash"], confirmation="wrong",
                )
                raise AssertionError("誤確認文字列が許可された")
            except DataEditError as exc:
                assert "確認文字列" in str(exc)

        check("型不一致・行数上限・確認文字列ガード", guards)

        def rls_guard():
            limited = Profile(
                name="rls", host="localhost", port=PORT,
                user="pgbackup_data_rls", dbname="postgres",
            )
            meta = dataedit.table_metadata(limited, "shop_db", "public", "edit_rls")
            assert meta["row_security_effective"] is True
            assert dataedit.read_rows(
                limited, "shop_db", "public", "edit_rls", limit=10
            )["total_rows"] == 1
            try:
                dataedit.compare_tables(
                    limited, "shop_db", "public", "edit_rls",
                    limited, "shop_db", "public", "edit_rls",
                )
                raise AssertionError("実効RLSテーブルの比較が許可された")
            except DataEditError as exc:
                assert "Row Level Security" in str(exc)

        check("実効RLSでは可視行だけ編集し全体比較・同期を拒否", rls_guard)

        def python_merge_sync():
            seed()
            plan = dataedit.compare_tables(
                profile, "shop_db", "public", "edit_source",
                profile, "shop_db", "public", "edit_target",
            )
            merged = dataedit.apply_table_plan(
                profile, "shop_db", "public", "edit_source",
                profile, "shop_db", "public", "edit_target",
                strategy="merge", expected_plan_hash=plan["plan_hash"], confirmation=plan["confirmation"],
            )
            assert (merged["inserted"], merged["updated"], merged["deleted"]) == (1, 1, 0)
            plan2 = dataedit.compare_tables(
                profile, "shop_db", "public", "edit_source",
                profile, "shop_db", "public", "edit_target",
            )
            synced = dataedit.apply_table_plan(
                profile, "shop_db", "public", "edit_source",
                profile, "shop_db", "public", "edit_target",
                strategy="sync", expected_plan_hash=plan2["plan_hash"], confirmation=plan2["confirmation"],
            )
            assert synced["deleted"] == 1

        check("Pythonコア merge → sync", python_merge_sync)

        def sequence_integrity(kind: str):
            seed()
            if kind == "python":
                plan = dataedit.compare_tables(
                    profile, "shop_db", "public", "edit_identity_source",
                    profile, "shop_db", "public", "edit_identity_target",
                )
                dataedit.apply_table_plan(
                    profile, "shop_db", "public", "edit_identity_source",
                    profile, "shop_db", "public", "edit_identity_target",
                    strategy="merge", expected_plan_hash=plan["plan_hash"],
                    confirmation=plan["confirmation"],
                )
            else:
                plan = json.loads(run_cli(
                    "node", "data", "compare", "local", "shop_db", "edit_identity_source",
                    "--target-profile", "local", "--target-db", "shop_db",
                    "--target-table", "edit_identity_target", "--json",
                ))
                run_cli(
                    "node", "data", "apply", "local", "shop_db", "edit_identity_source",
                    "--target-profile", "local", "--target-db", "shop_db",
                    "--target-table", "edit_identity_target", "--strategy", "merge",
                    "--plan-hash", plan["plan_hash"], "--confirm", plan["confirmation"],
                )
            next_id = query(
                "INSERT INTO edit_identity_target(value) VALUES ('next') RETURNING id"
            )[0][0]
            assert next_id == 11

        check("Python merge後もidentity sequenceが衝突しない", lambda: sequence_integrity("python"))
        check("Node merge後もidentity sequenceが衝突しない", lambda: sequence_integrity("node"))

        def transactional_rollback(kind: str):
            seed()
            if kind == "python":
                plan = dataedit.compare_tables(
                    profile, "shop_db", "public", "edit_rollback_source",
                    profile, "shop_db", "public", "edit_rollback_target",
                )
                try:
                    dataedit.apply_table_plan(
                        profile, "shop_db", "public", "edit_rollback_source",
                        profile, "shop_db", "public", "edit_rollback_target",
                        strategy="merge", expected_plan_hash=plan["plan_hash"],
                        confirmation=plan["confirmation"],
                    )
                    raise AssertionError("制約違反のmergeが成功した")
                except DataEditError as exc:
                    assert "ロールバック" in str(exc)
            else:
                plan = json.loads(run_cli(
                    "node", "data", "compare", "local", "shop_db", "edit_rollback_source",
                    "--target-profile", "local", "--target-db", "shop_db",
                    "--target-table", "edit_rollback_target", "--json",
                ))
                output = run_cli(
                    "node", "data", "apply", "local", "shop_db", "edit_rollback_source",
                    "--target-profile", "local", "--target-db", "shop_db",
                    "--target-table", "edit_rollback_target", "--strategy", "merge",
                    "--plan-hash", plan["plan_hash"], "--confirm", plan["confirmation"],
                    expect_fail=True,
                )
                assert "ロールバック" in output
            assert query("SELECT id, value FROM edit_rollback_target ORDER BY id") == [(1, 1)]

        check("Python merge途中の制約違反を全ロールバック", lambda: transactional_rollback("python"))
        check("Node merge途中の制約違反を全ロールバック", lambda: transactional_rollback("node"))

        def cli_flow(kind: str):
            seed()
            page = json.loads(run_cli(
                kind, "data", "rows", "local", "shop_db", "edit_target", "--limit", "10", "--json"
            ))
            assert len(page["rows"]) == 3
            plan = json.loads(run_cli(
                kind, "data", "compare", "local", "shop_db", "edit_source",
                "--target-profile", "local", "--target-db", "shop_db",
                "--target-table", "edit_target", "--json",
            ))
            run_cli(
                kind, "data", "apply", "local", "shop_db", "edit_source",
                "--target-profile", "local", "--target-db", "shop_db",
                "--target-table", "edit_target", "--strategy", "merge",
                "--plan-hash", plan["plan_hash"], "--confirm", plan["confirmation"],
            )
            plan2 = json.loads(run_cli(
                kind, "data", "compare", "local", "shop_db", "edit_source",
                "--target-profile", "local", "--target-db", "shop_db",
                "--target-table", "edit_target", "--json",
            ))
            run_cli(
                kind, "data", "apply", "local", "shop_db", "edit_source",
                "--target-profile", "local", "--target-db", "shop_db",
                "--target-table", "edit_target", "--strategy", "sync",
                "--plan-hash", plan2["plan_hash"], "--confirm", plan2["confirmation"],
            )
            assert query("SELECT count(*) FROM edit_target")[0][0] == 3

        check("Python CLI rows/compare/merge/sync", lambda: cli_flow("python"))
        check("Node CLI rows/compare/merge/sync", lambda: cli_flow("node"))
        check("Python Web CRUD/compare/merge/sync/UI", lambda: exercise_web("python"))
        check("Node Web CRUD/compare/merge/sync/UI", lambda: exercise_web("node"))

    print(f"\n結果: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
