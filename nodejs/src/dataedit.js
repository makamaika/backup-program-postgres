// 安全な行編集、テーブル比較、merge/sync。
// 任意SQLは受け取らず、識別子をquoteIdentし、値はすべてバインドする。

import crypto from "node:crypto";

import { connect, quoteIdent, sameCluster, withConn } from "./db.js";
import { DataEditError } from "./errors.js";

export const DEFAULT_PAGE_SIZE = 50;
export const MAX_PAGE_SIZE = 200;
export const MAX_PAGE_BYTES = 32 * 1024 * 1024;
export const DEFAULT_COMPARE_ROWS = 10_000;
export const MAX_COMPARE_ROWS = 50_000;
export const MAX_COMPARE_BYTES = 128 * 1024 * 1024;
export const DEFAULT_PREVIEW_ROWS = 100;
export const MAX_PREVIEW_ROWS = 500;

const NUMERIC_UDTS = new Set(["int2", "int4", "int8", "numeric", "float4", "float8", "money", "oid"]);
const TEXT_UDTS = new Set(["text", "varchar", "bpchar", "name", "citext", "char"]);
const JSON_UDTS = new Set(["json", "jsonb"]);
const DATETIME_UDTS = new Set(["timestamp", "timestamptz"]);
const TIME_UDTS = new Set(["time", "timetz"]);
const NETWORK_UDTS = new Set(["inet", "cidr", "macaddr", "macaddr8"]);

function ident(schema, table) {
  return `${quoteIdent(schema)}.${quoteIdent(table)}`;
}

function typeFamily(column) {
  const udt = column.udt_name;
  if (NUMERIC_UDTS.has(udt)) return "numeric";
  if (TEXT_UDTS.has(udt)) return "text";
  if (JSON_UDTS.has(udt)) return "json";
  if (DATETIME_UDTS.has(udt)) return "datetime";
  if (TIME_UDTS.has(udt)) return "time";
  if (udt === "date") return "date";
  if (udt === "bool") return "boolean";
  if (udt === "uuid") return "uuid";
  if (udt === "bytea") return "binary";
  if (udt.startsWith("_") || column.data_type === "ARRAY") return "array";
  if (NETWORK_UDTS.has(udt)) return "network";
  return `exact:${udt}`;
}

export async function tableMetadata(profile, db, schema, table) {
  return withConn(profile, db, async (client) => {
    const relation = (await client.query(
      `SELECT c.relkind,
              has_table_privilege(c.oid, 'SELECT') AS can_select,
              has_table_privilege(c.oid, 'INSERT') AS can_insert,
              has_table_privilege(c.oid, 'UPDATE') AS can_update,
              has_table_privilege(c.oid, 'DELETE') AS can_delete,
              c.relrowsecurity AS row_security,
              c.relforcerowsecurity AS force_row_security,
              ((SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)
               OR (pg_get_userbyid(c.relowner) = current_user AND NOT c.relforcerowsecurity))
                AS row_security_bypassed
       FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
       WHERE n.nspname = $1 AND c.relname = $2`,
      [schema, table]
    )).rows[0];
    if (!relation) throw new DataEditError(`テーブル ${schema}.${table} が見つかりません`);

    const columns = (await client.query(
      `SELECT a.attname AS name,
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
       WHERE n.nspname = $1 AND c.relname = $2
         AND a.attnum > 0 AND NOT a.attisdropped
       ORDER BY a.attnum`,
      [schema, table]
    )).rows;
    if (!columns.length) throw new DataEditError(`${schema}.${table} に編集可能な列がありません`);

    const constraints = (await client.query(
      `SELECT con.conname,
              con.contype,
              con.condeferrable,
              json_agg(att.attname ORDER BY key.ord) AS columns
       FROM pg_constraint con
       JOIN pg_class c ON c.oid = con.conrelid
       JOIN pg_namespace n ON n.oid = c.relnamespace
       CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS key(attnum, ord)
       JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = key.attnum
       WHERE n.nspname = $1 AND c.relname = $2 AND con.contype IN ('p', 'u')
       GROUP BY con.conname, con.contype, con.condeferrable
       ORDER BY CASE con.contype WHEN 'p' THEN 0 ELSE 1 END, con.conname`,
      [schema, table]
    )).rows;

    const cmap = Object.fromEntries(columns.map((column) => [column.name, column]));
    const uniqueKeys = constraints.map((constraint) => ({
      name: constraint.conname,
      columns: constraint.columns,
      primary: constraint.contype === "p",
      deferrable: constraint.condeferrable,
    }));
    const primaryKey = uniqueKeys.find((key) => key.primary)?.columns ?? [];
    let rowKey = [...primaryKey];
    if (!rowKey.length) {
      const fallback = uniqueKeys.find((key) =>
        !key.deferrable && key.columns.every((name) => !cmap[name].nullable));
      if (fallback) rowKey = [...fallback.columns];
    }
    const baseTable = ["r", "p"].includes(relation.relkind);
    for (const column of columns) {
      column.ordinal = Number(column.ordinal);
      column.type_family = typeFamily(column);
      column.is_primary_key = primaryKey.includes(column.name);
      column.is_row_key = rowKey.includes(column.name);
      column.insertable = baseTable && !column.generated && column.identity !== "a";
      column.updatable = baseTable && !column.generated && !column.is_row_key;
    }
    return {
      profile: profile.name, host: profile.host, port: profile.port, database: db, schema, table,
      relation_kind: relation.relkind, base_table: baseTable,
      columns, primary_key: primaryKey, unique_keys: uniqueKeys, row_key: rowKey,
      can_select: Boolean(relation.can_select),
      can_insert: Boolean(relation.can_insert) && baseTable,
      can_update: Boolean(relation.can_update) && baseTable && rowKey.length > 0,
      can_delete: Boolean(relation.can_delete) && baseTable && rowKey.length > 0,
      row_security: Boolean(relation.row_security),
      force_row_security: Boolean(relation.force_row_security),
      row_security_effective: Boolean(relation.row_security) && !Boolean(relation.row_security_bypassed),
    };
  });
}

