// pg (node-postgres) による接続ヘルパー。

import crypto from "node:crypto";

import pg from "pg";

import { profileDisplay } from "./config.js";
import { PgBackupError } from "./errors.js";

const { Client } = pg;

export const CONNECT_TIMEOUT_MS = 10_000;

// SQL 識別子 (DB 名等) のクオート
export function quoteIdent(name) {
  return '"' + String(name).replaceAll('"', '""') + '"';
}

// SQL 文字列リテラルのクオート
export function quoteLiteral(value) {
  return "'" + String(value).replaceAll("'", "''") + "'";
}

function sslOption(sslmode) {
  if (!sslmode || sslmode === "disable") return undefined;
  if (sslmode === "require" || sslmode === "prefer" || sslmode === "allow") {
    return { rejectUnauthorized: false };
  }
  return true; // verify-ca / verify-full: 証明書検証あり
}

export async function connect(profile, dbname = null) {
  const client = new Client({
    host: profile.host,
    port: profile.port,
    user: profile.user,
    password: profile.password ?? undefined, // 未設定なら PGPASSWORD 環境変数等に委ねる
    database: dbname || profile.dbname,
    connectionTimeoutMillis: CONNECT_TIMEOUT_MS,
    ssl: sslOption(profile.sslmode),
  });
  try {
    await client.connect();
  } catch (e) {
    throw new PgBackupError(
      `接続失敗 [${profile.name}] ${profileDisplay(profile)} db=${dbname || profile.dbname}: ${e.message}`
    );
  }
  return client;
}

// 接続を開いて fn を実行し、必ずクローズする
export async function withConn(profile, dbname, fn) {
  const client = await connect(profile, dbname);
  try {
    return await fn(client);
  } finally {
    await client.end().catch(() => {});
  }
}

export async function serverVersion(profile) {
  return withConn(profile, null, async (c) => (await c.query("SHOW server_version")).rows[0].server_version);
}

export async function databaseExists(profile, dbname) {
  return withConn(profile, null, async (c) => {
    const r = await c.query("SELECT 1 FROM pg_database WHERE datname = $1", [dbname]);
    return r.rowCount > 0;
  });
}

export async function databaseEncoding(profile, dbname) {
  return withConn(profile, null, async (c) => {
    const r = await c.query(
      "SELECT pg_encoding_to_char(encoding) AS enc FROM pg_database WHERE datname = $1",
      [dbname]
    );
    return r.rowCount ? r.rows[0].enc : null;
  });
}

// CREATE DATABASE。encoding 指定が locale と非互換なら指定なしで再試行する。
export async function createDatabase(profile, dbname, encoding = null) {
  await withConn(profile, null, async (c) => {
    if (encoding) {
      try {
        await c.query(
          `CREATE DATABASE ${quoteIdent(dbname)} ENCODING ${quoteLiteral(encoding)} TEMPLATE template0`
        );
        return;
      } catch {
        // フォールバックへ
      }
    }
    await c.query(`CREATE DATABASE ${quoteIdent(dbname)} TEMPLATE template0`);
  });
}

async function terminateBackends(client, dbname) {
  await client.query(
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
    [dbname]
  );
}

export async function dropDatabase(profile, dbname) {
  await withConn(profile, null, async (c) => {
    await terminateBackends(c, dbname);
    await c.query(`DROP DATABASE IF EXISTS ${quoteIdent(dbname)}`);
  });
}

export async function renameDatabase(profile, oldName, newName) {
  await withConn(profile, null, async (c) => {
    await terminateBackends(c, oldName);
    await c.query(`ALTER DATABASE ${quoteIdent(oldName)} RENAME TO ${quoteIdent(newName)}`);
  });
}

// 退避用の一時 DB 名 (識別子の 63 バイト制限内に収める)
function trashName(dbname) {
  const suffix = `_pgb_old_${crypto.randomBytes(4).toString("hex")}`;
  let base = dbname;
  while (Buffer.byteLength(base + suffix, "utf-8") > 63) base = base.slice(0, -1);
  return base + suffix;
}

// クラスタの一意識別子。ホスト名の表記ゆれ (localhost / 127.0.0.1 等) に
// 影響されない同一サーバー判定に使う。取得できない場合は host:port にフォールバック。
export async function clusterIdentity(profile) {
  try {
    return await withConn(profile, null, async (c) => {
      const r = await c.query("SELECT system_identifier FROM pg_control_system()");
      return `pg:${r.rows[0].system_identifier}:${profile.port}`;
    });
  } catch {
    return `addr:${profile.host.toLowerCase()}:${profile.port}`;
  }
}

export async function sameCluster(a, b) {
  if (a.host === b.host && a.port === b.port) return true;
  return (await clusterIdentity(a)) === (await clusterIdentity(b));
}

// リストア/複製先の DB を安全に用意して fn を実行する。
// 既存 DB は削除せず一時名に退避してから新 DB を作る。
// fn が失敗したら新 DB を消して退避 DB を元に戻し、成功したら退避 DB を削除する。
// 途中失敗で「空の DB だけが残る」事故を防ぐ。
export async function withReplaceDatabase(profile, dbname, { drop = false, encoding = null }, fn) {
  let trash = null;
  if (await databaseExists(profile, dbname)) {
    if (!drop) {
      throw new PgBackupError(
        `データベース '${dbname}' は ${profileDisplay(profile)} に既に存在します。` +
        "上書きするには --drop を指定してください。"
      );
    }
    trash = trashName(dbname);
    await renameDatabase(profile, dbname, trash);
  }
  try {
    await createDatabase(profile, dbname, encoding);
    const result = await fn();
    if (trash !== null) await dropDatabase(profile, trash).catch(() => {});
    return result;
  } catch (e) {
    // 失敗: 作りかけの DB を消し、退避していた元 DB を戻す
    await dropDatabase(profile, dbname).catch(() => {});
    if (trash !== null) await renameDatabase(profile, trash, dbname).catch(() => {});
    throw e;
  }
}
