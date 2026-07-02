// アーカイブモード: 格納先サーバー上の BACK_UP_DB (既定) にダンプを保存する。
//
// pgbackup_archive       : バックアップのメタデータ (1 行 = 1 バックアップ)
// pgbackup_archive_chunk : ダンプ本体 (bytea を 16MB チャンクに分割して格納)
//
// チャンク分割により bytea の 1GB 制限と巨大メモリ確保を回避する。
// テーブル定義は Python 版と互換。

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { createDatabase, databaseExists, withConn } from "./db.js";
import { BackupError, RestoreError } from "./errors.js";

export const CHUNK_SIZE = 16 * 1024 * 1024; // 16MB

const ARCHIVE_DDL = `
CREATE TABLE IF NOT EXISTS pgbackup_archive (
    id bigserial PRIMARY KEY,
    version_id text UNIQUE NOT NULL,
    source_profile text,
    source_host text,
    source_port integer,
    source_db text NOT NULL,
    version_no integer NOT NULL,
    fmt text NOT NULL,
    size bigint,
    sha256 text,
    pg_dump_version text,
    note text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pgbackup_archive_chunk (
    backup_id bigint NOT NULL REFERENCES pgbackup_archive(id) ON DELETE CASCADE,
    seq integer NOT NULL,
    data bytea NOT NULL,
    PRIMARY KEY (backup_id, seq)
);
`;

// アーカイブ用 DB とテーブルがなければ作成する
export async function ensureArchiveDb(profile, archiveDb) {
  if (!(await databaseExists(profile, archiveDb))) {
    await createDatabase(profile, archiveDb, "UTF8");
  }
  await withConn(profile, archiveDb, (c) => c.query(ARCHIVE_DDL));
}

// アーカイブ内での次バージョン番号 (他クライアントとの重複を避けるため参照する)
export async function nextArchiveVersionNo(profile, archiveDb, sourceDb) {
  return withConn(profile, archiveDb, async (c) => {
    const r = await c.query(
      "SELECT COALESCE(MAX(version_no), 0) + 1 AS vn FROM pgbackup_archive WHERE source_db = $1",
      [sourceDb]
    );
    return Number(r.rows[0].vn);
  });
}

// ダンプファイルをチャンク分割してアーカイブ DB に格納し、行 id を返す
export async function storeDump(profile, archiveDb, meta, dumpPath) {
  const size = fs.statSync(dumpPath).size;
  return withConn(profile, archiveDb, async (c) => {
    await c.query("BEGIN");
    try {
      const r = await c.query(
        `INSERT INTO pgbackup_archive
           (version_id, source_profile, source_host, source_port, source_db,
            version_no, fmt, size, sha256, pg_dump_version, note)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
         RETURNING id`,
        [
          meta.versionId, meta.sourceProfile, meta.sourceHost, meta.sourcePort,
          meta.sourceDb, meta.versionNo, meta.fmt, size, meta.sha256,
          meta.pgDumpVersion ?? null, meta.note ?? null,
        ]
      );
      const backupId = Number(r.rows[0].id);
      const fh = fs.openSync(dumpPath, "r");
      try {
        const buf = Buffer.alloc(CHUNK_SIZE);
        for (let seq = 0; ; seq++) {
          const n = fs.readSync(fh, buf, 0, CHUNK_SIZE, null);
          if (n <= 0) break;
          await c.query(
            "INSERT INTO pgbackup_archive_chunk (backup_id, seq, data) VALUES ($1,$2,$3)",
            [backupId, seq, buf.subarray(0, n)]
          );
        }
      } finally {
        fs.closeSync(fh);
      }
      await c.query("COMMIT");
      return backupId;
    } catch (e) {
      await c.query("ROLLBACK").catch(() => {});
      throw e;
    }
  });
}

// アーカイブ DB 内のバックアップ一覧 (ダンプ本体は含まない)
export async function listArchive(profile, archiveDb) {
  if (!(await databaseExists(profile, archiveDb))) return [];
  return withConn(profile, archiveDb, async (c) => {
    const exists = await c.query("SELECT to_regclass('public.pgbackup_archive') AS t");
    if (exists.rows[0].t === null) return [];
    const r = await c.query(
      `SELECT id, version_id, source_profile, source_host, source_port, source_db,
              version_no, fmt, size, sha256, pg_dump_version, note, created_at
       FROM pgbackup_archive ORDER BY source_db, version_no`
    );
    return r.rows;
  });
}

async function findRow(client, versionId, rowId) {
  let r;
  if (rowId !== null && rowId !== undefined) {
    r = await client.query("SELECT * FROM pgbackup_archive WHERE id = $1", [rowId]);
  } else if (versionId !== null && versionId !== undefined) {
    r = await client.query("SELECT * FROM pgbackup_archive WHERE version_id = $1", [versionId]);
  } else {
    throw new RestoreError("versionId か rowId のいずれかを指定してください");
  }
  if (!r.rowCount) {
    const key = versionId ?? `id=${rowId}`;
    throw new RestoreError(`アーカイブに '${key}' が見つかりません`);
  }
  return r.rows[0];
}

// アーカイブからダンプを取り出してファイルに書き出し、メタデータを返す。
// 取り出し後に sha256 を照合し、破損があれば例外を投げる。
// チャンクは 1 行ずつ取得するのでメモリ使用は CHUNK_SIZE に収まる。
export async function fetchDump(profile, archiveDb, outPath, { versionId = null, rowId = null }) {
  if (!(await databaseExists(profile, archiveDb))) {
    throw new RestoreError(`アーカイブ DB '${archiveDb}' が ${profile.host}:${profile.port} に存在しません`);
  }
  const meta = await withConn(profile, archiveDb, async (c) => {
    const row = await findRow(c, versionId, rowId);
    const hash = crypto.createHash("sha256");
    fs.mkdirSync(path.dirname(String(outPath)), { recursive: true });
    const fh = fs.openSync(outPath, "w");
    try {
      for (let seq = 0; ; seq++) {
        const r = await c.query(
          "SELECT data FROM pgbackup_archive_chunk WHERE backup_id = $1 AND seq = $2",
          [row.id, seq]
        );
        if (!r.rowCount) break;
        const data = r.rows[0].data;
        fs.writeSync(fh, data);
        hash.update(data);
      }
    } finally {
      fs.closeSync(fh);
    }
    row._fetchedSha = hash.digest("hex");
    return row;
  });
  if (meta.sha256 && meta._fetchedSha !== meta.sha256) {
    throw new RestoreError(
      `アーカイブ '${meta.version_id}' の整合性チェックに失敗しました ` +
      "(sha256 不一致)。データが破損している可能性があります。"
    );
  }
  return meta;
}

export async function deleteArchiveRow(profile, archiveDb, versionId) {
  await withConn(profile, archiveDb, async (c) => {
    const r = await c.query("DELETE FROM pgbackup_archive WHERE version_id = $1", [versionId]);
    if (!r.rowCount) throw new BackupError(`アーカイブに '${versionId}' が見つかりません`);
  });
}
