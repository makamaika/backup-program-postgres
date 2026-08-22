"""HTTPS バックアップエージェントのクライアント。"""

from __future__ import annotations

import hashlib
import base64
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..errors import AgentError
from . import PROTOCOL_VERSION
from .config import AgentProfile

_JSON_LIMIT = 8 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """認証ヘッダーが別ホストへ転送されないよう redirect を禁止する。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _FileChunks:
    def __init__(self, path: Path, chunk_size: int = 1024 * 1024):
        self.path = path
        self.chunk_size = chunk_size
        self._file = None

    def __iter__(self):
        self._file = open(self.path, "rb")
        return self

    def __next__(self):
        if self._file is None:
            self._file = open(self.path, "rb")
        chunk = self._file.read(self.chunk_size)
        if chunk:
            return chunk
        self.close()
        raise StopIteration

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


class AgentClient:
    def __init__(self, profile: AgentProfile, timeout: float = 30):
        profile.validate()
        self.profile = profile
        self.timeout = timeout
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cadata=profile.ca_cert)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            _NoRedirect(),
        )

    def _url(self, path: str, query: dict | None = None) -> str:
        url = self.profile.url + path
        if query:
            values = {key: value for key, value in query.items() if value is not None}
            url += "?" + urllib.parse.urlencode(values, doseq=True)
        return url

    def _open(
        self,
        method: str,
        path: str,
        *,
        query: dict | None = None,
        data=None,
        headers: dict | None = None,
        timeout: float | None = None,
    ):
        request_headers = {
            "Authorization": f"Bearer {self.profile.token}",
            "User-Agent": "pgbackup-agent-client/1",
            **(headers or {}),
        }
        request = urllib.request.Request(
            self._url(path, query), data=data, headers=request_headers, method=method
        )
        try:
            return self._opener.open(request, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read(_JSON_LIMIT)
            try:
                detail = json.loads(body.decode("utf-8")).get("detail")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = body.decode("utf-8", errors="replace").strip()
            raise AgentError(
                f"エージェント {self.profile.name} が HTTP {exc.code} を返しました: "
                f"{detail or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise AgentError(
                f"エージェント {self.profile.name} ({self.profile.url}) に接続できません: {reason}"
            ) from exc

    def _json(
        self,
        method: str,
        path: str,
        *,
        query: dict | None = None,
        body: dict | None = None,
        timeout: float | None = None,
    ):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        with self._open(
            method, path, query=query, data=data, headers=headers, timeout=timeout
        ) as response:
            raw = response.read(_JSON_LIMIT + 1)
            if len(raw) > _JSON_LIMIT:
                raise AgentError("エージェントの JSON 応答が大きすぎます")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError("エージェントから不正な JSON 応答を受信しました") from exc

    def health(self) -> dict:
        result = self._json("GET", "/v1/health")
        if result.get("protocol") != PROTOCOL_VERSION:
            raise AgentError(
                f"エージェントプロトコルが一致しません: server={result.get('protocol')}, "
                f"client={PROTOCOL_VERSION}"
            )
        return result

    def list_backups(self, limit: int = 200) -> list[dict]:
        return self._json("GET", "/v1/backups", query={"limit": limit})

    def get_backup(self, backup_id: str) -> dict:
        return self._json("GET", f"/v1/backups/{urllib.parse.quote(backup_id, safe='')}")

    def upload_backup(
        self,
        path: Path,
        *,
        version_id: str,
        source_profile: str,
        source_db: str,
        fmt: str,
        sha256: str,
        note: str | None = None,
    ) -> dict:
        path = Path(path)
        if not path.is_file():
            raise AgentError(f"アップロードするバックアップファイルが見つかりません: {path}")
        if note is not None and len(note) > 1000:
            raise AgentError("エージェントへ送るメモは 1000 文字以内にしてください")
        size = path.stat().st_size
        health = self.health()
        max_upload = health.get("max_upload_bytes")
        if max_upload is not None and size > max_upload:
            raise AgentError(
                f"バックアップ ({size} bytes) がエージェントのアップロード上限 "
                f"({max_upload} bytes) を超えています"
            )
        metadata = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "version_id": version_id,
                    "source_profile": source_profile,
                    "source_db": source_db,
                    "fmt": fmt,
                    "note": note,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii").rstrip("=")
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            "X-Pgbackup-Sha256": sha256,
            "X-Pgbackup-Metadata": metadata,
            "Accept": "application/json",
        }
        body = _FileChunks(path)
        try:
            with self._open(
                "POST",
                "/v1/backups",
                data=body,
                headers=headers,
                timeout=max(self.timeout, 3600),
            ) as response:
                raw = response.read(_JSON_LIMIT + 1)
        finally:
            body.close()
        if len(raw) > _JSON_LIMIT:
            raise AgentError("エージェントの応答が大きすぎます")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError("アップロード後の応答が不正です") from exc

    def _download_response(
        self, response, destination: Path, *, expected_sha256: str | None = None
    ) -> tuple[int, str]:
        destination = Path(destination)
        if destination.exists():
            raise AgentError(f"出力先は既に存在します: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with open(temp, "xb") as file:
                while chunk := response.read(1024 * 1024):
                    file.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            actual = digest.hexdigest()
            header_sha = response.headers.get("X-Pgbackup-Sha256")
            expected = expected_sha256 or header_sha
            if expected and actual != expected:
                raise AgentError(
                    f"ダウンロードしたバックアップの SHA-256 が一致しません: "
                    f"expected={expected}, actual={actual}"
                )
            os.replace(temp, destination)
            return size, actual
        finally:
            temp.unlink(missing_ok=True)

    def download_backup(self, backup_id: str, destination: Path) -> dict:
        record = self.get_backup(backup_id)
        path = f"/v1/backups/{urllib.parse.quote(backup_id, safe='')}/content"
        with self._open("GET", path, timeout=max(self.timeout, 3600)) as response:
            size, digest = self._download_response(
                response, destination, expected_sha256=record.get("sha256")
            )
        if record.get("size") is not None and size != record["size"]:
            Path(destination).unlink(missing_ok=True)
            raise AgentError(
                f"ダウンロードサイズが一致しません: expected={record['size']}, actual={size}"
            )
        return {**record, "size": size, "sha256": digest}

    def delete_backup(self, backup_id: str) -> dict:
        return self._json(
            "DELETE", f"/v1/backups/{urllib.parse.quote(backup_id, safe='')}"
        )

    def databases(self, remote_profile: str, include_templates: bool = False) -> list[dict]:
        return self._json(
            "GET",
            "/v1/databases",
            query={"profile": remote_profile, "templates": str(include_templates).lower()},
        )

    def structure(self, remote_profile: str, db: str) -> dict:
        return self._json(
            "GET", "/v1/structure", query={"profile": remote_profile, "db": db}
        )

    def table(self, remote_profile: str, db: str, schema: str, table: str) -> dict:
        return self._json(
            "GET",
            "/v1/table",
            query={"profile": remote_profile, "db": db, "schema": schema, "table": table},
        )

    def sample(
        self, remote_profile: str, db: str, schema: str, table: str, limit: int = 10
    ) -> dict:
        return self._json(
            "GET",
            "/v1/sample",
            query={
                "profile": remote_profile,
                "db": db,
                "schema": schema,
                "table": table,
                "limit": limit,
            },
        )

    def download_database(
        self,
        remote_profile: str,
        db: str,
        destination: Path,
        *,
        fmt: str = "custom",
        keep_owner: bool = False,
    ) -> dict:
        body = json.dumps(
            {
                "profile": remote_profile,
                "db": db,
                "fmt": fmt,
                "keep_owner": keep_owner,
            }
        ).encode("utf-8")
        with self._open(
            "POST",
            "/v1/dump",
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=max(self.timeout, 3600),
        ) as response:
            size, digest = self._download_response(response, destination)
        return {"size": size, "sha256": digest, "fmt": fmt}

    def create_snapshot(
        self,
        remote_profile: str,
        db: str,
        *,
        version_id: str | None = None,
        fmt: str = "custom",
        keep_owner: bool = False,
        note: str | None = None,
    ) -> dict:
        return self._json(
            "POST",
            "/v1/snapshots",
            body={
                "profile": remote_profile,
                "db": db,
                "version_id": version_id,
                "fmt": fmt,
                "keep_owner": keep_owner,
                "note": note,
            },
            timeout=max(self.timeout, 3600),
        )

    def restore_remote_backup(
        self,
        backup_id: str,
        remote_profile: str,
        *,
        dest_db: str | None = None,
        drop: bool = False,
        keep_owner: bool = False,
    ) -> dict:
        return self._json(
            "POST",
            f"/v1/backups/{urllib.parse.quote(backup_id, safe='')}/restore",
            body={
                "profile": remote_profile,
                "db": dest_db,
                "drop": drop,
                "keep_owner": keep_owner,
            },
            timeout=max(self.timeout, 3600),
        )
