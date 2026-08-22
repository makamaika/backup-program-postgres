"""一時起動する HTTPS バックアップエージェントサーバー。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .. import inspect as inspect_mod
from .. import pgtools, restore as restore_mod
from ..catalog import Catalog
from ..config import ProfileStore
from ..errors import AgentError, PgBackupError
from . import PROTOCOL_VERSION
from .config import AgentServerConfig, current_server_fingerprint
from .store import AgentRepository


class DumpIn(BaseModel):
    profile: str
    db: str
    fmt: str = "custom"
    keep_owner: bool = False


class SnapshotIn(DumpIn):
    version_id: str | None = None
    note: str | None = Field(default=None, max_length=1000)


class RemoteRestoreIn(BaseModel):
    profile: str
    db: str | None = None
    drop: bool = False
    keep_owner: bool = False


class UploadMetadata(BaseModel):
    version_id: str = Field(min_length=1, max_length=256)
    source_profile: str = Field(min_length=1, max_length=128)
    source_db: str = Field(min_length=1, max_length=256)
    fmt: str
    note: str | None = Field(default=None, max_length=2000)


class _AuthGuard:
    """高エントロピートークン認証 + 認証失敗の簡易レート制限。"""

    def __init__(self, token_sha256: str):
        self.expected = token_sha256
        self.failures: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with self.lock:
            recent = self.failures[client]
            while recent and recent[0] < now - 60:
                recent.popleft()
            if len(recent) >= 10:
                raise HTTPException(429, "認証失敗が多すぎます。しばらく待ってください")

        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not token or not hmac.compare_digest(actual, self.expected):
            with self.lock:
                self.failures[client].append(now)
            raise HTTPException(
                401,
                "エージェントトークンが不正です",
                headers={"WWW-Authenticate": "Bearer"},
            )
        with self.lock:
            self.failures.pop(client, None)


def _snapshot_version(agent_name: str, db: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{agent_name}/{db}@{stamp}-{uuid.uuid4().hex[:8]}"


def create_agent_app(config: AgentServerConfig) -> FastAPI:
    config.validate()
    repository = AgentRepository(Path(config.storage_dir))
    tls_fingerprint = current_server_fingerprint(config)
    profiles = ProfileStore()
    catalog = Catalog()
    auth = _AuthGuard(config.token_sha256)
    operation_slots = asyncio.Semaphore(config.max_concurrent_operations)

    app = FastAPI(
        title="pgbackup-agent",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def require_auth(request: Request) -> None:
        auth.check(request)

    def require_db_read() -> None:
        if not config.allow_db_read:
            raise HTTPException(
                403,
                "このエージェントでは DB 読み取り機能が無効です。"
                "server.json の allow_db_read を明示的に有効化してください。",
            )

    def require_db_restore() -> None:
        if not config.allow_db_restore:
            raise HTTPException(
                403,
                "このエージェントでは DB 復元機能が無効です。"
                "server.json の allow_db_restore を明示的に有効化してください。",
            )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(PgBackupError)
    async def pgbackup_error(request: Request, exc: PgBackupError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/v1/health", dependencies=[Depends(require_auth)])
    def health():
        return {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "name": config.name,
            "capabilities": config.capabilities,
            "max_upload_bytes": config.max_upload_bytes,
            "max_storage_bytes": config.max_storage_bytes,
            "storage_bytes_used": repository.storage_size(),
            "fingerprint_sha256": tls_fingerprint,
        }

    @app.get("/v1/backups", dependencies=[Depends(require_auth)])
    def list_backups(limit: int = 200):
        return repository.list(limit=limit)

    @app.get("/v1/backups/{backup_id}", dependencies=[Depends(require_auth)])
    def get_backup(backup_id: str):
        return repository.get(backup_id)

    @app.get("/v1/backups/{backup_id}/content", dependencies=[Depends(require_auth)])
    def download_backup(backup_id: str):
        record = repository.get(backup_id)
        path = repository.object_path(record)
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=f"{record['version_id'].replace('/', '_')}.dump",
            headers={
                "X-Pgbackup-Sha256": record["sha256"],
                "X-Pgbackup-Format": record["fmt"],
            },
        )

    @app.post("/v1/backups", dependencies=[Depends(require_auth)], status_code=201)
    async def upload_backup(request: Request):
        encoded_metadata = request.headers.get("x-pgbackup-metadata", "")
        if not encoded_metadata or len(encoded_metadata) > 8192:
            raise HTTPException(400, "X-Pgbackup-Metadata ヘッダーが不正です")
        try:
            padding = "=" * (-len(encoded_metadata) % 4)
            metadata = UploadMetadata.model_validate_json(
                base64.b64decode(
                    encoded_metadata + padding, altchars=b"-_", validate=True
                ).decode("utf-8")
            )
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise HTTPException(400, "X-Pgbackup-Metadata ヘッダーが不正です") from exc
        if metadata.fmt not in ("plain", "custom"):
            raise HTTPException(400, "fmt は plain または custom を指定してください")
        for value in (metadata.version_id, metadata.source_profile, metadata.source_db):
            if any(ord(ch) < 32 for ch in value):
                raise HTTPException(400, "バックアップメタデータに制御文字は使用できません")
        content_encoding = request.headers.get("content-encoding")
        if content_encoding:
            raise HTTPException(415, "Content-Encoding は使用できません")
        content_length = request.headers.get("content-length")
        declared_size: int | None = None
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise HTTPException(400, "Content-Length が不正です") from exc
            if declared_size < 0:
                raise HTTPException(400, "Content-Length が不正です")
            if declared_size > config.max_upload_bytes:
                raise HTTPException(413, "アップロードサイズが上限を超えています")

        expected_sha = request.headers.get("x-pgbackup-sha256", "").lower()
        if len(expected_sha) != 64 or any(ch not in "0123456789abcdef" for ch in expected_sha):
            raise HTTPException(400, "X-Pgbackup-Sha256 ヘッダーが不正です")

        temp_path = repository.new_temp_path()
        digest = hashlib.sha256()
        size = 0
        try:
            async with operation_slots:
                with open(temp_path, "xb") as file:
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > config.max_upload_bytes:
                            raise HTTPException(413, "アップロードサイズが上限を超えています")
                        file.write(chunk)
                        digest.update(chunk)
                if declared_size is not None and size != declared_size:
                    raise HTTPException(
                        400,
                        f"受信サイズが Content-Length と一致しません: {size} != {declared_size}",
                    )
                actual_sha = digest.hexdigest()
                if actual_sha != expected_sha:
                    raise HTTPException(400, "アップロード内容の SHA-256 が一致しません")
                return await run_in_threadpool(
                    repository.commit_temp,
                    temp_path,
                    version_id=metadata.version_id,
                    source_profile=metadata.source_profile,
                    source_db=metadata.source_db,
                    fmt=metadata.fmt,
                    size=size,
                    sha256=actual_sha,
                    note=metadata.note,
                    max_storage_bytes=config.max_storage_bytes,
                )
        finally:
            temp_path.unlink(missing_ok=True)

    @app.delete("/v1/backups/{backup_id}", dependencies=[Depends(require_auth)])
    def delete_backup(backup_id: str):
        return repository.delete(backup_id)

    @app.get("/v1/databases", dependencies=[Depends(require_auth)])
    def databases(profile: str, templates: bool = False):
        require_db_read()
        return inspect_mod.list_databases(
            profiles.get(profile), include_templates=templates
        )

    @app.get("/v1/structure", dependencies=[Depends(require_auth)])
    def structure(profile: str, db: str):
        require_db_read()
        return inspect_mod.database_structure(profiles.get(profile), db)

    @app.get("/v1/table", dependencies=[Depends(require_auth)])
    def table(profile: str, db: str, schema: str, table: str):
        require_db_read()
        connection = profiles.get(profile)
        return {
            "columns": inspect_mod.table_columns(connection, db, schema, table),
            "indexes": inspect_mod.table_indexes(connection, db, schema, table),
        }

    @app.get("/v1/sample", dependencies=[Depends(require_auth)])
    def sample(profile: str, db: str, schema: str, table: str, limit: int = 10):
        require_db_read()
        if not 1 <= limit <= 100:
            raise HTTPException(400, "limit は 1〜100 の範囲で指定してください")
        return inspect_mod.table_sample(
            profiles.get(profile), db, schema, table, limit
        )

    @app.post("/v1/dump", dependencies=[Depends(require_auth)])
    async def dump_database(body: DumpIn):
        require_db_read()
        if body.fmt not in ("plain", "custom"):
            raise HTTPException(400, "fmt は plain または custom を指定してください")
        temp_path = repository.new_temp_path()
        async with operation_slots:
            try:
                await run_in_threadpool(
                    pgtools.dump_to_file,
                    profiles.get(body.profile),
                    body.db,
                    temp_path,
                    body.fmt,
                    body.keep_owner,
                )
                digest = await run_in_threadpool(_sha256_file, temp_path)
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
        return FileResponse(
            temp_path,
            media_type="application/octet-stream",
            filename=f"{body.db}.{'sql' if body.fmt == 'plain' else 'dump'}",
            headers={
                "X-Pgbackup-Sha256": digest,
                "X-Pgbackup-Format": body.fmt,
            },
            background=BackgroundTask(temp_path.unlink, missing_ok=True),
        )

    @app.post("/v1/snapshots", dependencies=[Depends(require_auth)], status_code=201)
    async def create_snapshot(body: SnapshotIn):
        require_db_read()
        if body.fmt not in ("plain", "custom"):
            raise HTTPException(400, "fmt は plain または custom を指定してください")
        temp_path = repository.new_temp_path()
        async with operation_slots:
            try:
                await run_in_threadpool(
                    pgtools.dump_to_file,
                    profiles.get(body.profile),
                    body.db,
                    temp_path,
                    body.fmt,
                    body.keep_owner,
                )
                size = temp_path.stat().st_size
                digest = await run_in_threadpool(_sha256_file, temp_path)
                return await run_in_threadpool(
                    repository.commit_temp,
                    temp_path,
                    version_id=body.version_id or _snapshot_version(config.name, body.db),
                    source_profile=body.profile,
                    source_db=body.db,
                    fmt=body.fmt,
                    size=size,
                    sha256=digest,
                    note=body.note,
                    max_storage_bytes=config.max_storage_bytes,
                )
            finally:
                temp_path.unlink(missing_ok=True)

    @app.post(
        "/v1/backups/{backup_id}/restore", dependencies=[Depends(require_auth)]
    )
    async def restore_backup(backup_id: str, body: RemoteRestoreIn):
        require_db_restore()
        record = repository.get(backup_id)
        path = repository.object_path(record)
        if record["fmt"] != "custom":
            raise HTTPException(
                400,
                "エージェント側のリモート復元は custom 形式だけを許可します。"
                "plain SQL は手元へ取得し、内容を確認してから復元してください。",
            )
        digest = await run_in_threadpool(_sha256_file, path)
        if not hmac.compare_digest(digest, record["sha256"]):
            raise HTTPException(
                409,
                "保管済みバックアップの SHA-256 が一致しません。"
                "ディスク上で破損または変更された可能性があります。",
            )
        dest_db = body.db or record["source_db"]
        async with operation_slots:
            result = await run_in_threadpool(
                restore_mod.restore_dump_file,
                catalog,
                path,
                profiles.get(body.profile),
                dest_db,
                body.drop,
                body.keep_owner,
            )
        return result

    return app


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run_agent_server(
    config: AgentServerConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8510,
) -> None:
    import uvicorn

    if not 1 <= port <= 65535:
        raise AgentError("ポート番号は 1〜65535 の範囲で指定してください")
    uvicorn.run(
        create_agent_app(config),
        host=host,
        port=port,
        ssl_certfile=config.certfile,
        ssl_keyfile=config.keyfile,
        proxy_headers=False,
        server_header=False,
        date_header=False,
        log_level="warning",
        access_log=False,
        limit_concurrency=config.max_concurrent_operations + 20,
    )
