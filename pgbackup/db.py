"""psycopg による接続ヘルパー。"""

from __future__ import annotations

import psycopg
from psycopg import sql

from .config import Profile
from .errors import PgBackupError

CONNECT_TIMEOUT = 10


def connect(profile: Profile, dbname: str | None = None, autocommit: bool = False) -> psycopg.Connection:
    """プロファイルの接続情報で psycopg 接続を開く。"""
    kwargs: dict = dict(
        host=profile.host,
        port=profile.port,
        user=profile.user,
        dbname=dbname or profile.dbname,
        connect_timeout=CONNECT_TIMEOUT,
        autocommit=autocommit,
    )
    if profile.password is not None:
        kwargs["password"] = profile.password
    if profile.sslmode:
        kwargs["sslmode"] = profile.sslmode
    try:
        return psycopg.connect(**kwargs)
    except psycopg.OperationalError as e:
        raise PgBackupError(
            f"接続失敗 [{profile.name}] {profile.display()} db={dbname or profile.dbname}: {e}"
        ) from e


def server_version(profile: Profile) -> str:
    with connect(profile) as conn:
        return conn.execute("SHOW server_version").fetchone()[0]


def database_exists(profile: Profile, dbname: str) -> bool:
    with connect(profile) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        return row is not None


def database_encoding(profile: Profile, dbname: str) -> str | None:
    with connect(profile) as conn:
        row = conn.execute(
            "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        return row[0] if row else None


def create_database(profile: Profile, dbname: str, encoding: str | None = None) -> None:
    """CREATE DATABASE (autocommit 必須)。encoding 指定が locale と非互換なら指定なしで再試行する。"""
    with connect(profile, autocommit=True) as conn:
        if encoding:
            try:
                conn.execute(
                    sql.SQL("CREATE DATABASE {} ENCODING {} TEMPLATE template0").format(
                        sql.Identifier(dbname), sql.Literal(encoding)
                    )
                )
                return
            except psycopg.Error:
                pass
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(dbname)))


def drop_database(profile: Profile, dbname: str) -> None:
    """接続を強制切断してから DROP DATABASE。"""
    with connect(profile, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (dbname,),
        )
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)))


def ensure_database(profile: Profile, dbname: str, drop: bool = False, encoding: str | None = None) -> None:
    """リストア/複製先の DB を用意する。既存なら drop=True のときのみ作り直す。"""
    if database_exists(profile, dbname):
        if not drop:
            raise PgBackupError(
                f"データベース '{dbname}' は {profile.display()} に既に存在します。"
                "上書きするには --drop を指定してください。"
            )
        drop_database(profile, dbname)
    create_database(profile, dbname, encoding=encoding)