function columnMap(metadata) {
  return Object.fromEntries(metadata.columns.map((column) => [column.name, column]));
}

function jsonable(value) {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") {
    if (typeof value === "number" && !Number.isFinite(value)) return String(value);
    return value;
  }
  if (typeof value === "bigint") return value.toString();
  if (Buffer.isBuffer(value)) return `\\x${value.toString("hex")}`;
  if (value instanceof Date) return value.toISOString();
  if (Array.isArray(value)) return value.map(jsonable);
  if (typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, jsonable(item)]));
  }
  return String(value);
}

function canonicalDecimal(value) {
  let text = String(value).trim().toLowerCase();
  if (["nan", "infinity", "+infinity", "-infinity"].includes(text)) return text;
  const match = /^([+-]?)(\d*)(?:\.(\d*))?(?:e([+-]?\d+))?$/.exec(text);
  if (!match || (!match[2] && !match[3])) throw new DataEditError(`数値として比較できない値です: ${value}`);
  const sign = match[1] === "-" ? "-" : "";
  let integer = match[2] || "0";
  let fraction = match[3] || "";
  const exponent = Number(match[4] || 0);
  let digits = integer + fraction;
  let point = integer.length + exponent;
  if (point <= 0) {
    digits = "0".repeat(-point) + digits;
    point = 0;
  } else if (point >= digits.length) {
    digits += "0".repeat(point - digits.length);
    point = digits.length;
  }
  integer = (point ? digits.slice(0, point) : "0").replace(/^0+(?=\d)/, "");
  fraction = digits.slice(point).replace(/0+$/, "");
  if (!integer) integer = "0";
  const result = fraction ? `${integer}.${fraction}` : integer;
  return result === "0" ? "0" : sign + result;
}

function normalizeTemporal(value) {
  let text = value instanceof Date ? value.toISOString() : String(value).replace(" ", "T");
  text = text.replace(/\+00(?::?00)?$/, "Z");
  text = text.replace(/\.(\d+)(?=Z|[+-]\d{2}(?::?\d{2})?$|$)/, (match, digits) => {
    const trimmed = digits.replace(/0+$/, "");
    return trimmed ? `.${trimmed}` : "";
  });
  return text;
}

function canonicalValue(value, column) {
  if (value === null || value === undefined) return null;
  const family = column.type_family;
  if (family === "numeric") return { $numeric: canonicalDecimal(value) };
  if (family === "binary") {
    const raw = Buffer.isBuffer(value) ? value : Buffer.from(String(value).replace(/^\\x/, ""), "hex");
    return { $binary: raw.toString("base64") };
  }
  if (family === "datetime") return { $datetime: normalizeTemporal(value) };
  if (family === "date" || family === "time") return { [`$${family}`]: normalizeTemporal(value) };
  if (family === "json") return { $json: typeof value === "string" ? JSON.parse(value) : jsonable(value) };
  if (family === "array") return { $array: value.map(jsonable) };
  return jsonable(value);
}

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object" && !Buffer.isBuffer(value) && !(value instanceof Date)) {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
  }
  return value;
}

function hashPayload(value) {
  return crypto.createHash("sha256").update(JSON.stringify(stable(value)), "utf-8").digest("hex");
}

function canonicalRow(row, metadata, columns = null) {
  const cmap = columnMap(metadata);
  const selected = columns ?? metadata.columns.map((column) => column.name);
  return Object.fromEntries(selected.map((name) => [name, canonicalValue(row[name], cmap[name])]));
}

