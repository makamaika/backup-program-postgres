"""ローカル Web UI (FastAPI)。

バックアップ / リストアは時間がかかるためバックグラウンドジョブとして実行し、
/api/jobs/{id} のポーリングで進捗と結果を返す。
"""

from __future__ import annotations

import os
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel

from .. import archive as archive_mod
from .. import backup as backup_mod
from .. import config
from .. import inspect as inspect_mod
from .. import restore as restore_mod
from ..agent.client import AgentClient
from ..agent.config import AgentProfile, AgentProfileStore
from ..catalog import Catalog
from ..config import DEFAULT_ARCHIVE_DB, Profile, ProfileStore
from ..db import server_version
from ..errors import PgBackupError

STATIC_DIR = Path(__file__).parent / "static"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobManager:
    """メモリ上の簡易ジョブ管理 (Web プロセス内のみ)。"""

    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, title: str, fn) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id, "kind": kind, "title": title, "status": "running",
                "log": [], "result": None, "error": None,
                "started_at": _now(), "finished_at": None,
            }

        def runner():
            try:
                result = fn(lambda msg: self.append_log(job_id, msg))
                with self._lock:
                    self._jobs[job_id]["status"] = "done"
                    self._jobs[job_id]["result"] = result
            except Exception as e:  # noqa: BLE001 - ジョブ失敗はすべて捕捉して UI に返す
                with self._lock:
                    self._jobs[job_id]["status"] = "error"
                    self._jobs[job_id]["error"] = str(e)
                    self._jobs[job_id]["log"].append(traceback.format_exc(limit=3))
            finally:
                with self._lock:
                    self._jobs[job_id]["finished_at"] = _now()

        threading.Thread(target=runner, daemon=True).start()
        return job_id

    def append_log(self, job_id: str, msg: str) -> None:
        with self._lock:
            self._jobs[job_id]["log"].append(f"[{_now()}] {msg}")

    def get(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise HTTPException(404, f"ジョブ {job_id} が見つかりません")
            return dict(job)

    def list(self) -> list[dict]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j["started_at"], reverse=True)


# ---------------------------------------------------------------- リクエストモデル

class ProfileIn(BaseModel):
    name: str
    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: str | None = None
    dbname: str = "postgres"
    sslmode: str | None = None
    overwrite: bool = False


class BackupIn(BaseModel):
    profile: str
    dbs: list[str] = []
    all: bool = False
    mode: str = "file"  # file | archive | clone
    fmt: str | None = None
    dest: str | None = None
    agent: str | None = None
    dest_db: str | None = None
    archive_db: str = DEFAULT_ARCHIVE_DB
    drop: bool = False
    keep_owner: bool = False
    note: str | None = None


class RestoreIn(BaseModel):
    version_id: str
    dest: str
    db: str | None = None
    drop: bool = False
    keep_owner: bool = False


class RestoreFileIn(BaseModel):
    path: str
    dest: str
    db: str
    drop: bool = False
    keep_owner: bool = False


class ArchiveRestoreIn(BaseModel):
    profile: str
    archive_db: str = DEFAULT_ARCHIVE_DB
    version_id: str
    dest: str
    db: str | None = None
    drop: bool = False
    keep_owner: bool = False


class AgentIn(BaseModel):
    name: str
    url: str
    token: str
    ca_cert: str
    overwrite: bool = False


class AgentPullIn(BaseModel):
    agent: str
    remote_profile: str
    db: str
    fmt: str = "custom"
    keep_owner: bool = False
    note: str | None = None


class AgentSnapshotIn(AgentPullIn):
    version_id: str | None = None


class AgentRemoteRestoreIn(BaseModel):
    agent: str
    backup_id: str
    remote_profile: str
    db: str | None = None
    drop: bool = False
    keep_owner: bool = False


# ---------------------------------------------------------------- アプリ本体

