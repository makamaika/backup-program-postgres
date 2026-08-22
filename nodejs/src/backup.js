// バックアップエンジン。
//
// 4 つのモードを提供する:
//   - file    : pg_dump でファイルに出力 (SQL またはカスタム形式) + バージョン登録
//   - archive : 格納先サーバーの BACK_UP_DB にダンプを保存 + バージョン登録
//   - clone   : pg_dump | pg_restore でそのまま別 DB に複製 (ローカル/リモートの任意の組合せ)
//   - agent   : pg_dump の結果を HTTPS バックアップエージェントへ保存

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import * as archiveMod from "./archive.js";
import { AgentClient } from "./agent.js";
import * as pgtools from "./pgtools.js";
import { DEFAULT_ARCHIVE_DB, backupsDir, profileDisplay, tmpDir } from "./config.js";
import { databaseEncoding, sameCluster, serverVersion, withReplaceDatabase } from "./db.js";
import { BackupError } from "./errors.js";
import { listDatabases } from "./inspect.js";

// バックアップ対象から常に除外する DB
export const SYSTEM_DBS = new Set(["template0", "template1"]);

export function sha256OfFile(filePath) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash("sha256");
    fs.createReadStream(filePath)
      .on("data", (d) => hash.update(d))
      .on("end", () => resolve(hash.digest("hex")))
      .on("error", reject);
  });
}

// DB 名をファイル/ディレクトリ名に使える形に変換する。
// JS の \w は ASCII 限定のため、Python 版 (Unicode \w) と揃えて
// Unicode の文字・数字を残す — 日本語 DB 名同士がパス衝突しないようにする。
function safeName(name) {
  return String(name).replace(/[^\p{L}\p{N}_.-]/gu, "_");
}

function majorOf(versionText) {
  const afterParen = String(versionText).split(")").pop();
  const m = /(\d+)/.exec(afterParen);
  return m ? Number(m[1]) : 0;
}

// pg_dump がソースサーバーより古いメジャーバージョンなら実行前に止める。
// clone は失敗時の影響が大きい (複製先を作り直す) ため、事前に検出する。
export async function checkDumpCompat(source) {
  const dumpMajor = majorOf(pgtools.pgDumpVersion());
  const serverMajor = majorOf(await serverVersion(source));
  if (dumpMajor > 0 && dumpMajor < serverMajor) {
    throw new BackupError(
      `pg_dump (メジャー ${dumpMajor}) がサーバー (メジャー ${serverMajor}) より古いため` +
      "実行できません。新しい PostgreSQL クライアントツールを PGBACKUP_PG_BIN に指定してください。"
    );
  }
}

// モードごとに無効なオプションの組合せを検出する (CLI / Web 共通)。
// 戻り値は解決済みの fmt。無効な指定は黙って無視せずエラーにする。
export function checkModeOptions(mode, {
  fmt = null, out = null, dest = null, destDb = null,
  agent = null, drop = false, keepOwner = false, nDbs = 1,
} = {}) {
  if (!["file", "archive", "clone", "agent"].includes(mode)) {
    throw new BackupError(`不明なモード: ${mode}`);
  }
  if (!["file", "agent"].includes(mode)) {
    if (fmt !== null) throw new BackupError(`--fmt は file / agent モード専用です (mode=${mode})`);
  }
  if (mode !== "file") {
    if (out) throw new BackupError(`--out は file モード専用です (mode=${mode})`);
  }
  if (mode !== "clone") {
    if (destDb) throw new BackupError(`--dest-db は clone モード専用です (mode=${mode})`);
    if (drop) throw new BackupError(`--drop は clone モード専用です (mode=${mode})`);
  }
  if (mode === "archive" && keepOwner) {
    throw new BackupError("--keep-owner は archive モードでは指定できません (復元時 'restore --keep-owner' で指定します)");
  }
  if (["file", "agent"].includes(mode) && dest) throw new BackupError("--dest は archive / clone モード専用です");
  if (mode === "clone" && !dest) throw new BackupError("clone モードでは --dest (複製先プロファイル) が必須です");
  if (mode === "agent" && !agent) throw new BackupError("agent モードでは --agent が必須です");
  if (mode !== "agent" && agent) throw new BackupError("--agent は agent モード専用です");
  if (destDb && nDbs > 1) throw new BackupError("--dest-db は対象 DB が 1 つのときのみ指定できます");
  if (mode === "file" && out && nDbs > 1) {
    const isDir = (fs.existsSync(out) && fs.statSync(out).isDirectory()) || /[\\/]$/.test(String(out));
    if (!isDir) {
      throw new BackupError(
        "--out にファイルパスを指定した場合、対象 DB は 1 つだけにしてください (複数 DB はディレクトリを指定)"
      );
    }
  }
  const resolved = fmt ?? (mode === "agent" ? "custom" : "plain");
  if (!["plain", "custom"].includes(resolved)) {
    throw new BackupError(`不明なフォーマット: ${resolved} (plain または custom)`);
  }
  return resolved;
}