function displayValue(value, column) {
  if (value !== null && value !== undefined && ["datetime", "date", "time"].includes(column.type_family)) {
    return normalizeTemporal(value);
  }
  return jsonable(value);
}

function displayRow(row, metadata) {
  const cmap = columnMap(metadata);
  return Object.fromEntries(Object.entries(row).map(([name, value]) => [name, displayValue(value, cmap[name])]));
}

function rowEtag(row, metadata) {
  return hashPayload(canonicalRow(row, metadata));
}

function rowOutput(row, metadata) {
  const cmap = columnMap(metadata);
  return {
    values: displayRow(row, metadata),
    key: Object.fromEntries(metadata.row_key.map((name) => [name, displayValue(row[name], cmap[name])])),
    etag: rowEtag(row, metadata),
  };
}

async function fetchCount(client, schema, table) {
  try {
    await client.query("BEGIN");
    await client.query("SET LOCAL statement_timeout = '3000ms'");
    const total = Number((await client.query(`SELECT count(*) AS n FROM ${ident(schema, table)}`)).rows[0].n);
    await client.query("COMMIT");
    return [total, false];
  } catch (error) {
    await client.query("ROLLBACK").catch(() => {});
    if (error.code !== "57014") throw error;
    const row = (await client.query(
      `SELECT GREATEST(c.reltuples, 0)::bigint AS n
       FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
       WHERE n.nspname = $1 AND c.relname = $2`, [schema, table]
    )).rows[0];
    return [Number(row.n), true];
  }
}

export async function readRows(profile, db, schema, table, {
  limit = DEFAULT_PAGE_SIZE, offset = 0, orderBy = null, direction = "asc",
} = {}) {
  const metadata = await tableMetadata(profile, db, schema, table);
  if (!metadata.can_select) throw new DataEditError(`${schema}.${table} の SELECT 権限がありません`);
  limit = Number(limit); offset = Number(offset);
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_PAGE_SIZE) {
    throw new DataEditError(`limit は 1〜${MAX_PAGE_SIZE} の範囲で指定してください`);
  }
  if (!Number.isInteger(offset) || offset < 0) throw new DataEditError("offset は 0 以上で指定してください");
  const cmap = columnMap(metadata);
  orderBy = orderBy || metadata.row_key[0] || metadata.columns[0].name;
  if (!(orderBy in cmap)) throw new DataEditError(`並び替え列 '${orderBy}' は存在しません`);
  direction = String(direction).toLowerCase();
  if (!["asc", "desc"].includes(direction)) throw new DataEditError("direction は asc または desc を指定してください");
  return withConn(profile, db, async (client) => {
    const size = Number((await client.query(
      `SELECT COALESCE(sum(pg_column_size(page_row)), 0) AS bytes FROM ` +
      `(SELECT * FROM ${ident(schema, table)} ORDER BY ${quoteIdent(orderBy)} ${direction.toUpperCase()} ` +
      `LIMIT $1 OFFSET $2) AS page_row`, [limit, offset]
    )).rows[0].bytes);
    if (size > MAX_PAGE_BYTES) {
      throw new DataEditError(`このページは ${MAX_PAGE_BYTES / 1024 / 1024} MiB を超えます。limit を小さくしてください`);
    }
    const rows = (await client.query(
      `SELECT * FROM ${ident(schema, table)} ORDER BY ${quoteIdent(orderBy)} ${direction.toUpperCase()} LIMIT $1 OFFSET $2`,
      [limit, offset]
    )).rows;
    const [total, approximate] = await fetchCount(client, schema, table);
    return {
      metadata, rows: rows.map((row) => rowOutput(row, metadata)), limit, offset,
      total_rows: total, total_approximate: approximate, order_by: orderBy, direction,
    };
  });
}

function prepareValue(value, column) {
  if (value === null || value === undefined) return null;
  const family = column.type_family;
  if (family === "json" && typeof value === "string") return JSON.parse(value);
  if (family === "binary") {
    if (Buffer.isBuffer(value)) return value;
    const text = String(value).replace(/^\\x/, "");
    if (!/^(?:[0-9a-fA-F]{2})*$/.test(text)) {
      throw new DataEditError(`${column.name} は \\x 形式の16進 byteaで指定してください`);
    }
    return Buffer.from(text, "hex");
  }
  if (family === "boolean" && typeof value === "string") {
    if (["true", "t", "1", "yes", "on"].includes(value.toLowerCase())) return true;
    if (["false", "f", "0", "no", "off"].includes(value.toLowerCase())) return false;
    throw new DataEditError(`${column.name} は true または false で指定してください`);
  }
  if (family === "array" && typeof value === "string") {
    const parsed = JSON.parse(value);
    if (!Array.isArray(parsed)) throw new DataEditError(`${column.name} は JSON 配列で指定してください`);
    return parsed;
  }
  return value;
}

