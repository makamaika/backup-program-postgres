"""psycopg による接続ヘルパー。"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import psycopg
from psycopg import sql

from .config import Profile
from .errors import PgBackupError
from .restore_lock import replacement_lock

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


def rename_database(profile: Profile, old: str, new: str) -> None:
    """接続を強制切断してから ALTER DATABASE ... RENAME TO。"""
    with connect(profile, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (old,),
        )
        conn.execute(
            sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                sql.Identifier(old), sql.Identifier(new)
            )
        )


def _trash_name(dbname: str) -> str:
    """退避用の一時 DB 名 (識別子の 63 バイト制限内に収める)。"""
    suffix = f"_pgb_old_{uuid.uuid4().hex[:8]}"
    base = dbname
    while len((base + suffix).encode("utf-8")) > 63:
        base = base[:-1]
    return base + suffix


def cluster_identity(profile: Profile) -> str:
    """クラスタの一意識別子。ホスト名の表記ゆれ (localhost / 127.0.0.1 等) に
    影響されない同一サーバー判定に使う。取得できない場合は host:port にフォールバック。"""
    try:
        with connect(profile) as conn:
            row = conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone()
            return f"pg:{row[0]}:{profile.port}"
    except psycopg.Error:
        return f"addr:{profile.host.lower()}:{profile.port}"


def same_cluster(a: Profile, b: Profile) -> bool:
    if a.host == b.host and a.port == b.port:
        return True
    return cluster_identity(a) == cluster_identity(b)


@contextmanager
def replace_database(profile: Profile, dbname: str, drop: bool = False,
                     encoding: str | None = None):
    """リストア/複製先の DB を安全に用意するコンテキストマネージャ。

    既存 DB は削除せず一時名に退避してから新 DB を作る。
    with ブロックが失敗したら新 DB を消して退避 DB を元に戻し、
    成功したら退避 DB を削除する。途中失敗で「空の DB だけが残る」事故を防ぐ。
    """
    # 同じデータ領域の job を DB 操作から後片付けまで直列化する。
    # DB 側に新しい CONNECT 権限を要求せず、置換対象も制限しない。
    with replacement_lock():
        trash: str | None = None
        created = False
        if database_exists(profile, dbname):
            if not drop:
                raise PgBackupError(
                    f"データベース '{dbname}' は {profile.display()} に既に存在します。"
                    "上書きするには --drop を指定してください。"
                )
            trash = _trash_name(dbname)
            rename_database(profile, dbname, trash)
        try:
            create_database(profile, dbname, encoding=encoding)
            created = True
            yield
        except BaseException:
            # CREATE に成功した場合だけ、自分の作成した DB を片付ける。
            if created:
                try:
                    drop_database(profile, dbname)
                except psycopg.Error:
                    pass
            if trash is not None:
                try:
                    rename_database(profile, trash, dbname)
                except psycopg.Error:
                    pass
            raise
        else:
            if trash is not None:
                try:
                    drop_database(profile, trash)
                except psycopg.Error:
                    pass