// バックアップ対象 DB を決定する。--all の場合はテンプレートとアーカイブ DB を除く全 DB
export async function resolveTargetDbs(profile, dbs, allDbs = false, archiveDb = DEFAULT_ARCHIVE_DB) {
  const existing = new Set((await listDatabases(profile, true)).map((d) => d.name));
  if (allDbs) {
    return [...existing].filter((d) => !SYSTEM_DBS.has(d) && d !== archiveDb).sort();
  }
  if (!dbs || !dbs.length) {
    throw new BackupError("バックアップ対象の DB を指定してください (--db または --all)");
  }
  const missing = dbs.filter((d) => !existing.has(d));
  if (missing.length) {
    throw new BackupError(`${profileDisplay(profile)} に存在しない DB: ${missing.join(", ")}`);
  }
  return dbs;
}

function resolveOutPath(out, profile, db, versionNo, fmt) {
  const ext = fmt === "plain" ? "sql" : "dump";
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "").replace("T", "T");
  const autoName = `${safeName(db)}_v${versionNo}_${stamp}.${ext}`;
  if (out === null || out === undefined) {
    let p = path.join(backupsDir(), safeName(profile.name), safeName(db), autoName);
    if (fs.existsSync(p)) {
      // safeName で別 DB 名が同じ表記に潰れた場合等でも上書きしない
      p = p.replace(`.${ext}`, `_${crypto.randomBytes(2).toString("hex")}.${ext}`);
    }
    return p;
  }
  const isDir = (fs.existsSync(out) && fs.statSync(out).isDirectory()) || /[\\/]$/.test(String(out));
  if (isDir) return path.join(String(out), autoName);
  if (fs.existsSync(out)) {
    throw new BackupError(`出力先 ${out} は既に存在します。上書きを避けるため別のパスを指定してください。`);
  }
  return String(out);
}

// pg_dump でファイルへバックアップし、バージョンをカタログに登録する
export async function backupToFile(catalog, profile, db, {
  fmt = "plain", out = null, note = null, keepOwner = false,
} = {}) {
  if (!["plain", "custom"].includes(fmt)) {
    throw new BackupError(`不明なフォーマット: ${fmt} (plain または custom)`);
  }
  let rec = catalog.reserveVersion(profile.name, db, "file", fmt);
  let outPath = null;
  try {
    outPath = resolveOutPath(out, profile, db, rec.version_no, fmt);
    await pgtools.dumpToFile(profile, db, outPath, fmt, keepOwner);
    rec = catalog.finalizeBackup(rec.id, {
      location: path.resolve(outPath),
      size: fs.statSync(outPath).size,
      sha256: await sha256OfFile(outPath),
      pg_dump_version: pgtools.pgDumpVersion(),
      note,
    });
  } catch (e) {
    // 作りかけのダンプファイルを残さない (自分で作ったパスのみ削除)
    if (outPath !== null) fs.rmSync(outPath, { force: true });
    catalog.abortBackup(rec.id);
    catalog.logOperation("backup", false, `file バックアップ失敗: ${profile.name}/${db}: ${e.message}`,
      { profile: profile.name, db, mode: "file" });
    throw e;
  }
  catalog.logOperation("backup", true, `file バックアップ: ${rec.version_id} -> ${outPath}`,
    { version_id: rec.version_id, mode: "file", location: outPath });
  return rec;
}