function castPlaceholder(column, index) {
  return `$${index}::${column.data_type}`;
}

function validateValues(metadata, values, operation) {
  if (!values || typeof values !== "object" || Array.isArray(values)) {
    throw new DataEditError("values は JSON オブジェクトで指定してください");
  }
  const cmap = columnMap(metadata);
  const unknown = Object.keys(values).filter((name) => !(name in cmap));
  if (unknown.length) throw new DataEditError(`存在しない列です: ${unknown.join(", ")}`);
  return Object.entries(values).map(([name, value]) => {
    const column = cmap[name];
    if (operation === "insert" && !column.insertable) {
      throw new DataEditError(`列 '${name}' は自動生成されるため INSERT で指定できません`);
    }
    if (operation === "update" && !column.updatable) {
      throw new DataEditError(`列 '${name}' は行キーまたは自動生成列のため UPDATE できません`);
    }
    return [column, prepareValue(value, column)];
  });
}

async function advanceSequences(client, metadata, columnNames) {
  const cmap = columnMap(metadata);
  for (const name of columnNames) {
    const column = cmap[name];
    if (!column.identity && !(column.default && column.default.includes("nextval("))) continue;
    const sequence = (await client.query(
      "SELECT pg_get_serial_sequence(format('%I.%I', $1::text, $2::text), $3::text) AS sequence",
      [metadata.schema, metadata.table, name]
    )).rows[0].sequence;
    if (!sequence) continue;
    const maximum = (await client.query(
      `SELECT max(${quoteIdent(name)}) AS maximum FROM ${ident(metadata.schema, metadata.table)}`
    )).rows[0].maximum;
    if (maximum === null || maximum === undefined) continue;
    const current = (await client.query(
      "SELECT pg_sequence_last_value($1::regclass) AS current", [sequence]
    )).rows[0].current;
    if (current === null || BigInt(maximum) > BigInt(current)) {
      await client.query("SELECT setval($1::regclass, $2, true)", [sequence, maximum]);
    }
  }
}

export async function insertRow(profile, db, schema, table, values) {
  const metadata = await tableMetadata(profile, db, schema, table);
  if (!metadata.can_insert) throw new DataEditError(`${schema}.${table} の INSERT 権限がありません`);
  const prepared = validateValues(metadata, values, "insert");
  let query, params;
  if (prepared.length) {
    query = `INSERT INTO ${ident(schema, table)} (${prepared.map(([c]) => quoteIdent(c.name)).join(", ")}) ` +
      `VALUES (${prepared.map(([c], i) => castPlaceholder(c, i + 1)).join(", ")}) RETURNING *`;
    params = prepared.map(([, value]) => value);
  } else {
    query = `INSERT INTO ${ident(schema, table)} DEFAULT VALUES RETURNING *`;
    params = [];
  }
  const client = await connect(profile, db);
  try {
    await client.query("BEGIN");
    const row = (await client.query(query, params)).rows[0];
    await advanceSequences(client, metadata, prepared.map(([column]) => column.name));
    await client.query("COMMIT");
    return rowOutput(row, metadata);
  } catch (error) {
    await client.query("ROLLBACK").catch(() => {});
    if (error instanceof DataEditError) throw error;
    throw new DataEditError(`INSERT に失敗しました: ${error.message}`);
  } finally {
    await client.end().catch(() => {});
  }
}

function keyConditions(metadata, key, startIndex = 1) {
  if (!metadata.row_key.length) throw new DataEditError("主キーまたは非NULL一意キーがないテーブルは更新・削除できません");
  if (!key || typeof key !== "object" || Array.isArray(key)
      || Object.keys(key).length !== metadata.row_key.length
      || metadata.row_key.some((name) => !(name in key))) {
    throw new DataEditError(`行キーは ${metadata.row_key.join(", ")} を正確に指定してください`);
  }
  const cmap = columnMap(metadata);
  return {
    sql: metadata.row_key.map((name, index) =>
      `${quoteIdent(name)} IS NOT DISTINCT FROM ${castPlaceholder(cmap[name], startIndex + index)}`).join(" AND "),
    params: metadata.row_key.map((name) => prepareValue(key[name], cmap[name])),
  };
}

async function lockedRow(client, metadata, key) {
  const conditions = keyConditions(metadata, key);
  const result = await client.query(
    `SELECT * FROM ${ident(metadata.schema, metadata.table)} WHERE ${conditions.sql} FOR UPDATE`,
    conditions.params
  );
  if (!result.rows.length) throw new DataEditError("対象行が見つかりません。既に削除された可能性があります");
  return [result.rows[0], conditions];
}

