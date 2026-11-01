// pg (node-postgres) による接続ヘルパー。

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import pg from "pg";

import { profileDisplay } from "./config.js";
import { PgBackupError } from "./errors.js";
import { withReplacementLock } from "./restore-lock.js";

const { Client } = pg;

const RAW_TEMPORAL_OIDS = new Set([1082, 1083, 1114, 1184, 1186, 1266]);
const typeOverrides = {
  getTypeParser(oid, format) {
    if (format === "text" && RAW_TEMPORAL_OIDS.has(Number(oid))) return (value) => value;
    return pg.types.getTypeParser(oid, format);
  },
};

export const CONNECT_TIMEOUT_MS = 10_000;

// SQL 識別子 (DB 名等) のクオート
export function quoteIdent(name) {
  return '"' + String(name).replaceAll('"', '""') + '"';
}

// SQL 文字列リテラルのクオート
export function quoteLiteral(value) {
  return "'" + String(value).replaceAll("'", "''") + "'";
}

// libpq の sslmode 互換の接続試行リスト (pg ドライバは fallback を持たないため自前で行う)。
// 既定 (未指定) は libpq と同じ 'prefer' 相当: SSL を試し、非対応サーバーには平文で再接続。
function sslAttempts(sslmode) {
  if (sslmode === "disable") return [false];
  if (sslmode === "require") return [{ rejectUnauthorized: false }];
  if (sslmode === "verify-ca" || sslmode === "verify-full") {
    const rootCrt = path.join(os.homedir(), ".postgresql", "root.crt");
    return [fs.existsSync(rootCrt) ? { ca: fs.readFileSync(rootCrt, "utf-8") } : true];
  }
  // prefer / allow / 未指定
  return [{ rejectUnauthorized: false }, false];
}

function isSslUnsupported(err) {
  return /does not support SSL/i.test(err?.message ?? "");
}

export async function connect(profile, dbname = null) {
  const attempts = sslAttempts(profile.sslmode);
  let lastErr = null;
  for (let i = 0; i < attempts.length; i++) {
    const client = new Client({
      host: profile.host,
      port: profile.port,
      user: profile.user,
      password: profile.password ?? undefined, // 未設定なら PGPASSWORD 環境変数等に委ねる
      database: dbname || profile.dbname,
      connectionTimeoutMillis: CONNECT_TIMEOUT_MS,
      types: typeOverrides,
      ssl: attempts[i] === false ? undefined : attempts[i],
    });
    try {
      await client.connect();
      await client.query("SET TIME ZONE 'UTC'");
      return client;
    } catch (e) {
      lastErr = e;
      await client.end().catch(() => {});
      // SSL 非対応サーバーだった場合のみ次の試行 (平文) へフォールバック
      if (!(i + 1 < attempts.length && isSslUnsupported(e))) break;
    }
  }
  throw new PgBackupError(
    `接続失敗 [${profile.name}] ${profileDisplay(profile)} db=${dbname || profile.dbname}: ${lastErr.message}`
  );
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
// 影響されない同一サーバー判定に使う。
// 接続自体の失敗は握りつぶさず伝播させる (安全ガードを fail-open にしない)。
// クエリが権限等で失敗した場合のみ host:port にフォールバックする。
export async function clusterIdentity(profile) {
  return withConn(profile, null, async (c) => {
    try {
      const r = await c.query("SELECT system_identifier FROM pg_control_system()");
      return `pg:${r.rows[0].system_identifier}:${profile.port}`;
    } catch {
      return `addr:${profile.host.toLowerCase()}:${profile.port}`;
    }
  });
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
  // 同じデータ領域の job を DB 操作から後片付けまで直列化する。
  // DB 側に新しい CONNECT 権限を要求せず、置換対象も制限しない。
  return withReplacementLock(async () => {
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
    // Ctrl+C の既定動作 (即プロセス終了) では退避 DB が置き去りになる。
    // クリティカルセクション中はシグナルでの即死を防ぎ、子プロセスの死による
    // 失敗経路 (下の catch) にロールバックさせてから終了コード 130 で抜ける。
    let interrupted = false;
    let created = false;
    const onSignal = () => { interrupted = true; };
    process.on("SIGINT", onSignal);
    process.on("SIGTERM", onSignal);
    try {
      await createDatabase(profile, dbname, encoding);
      created = true;
      const result = await fn();
      if (trash !== null) await dropDatabase(profile, trash).catch(() => {});
      return result;
    } catch (e) {
      // CREATE が成功したと確認できた DB だけ削除する。
      if (created) await dropDatabase(profile, dbname).catch(() => {});
      if (trash !== null) await renameDatabase(profile, trash, dbname).catch(() => {});
      throw e;
    } finally {
      process.removeListener("SIGINT", onSignal);
      process.removeListener("SIGTERM", onSignal);
      if (interrupted) process.exitCode = 130;
    }
  });
}