// ダンプを取得し、格納先サーバーのアーカイブ DB (BACK_UP_DB) に保存する
export async function backupToArchive(catalog, source, db, dest, {
  archiveDb = DEFAULT_ARCHIVE_DB, note = null,
} = {}) {
  if (db === archiveDb && (await sameCluster(source, dest))) {
    throw new BackupError("アーカイブ DB 自身をアーカイブへバックアップすることはできません");
  }

  await archiveMod.ensureArchiveDb(dest, archiveDb);
  let rec = catalog.reserveVersion(
    source.name, db, "archive", "custom",
    await archiveMod.nextArchiveVersionNo(dest, archiveDb, db)
  );
  const tmpPath = path.join(tmpDir(), `archive_${crypto.randomBytes(8).toString("hex")}.dump`);
  try {
    await pgtools.dumpToFile(source, db, tmpPath, "custom");
    const digest = await sha256OfFile(tmpPath);
    const rowId = await archiveMod.storeDump(dest, archiveDb, {
      versionId: rec.version_id,
      sourceProfile: source.name,
      sourceHost: source.host,
      sourcePort: source.port,
      sourceDb: db,
      versionNo: rec.version_no,
      fmt: "custom",
      sha256: digest,
      pgDumpVersion: pgtools.pgDumpVersion(),
      note,
    }, tmpPath);
    rec = catalog.finalizeBackup(rec.id, {
      archive_profile: dest.name,
      archive_db: archiveDb,
      archive_row_id: rowId,
      size: fs.statSync(tmpPath).size,
      sha256: digest,
      pg_dump_version: pgtools.pgDumpVersion(),
      note,
    });
  } catch (e) {
    catalog.abortBackup(rec.id);
    catalog.logOperation("backup", false, `archive バックアップ失敗: ${source.name}/${db}: ${e.message}`,
      { profile: source.name, db, mode: "archive", dest: dest.name });
    throw e;
  } finally {
    fs.rmSync(tmpPath, { force: true });
  }
  catalog.logOperation("backup", true,
    `archive バックアップ: ${rec.version_id} -> ${dest.name}:${archiveDb}`,
    { version_id: rec.version_id, mode: "archive", dest: dest.name, archive_db: archiveDb });
  return rec;
}

export async function backupToAgent(catalog, source, db, agent, {
  fmt = "custom", note = null, keepOwner = false,
} = {}) {
  if (!["plain", "custom"].includes(fmt)) throw new BackupError(`不明なフォーマット: ${fmt}`);
  let rec = catalog.reserveVersion(source.name, db, "agent", fmt);
  const ext = fmt === "plain" ? "sql" : "dump";
  const tmpPath = path.join(tmpDir(), `agent_upload_${crypto.randomBytes(8).toString("hex")}.${ext}`);
  const client = new AgentClient(agent);
  let remoteId = null;
  try {
    await pgtools.dumpToFile(source, db, tmpPath, fmt, keepOwner);
    const digest = await sha256OfFile(tmpPath);
    const remote = await client.uploadBackup(tmpPath, {
      versionId: rec.version_id, sourceProfile: source.name, sourceDb: db,
      fmt, sha256: digest, note,
    });
    remoteId = remote.id;
    rec = catalog.finalizeBackup(rec.id, {
      agent_profile: agent.name,
      agent_backup_id: remoteId,
      size: fs.statSync(tmpPath).size,
      sha256: digest,
      pg_dump_version: pgtools.pgDumpVersion(),
      note,
    });
  } catch (e) {
    if (remoteId !== null) {
      try { await client.deleteBackup(remoteId); } catch { /* best effort rollback */ }
    }
    catalog.abortBackup(rec.id);
    catalog.logOperation("backup", false, `agent バックアップ失敗: ${source.name}/${db}: ${e.message}`,
      { profile: source.name, db, mode: "agent", agent: agent.name });
    throw e;
  } finally {
    fs.rmSync(tmpPath, { force: true });
  }
  catalog.logOperation("backup", true, `agent バックアップ: ${rec.version_id} -> ${agent.name}:${remoteId}`,
    { version_id: rec.version_id, mode: "agent", agent: agent.name, agent_backup_id: remoteId });
  return rec;
}

