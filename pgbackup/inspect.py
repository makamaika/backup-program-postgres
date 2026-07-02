"""データベースの一覧・構造・データ種類の検査。"""

from __future__ import annotations

import psycopg
from psycopg import sql

from .config import Profile
from .db import connect

# ユーザー領域外のシステムスキーマを除外する条件
# (LIKE の % はパラメータバインド時にプレースホルダと衝突するため正規表現で書く)
_USER_SCHEMA_COND = "n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"

_RELKIND_LABEL = {
    "r": "table",
    "p": "partitioned table",
    "v": "view",
    "m": "materialized view",
    "f": "foreign table",
}


def server_info(profile: Profile) -> dict:
    with connect(profile) as conn:
        version = conn.execute("SELECT version()").fetchone()[0]
        short = conn.execute("SHOW server_version").fetchone()[0]
    return {"version": version, "server_version": short}


def list_databases(profile: Profile, include_templates: bool = False) -> list[dict]:
    q = """
        SELECT d.datname AS name,
               pg_get_userbyid(d.datdba) AS owner,
               pg_encoding_to_char(d.encoding) AS encoding,
               d.datistemplate AS is_template,
               d.datallowconn AS allow_conn,
               CASE WHEN d.datallowconn THEN pg_database_size(d.datname) END AS size_bytes,
               CASE WHEN d.datallowconn
                    THEN pg_size_pretty(pg_database_size(d.datname)) END AS size
        FROM pg_database d
        {where}
        ORDER BY d.datname
    """.format(where="" if include_templates else "WHERE NOT d.datistemplate")
    with connect(profile) as conn:
        cur = conn.execute(q)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def list_schemas(profile: Profile, dbname: str) -> list[str]:
    q = f"SELECT n.nspname FROM pg_namespace n WHERE {_USER_SCHEMA_COND} ORDER BY 1"
    with connect(profile, dbname) as conn:
        return [r[0] for r in conn.execute(q).fetchall()]


def list_tables(profile: Profile, dbname: str, schema: str | None = None) -> list[dict]:
    q = """
        SELECT n.nspname AS schema,
               c.relname AS name,
               c.relkind AS kind,
               GREATEST(c.reltuples, 0)::bigint AS est_rows,
               pg_total_relation_size(c.oid) AS size_bytes,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
               obj_description(c.oid, 'pg_class') AS comment
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND {user_cond}
          {schema_cond}
        ORDER BY n.nspname, c.relname
    """.format(
        user_cond=_USER_SCHEMA_COND,
        schema_cond="AND n.nspname = %(schema)s" if schema else "",
    )
    with connect(profile, dbname) as conn:
        cur = conn.execute(q, {"schema": schema} if schema else None)
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for r in rows:
        r["kind"] = _RELKIND_LABEL.get(r["kind"], r["kind"])
    return rows


def table_columns(profile: Profile, dbname: str, schema: str, table: str) -> list[dict]:
    q = """
        SELECT a.attname AS name,
               format_type(a.atttypid, a.atttypmod) AS data_type,
               NOT a.attnotnull AS nullable,
               pg_get_expr(ad.adbin, ad.adrelid) AS default,
               COALESCE(i.indisprimary, false) AS is_primary_key,
               col_description(a.attrelid, a.attnum) AS comment
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
        LEFT JOIN pg_index i ON i.indrelid = a.attrelid AND i.indisprimary
                             AND a.attnum = ANY(i.indkey)
        WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
    """
    with connect(profile, dbname) as conn:
        cur = conn.execute(q, (schema, table))
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def table_indexes(profile: Profile, dbname: str, schema: str, table: str) -> list[dict]:
    q = """
        SELECT indexname AS name, indexdef AS definition
        FROM pg_indexes
        WHERE schemaname = %s AND tablename = %s
        ORDER BY indexname
    """
    with connect(profile, dbname) as conn:
        cur = conn.execute(q, (schema, table))
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def table_sample(
    profile: Profile, dbname: str, schema: str, table: str, limit: int = 10
) -> dict:
    """テーブルの先頭数行と行数を返す (データの中身確認用)。

    行数の正確なカウントは巨大テーブルではフルスキャンになるため 5 秒で打ち切り、
    タイムアウト時は統計情報による推定値 (approx=True) を返す。
    """
    limit = max(1, min(int(limit), 200))
    with connect(profile, dbname) as conn:
        ident = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
        cur = conn.execute(sql.SQL("SELECT * FROM {} LIMIT {}").format(ident, sql.Literal(limit)))
        columns = [c.name for c in cur.description]
        rows = [[_jsonable(v) for v in row] for row in cur.fetchall()]
        approx = False
        try:
            with conn.transaction():
                conn.execute("SET LOCAL statement_timeout = '5000ms'")
                total = conn.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(ident)
                ).fetchone()[0]
        except psycopg.errors.QueryCanceled:
            approx = True
            total = conn.execute(
                """SELECT GREATEST(c.reltuples, 0)::bigint
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = %s AND c.relname = %s""",
                (schema, table),
            ).fetchone()[0]
    return {"columns": columns, "rows": rows, "total_rows": total, "approx": approx}


def datatype_summary(profile: Profile, dbname: str) -> list[dict]:
    """この DB にどんな種類のデータ (型) がどれだけ保存されているかの集計。"""
    q = f"""
        SELECT format_type(a.atttypid, a.atttypmod) AS data_type,
               count(*) AS column_count,
               count(DISTINCT c.oid) AS table_count
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
          AND {_USER_SCHEMA_COND}
        GROUP BY 1
        ORDER BY column_count DESC, data_type
    """
    with connect(profile, dbname) as conn:
        cur = conn.execute(q)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def database_structure(profile: Profile, dbname: str) -> dict:
    """Web UI 用: スキーマ→テーブル→カラムまでまとめて返す。"""
    tables = list_tables(profile, dbname)
    schemas: dict[str, list] = {}
    for t in tables:
        schemas.setdefault(t["schema"], []).append(t)
    return {
        "database": dbname,
        "schemas": [{"name": s, "tables": ts} for s, ts in sorted(schemas.items())],
        "datatype_summary": datatype_summary(profile, dbname),
    }


def _jsonable(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (bytes, memoryview)):
        b = bytes(v)
        return f"\\x{b[:64].hex()}" + ("…" if len(b) > 64 else "")
    return str(v)
