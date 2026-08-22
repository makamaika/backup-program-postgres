"""安全な行編集、テーブル比較、merge/sync。

任意 SQL は受け取らず、識別子は psycopg.sql.Identifier、値はすべてバインドする。
更新・削除は主キー、または全列 NOT NULL の一意制約を行キーとして使用する。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Json, Jsonb

from .config import Profile
from .db import connect, same_cluster
from .errors import DataEditError

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_PAGE_BYTES = 32 * 1024 * 1024
DEFAULT_COMPARE_ROWS = 10_000
MAX_COMPARE_ROWS = 50_000
MAX_COMPARE_BYTES = 128 * 1024 * 1024
DEFAULT_PREVIEW_ROWS = 100
MAX_PREVIEW_ROWS = 500

_NUMERIC_UDTS = {
    "int2", "int4", "int8", "numeric", "float4", "float8", "money", "oid",
}
_TEXT_UDTS = {"text", "varchar", "bpchar", "name", "citext", "char"}
_JSON_UDTS = {"json", "jsonb"}
_DATETIME_UDTS = {"timestamp", "timestamptz"}
_TIME_UDTS = {"time", "timetz"}
_NETWORK_UDTS = {"inet", "cidr", "macaddr", "macaddr8"}


def _type_family(column: dict) -> str:
    udt = column["udt_name"]
    if udt in _NUMERIC_UDTS:
        return "numeric"
    if udt in _TEXT_UDTS:
        return "text"
    if udt in _JSON_UDTS:
        return "json"
    if udt in _DATETIME_UDTS:
        return "datetime"
    if udt in _TIME_UDTS:
        return "time"
    if udt == "date":
        return "date"
    if udt == "bool":
        return "boolean"
    if udt == "uuid":
        return "uuid"
    if udt == "bytea":
        return "binary"
    if udt.startswith("_") or column["data_type"] == "ARRAY":
        return "array"
    if udt in _NETWORK_UDTS:
        return "network"
    return f"exact:{udt}"


def _ident(schema: str, table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def table_metadata(profile: Profile, db: str, schema: str, table: str) -> dict:
    """編集・比較に必要な列、制約、権限を返す。"""
    with connect(profile, db) as conn:
        relation = conn.execute(
            """SELECT c.relkind,
                      has_table_privilege(c.oid, 'SELECT') AS can_select,
                      has_table_privilege(c.oid, 'INSERT') AS can_insert,
                      has_table_privilege(c.oid, 'UPDATE') AS can_update,
                      has_table_privilege(c.oid, 'DELETE') AS can_delete,
                      c.relrowsecurity AS row_security,
                      c.relforcerowsecurity AS force_row_security,
                      ((SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)
                       OR (pg_get_userbyid(c.relowner) = current_user AND NOT c.relforcerowsecurity))
                        AS row_security_bypassed
               FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = %s AND c.relname = %s""",
            (schema, table),
        ).fetchone()
        if relation is None:
            raise DataEditError(f"テーブル {schema}.{table} が見つかりません")

        cur = conn.execute(
            """SELECT a.attname AS name,
                      format_type(a.atttypid, a.atttypmod) AS data_type,
                      t.typname AS udt_name,
                      NOT a.attnotnull AS nullable,
                      pg_get_expr(ad.adbin, ad.adrelid) AS default,
                      a.attidentity AS identity,
                      a.attgenerated AS generated,
                      a.attnum AS ordinal
               FROM pg_attribute a
               JOIN pg_class c ON c.oid = a.attrelid
               JOIN pg_namespace n ON n.oid = c.relnamespace
               JOIN pg_type t ON t.oid = a.atttypid
               LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
               WHERE n.nspname = %s AND c.relname = %s
                 AND a.attnum > 0 AND NOT a.attisdropped
               ORDER BY a.attnum""",
            (schema, table),
        )
        names = [desc.name for desc in cur.description]
        columns = [dict(zip(names, row)) for row in cur.fetchall()]
        if not columns:
            raise DataEditError(f"{schema}.{table} に編集可能な列がありません")

        constraints = conn.execute(
            """SELECT con.conname,
                      con.contype,
                      con.condeferrable,
                      array_agg(att.attname ORDER BY key.ord) AS columns
               FROM pg_constraint con
               JOIN pg_class c ON c.oid = con.conrelid
               JOIN pg_namespace n ON n.oid = c.relnamespace
               CROSS JOIN LATERAL unnest(con.conkey)
                    WITH ORDINALITY AS key(attnum, ord)
               JOIN pg_attribute att
                 ON att.attrelid = con.conrelid AND att.attnum = key.attnum
               WHERE n.nspname = %s AND c.relname = %s
                 AND con.contype IN ('p', 'u')
               GROUP BY con.conname, con.contype, con.condeferrable
               ORDER BY CASE con.contype WHEN 'p' THEN 0 ELSE 1 END, con.conname""",
            (schema, table),
        ).fetchall()

    column_map = {column["name"]: column for column in columns}
    unique_keys = []
    primary_key: list[str] = []
    for name, constraint_type, deferrable, key_columns in constraints:
        item = {
            "name": name,
            "columns": list(key_columns),
            "primary": constraint_type == "p",
            "deferrable": bool(deferrable),
        }
        unique_keys.append(item)
        if item["primary"]:
            primary_key = item["columns"]

    row_key = list(primary_key)
    if not row_key:
        for key in unique_keys:
            if not key["deferrable"] and all(
                not column_map[name]["nullable"] for name in key["columns"]
            ):
                row_key = list(key["columns"])
                break

    (
        relkind, can_select, can_insert, can_update, can_delete,
        row_security, force_row_security, row_security_bypassed,
    ) = relation
    base_table = relkind in ("r", "p")
    for column in columns:
        column["type_family"] = _type_family(column)
        column["is_primary_key"] = column["name"] in primary_key
        column["is_row_key"] = column["name"] in row_key
        column["insertable"] = base_table and not column["generated"] and column["identity"] != "a"
        column["updatable"] = base_table and not column["generated"] and not column["is_row_key"]

    return {
        "profile": profile.name,
        "host": profile.host,
        "port": profile.port,
        "database": db,
        "schema": schema,
        "table": table,
        "relation_kind": relkind,
        "base_table": base_table,
        "columns": columns,
        "primary_key": primary_key,
        "unique_keys": unique_keys,
        "row_key": row_key,
        "can_select": bool(can_select),
        "can_insert": bool(can_insert) and base_table,
        "can_update": bool(can_update) and base_table and bool(row_key),
        "can_delete": bool(can_delete) and base_table and bool(row_key),
        "row_security": bool(row_security),
        "force_row_security": bool(force_row_security),
        "row_security_effective": bool(row_security) and not bool(row_security_bypassed),
    }


def _column_map(metadata: dict) -> dict[str, dict]:
    return {column["name"]: column for column in metadata["columns"]}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _normalize_temporal(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        text = value.isoformat()
    elif isinstance(value, (date, time)):
        text = value.isoformat()
    else:
        text = str(value).replace(" ", "T", 1)
    text = text.replace("+00:00", "Z")
    if "." in text:
        head, tail = text.split(".", 1)
        zone_at = next((i for i, char in enumerate(tail) if char in "Z+-"), len(tail))
        fraction, zone = tail[:zone_at].rstrip("0"), tail[zone_at:]
        text = head + (("." + fraction) if fraction else "") + zone
    return text


def _canonical_decimal(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DataEditError(f"数値として比較できない値です: {value}") from exc
    if not number.is_finite():
        return str(number)
    if number == 0:
        return "0"
    normalized = number.normalize()
    text = format(normalized, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _canonical_value(value: Any, column: dict) -> Any:
    if value is None:
        return None
    family = column["type_family"]
    if family == "numeric":
        return {"$numeric": _canonical_decimal(value)}
    if family == "binary":
        raw = bytes(value) if not isinstance(value, str) else bytes.fromhex(value.removeprefix("\\x"))
        return {"$binary": base64.b64encode(raw).decode("ascii")}
    if family == "datetime":
        return {"$datetime": _normalize_temporal(value)}
    if family in ("date", "time"):
        return {f"${family}": _normalize_temporal(value)}
    if family == "json":
        parsed = json.loads(value) if isinstance(value, str) else value
        return {"$json": _jsonable(parsed)}
    if family == "array":
        return {"$array": [_jsonable(item) for item in value]}
    return _jsonable(value)


def _canonical_row(row: dict, metadata: dict, columns: list[str] | None = None) -> dict:
    cmap = _column_map(metadata)
    selected = columns or [column["name"] for column in metadata["columns"]]
    return {name: _canonical_value(row.get(name), cmap[name]) for name in selected}


def _display_value(value: Any, column: dict) -> Any:
    if value is not None and column["type_family"] in ("datetime", "date", "time"):
        return _normalize_temporal(value)
    return _jsonable(value)


def _display_row(row: dict, metadata: dict) -> dict:
    cmap = _column_map(metadata)
    return {name: _display_value(value, cmap[name]) for name, value in row.items()}


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _row_etag(row: dict, metadata: dict) -> str:
    return _hash_payload(_canonical_row(row, metadata))


def _row_output(row: dict, metadata: dict) -> dict:
    cmap = _column_map(metadata)
    key = {name: _display_value(row[name], cmap[name]) for name in metadata["row_key"]}
    return {
        "values": _display_row(row, metadata),
        "key": key,
        "etag": _row_etag(row, metadata),
    }


def _fetch_count(conn, schema: str, table: str) -> tuple[int, bool]:
    relation = _ident(schema, table)
    try:
        with conn.transaction():
            conn.execute("SET LOCAL statement_timeout = '3000ms'")
            return conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(relation)
            ).fetchone()[0], False
    except psycopg.errors.QueryCanceled:
        row = conn.execute(
            """SELECT GREATEST(c.reltuples, 0)::bigint
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = %s AND c.relname = %s""",
            (schema, table),
        ).fetchone()
        return int(row[0]), True


def read_rows(
    profile: Profile,
    db: str,
    schema: str,
    table: str,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
    order_by: str | None = None,
    direction: str = "asc",
) -> dict:
    metadata = table_metadata(profile, db, schema, table)
    if not metadata["can_select"]:
        raise DataEditError(f"{schema}.{table} の SELECT 権限がありません")
    limit = int(limit)
    offset = int(offset)
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise DataEditError(f"limit は 1〜{MAX_PAGE_SIZE} の範囲で指定してください")
    if offset < 0:
        raise DataEditError("offset は 0 以上で指定してください")
    cmap = _column_map(metadata)
    order_by = order_by or (metadata["row_key"][0] if metadata["row_key"] else metadata["columns"][0]["name"])
    if order_by not in cmap:
        raise DataEditError(f"並び替え列 '{order_by}' は存在しません")
    direction = direction.lower()
    if direction not in ("asc", "desc"):
        raise DataEditError("direction は asc または desc を指定してください")

    query = sql.SQL("SELECT * FROM {} ORDER BY {} {} LIMIT {} OFFSET {}").format(
        _ident(schema, table),
        sql.Identifier(order_by),
        sql.SQL(direction.upper()),
        sql.Literal(limit),
        sql.Literal(offset),
    )
    with connect(profile, db) as conn:
        size_row = conn.execute(
            sql.SQL(
                "SELECT COALESCE(sum(pg_column_size(page_row)), 0) "
                "FROM (SELECT * FROM {} ORDER BY {} {} LIMIT {} OFFSET {}) AS page_row"
            ).format(
                _ident(schema, table), sql.Identifier(order_by), sql.SQL(direction.upper()),
                sql.Literal(limit), sql.Literal(offset),
            )
        ).fetchone()
        if int(size_row[0]) > MAX_PAGE_BYTES:
            raise DataEditError(
                f"このページは {MAX_PAGE_BYTES // 1024 // 1024} MiB を超えます。limit を小さくしてください"
            )
        cur = conn.execute(query)
        names = [desc.name for desc in cur.description]
        rows = [dict(zip(names, row)) for row in cur.fetchall()]
        total, approximate = _fetch_count(conn, schema, table)
    return {
        "metadata": metadata,
        "rows": [_row_output(row, metadata) for row in rows],
        "limit": limit,
        "offset": offset,
        "total_rows": total,
        "total_approximate": approximate,
        "order_by": order_by,
        "direction": direction,
    }


def _prepare_value(value: Any, column: dict) -> Any:
    if value is None:
        return None
    family = column["type_family"]
    if family == "json":
        parsed = json.loads(value) if isinstance(value, str) else value
        return Jsonb(parsed) if column["udt_name"] == "jsonb" else Json(parsed)
    if family == "binary":
        if isinstance(value, str):
            text = value.removeprefix("\\x")
            try:
                return bytes.fromhex(text)
            except ValueError as exc:
                raise DataEditError(f"{column['name']} は \\x 形式の16進 byteaで指定してください") from exc
        return bytes(value)
    if family == "boolean" and isinstance(value, str):
        lowered = value.lower()
        if lowered in ("true", "t", "1", "yes", "on"):
            return True
        if lowered in ("false", "f", "0", "no", "off"):
            return False
        raise DataEditError(f"{column['name']} は true または false で指定してください")
    if family == "array" and isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise DataEditError(f"{column['name']} は JSON 配列で指定してください")
        return parsed
    return value


def _cast_placeholder(column: dict) -> sql.Composed:
    return sql.SQL("%s::{}").format(sql.SQL(column["data_type"]))


def _validate_values(
    metadata: dict,
    values: dict,
    *,
    operation: str,
) -> list[tuple[dict, Any]]:
    if not isinstance(values, dict):
        raise DataEditError("values は JSON オブジェクトで指定してください")
    cmap = _column_map(metadata)
    unknown = sorted(set(values) - set(cmap))
    if unknown:
        raise DataEditError(f"存在しない列です: {', '.join(unknown)}")
    prepared = []
    for name, value in values.items():
        column = cmap[name]
        if operation == "insert" and not column["insertable"]:
            raise DataEditError(f"列 '{name}' は自動生成されるため INSERT で指定できません")
        if operation == "update" and not column["updatable"]:
            raise DataEditError(f"列 '{name}' は行キーまたは自動生成列のため UPDATE できません")
        prepared.append((column, _prepare_value(value, column)))
    return prepared


def _advance_sequences(conn, metadata: dict, column_names: list[str]) -> None:
    cmap = _column_map(metadata)
    for name in column_names:
        column = cmap[name]
        if not column["identity"] and not (column["default"] and "nextval(" in column["default"]):
            continue
        sequence = conn.execute(
            "SELECT pg_get_serial_sequence(format('%%I.%%I', %s::text, %s::text), %s::text)",
            (metadata["schema"], metadata["table"], name),
        ).fetchone()[0]
        if not sequence:
            continue
        maximum = conn.execute(
            sql.SQL("SELECT max({}) FROM {}").format(
                sql.Identifier(name), _ident(metadata["schema"], metadata["table"])
            )
        ).fetchone()[0]
        if maximum is None:
            continue
        current = conn.execute(
            "SELECT pg_sequence_last_value(%s::regclass)", (sequence,)
        ).fetchone()[0]
        if current is None or maximum > current:
            conn.execute("SELECT setval(%s::regclass, %s, true)", (sequence, maximum))


def insert_row(profile: Profile, db: str, schema: str, table: str, values: dict) -> dict:
    metadata = table_metadata(profile, db, schema, table)
    if not metadata["can_insert"]:
        raise DataEditError(f"{schema}.{table} の INSERT 権限がないか、実テーブルではありません")
    prepared = _validate_values(metadata, values, operation="insert")
    if prepared:
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
            _ident(schema, table),
            sql.SQL(", ").join(sql.Identifier(column["name"]) for column, _ in prepared),
            sql.SQL(", ").join(_cast_placeholder(column) for column, _ in prepared),
        )
        params = [value for _, value in prepared]
    else:
        query = sql.SQL("INSERT INTO {} DEFAULT VALUES RETURNING *").format(_ident(schema, table))
        params = []
    try:
        with connect(profile, db) as conn:
            cur = conn.execute(query, params)
            names = [desc.name for desc in cur.description]
            row = dict(zip(names, cur.fetchone()))
            _advance_sequences(conn, metadata, [column["name"] for column, _ in prepared])
    except psycopg.Error as exc:
        raise DataEditError(f"INSERT に失敗しました: {exc}") from exc
    return _row_output(row, metadata)


def _key_conditions(metadata: dict, key: dict) -> tuple[sql.Composed, list[Any]]:
    expected = metadata["row_key"]
    if not expected:
        raise DataEditError("主キーまたは非NULL一意キーがないテーブルは更新・削除できません")
    if not isinstance(key, dict) or set(key) != set(expected):
        raise DataEditError(f"行キーは {', '.join(expected)} を正確に指定してください")
    cmap = _column_map(metadata)
    parts, params = [], []
    for name in expected:
        column = cmap[name]
        parts.append(
            sql.SQL("{} IS NOT DISTINCT FROM {}").format(
                sql.Identifier(name), _cast_placeholder(column)
            )
        )
        params.append(_prepare_value(key[name], column))
    return sql.SQL(" AND ").join(parts), params


def _locked_row(conn, metadata: dict, key: dict) -> tuple[dict, sql.Composed, list[Any]]:
    conditions, params = _key_conditions(metadata, key)
    cur = conn.execute(
        sql.SQL("SELECT * FROM {} WHERE {} FOR UPDATE").format(
            _ident(metadata["schema"], metadata["table"]), conditions
        ),
        params,
    )
    row = cur.fetchone()
    if row is None:
        raise DataEditError("対象行が見つかりません。既に削除された可能性があります")
    names = [desc.name for desc in cur.description]
    return dict(zip(names, row)), conditions, params


def _check_etag(row: dict, metadata: dict, etag: str | None, force: bool) -> None:
    if not etag and not force:
        raise DataEditError("更新・削除には行の etag が必要です (--force で明示的に省略可能)")
    if etag and not hmac.compare_digest(_row_etag(row, metadata), etag):
        raise DataEditError(
            "この行は表示後に別の処理で変更されています。再読み込みしてからやり直してください"
        )


def update_row(
    profile: Profile,
    db: str,
    schema: str,
    table: str,
    key: dict,
    values: dict,
    *,
    etag: str | None,
    force: bool = False,
) -> dict:
    metadata = table_metadata(profile, db, schema, table)
    if not metadata["can_update"]:
        raise DataEditError(f"{schema}.{table} は更新できません (権限または行キーを確認)")
    prepared = _validate_values(metadata, values, operation="update")
    if not prepared:
        raise DataEditError("更新する値を1つ以上指定してください")
    try:
        with connect(profile, db) as conn:
            current, conditions, key_params = _locked_row(conn, metadata, key)
            _check_etag(current, metadata, etag, force)
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = {}").format(sql.Identifier(column["name"]), _cast_placeholder(column))
                for column, _ in prepared
            )
            params = [value for _, value in prepared] + key_params
            cur = conn.execute(
                sql.SQL("UPDATE {} SET {} WHERE {} RETURNING *").format(
                    _ident(schema, table), assignments, conditions
                ),
                params,
            )
            names = [desc.name for desc in cur.description]
            row = dict(zip(names, cur.fetchone()))
    except DataEditError:
        raise
    except psycopg.Error as exc:
        raise DataEditError(f"UPDATE に失敗しました: {exc}") from exc
    return _row_output(row, metadata)


def delete_row(
    profile: Profile,
    db: str,
    schema: str,
    table: str,
    key: dict,
    *,
    etag: str | None,
    force: bool = False,
) -> dict:
    metadata = table_metadata(profile, db, schema, table)
    if not metadata["can_delete"]:
        raise DataEditError(f"{schema}.{table} は削除できません (権限または行キーを確認)")
    try:
        with connect(profile, db) as conn:
            current, conditions, key_params = _locked_row(conn, metadata, key)
            _check_etag(current, metadata, etag, force)
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE {}").format(_ident(schema, table), conditions),
                key_params,
            )
    except DataEditError:
        raise
    except psycopg.Error as exc:
        raise DataEditError(f"DELETE に失敗しました: {exc}") from exc
    return _row_output(current, metadata)


def _resolve_compare_shape(source_meta: dict, target_meta: dict, key_columns: list[str] | None) -> dict:
    source_map = _column_map(source_meta)
    target_map = _column_map(target_meta)
    source_names = [column["name"] for column in source_meta["columns"]]
    shared = [name for name in source_names if name in target_map]
    keys = list(key_columns or source_meta["row_key"])
    if not keys:
        raise DataEditError("比較元に主キーまたは非NULL一意キーがありません。--key を指定してください")
    if any(name not in source_map or name not in target_map for name in keys):
        raise DataEditError("比較キーが両方のテーブルに存在しません")

    def valid_unique(meta: dict) -> list[set[str]]:
        cmap = _column_map(meta)
        return [
            set(item["columns"])
            for item in meta["unique_keys"]
            if not item["deferrable"] and all(not cmap[name]["nullable"] for name in item["columns"])
        ]

    if set(keys) not in valid_unique(source_meta) or set(keys) not in valid_unique(target_meta):
        raise DataEditError("比較キーは両方のテーブルで非NULLの PRIMARY KEY / UNIQUE である必要があります")

    warnings = []
    for name in shared:
        source_column, target_column = source_map[name], target_map[name]
        source_family, target_family = source_column["type_family"], target_column["type_family"]
        compatible = source_family == target_family and (
            source_column["udt_name"] == target_column["udt_name"]
            or source_family in ("numeric", "text", "json")
        )
        if not compatible:
            raise DataEditError(
                f"列 '{name}' の型に互換性がありません: "
                f"{source_column['data_type']} -> {target_column['data_type']}"
            )
        if source_column["udt_name"] != target_column["udt_name"]:
            warnings.append(
                f"{name}: {source_column['data_type']} -> {target_column['data_type']} "
                "(代入時に PostgreSQL の型変換を使用)"
            )

    missing_required = [
        column["name"]
        for column in target_meta["columns"]
        if column["name"] not in source_map
        and not column["nullable"]
        and column["default"] is None
        and not column["identity"]
        and not column["generated"]
    ]
    if missing_required:
        raise DataEditError(
            "比較先の必須列が比較元にありません: " + ", ".join(missing_required)
        )

    generated = [name for name in shared if target_map[name]["generated"]]
    if generated:
        warnings.append(
            "比較先の生成列は比較・上書き対象外です: " + ", ".join(generated)
        )
    comparable = [name for name in shared if not target_map[name]["generated"]]
    writable = [name for name in comparable if name not in keys]
    insertable = list(comparable)
    return {
        "key_columns": keys,
        "compare_columns": comparable,
        "writable_columns": writable,
        "insert_columns": insertable,
        "warnings": warnings,
    }


def _fetch_compare_rows(
    conn,
    metadata: dict,
    columns: list[str],
    keys: list[str],
    max_rows: int,
) -> list[dict]:
    selected = sql.SQL(", ").join(sql.Identifier(name) for name in columns)
    ordering = sql.SQL(", ").join(sql.Identifier(name) for name in keys)
    count, byte_size = conn.execute(
        sql.SQL(
            "SELECT count(*), COALESCE(sum(pg_column_size(compare_row)), 0) FROM "
            "(SELECT {} FROM {} ORDER BY {} LIMIT {}) AS compare_row"
        ).format(selected, _ident(metadata["schema"], metadata["table"]), ordering,
                 sql.Literal(max_rows + 1))
    ).fetchone()
    if count > max_rows:
        raise DataEditError(
            f"{metadata['schema']}.{metadata['table']} が比較上限 {max_rows} 行を超えています"
        )
    if byte_size > MAX_COMPARE_BYTES:
        raise DataEditError(
            f"{metadata['schema']}.{metadata['table']} の比較データが "
            f"{MAX_COMPARE_BYTES // 1024 // 1024} MiB を超えています"
        )
    query = sql.SQL("SELECT {} FROM {} ORDER BY {} LIMIT {}").format(
        selected,
        _ident(metadata["schema"], metadata["table"]),
        ordering,
        sql.Literal(max_rows),
    )
    cur = conn.execute(query)
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    return rows


def _build_plan(
    source_rows: list[dict],
    target_rows: list[dict],
    source_meta: dict,
    target_meta: dict,
    shape: dict,
) -> dict:
    keys = shape["key_columns"]

    def index(rows: list[dict], metadata: dict) -> dict[str, dict]:
        cmap = _column_map(metadata)
        result = {}
        for row in rows:
            if any(row[name] is None for name in keys):
                raise DataEditError("比較キーに NULL が含まれています")
            token = _hash_payload(
                {name: _canonical_value(row[name], cmap[name]) for name in keys}
            )
            if token in result:
                raise DataEditError("比較キーが重複しています")
            result[token] = row
        return result

    source_index = index(source_rows, source_meta)
    target_index = index(target_rows, target_meta)
    source_map, target_map = _column_map(source_meta), _column_map(target_meta)
    only_source, only_target, changed = [], [], []
    same = 0
    for token in sorted(set(source_index) | set(target_index)):
        source = source_index.get(token)
        target = target_index.get(token)
        if target is None:
            only_source.append(source)
            continue
        if source is None:
            only_target.append(target)
            continue
        changed_columns = []
        for name in shape["compare_columns"]:
            left = _canonical_value(source[name], source_map[name])
            right = _canonical_value(target[name], target_map[name])
            if left != right:
                changed_columns.append(name)
        if changed_columns:
            changed.append({"source": source, "target": target, "columns": changed_columns})
        else:
            same += 1

    hash_payload = {
        "version": 1,
        "source": {
            "profile": source_meta["profile"], "database": source_meta["database"],
            "host": source_meta["host"], "port": source_meta["port"],
            "schema": source_meta["schema"], "table": source_meta["table"],
        },
        "target": {
            "profile": target_meta["profile"], "database": target_meta["database"],
            "host": target_meta["host"], "port": target_meta["port"],
            "schema": target_meta["schema"], "table": target_meta["table"],
        },
        "shape": shape,
        "source_rows": [
            _canonical_row(row, source_meta, shape["compare_columns"])
            for row in source_rows
        ],
        "target_rows": [
            _canonical_row(row, target_meta, shape["compare_columns"])
            for row in target_rows
        ],
    }
    return {
        "only_source": only_source,
        "only_target": only_target,
        "changed": changed,
        "same": same,
        "plan_hash": _hash_payload(hash_payload),
    }


def _public_plan(plan: dict, shape: dict, source_meta: dict, target_meta: dict, preview: int) -> dict:
    keys = shape["key_columns"]

    source_map, target_map = _column_map(source_meta), _column_map(target_meta)

    def key_json(row: dict, cmap: dict) -> dict:
        return {name: _display_value(row[name], cmap[name]) for name in keys}

    return {
        "source": {
            "profile": source_meta["profile"], "database": source_meta["database"],
            "host": source_meta["host"], "port": source_meta["port"],
            "schema": source_meta["schema"], "table": source_meta["table"],
        },
        "target": {
            "profile": target_meta["profile"], "database": target_meta["database"],
            "host": target_meta["host"], "port": target_meta["port"],
            "schema": target_meta["schema"], "table": target_meta["table"],
        },
        "key_columns": keys,
        "compare_columns": shape["compare_columns"],
        "warnings": shape["warnings"],
        "summary": {
            "source_rows": len(plan["only_source"]) + len(plan["changed"]) + plan["same"],
            "target_rows": len(plan["only_target"]) + len(plan["changed"]) + plan["same"],
            "only_source": len(plan["only_source"]),
            "only_target": len(plan["only_target"]),
            "changed": len(plan["changed"]),
            "same": plan["same"],
        },
        "preview": {
            "only_source": [
                {"key": key_json(row, source_map), "row": _display_row(row, source_meta)}
                for row in plan["only_source"][:preview]
            ],
            "only_target": [
                {"key": key_json(row, target_map), "row": _display_row(row, target_meta)}
                for row in plan["only_target"][:preview]
            ],
            "changed": [
                {
                    "key": key_json(item["source"], source_map),
                    "columns": item["columns"],
                    "source": _display_row(item["source"], source_meta),
                    "target": _display_row(item["target"], target_meta),
                }
                for item in plan["changed"][:preview]
            ],
        },
        "plan_hash": plan["plan_hash"],
        "confirmation": (
            f"{target_meta['profile']}@{target_meta['host']}:{target_meta['port']}/"
            f"{target_meta['database']}/"
            f"{target_meta['schema']}.{target_meta['table']}"
        ),
    }


def compare_tables(
    source: Profile,
    source_db: str,
    source_schema: str,
    source_table: str,
    target: Profile,
    target_db: str,
    target_schema: str,
    target_table: str,
    *,
    key_columns: list[str] | None = None,
    max_rows: int = DEFAULT_COMPARE_ROWS,
    preview_rows: int = DEFAULT_PREVIEW_ROWS,
) -> dict:
    max_rows = int(max_rows)
    preview_rows = int(preview_rows)
    if not 1 <= max_rows <= MAX_COMPARE_ROWS:
        raise DataEditError(f"max_rows は 1〜{MAX_COMPARE_ROWS} の範囲で指定してください")
    if not 1 <= preview_rows <= MAX_PREVIEW_ROWS:
        raise DataEditError(f"preview_rows は 1〜{MAX_PREVIEW_ROWS} の範囲で指定してください")
    source_meta = table_metadata(source, source_db, source_schema, source_table)
    target_meta = table_metadata(target, target_db, target_schema, target_table)
    if not source_meta["can_select"] or not target_meta["can_select"]:
        raise DataEditError("比較には両方のテーブルの SELECT 権限が必要です")
    if source_meta["row_security_effective"] or target_meta["row_security_effective"]:
        raise DataEditError(
            "実効的な Row Level Security があるテーブルは全行を確認できないため比較・同期できません"
        )
    shape = _resolve_compare_shape(source_meta, target_meta, key_columns)
    with connect(source, source_db) as source_conn:
        source_rows = _fetch_compare_rows(
            source_conn, source_meta, shape["compare_columns"], shape["key_columns"], max_rows
        )
    with connect(target, target_db) as target_conn:
        target_rows = _fetch_compare_rows(
            target_conn, target_meta, shape["compare_columns"], shape["key_columns"], max_rows
        )
    plan = _build_plan(source_rows, target_rows, source_meta, target_meta, shape)
    return _public_plan(plan, shape, source_meta, target_meta, preview_rows)


def _insert_from_source(conn, metadata: dict, columns: list[str], row: dict) -> None:
    cmap = _column_map(metadata)
    overriding = sql.SQL(" OVERRIDING SYSTEM VALUE") if any(
        cmap[name]["identity"] == "a" for name in columns
    ) else sql.SQL("")
    query = sql.SQL("INSERT INTO {} ({}){} VALUES ({})").format(
        _ident(metadata["schema"], metadata["table"]),
        sql.SQL(", ").join(sql.Identifier(name) for name in columns),
        overriding,
        sql.SQL(", ").join(_cast_placeholder(cmap[name]) for name in columns),
    )
    conn.execute(query, [_prepare_value(row[name], cmap[name]) for name in columns])


def _update_from_source(
    conn, metadata: dict, keys: list[str], columns: list[str], source_row: dict
) -> None:
    if not columns:
        return
    cmap = _column_map(metadata)
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(name), _cast_placeholder(cmap[name]))
        for name in columns
    )
    conditions = sql.SQL(" AND ").join(
        sql.SQL("{} IS NOT DISTINCT FROM {}").format(
            sql.Identifier(name), _cast_placeholder(cmap[name])
        )
        for name in keys
    )
    params = [_prepare_value(source_row[name], cmap[name]) for name in columns]
    params.extend(_prepare_value(source_row[name], cmap[name]) for name in keys)
    result = conn.execute(
        sql.SQL("UPDATE {} SET {} WHERE {}").format(
            _ident(metadata["schema"], metadata["table"]), assignments, conditions
        ),
        params,
    )
    if result.rowcount != 1:
        raise DataEditError("同期中に対象行が変化しました。全処理をロールバックします")


def _delete_target_row(conn, metadata: dict, keys: list[str], row: dict) -> None:
    cmap = _column_map(metadata)
    conditions = sql.SQL(" AND ").join(
        sql.SQL("{} IS NOT DISTINCT FROM {}").format(
            sql.Identifier(name), _cast_placeholder(cmap[name])
        )
        for name in keys
    )
    result = conn.execute(
        sql.SQL("DELETE FROM {} WHERE {}").format(
            _ident(metadata["schema"], metadata["table"]), conditions
        ),
        [_prepare_value(row[name], cmap[name]) for name in keys],
    )
    if result.rowcount != 1:
        raise DataEditError("同期中に削除対象行が変化しました。全処理をロールバックします")


def apply_table_plan(
    source: Profile,
    source_db: str,
    source_schema: str,
    source_table: str,
    target: Profile,
    target_db: str,
    target_schema: str,
    target_table: str,
    *,
    strategy: str,
    expected_plan_hash: str,
    confirmation: str,
    key_columns: list[str] | None = None,
    max_rows: int = DEFAULT_COMPARE_ROWS,
) -> dict:
    if strategy not in ("merge", "sync"):
        raise DataEditError("strategy は merge または sync を指定してください")
    if not expected_plan_hash or len(expected_plan_hash) != 64:
        raise DataEditError("比較結果の plan_hash が必要です")
    expected_confirmation = (
        f"{target.name}@{target.host}:{target.port}/{target_db}/{target_schema}.{target_table}"
    )
    if confirmation != expected_confirmation:
        raise DataEditError(f"確認文字列が一致しません。'{expected_confirmation}' を指定してください")
    max_rows = int(max_rows)
    if not 1 <= max_rows <= MAX_COMPARE_ROWS:
        raise DataEditError(f"max_rows は 1〜{MAX_COMPARE_ROWS} の範囲で指定してください")
    if (
        source_db == target_db
        and source_schema == target_schema
        and source_table == target_table
        and same_cluster(source, target)
    ):
        raise DataEditError("比較元と上書き先が同一テーブルです")

    source_meta = table_metadata(source, source_db, source_schema, source_table)
    target_meta = table_metadata(target, target_db, target_schema, target_table)
    if source_meta["row_security_effective"] or target_meta["row_security_effective"]:
        raise DataEditError(
            "実効的な Row Level Security があるテーブルは全行を確認できないためmerge/syncできません"
        )
    if not target_meta["can_insert"] or not target_meta["can_update"]:
        raise DataEditError("merge には対象テーブルの INSERT / UPDATE 権限と行キーが必要です")
    if strategy == "sync" and not target_meta["can_delete"]:
        raise DataEditError("sync には対象テーブルの DELETE 権限も必要です")
    shape = _resolve_compare_shape(source_meta, target_meta, key_columns)

    source_conn = connect(source, source_db)
    target_conn = connect(target, target_db)
    try:
        source_conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        target_conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        target_conn.execute("SET LOCAL lock_timeout = '5000ms'")
        target_conn.execute("SET LOCAL statement_timeout = '300000ms'")
        target_conn.execute(
            sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(
                _ident(target_schema, target_table)
            )
        )
        source_rows = _fetch_compare_rows(
            source_conn, source_meta, shape["compare_columns"], shape["key_columns"], max_rows
        )
        target_rows = _fetch_compare_rows(
            target_conn, target_meta, shape["compare_columns"], shape["key_columns"], max_rows
        )
        plan = _build_plan(source_rows, target_rows, source_meta, target_meta, shape)
        if not hmac.compare_digest(plan["plan_hash"], expected_plan_hash):
            raise DataEditError(
                "比較後に元または対象データが変更されました。再比較してから実行してください"
            )

        for row in plan["only_source"]:
            _insert_from_source(target_conn, target_meta, shape["insert_columns"], row)
        if plan["only_source"]:
            _advance_sequences(target_conn, target_meta, shape["insert_columns"])
        for item in plan["changed"]:
            columns = [
                name for name in item["columns"] if name in shape["writable_columns"]
            ]
            _update_from_source(
                target_conn, target_meta, shape["key_columns"], columns, item["source"]
            )
        if strategy == "sync":
            for row in plan["only_target"]:
                _delete_target_row(target_conn, target_meta, shape["key_columns"], row)
        source_conn.commit()
        target_conn.commit()
    except DataEditError:
        target_conn.rollback()
        source_conn.rollback()
        raise
    except psycopg.Error as exc:
        target_conn.rollback()
        source_conn.rollback()
        raise DataEditError(f"{strategy} の適用に失敗し、ロールバックしました: {exc}") from exc
    finally:
        target_conn.close()
        source_conn.close()

    return {
        "strategy": strategy,
        "inserted": len(plan["only_source"]),
        "updated": len(plan["changed"]),
        "deleted": len(plan["only_target"]) if strategy == "sync" else 0,
        "unchanged": plan["same"],
        "plan_hash": plan["plan_hash"],
        "target": expected_confirmation,
    }
