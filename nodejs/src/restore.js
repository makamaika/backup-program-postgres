// リストアエンジン。
//
// - カタログのバージョン ID を指定して復元 (file / archive どちらのバックアップも可)
// - 任意のダンプファイル (SQL / カスタム形式) を直接復元
// - 復元先はローカル / リモートの任意のサーバー・任意の DB 名を指定可能

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import * as archiveMod from "./archive.js";
import * as pgtools from "./pgtools.js";
import { sha256OfFile } from "./backup.js";
import { tmpDir } from "./config.js";
import { withReplaceDatabase } from "./db.js";
import { RestoreError } from "./errors.js";

// ダンプファイルの形式を判定する。カスタム形式は先頭が 'PGDMP'
function detectFormat(filePath) {
  const fh = fs.openSync(filePath, "r");
  try {
    const buf = Buffer.alloc(5);
    fs.readSync(fh, buf, 0, 5, 0);
    return buf.toString("latin1") === "PGDMP" ? "custom" : "plain";
  } finally {
    fs.closeSync(fh);
  }
}

async function restoreDump(dest, destDb, dumpPath, fmt, keepOwner = false) {
  if (fmt === "custom") {
    await pgtools.restoreCustom(dest, destDb, dumpPath, keepOwner);
  } else {
    await pgtools.restorePlain(dest, destDb, dumpPath);
  }
}

// カタログに登録されたバージョンを指定環境へ復元する
export async function restoreVersion(catalog, profiles, versionId, dest, {
  destDb = null, drop = false, keepOwner = false,
} = {}) {
  const rec = catalog.getBackup(versionId);
  destDb = destDb || rec.db;
  const detail = { version_id: versionId, dest: `${dest.name}:${destDb}`, drop };

  let fetchedTmp = null;
  try {
    let dumpPath;
    if (rec.mode === "file") {
      dumpPath = rec.location;
      if (!fs.existsSync(dumpPath)) {
        throw new RestoreError(
          `バックアップファイルが見つかりません: ${dumpPath}\n` +
          "移動・削除された場合は 'pgbackup versions' の他のバージョンを利用してください。"
        );
      }
      if (rec.sha256 && (await sha256OfFile(dumpPath)) !== rec.sha256) {
        throw new RestoreError(`バックアップファイルの整合性チェックに失敗しました (sha256 不一致): ${dumpPath}`);
      }
    } else if (rec.mode === "archive") {
      const srcProfile = profiles.get(rec.archive_profile);
      fetchedTmp = path.join(tmpDir(), `restore_${crypto.randomBytes(8).toString("hex")}.dump`);
      // 行 id で特定する (行が削除→同じ version_id で再作成されていても別物を掴まない)
      const meta = await archiveMod.fetchDump(srcProfile, rec.archive_db, fetchedTmp, {
        versionId, rowId: rec.archive_row_id ?? null,
      });
      if (rec.sha256 && meta.sha256 !== rec.sha256) {
        throw new RestoreError(
          `アーカイブ '${versionId}' の内容がカタログの記録と一致しません ` +
          "(sha256 不一致)。アーカイブが差し替えられた可能性があります。"
        );
      }
      dumpPath = fetchedTmp;
    } else {
      throw new RestoreError(`復元できないモードです: ${rec.mode}`);
    }

    await withReplaceDatabase(dest, destDb, { drop }, () =>
      restoreDump(dest, destDb, dumpPath, rec.fmt, keepOwner)
    );
  } catch (e) {
    catalog.logOperation("restore", false, `リストア失敗: ${versionId} -> ${detail.dest}: ${e.message}`, detail);
    throw e;
  } finally {
    if (fetchedTmp !== null) fs.rmSync(fetchedTmp, { force: true });
  }

  catalog.logOperation("restore", true, `リストア: ${versionId} -> ${detail.dest}`, detail);
  return { version_id: versionId, dest_profile: dest.name, dest_db: destDb, mode: rec.mode };
}

// 任意のダンプファイル (SQL / カスタム形式を自動判定) を復元する
export async function restoreDumpFile(catalog, filePath, dest, destDb, {
  drop = false, keepOwner = false,
} = {}) {
  if (!fs.existsSync(filePath)) {
    throw new RestoreError(`ダンプファイルが見つかりません: ${filePath}`);
  }
  const fmt = detectFormat(filePath);
  const detail = { file: String(filePath), fmt, dest: `${dest.name}:${destDb}`, drop };
  try {
    await withReplaceDatabase(dest, destDb, { drop }, () =>
      restoreDump(dest, destDb, filePath, fmt, keepOwner)
    );
  } catch (e) {
    catalog.logOperation("restore", false, `ファイルからのリストア失敗: ${filePath} -> ${detail.dest}: ${e.message}`, detail);
    throw e;
  }
  catalog.logOperation("restore", true, `ファイルからのリストア: ${filePath} -> ${detail.dest}`, detail);
  return { file: String(filePath), fmt, dest_profile: dest.name, dest_db: destDb };
}

// ローカルカタログを介さず、アーカイブ DB から直接復元する。
// 別マシンで作成されたアーカイブバックアップの復元に使う。
export async function restoreFromArchive(catalog, archiveHost, archiveDb, versionId, dest, {
  destDb = null, drop = false, keepOwner = false,
} = {}) {
  const fetched = path.join(tmpDir(), `restore_${crypto.randomBytes(8).toString("hex")}.dump`);
  const detail = { version_id: versionId, archive: `${archiveHost.name}:${archiveDb}`, dest: null, drop };
  try {
    const meta = await archiveMod.fetchDump(archiveHost, archiveDb, fetched, { versionId });
    destDb = destDb || meta.source_db;
    detail.dest = `${dest.name}:${destDb}`;
    await withReplaceDatabase(dest, destDb, { drop }, () =>
      restoreDump(dest, destDb, fetched, meta.fmt, keepOwner)
    );
  } catch (e) {
    catalog.logOperation("restore", false, `アーカイブからのリストア失敗: ${versionId}: ${e.message}`, detail);
    throw e;
  } finally {
    fs.rmSync(fetched, { force: true });
  }
  catalog.logOperation("restore", true, `アーカイブからのリストア: ${versionId} -> ${detail.dest}`, detail);
  return { version_id: versionId, dest_profile: dest.name, dest_db: destDb, mode: "archive" };
}
