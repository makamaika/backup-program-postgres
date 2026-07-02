// データベースの一覧・構造・データ種類の検査。

import { quoteIdent, withConn } from "./db.js";

// ユーザー領域外のシステムスキーマを除外する条件
const USER_SCHEMA_COND = "n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'";

const RELKIND_LABEL = {
  r: "table",
  p: "partitioned table",
  v: "view",
  m: "materialized view",
  f: "foreign table",
};

export async function serverInfo(profile) {
  return withConn(profile, null, async (c) => ({
    version: (await c.query("SELECT version() AS v")).rows[0].v,
    server_version: (await c.query("SHOW server_version")).rows[0].server_version,
  }));
}

export async function listDatabases(profile, includeTemplates = false) {
  const q = `
    SELECT d.datname AS name,
           pg_get_userbyid(d.datdba) AS owner,
           pg_encoding_to_char(d.encoding) AS encoding,
           d.datistemplate AS is_template,
           d.datallowconn AS allow_conn,
           CASE WHEN d.datallowconn THEN pg_database_size(d.datname) END AS size_bytes,
           CASE WHEN d.datallowconn
                THEN pg_size_pretty(pg_database_size(d.datname)) END AS size
    FROM pg_database d
    ${includeTemplates ? "" : "WHERE NOT d.datistemplate"}
    ORDER BY d.datname
  `;
  return withConn(profile, null, async (c) =>
    (await c.query(q)).rows.map((r) => ({ ...r, size_bytes: r.size_bytes == null ? null : Number(r.size_bytes) }))
  );
}

export async function listSchemas(profile, dbname) {
  return withConn(profile, dbname, async (c) =>
    (await c.query(`SELECT n.nspname FROM pg_namespace n WHERE ${USER_SCHEMA_COND} ORDER BY 1`))
      .rows.map((r) => r.nspname)
  );
}

export async function listTables(profile, dbname, schema = null) {
  const q = `
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
      AND ${USER_SCHEMA_COND}
      ${schema ? "AND n.nspname = $1" : ""}
    ORDER BY n.nspname, c.relname
  `;
  return withConn(profile, dbname, async (c) => {
    const r = await c.query(q, schema ? [schema] : []);
    return r.rows.map((row) => ({
      ...row,
      kind: RELKIND_LABEL[row.kind] ?? row.kind,
      est_rows: Number(row.est_rows),
      size_bytes: Number(row.size_bytes),
    }));
  });
}

export async function tableColumns(profile, dbname, schema, table) {
  const q = `
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
    WHERE n.nspname = $1 AND c.relname = $2 AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
  `;
  return withConn(profile, dbname, async (c) => (await c.query(q, [schema, table])).rows);
}

export async function tableIndexes(profile, dbname, schema, table) {
  const q = `
    SELECT indexname AS name, indexdef AS definition
    FROM pg_indexes
    WHERE schemaname = $1 AND tablename = $2
    ORDER BY indexname
  `;
  return withConn(profile, dbname, async (c) => (await c.query(q, [schema, table])).rows);
}

function jsonable(v) {
  if (v === null || typeof v === "boolean" || typeof v === "number" || typeof v === "string") return v;
  if (typeof v === "bigint") return v.toString();
  if (Buffer.isBuffer(v)) {
    return "\\x" + v.subarray(0, 64).toString("hex") + (v.length > 64 ? "…" : "");
  }
  if (v instanceof Date) return v.toISOString();
  if (Array.isArray(v)) return JSON.stringify(v.map(jsonable));
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

// テーブルの先頭数行と行数を返す (データの中身確認用)。
// 行数の正確なカウントは巨大テーブルではフルスキャンになるため 5 秒で打ち切り、
// タイムアウト時は統計情報による推定値 (approx=true) を返す。
export async function tableSample(profile, dbname, schema, table, limit = 10) {
  const n = Math.max(1, Math.min(Number(limit) || 10, 200));
  const ident = `${quoteIdent(schema)}.${quoteIdent(table)}`;
  return withConn(profile, dbname, async (c) => {
    const r = await c.query({ text: `SELECT * FROM ${ident} LIMIT ${n}`, rowMode: "array" });
    const columns = r.fields.map((f) => f.name);
    const rows = r.rows.map((row) => row.map(jsonable));
    let total, approx = false;
    try {
      await c.query("BEGIN");
      await c.query("SET LOCAL statement_timeout = '5000ms'");
      total = Number((await c.query(`SELECT count(*) AS n FROM ${ident}`)).rows[0].n);
      await c.query("COMMIT");
    } catch (e) {
      await c.query("ROLLBACK").catch(() => {});
      if (e.code !== "57014") throw e; // query_canceled 以外は本当のエラー
      approx = true;
      total = Number((await c.query(
        `SELECT GREATEST(cl.reltuples, 0)::bigint AS n
         FROM pg_class cl JOIN pg_namespace ns ON ns.oid = cl.relnamespace
         WHERE ns.nspname = $1 AND cl.relname = $2`,
        [schema, table]
      )).rows[0].n);
    }
    return { columns, rows, total_rows: total, approx };
  });
}

// この DB にどんな種類のデータ (型) がどれだけ保存されているかの集計
export async function datatypeSummary(profile, dbname) {
  const q = `
    SELECT format_type(a.atttypid, a.atttypmod) AS data_type,
           count(*) AS column_count,
           count(DISTINCT c.oid) AS table_count
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
      AND ${USER_SCHEMA_COND}
    GROUP BY 1
    ORDER BY column_count DESC, data_type
  `;
  return withConn(profile, dbname, async (c) =>
    (await c.query(q)).rows.map((r) => ({
      ...r,
      column_count: Number(r.column_count),
      table_count: Number(r.table_count),
    }))
  );
}

// Web UI 用: スキーマ→テーブル→カラムまでまとめて返す
export async function databaseStructure(profile, dbname) {
  const tables = await listTables(profile, dbname);
  const bySchema = new Map();
  for (const t of tables) {
    if (!bySchema.has(t.schema)) bySchema.set(t.schema, []);
    bySchema.get(t.schema).push(t);
  }
  return {
    database: dbname,
    schemas: [...bySchema.keys()].sort().map((name) => ({ name, tables: bySchema.get(name) })),
    datatype_summary: await datatypeSummary(profile, dbname),
  };
}