export async function backupAgentDatabaseToFile(catalog, agent, remoteProfile, db, {
  fmt = "custom", out = null, note = null, keepOwner = false,
} = {}) {
  let rec = catalog.reserveVersion(agent.name, db, "file", fmt);
  let outPath = null;
  try {
    outPath = resolveOutPath(out, agent, db, rec.version_no, fmt);
    const meta = await new AgentClient(agent).downloadDatabase(remoteProfile, db, outPath, {
      fmt, keepOwner,
    });
    rec = catalog.finalizeBackup(rec.id, {
      location: path.resolve(outPath), size: meta.size, sha256: meta.sha256, note,
    });
  } catch (e) {
    if (outPath !== null) fs.rmSync(outPath, { force: true });
    catalog.abortBackup(rec.id);
    catalog.logOperation("backup", false, `agent pull 失敗: ${agent.name}/${remoteProfile}/${db}: ${e.message}`,
      { agent: agent.name, remote_profile: remoteProfile, db, mode: "agent-pull" });
    throw e;
  }
  catalog.logOperation("backup", true, `agent pull: ${rec.version_id} <- ${agent.name}:${remoteProfile}/${db}`,
    { version_id: rec.version_id, agent: agent.name, remote_profile: remoteProfile, location: outPath });
  return rec;
}

// DB をそのまま複製する。ローカル/リモートの任意の組合せに対応。
// 複製先 DB は自動作成される (既存の場合は drop=true が必要)。
// 同一クラスタ判定はホスト名の表記ゆれ (localhost / 127.0.0.1 等) に騙されないよう
// サーバーの system_identifier で行い、複製元自身の破壊を防ぐ。
// 複製に失敗した場合、既存の複製先 DB は元の状態に戻る。
export async function cloneDatabase(catalog, source, sourceDb, dest, destDb, {
  drop = false, keepOwner = false, note = null,
} = {}) {
  if (sourceDb === destDb && (await sameCluster(source, dest))) {
    throw new BackupError("複製元と複製先が同一です (同じサーバーの同じ DB)。別の DB 名を指定してください。");
  }

  const detail = {
    source: `${source.name}:${sourceDb}`,
    dest: `${dest.name}:${destDb}`,
    drop,
    note,
  };
  try {
    // 破壊的操作 (複製先の入替) の前に、失敗が予見できるものは検出しておく
    await checkDumpCompat(source);
    const encoding = await databaseEncoding(source, sourceDb);
    if (encoding === null) {
      throw new BackupError(`複製元 DB '${sourceDb}' が ${profileDisplay(source)} に存在しません`);
    }
    await withReplaceDatabase(dest, destDb, { drop, encoding }, () =>
      pgtools.pipeDumpRestore(source, sourceDb, dest, destDb, keepOwner)
    );
  } catch (e) {
    catalog.logOperation("clone", false, `複製失敗: ${detail.source} -> ${detail.dest}: ${e.message}`, detail);
    throw e;
  }
  catalog.logOperation("clone", true, `複製: ${detail.source} -> ${detail.dest}`, detail);
  return {
    mode: "clone",
    source_profile: source.name,
    source_db: sourceDb,
    dest_profile: dest.name,
    dest_db: destDb,
  };
}
