"""設定・接続プロファイル管理。

データディレクトリ (PGBACKUP_HOME, 既定 ~/.pgbackup) に以下を保持する:
  - connections.json : 接続プロファイル (パスワードを含むため git 管理外に置くこと)
  - catalog.db       : バックアップのバージョンカタログ (SQLite)
  - backups/         : ファイルバックアップの保存先
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from .errors import ProfileError

# アーカイブモードの既定の格納先データベース名
DEFAULT_ARCHIVE_DB = "BACK_UP_DB"

# プロファイル名: パス・URL・バージョン ID ("profile/db@vN") に安全に埋め込める文字のみ
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


def home_dir() -> Path:
    """データディレクトリを返す (なければ作成する)。"""
    root = Path(os.environ.get("PGBACKUP_HOME", "")) if os.environ.get("PGBACKUP_HOME") else Path.home() / ".pgbackup"
    root.mkdir(parents=True, exist_ok=True)
    return root


def backups_dir() -> Path:
    d = home_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tmp_dir() -> Path:
    d = home_dir() / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_tmp(max_age_hours: float = 24) -> None:
    """異常終了で残った一時ファイルを掃除する (CLI / Web の起動時に呼ぶ)。"""
    cutoff = time.time() - max_age_hours * 3600
    try:
        entries = list(tmp_dir().iterdir())
    except OSError:
        return
    for p in entries:
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass  # 使用中・権限なし等は無視


def catalog_path() -> Path:
    return home_dir() / "catalog.db"


@dataclass
class Profile:
    """PostgreSQL サーバーへの接続情報。

    dbname はメンテナンス用 (一覧取得や CREATE DATABASE 発行時) に接続する DB。
    password が None の場合は環境変数 PGPASSWORD や ~/.pgpass に委ねる。
    """

    name: str
    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: str | None = None
    dbname: str = "postgres"
    sslmode: str | None = None

    def display(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"

    def to_dict(self, mask_password: bool = False) -> dict:
        d = asdict(self)
        if mask_password and d.get("password"):
            d["password"] = "********"
        return d


class ProfileStore:
    """connections.json の読み書き。

    同一プロセス内 (Web の並行リクエスト等) の読み書きはロックで直列化する。
    別プロセス同時書き込みの保護はない (最後の書き込みが勝つ)。
    """

    _lock = threading.Lock()

    def __init__(self, path: Path | None = None):
        self.path = path or (home_dir() / "connections.json")

    def _load_raw(self) -> dict:
        if not self.path.exists():
            return {"profiles": {}}
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _save_raw(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Windows では対象を開いているプロセスがいると os.replace が一時的に失敗する
        for attempt in range(5):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))

    def list(self) -> list[Profile]:
        raw = self._load_raw()
        return [Profile(name=name, **attrs) for name, attrs in sorted(raw["profiles"].items())]

    def get(self, name: str) -> Profile:
        raw = self._load_raw()
        attrs = raw["profiles"].get(name)
        if attrs is None:
            known = ", ".join(sorted(raw["profiles"])) or "(なし)"
            raise ProfileError(f"接続プロファイル '{name}' が見つかりません。登録済み: {known}")
        return Profile(name=name, **attrs)

    def add(self, profile: Profile, overwrite: bool = False) -> None:
        if not _PROFILE_NAME_RE.match(profile.name):
            raise ProfileError(
                f"プロファイル名 '{profile.name}' は使用できません。"
                "英数字・アンダースコアで始まり、英数字・「_ . -」のみ 64 文字以内で指定してください。"
            )
        with self._lock:
            raw = self._load_raw()
            if profile.name in raw["profiles"] and not overwrite:
                raise ProfileError(f"接続プロファイル '{profile.name}' は既に存在します (--overwrite で上書き)")
            attrs = asdict(profile)
            attrs.pop("name")
            raw["profiles"][profile.name] = attrs
            self._save_raw(raw)

    def remove(self, name: str) -> None:
        with self._lock:
            raw = self._load_raw()
            if name not in raw["profiles"]:
                raise ProfileError(f"接続プロファイル '{name}' が見つかりません")
            del raw["profiles"][name]
            self._save_raw(raw)