function secureEqual(left, right) {
  const a = Buffer.from(String(left));
  const b = Buffer.from(String(right));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function checkEtag(row, metadata, etag, force) {
  if (!etag && !force) throw new DataEditError("更新・削除には行の etag が必要です (--force で省略可能)");
  if (etag && !secureEqual(rowEtag(row, metadata), etag)) {
    throw new DataEditError("この行は表示後に別の処理で変更されています。再読み込みしてください");
  }
}

export async function updateRow(profile, db, schema, table, key, values, { etag = null, force = false } = {}) {
  const metadata = await tableMetadata(profile, db, schema, table);
  if (!metadata.can_update) throw new DataEditError(`${schema}.${table} は更新できません`);
  const prepared = validateValues(metadata, values, "update");
  if (!prepared.length) throw new DataEditError("更新する値を1つ以上指定してください");
  const client = await connect(profile, db);
  try {
    await client.query("BEGIN");
    const [current] = await lockedRow(client, metadata, key);
    checkEtag(current, metadata, etag, force);
    const conditions = keyConditions(metadata, key, prepared.length + 1);
    const assignments = prepared.map(([column], i) =>
      `${quoteIdent(column.name)} = ${castPlaceholder(column, i + 1)}`).join(", ");
    const result = await client.query(
      `UPDATE ${ident(schema, table)} SET ${assignments} WHERE ${conditions.sql} RETURNING *`,
      [...prepared.map(([, value]) => value), ...conditions.params]
    );
    await client.query("COMMIT");
    return rowOutput(result.rows[0], metadata);
  } catch (error) {
    await client.query("ROLLBACK").catch(() => {});
    if (error instanceof DataEditError) throw error;
    throw new DataEditError(`UPDATE に失敗しました: ${error.message}`);
  } finally {
    await client.end().catch(() => {});
  }
}

export async function deleteRow(profile, db, schema, table, key, { etag = null, force = false } = {}) {
  const metadata = await tableMetadata(profile, db, schema, table);
  if (!metadata.can_delete) throw new DataEditError(`${schema}.${table} は削除できません`);
  const client = await connect(profile, db);
  try {
    await client.query("BEGIN");
    const [current, conditions] = await lockedRow(client, metadata, key);
    checkEtag(current, metadata, etag, force);
    await client.query(`DELETE FROM ${ident(schema, table)} WHERE ${conditions.sql}`, conditions.params);
    await client.query("COMMIT");
    return rowOutput(current, metadata);
  } catch (error) {
    await client.query("ROLLBACK").catch(() => {});
    if (error instanceof DataEditError) throw error;
    throw new DataEditError(`DELETE に失敗しました: ${error.message}`);
  } finally {
    await client.end().catch(() => {});
  }
}

function resolveCompareShape(sourceMeta, targetMeta, keyColumns = null) {
  const sourceMap = columnMap(sourceMeta), targetMap = columnMap(targetMeta);
  const shared = sourceMeta.columns.map((column) => column.name).filter((name) => name in targetMap);
  const keys = [...(keyColumns?.length ? keyColumns : sourceMeta.row_key)];
  if (!keys.length) throw new DataEditError("比較元に主キーまたは非NULL一意キーがありません");
  if (keys.some((name) => !(name in sourceMap) || !(name in targetMap))) {
    throw new DataEditError("比較キーが両方のテーブルに存在しません");
  }
  const validUnique = (metadata, cmap) => metadata.unique_keys
    .filter((key) => !key.deferrable && key.columns.every((name) => !cmap[name].nullable))
    .map((key) => new Set(key.columns));
  const sameSet = (left, right) => left.size === right.size && [...left].every((value) => right.has(value));
  const keySet = new Set(keys);
  if (!validUnique(sourceMeta, sourceMap).some((key) => sameSet(key, keySet))
      || !validUnique(targetMeta, targetMap).some((key) => sameSet(key, keySet))) {
    throw new DataEditError("比較キーは両方のテーブルで非NULLの PRIMARY KEY / UNIQUE である必要があります");
  }
  const warnings = [];
  for (const name of shared) {
    const source = sourceMap[name], target = targetMap[name];
    const compatible = source.type_family === target.type_family
      && (source.udt_name === target.udt_name || ["numeric", "text", "json"].includes(source.type_family));
    if (!compatible) {
      throw new DataEditError(`列 '${name}' の型に互換性がありません: ${source.data_type} -> ${target.data_type}`);
    }
    if (source.udt_name !== target.udt_name) {
      warnings.push(`${name}: ${source.data_type} -> ${target.data_type} (PostgreSQL の型変換を使用)`);
    }
  }
  const missing = targetMeta.columns.filter((column) => !(column.name in sourceMap)
    && !column.nullable && column.default === null && !column.identity && !column.generated)
    .map((column) => column.name);
  if (missing.length) throw new DataEditError(`比較先の必須列が比較元にありません: ${missing.join(", ")}`);
  const generated = shared.filter((name) => targetMap[name].generated);
  if (generated.length) warnings.push(`比較先の生成列は比較・上書き対象外です: ${generated.join(", ")}`);
  const comparable = shared.filter((name) => !targetMap[name].generated);
  return {
    key_columns: keys,
    compare_columns: comparable,
    writable_columns: comparable.filter((name) => !keys.includes(name)),
    insert_columns: [...comparable],
    warnings,
  };
}

async function fetchCompareRows(client, metadata, columns, keys, maxRows) {
  const selected = columns.map(quoteIdent).join(", ");
  const ordering = keys.map(quoteIdent).join(", ");
  const measured = (await client.query(
    `SELECT count(*) AS count, COALESCE(sum(pg_column_size(compare_row)), 0) AS bytes FROM ` +
    `(SELECT ${selected} FROM ${ident(metadata.schema, metadata.table)} ` +
    `ORDER BY ${ordering} LIMIT $1) AS compare_row`, [maxRows + 1]
  )).rows[0];
  if (Number(measured.count) > maxRows) {
    throw new DataEditError(`${metadata.schema}.${metadata.table} が比較上限 ${maxRows} 行を超えています`);
  }
  if (Number(measured.bytes) > MAX_COMPARE_BYTES) {
    throw new DataEditError(
      `${metadata.schema}.${metadata.table} の比較データが ${MAX_COMPARE_BYTES / 1024 / 1024} MiB を超えています`
    );
  }
  const result = await client.query(
    `SELECT ${selected} FROM ${ident(metadata.schema, metadata.table)} ` +
    `ORDER BY ${ordering} LIMIT $1`, [maxRows]
  );
  return result.rows;
}

function buildPlan(sourceRows, targetRows, sourceMeta, targetMeta, shape) {
  const keys = shape.key_columns;
  const index = (rows, metadata) => {
    const cmap = columnMap(metadata), result = new Map();
    for (const row of rows) {
      if (keys.some((name) => row[name] === null || row[name] === undefined)) {
        throw new DataEditError("比較キーに NULL が含まれています");
      }
      const token = hashPayload(Object.fromEntries(keys.map((name) => [name, canonicalValue(row[name], cmap[name])])));
      if (result.has(token)) throw new DataEditError("比較キーが重複しています");
      result.set(token, row);
    }
    return result;
  };
  const sourceIndex = index(sourceRows, sourceMeta), targetIndex = index(targetRows, targetMeta);
  const sourceMap = columnMap(sourceMeta), targetMap = columnMap(targetMeta);
  const onlySource = [], onlyTarget = [], changed = [];
  let same = 0;
  const tokens = [...new Set([...sourceIndex.keys(), ...targetIndex.keys()])].sort();
  for (const token of tokens) {
    const source = sourceIndex.get(token), target = targetIndex.get(token);
    if (!target) { onlySource.push(source); continue; }
    if (!source) { onlyTarget.push(target); continue; }
    const columns = shape.compare_columns.filter((name) =>
      JSON.stringify(stable(canonicalValue(source[name], sourceMap[name])))
      !== JSON.stringify(stable(canonicalValue(target[name], targetMap[name]))));
    if (columns.length) changed.push({ source, target, columns }); else same++;
  }
  const payload = {
    version: 1,
    source: { profile: sourceMeta.profile, host: sourceMeta.host, port: sourceMeta.port,
      database: sourceMeta.database, schema: sourceMeta.schema, table: sourceMeta.table },
    target: { profile: targetMeta.profile, host: targetMeta.host, port: targetMeta.port,
      database: targetMeta.database, schema: targetMeta.schema, table: targetMeta.table },
    shape,
    source_rows: sourceRows.map((row) => canonicalRow(row, sourceMeta, shape.compare_columns)),
    target_rows: targetRows.map((row) => canonicalRow(row, targetMeta, shape.compare_columns)),
  };
  return { only_source: onlySource, only_target: onlyTarget, changed, same, plan_hash: hashPayload(payload) };
}

function publicPlan(plan, shape, sourceMeta, targetMeta, previewRows) {
  const sourceMap = columnMap(sourceMeta), targetMap = columnMap(targetMeta);
  const keyJson = (row, cmap) => Object.fromEntries(
    shape.key_columns.map((name) => [name, displayValue(row[name], cmap[name])])
  );
  return {
    source: { profile: sourceMeta.profile, host: sourceMeta.host, port: sourceMeta.port,
      database: sourceMeta.database, schema: sourceMeta.schema, table: sourceMeta.table },
    target: { profile: targetMeta.profile, host: targetMeta.host, port: targetMeta.port,
      database: targetMeta.database, schema: targetMeta.schema, table: targetMeta.table },
    key_columns: shape.key_columns,
    compare_columns: shape.compare_columns,
    warnings: shape.warnings,
    summary: {
      source_rows: plan.only_source.length + plan.changed.length + plan.same,
      target_rows: plan.only_target.length + plan.changed.length + plan.same,
      only_source: plan.only_source.length, only_target: plan.only_target.length,
      changed: plan.changed.length, same: plan.same,
    },
    preview: {
      only_source: plan.only_source.slice(0, previewRows).map((row) => (
        { key: keyJson(row, sourceMap), row: displayRow(row, sourceMeta) }
      )),
      only_target: plan.only_target.slice(0, previewRows).map((row) => (
        { key: keyJson(row, targetMap), row: displayRow(row, targetMeta) }
      )),
      changed: plan.changed.slice(0, previewRows).map((item) => ({
        key: keyJson(item.source, sourceMap), columns: item.columns,
        source: displayRow(item.source, sourceMeta), target: displayRow(item.target, targetMeta),
      })),
    },
    plan_hash: plan.plan_hash,
    confirmation: `${targetMeta.profile}@${targetMeta.host}:${targetMeta.port}/` +
      `${targetMeta.database}/${targetMeta.schema}.${targetMeta.table}`,
  };
}

export async function compareTables(source, sourceDb, sourceSchema, sourceTable,
  target, targetDb, targetSchema, targetTable, {
    keyColumns = null, maxRows = DEFAULT_COMPARE_ROWS, previewRows = DEFAULT_PREVIEW_ROWS,
  } = {}) {
  maxRows = Number(maxRows); previewRows = Number(previewRows);
  if (!Number.isInteger(maxRows) || maxRows < 1 || maxRows > MAX_COMPARE_ROWS) {
    throw new DataEditError(`maxRows は 1〜${MAX_COMPARE_ROWS} の範囲で指定してください`);
  }
  if (!Number.isInteger(previewRows) || previewRows < 1 || previewRows > MAX_PREVIEW_ROWS) {
    throw new DataEditError(`previewRows は 1〜${MAX_PREVIEW_ROWS} の範囲で指定してください`);
  }
  const sourceMeta = await tableMetadata(source, sourceDb, sourceSchema, sourceTable);
  const targetMeta = await tableMetadata(target, targetDb, targetSchema, targetTable);
  if (!sourceMeta.can_select || !targetMeta.can_select) throw new DataEditError("比較には両テーブルの SELECT 権限が必要です");
  if (sourceMeta.row_security_effective || targetMeta.row_security_effective) {
    throw new DataEditError("実効的な Row Level Security があるテーブルは全行を確認できないため比較・同期できません");
  }
  const shape = resolveCompareShape(sourceMeta, targetMeta, keyColumns);
  const sourceRows = await withConn(source, sourceDb, (client) =>
    fetchCompareRows(client, sourceMeta, shape.compare_columns, shape.key_columns, maxRows));
  const targetRows = await withConn(target, targetDb, (client) =>
    fetchCompareRows(client, targetMeta, shape.compare_columns, shape.key_columns, maxRows));
  return publicPlan(buildPlan(sourceRows, targetRows, sourceMeta, targetMeta, shape),
    shape, sourceMeta, targetMeta, previewRows);
}

async function insertFromSource(client, metadata, columns, row) {
  const cmap = columnMap(metadata);
  const overriding = columns.some((name) => cmap[name].identity === "a") ? " OVERRIDING SYSTEM VALUE" : "";
  const placeholders = columns.map((name, index) => castPlaceholder(cmap[name], index + 1));
  await client.query(
    `INSERT INTO ${ident(metadata.schema, metadata.table)} (${columns.map(quoteIdent).join(", ")})` +
    `${overriding} VALUES (${placeholders.join(", ")})`,
    columns.map((name) => prepareValue(row[name], cmap[name]))
  );
}

async function updateFromSource(client, metadata, keys, columns, row) {
  if (!columns.length) return;
  const cmap = columnMap(metadata);
  const assignments = columns.map((name, index) =>
    `${quoteIdent(name)} = ${castPlaceholder(cmap[name], index + 1)}`).join(", ");
  const conditions = keys.map((name, index) =>
    `${quoteIdent(name)} IS NOT DISTINCT FROM ${castPlaceholder(cmap[name], columns.length + index + 1)}`).join(" AND ");
  const result = await client.query(
    `UPDATE ${ident(metadata.schema, metadata.table)} SET ${assignments} WHERE ${conditions}`,
    [...columns.map((name) => prepareValue(row[name], cmap[name])),
      ...keys.map((name) => prepareValue(row[name], cmap[name]))]
  );
  if (result.rowCount !== 1) throw new DataEditError("同期中に対象行が変化しました。ロールバックします");
}

async function deleteTargetRow(client, metadata, keys, row) {
  const cmap = columnMap(metadata);
  const conditions = keys.map((name, index) =>
    `${quoteIdent(name)} IS NOT DISTINCT FROM ${castPlaceholder(cmap[name], index + 1)}`).join(" AND ");
  const result = await client.query(
    `DELETE FROM ${ident(metadata.schema, metadata.table)} WHERE ${conditions}`,
    keys.map((name) => prepareValue(row[name], cmap[name]))
  );
  if (result.rowCount !== 1) throw new DataEditError("同期中に削除対象行が変化しました。ロールバックします");
}

export async function applyTablePlan(source, sourceDb, sourceSchema, sourceTable,
  target, targetDb, targetSchema, targetTable, {
    strategy, expectedPlanHash, confirmation, keyColumns = null, maxRows = DEFAULT_COMPARE_ROWS,
  }) {
  if (!["merge", "sync"].includes(strategy)) throw new DataEditError("strategy は merge または sync を指定してください");
  if (!/^[0-9a-f]{64}$/.test(expectedPlanHash || "")) throw new DataEditError("比較結果の plan_hash が必要です");
  const expectedConfirmation = `${target.name}@${target.host}:${target.port}/` +
    `${targetDb}/${targetSchema}.${targetTable}`;
  if (confirmation !== expectedConfirmation) {
    throw new DataEditError(`確認文字列が一致しません。'${expectedConfirmation}' を指定してください`);
  }
  maxRows = Number(maxRows);
  if (!Number.isInteger(maxRows) || maxRows < 1 || maxRows > MAX_COMPARE_ROWS) {
    throw new DataEditError(`maxRows は 1〜${MAX_COMPARE_ROWS} の範囲で指定してください`);
  }
  if (sourceDb === targetDb && sourceSchema === targetSchema && sourceTable === targetTable
      && await sameCluster(source, target)) throw new DataEditError("比較元と上書き先が同一テーブルです");

  const sourceMeta = await tableMetadata(source, sourceDb, sourceSchema, sourceTable);
  const targetMeta = await tableMetadata(target, targetDb, targetSchema, targetTable);
  if (sourceMeta.row_security_effective || targetMeta.row_security_effective) {
    throw new DataEditError("実効的な Row Level Security があるテーブルは全行を確認できないためmerge/syncできません");
  }
  if (!targetMeta.can_insert || !targetMeta.can_update) {
    throw new DataEditError("merge には対象テーブルの INSERT / UPDATE 権限と行キーが必要です");
  }
  if (strategy === "sync" && !targetMeta.can_delete) throw new DataEditError("sync には DELETE 権限も必要です");
  const shape = resolveCompareShape(sourceMeta, targetMeta, keyColumns);
  const sourceClient = await connect(source, sourceDb);
  const targetClient = await connect(target, targetDb);
  let plan;
  try {
    await sourceClient.query("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY");
    await targetClient.query("BEGIN ISOLATION LEVEL SERIALIZABLE");
    await targetClient.query("SET LOCAL lock_timeout = '5000ms'");
    await targetClient.query("SET LOCAL statement_timeout = '300000ms'");
    await targetClient.query(`LOCK TABLE ${ident(targetSchema, targetTable)} IN SHARE ROW EXCLUSIVE MODE`);
    const sourceRows = await fetchCompareRows(sourceClient, sourceMeta, shape.compare_columns, shape.key_columns, maxRows);
    const targetRows = await fetchCompareRows(targetClient, targetMeta, shape.compare_columns, shape.key_columns, maxRows);
    plan = buildPlan(sourceRows, targetRows, sourceMeta, targetMeta, shape);
    if (!secureEqual(plan.plan_hash, expectedPlanHash)) {
      throw new DataEditError("比較後に元または対象データが変更されました。再比較してください");
    }
    for (const row of plan.only_source) await insertFromSource(targetClient, targetMeta, shape.insert_columns, row);
    if (plan.only_source.length) await advanceSequences(targetClient, targetMeta, shape.insert_columns);
    for (const item of plan.changed) {
      const columns = item.columns.filter((name) => shape.writable_columns.includes(name));
      await updateFromSource(targetClient, targetMeta, shape.key_columns, columns, item.source);
    }
    if (strategy === "sync") {
      for (const row of plan.only_target) await deleteTargetRow(targetClient, targetMeta, shape.key_columns, row);
    }
    await sourceClient.query("COMMIT");
    await targetClient.query("COMMIT");
  } catch (error) {
    await targetClient.query("ROLLBACK").catch(() => {});
    await sourceClient.query("ROLLBACK").catch(() => {});
    if (error instanceof DataEditError) throw error;
    throw new DataEditError(`${strategy} の適用に失敗し、ロールバックしました: ${error.message}`);
  } finally {
    await targetClient.end().catch(() => {});
    await sourceClient.end().catch(() => {});
  }
  return {
    strategy, inserted: plan.only_source.length, updated: plan.changed.length,
    deleted: strategy === "sync" ? plan.only_target.length : 0,
    unchanged: plan.same, plan_hash: plan.plan_hash, target: expectedConfirmation,
  };
}
