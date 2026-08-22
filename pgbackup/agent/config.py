"""エージェントサーバー設定とクライアント側ペアリング情報。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509

from ..config import home_dir
from ..errors import AgentError
from .certs import certificate_fingerprint, generate_self_signed_certificate

_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


def agent_dir() -> Path:
    path = home_dir() / "agent"
    path.mkdir(parents=True, exist_ok=True)
    return path


def server_config_path() -> Path:
    return agent_dir() / "server.json"


def agent_profiles_path() -> Path:
    return home_dir() / "agents.json"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise AgentError(
            f"エージェント名 '{name}' は使用できません。英数字・アンダースコアで始まり、"
            "英数字・「_ . -」のみ 64 文字以内で指定してください。"
        )


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise AgentError("エージェント URL は https://ホスト:ポート の形式で指定してください")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AgentError("エージェント URL に認証情報・クエリ・フラグメントは含められません")
    if parsed.path not in ("", "/"):
        raise AgentError("エージェント URL にパスは含められません")
    try:
        port = parsed.port
    except ValueError as exc:
        raise AgentError(f"エージェント URL のポート番号が不正です: {url}") from exc
    if port is not None and not 1 <= port <= 65535:
        raise AgentError(f"エージェント URL のポート番号が不正です: {port}")
    return url.rstrip("/")


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


@dataclass
class AgentServerConfig:
    name: str
    public_url: str
    token_sha256: str
    certfile: str
    keyfile: str
    storage_dir: str
    max_upload_bytes: int = 100 * 1024**3
    max_storage_bytes: int = 500 * 1024**3
    allow_db_read: bool = False
    allow_db_restore: bool = False
    max_concurrent_operations: int = 2

    @classmethod
    def load(cls, path: Path | None = None) -> "AgentServerConfig":
        config_path = path or server_config_path()
        if not config_path.exists():
            raise AgentError(
                f"エージェント設定が見つかりません: {config_path}\n"
                "先に 'pgbackup agent init' を実行してください。"
            )
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            config = cls(**raw)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise AgentError(f"エージェント設定を読み込めません: {config_path}: {exc}") from exc
        config.validate()
        return config

    def validate(self) -> None:
        _validate_name(self.name)
        self.public_url = _validate_url(self.public_url)
        if not re.fullmatch(r"[0-9a-f]{64}", self.token_sha256):
            raise AgentError("server.json の token_sha256 が不正です")
        if not 1 <= self.max_upload_bytes <= 10 * 1024**4:
            raise AgentError("max_upload_bytes は 1 byte〜10 TiB の範囲で指定してください")
        if not 1 <= self.max_storage_bytes <= 100 * 1024**4:
            raise AgentError("max_storage_bytes は 1 byte〜100 TiB の範囲で指定してください")
        if not 1 <= self.max_concurrent_operations <= 32:
            raise AgentError("max_concurrent_operations は 1〜32 の範囲で指定してください")
        for label, value in (("certfile", self.certfile), ("keyfile", self.keyfile)):
            if not Path(value).is_file():
                raise AgentError(f"{label} が見つかりません: {value}")

    def save(self, path: Path | None = None) -> None:
        self.validate()
        _atomic_json(path or server_config_path(), asdict(self))

    @property
    def capabilities(self) -> list[str]:
        values = ["storage"]
        if self.allow_db_read:
            values.append("db-read")
        if self.allow_db_restore:
            values.append("db-restore")
        return values


@dataclass
class AgentProfile:
    name: str
    url: str
    token: str
    ca_cert: str

    def validate(self) -> None:
        _validate_name(self.name)
        self.url = _validate_url(self.url)
        if len(self.token) < 32:
            raise AgentError("エージェントトークンが短すぎます (32 文字以上が必要)")
        try:
            x509.load_pem_x509_certificate(self.ca_cert.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise AgentError("pairing bundle に有効な CA 証明書がありません") from exc

    def to_dict(self, mask_token: bool = False) -> dict:
        data = asdict(self)
        if mask_token:
            data["token"] = "********"
            data.pop("ca_cert", None)
        return data


def initialize_server(
    *,
    name: str,
    public_url: str,
    pairing_out: Path | None = None,
    storage_dir: Path | None = None,
    max_upload_bytes: int = 100 * 1024**3,
    max_storage_bytes: int = 500 * 1024**3,
    allow_db_read: bool = False,
    allow_db_restore: bool = False,
    overwrite: bool = False,
) -> tuple[AgentServerConfig, Path, str]:
    """サーバー設定を初期化し、秘密トークン入り pairing bundle を一度だけ出力する。"""
    _validate_name(name)
    public_url = _validate_url(public_url)
    config_path = server_config_path()
    if config_path.exists() and not overwrite:
        raise AgentError(
            f"エージェント設定は既に存在します: {config_path} "
            "(--overwrite で証明書とトークンを再生成)"
        )

    root = agent_dir()
    certfile = root / "agent-cert.pem"
    keyfile = root / "agent-key.pem"
    fingerprint = generate_self_signed_certificate(
        certfile, keyfile, common_name=name, public_url=public_url
    )
    token = secrets.token_urlsafe(48)
    config = AgentServerConfig(
        name=name,
        public_url=public_url,
        token_sha256=_token_hash(token),
        certfile=str(certfile.resolve()),
        keyfile=str(keyfile.resolve()),
        storage_dir=str((storage_dir or (root / "storage")).resolve()),
        max_upload_bytes=max_upload_bytes,
        max_storage_bytes=max_storage_bytes,
        allow_db_read=allow_db_read,
        allow_db_restore=allow_db_restore,
    )
    config.save(config_path)

    pairing_path = pairing_out or (root / f"pairing-{name}.json")
    pairing = {
        "protocol": 1,
        "name": name,
        "url": public_url,
        "token": token,
        "ca_cert": certfile.read_text(encoding="ascii"),
        "fingerprint_sha256": fingerprint,
    }
    _atomic_json(pairing_path, pairing)
    return config, pairing_path, fingerprint


class AgentProfileStore:
    _lock = threading.Lock()

    def __init__(self, path: Path | None = None):
        self.path = path or agent_profiles_path()

    def _load_raw(self) -> dict:
        if not self.path.exists():
            return {"agents": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentError(f"エージェントプロファイルを読み込めません: {self.path}: {exc}") from exc
        if not isinstance(raw.get("agents"), dict):
            raise AgentError(f"エージェントプロファイルの形式が不正です: {self.path}")
        return raw

    def list(self) -> list[AgentProfile]:
        raw = self._load_raw()
        return [AgentProfile(name=name, **attrs) for name, attrs in sorted(raw["agents"].items())]

    def get(self, name: str) -> AgentProfile:
        raw = self._load_raw()
        attrs = raw["agents"].get(name)
        if attrs is None:
            known = ", ".join(sorted(raw["agents"])) or "(なし)"
            raise AgentError(f"エージェント '{name}' が見つかりません。登録済み: {known}")
        profile = AgentProfile(name=name, **attrs)
        profile.validate()
        return profile

    def add(self, profile: AgentProfile, overwrite: bool = False) -> None:
        profile.validate()
        with self._lock:
            raw = self._load_raw()
            if profile.name in raw["agents"] and not overwrite:
                raise AgentError(f"エージェント '{profile.name}' は既に存在します (--overwrite で上書き)")
            attrs = asdict(profile)
            attrs.pop("name")
            raw["agents"][profile.name] = attrs
            _atomic_json(self.path, raw)

    def add_pairing_file(
        self, pairing_file: Path, *, name: str | None = None, overwrite: bool = False
    ) -> AgentProfile:
        try:
            raw = json.loads(pairing_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentError(f"pairing bundle を読み込めません: {pairing_file}: {exc}") from exc
        if raw.get("protocol") != 1:
            raise AgentError(f"未対応のエージェントプロトコルです: {raw.get('protocol')}")
        profile = AgentProfile(
            name=name or raw.get("name", ""),
            url=raw.get("url", ""),
            token=raw.get("token", ""),
            ca_cert=raw.get("ca_cert", ""),
        )
        self.add(profile, overwrite=overwrite)
        return profile

    def remove(self, name: str) -> None:
        with self._lock:
            raw = self._load_raw()
            if name not in raw["agents"]:
                raise AgentError(f"エージェント '{name}' が見つかりません")
            del raw["agents"][name]
            _atomic_json(self.path, raw)


def current_server_fingerprint(config: AgentServerConfig) -> str:
    return certificate_fingerprint(Path(config.certfile))