def create_app() -> FastAPI:
    app = FastAPI(title="pgbackup", docs_url="/api/docs")
    allowed_hosts = [
        host.strip()
        for host in os.environ.get(
            "PGBACKUP_WEB_ALLOWED_HOSTS", "127.0.0.1,localhost,::1"
        ).split(",")
        if host.strip()
    ]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    store = ProfileStore()
    agents = AgentProfileStore()
    catalog = Catalog()
    jobs = JobManager()
    config.cleanup_tmp()

    @app.middleware("http")
    async def same_origin_guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin:
                try:
                    origin_host = urlparse(origin).netloc.lower()
                except ValueError:
                    origin_host = ""
                request_host = request.headers.get("host", "").lower()
                if not origin_host or origin_host != request_host:
                    response = JSONResponse(
                        status_code=403,
                        content={"detail": "異なる Origin からの操作は拒否されました"},
                    )
                    response.headers["Cache-Control"] = "no-store"
                    return response
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(PgBackupError)
    async def _pgbackup_error(request: Request, exc: PgBackupError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # ---- 静的ファイル ----

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    # ---- 接続プロファイル ----

    @app.get("/api/profiles")
    def list_profiles():
        return [p.to_dict(mask_password=True) for p in store.list()]

    @app.post("/api/profiles")
    def add_profile(body: ProfileIn):
        profile = Profile(
            name=body.name, host=body.host, port=body.port, user=body.user,
            password=body.password, dbname=body.dbname, sslmode=body.sslmode,
        )
        store.add(profile, overwrite=body.overwrite)
        return {"ok": True}

    @app.delete("/api/profiles/{name}")
    def remove_profile(name: str):
        store.remove(name)
        return {"ok": True}

    @app.post("/api/profiles/{name}/test")
    def test_profile(name: str):
        profile = store.get(name)
        try:
            return {"ok": True, "server_version": server_version(profile)}
        except PgBackupError as e:
            return {"ok": False, "error": str(e)}

    # ---- HTTPS バックアップエージェント ----

    @app.get("/api/agents")
    def list_agents():
        return [profile.to_dict(mask_token=True) for profile in agents.list()]

    @app.post("/api/agents")
    def add_agent(body: AgentIn):
        agents.add(
            AgentProfile(
                name=body.name,
                url=body.url,
                token=body.token,
                ca_cert=body.ca_cert,
            ),
            overwrite=body.overwrite,
        )
        return {"ok": True}

    @app.delete("/api/agents/{name}")
    def remove_agent(name: str):
        agents.remove(name)
        return {"ok": True}

    @app.post("/api/agents/{name}/test")
    def test_agent(name: str):
        return AgentClient(agents.get(name)).health()

    @app.get("/api/agent-backups")
    def agent_backups(agent: str, limit: int = 200):
        return AgentClient(agents.get(agent)).list_backups(limit=limit)

    @app.delete("/api/agent-backups/{backup_id}")
    def delete_agent_backup(backup_id: str, agent: str):
        result = AgentClient(agents.get(agent)).delete_backup(backup_id)
        for rec in catalog.list_backups():
            if rec.get("agent_profile") == agent and rec.get("agent_backup_id") == backup_id:
                catalog.delete_backup(rec["version_id"])
        return result

    @app.get("/api/agent-databases")
    def agent_databases(agent: str, remote_profile: str, templates: bool = False):
        return AgentClient(agents.get(agent)).databases(remote_profile, templates)

    @app.get("/api/agent-structure")
    def agent_structure(agent: str, remote_profile: str, db: str):
        return AgentClient(agents.get(agent)).structure(remote_profile, db)

    @app.get("/api/agent-table")
    def agent_table(
        agent: str, remote_profile: str, db: str, schema: str, table: str
    ):
        return AgentClient(agents.get(agent)).table(
            remote_profile, db, schema, table
        )

    @app.get("/api/agent-sample")
    def agent_sample(
        agent: str,
        remote_profile: str,
        db: str,
        schema: str,
        table: str,
        limit: int = 10,
    ):
        return AgentClient(agents.get(agent)).sample(
            remote_profile, db, schema, table, limit
        )

    # ---- データベース検査 ----
    # DB / スキーマ / テーブル名は '/' 等を含み得るためパスではなくクエリで受ける

    @app.get("/api/databases")
    def list_dbs(profile: str, templates: bool = False):
        return inspect_mod.list_databases(store.get(profile), include_templates=templates)

    @app.get("/api/structure")
    def db_structure(profile: str, db: str):
        return inspect_mod.database_structure(store.get(profile), db)

    @app.get("/api/table")
    def table_detail(profile: str, db: str, schema: str, table: str):
        p = store.get(profile)
        return {
            "columns": inspect_mod.table_columns(p, db, schema, table),
            "indexes": inspect_mod.table_indexes(p, db, schema, table),
        }

    @app.get("/api/sample")
    def table_sample(profile: str, db: str, schema: str, table: str, limit: int = 10):
        return inspect_mod.table_sample(store.get(profile), db, schema, table, limit)

    # ---- バックアップ / リストア (ジョブ) ----

    @app.post("/api/backup")
    def do_backup(body: BackupIn):
        source = store.get(body.profile)
        dbs = backup_mod.resolve_target_dbs(
            source, body.dbs or None, all_dbs=body.all, archive_db=body.archive_db
        )
        fmt = backup_mod.check_mode_options(
            body.mode, fmt=body.fmt, dest=body.dest, dest_db=body.dest_db,
            agent=body.agent, drop=body.drop, keep_owner=body.keep_owner, n_dbs=len(dbs),
        )
        if body.mode == "clone":
            dest = store.get(body.dest)
        elif body.mode == "archive":
            dest = store.get(body.dest) if body.dest else source
        elif body.mode == "agent":
            agent_profile = agents.get(body.agent)
            dest = None
        else:
            dest = None

        def run(log):
            results = []
            for db in dbs:
                log(f"{db} のバックアップを開始 ({body.mode})")
                if body.mode == "clone":
                    r = backup_mod.clone_database(
                        catalog, source, db, dest, body.dest_db or db,
                        drop=body.drop, keep_owner=body.keep_owner, note=body.note,
                    )
                elif body.mode == "archive":
                    r = backup_mod.backup_to_archive(
                        catalog, source, db, dest, archive_db=body.archive_db, note=body.note
                    )
                elif body.mode == "agent":
                    r = backup_mod.backup_to_agent(
                        catalog,
                        source,
                        db,
                        agent_profile,
                        fmt=fmt,
                        note=body.note,
                        keep_owner=body.keep_owner,
                    )
                else:
                    r = backup_mod.backup_to_file(
                        catalog, source, db, fmt=fmt, note=body.note,
                        keep_owner=body.keep_owner,
                    )
                results.append(r)
                log(f"{db} 完了")
            return results

        title = f"バックアップ ({body.mode}): {body.profile} / {', '.join(dbs)}"
        return {"job_id": jobs.submit("backup", title, run)}

    @app.post("/api/restore")
    def do_restore(body: RestoreIn):
        dest = store.get(body.dest)
        rec = catalog.get_backup(body.version_id)  # 存在チェックを先に行う

        def run(log):
            log(f"{body.version_id} を {dest.name} に復元中")
            return restore_mod.restore_version(
                catalog, store, body.version_id, dest,
                dest_db=body.db, drop=body.drop, keep_owner=body.keep_owner,
                agents=agents,
            )

        title = f"リストア: {body.version_id} -> {body.dest}:{body.db or rec['db']}"
        return {"job_id": jobs.submit("restore", title, run)}

    @app.post("/api/agent-pull")
    def agent_pull(body: AgentPullIn):
        agent_profile = agents.get(body.agent)

        def run(log):
            log(f"{body.agent}:{body.remote_profile}/{body.db} を手元へ取得中")
            return backup_mod.backup_agent_database_to_file(
                catalog,
                agent_profile,
                body.remote_profile,
                body.db,
                fmt=body.fmt,
                note=body.note,
                keep_owner=body.keep_owner,
            )

        return {
            "job_id": jobs.submit(
                "backup",
                f"エージェントから取得: {body.agent}:{body.remote_profile}/{body.db}",
                run,
            )
        }

    @app.post("/api/agent-snapshot")
    def agent_snapshot(body: AgentSnapshotIn):
        client = AgentClient(agents.get(body.agent))

        def run(log):
            log(f"{body.agent} 内で {body.remote_profile}/{body.db} をスナップショット中")
            return client.create_snapshot(
                body.remote_profile,
                body.db,
                version_id=body.version_id,
                fmt=body.fmt,
                keep_owner=body.keep_owner,
                note=body.note,
            )

        return {
            "job_id": jobs.submit(
                "backup",
                f"エージェント内スナップショット: {body.agent}/{body.db}",
                run,
            )
        }

    @app.post("/api/agent-remote-restore")
    def agent_remote_restore(body: AgentRemoteRestoreIn):
        client = AgentClient(agents.get(body.agent))

        def run(log):
            log(f"{body.backup_id} を {body.remote_profile}:{body.db or '(元DB名)'} に復元中")
            return client.restore_remote_backup(
                body.backup_id,
                body.remote_profile,
                dest_db=body.db,
                drop=body.drop,
                keep_owner=body.keep_owner,
            )

        return {
            "job_id": jobs.submit(
                "restore",
                f"エージェント側リストア: {body.agent}:{body.backup_id}",
                run,
            )
        }

    @app.post("/api/restore-file")
    def do_restore_file(body: RestoreFileIn):
        dest = store.get(body.dest)

        def run(log):
            log(f"{body.path} を {dest.name}:{body.db} に復元中")
            return restore_mod.restore_dump_file(
                catalog, Path(body.path), dest, body.db,
                drop=body.drop, keep_owner=body.keep_owner,
            )

        title = f"ファイルからリストア: {body.path} -> {body.dest}:{body.db}"
        return {"job_id": jobs.submit("restore", title, run)}

    @app.post("/api/restore-archive")
    def do_restore_archive(body: ArchiveRestoreIn):
        host = store.get(body.profile)
        dest = store.get(body.dest)

        def run(log):
            log(f"アーカイブ {body.profile}:{body.archive_db} から {body.version_id} を復元中")
            return restore_mod.restore_from_archive(
                catalog, host, body.archive_db, body.version_id, dest,
                dest_db=body.db, drop=body.drop, keep_owner=body.keep_owner,
            )

        title = f"アーカイブからリストア: {body.version_id} -> {body.dest}"
        return {"job_id": jobs.submit("restore", title, run)}

    # ---- バージョン / アーカイブ / 履歴 ----

    @app.get("/api/versions")
    def versions(profile: str | None = None, db: str | None = None):
        return catalog.list_backups(profile=profile, db=db)

    @app.get("/api/archive")
    def archive_list(profile: str, archive_db: str = DEFAULT_ARCHIVE_DB):
        return archive_mod.list_archive(store.get(profile), archive_db)

    @app.get("/api/history")
    def history(limit: int = 100):
        return catalog.list_operations(limit=limit)

    # ---- ジョブ ----

    @app.get("/api/jobs")
    def job_list():
        return jobs.list()

    @app.get("/api/jobs/{job_id}")
    def job_get(job_id: str):
        return jobs.get(job_id)

    return app
