// pg_dump / pg_restore / psql の検出と実行ラッパー。

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import { ToolError } from "./errors.js";

const IS_WIN = process.platform === "win32";

// stderr が長大な場合にエラーメッセージへ含める最大文字数
const STDERR_TAIL = 4000;

let cachedBindir = null;

function exeName(name) {
  return IS_WIN ? `${name}.exe` : name;
}

function versionKey(binDir) {
  const m = /(\d+)(?:\.(\d+))?/.exec(path.basename(path.dirname(binDir)));
  return m ? Number(m[1]) * 1000 + Number(m[2] || 0) : 0;
}

// PostgreSQL クライアントツールのディレクトリを探す。
// 優先順: 環境変数 PGBACKUP_PG_BIN > PATH 上の pg_dump > OS 標準のインストール先。
export function findPgBindir() {
  if (cachedBindir) return cachedBindir;

  const env = process.env.PGBACKUP_PG_BIN;
  if (env) {
    if (fs.existsSync(path.join(env, exeName("pg_dump")))) {
      cachedBindir = env;
      return env;
    }
    throw new ToolError(`PGBACKUP_PG_BIN=${env} に pg_dump が見つかりません`);
  }

  const pathDirs = (process.env.PATH || "").split(path.delimiter);
  for (const dir of pathDirs) {
    if (dir && fs.existsSync(path.join(dir, exeName("pg_dump")))) {
      cachedBindir = dir;
      return dir;
    }
  }

  const candidates = [];
  if (IS_WIN) {
    for (const root of ["C:\\Program Files\\PostgreSQL", "C:\\Program Files (x86)\\PostgreSQL"]) {
      try {
        for (const v of fs.readdirSync(root)) candidates.push(path.join(root, v, "bin"));
      } catch { /* インストールなし */ }
    }
  } else {
    try {
      for (const v of fs.readdirSync("/usr/lib/postgresql")) {
        candidates.push(path.join("/usr/lib/postgresql", v, "bin"));
      }
    } catch { /* なし */ }
    try {
      for (const v of fs.readdirSync("/usr")) {
        if (v.startsWith("pgsql-")) candidates.push(path.join("/usr", v, "bin"));
      }
    } catch { /* なし */ }
    candidates.push("/opt/homebrew/opt/libpq/bin", "/usr/local/opt/libpq/bin");
  }
  const valid = candidates.filter((c) => fs.existsSync(path.join(c, exeName("pg_dump"))));
  if (!valid.length) {
    throw new ToolError(
      "pg_dump が見つかりません。PostgreSQL クライアントツールをインストールするか、" +
      "環境変数 PGBACKUP_PG_BIN に bin ディレクトリを設定してください。"
    );
  }
  valid.sort((a, b) => versionKey(a) - versionKey(b));
  cachedBindir = valid[valid.length - 1];
  return cachedBindir;
}

export function toolPath(name) {
  return path.join(findPgBindir(), exeName(name));
}

// libpq conninfo の値クオート (空文字・空白・クオート・バックスラッシュ・= を含むならクオート)
function quoteConninfoValue(v) {
  const s = String(v);
  if (s !== "" && /^[A-Za-z0-9_.:\-]+$/.test(s)) return s;
  return "'" + s.replaceAll("\\", "\\\\").replaceAll("'", "\\'") + "'";
}

// 接続文字列を組み立てる。
// DB 名を pg_dump/psql の位置引数や -d に生で渡すと、'=' を含む名前や
// postgresql:// で始まる名前が conninfo として、'-' で始まる名前がオプションとして
// 誤解釈される。常に完全な conninfo を組み立てて防ぐ。
// パスワードはプロセス一覧に出さないため conninfo に含めず PGPASSWORD 環境変数で渡す。
export function makeConninfo(profile, dbname) {
  const parts = [
    `host=${quoteConninfoValue(profile.host)}`,
    `port=${quoteConninfoValue(profile.port)}`,
    `user=${quoteConninfoValue(profile.user)}`,
    `dbname=${quoteConninfoValue(dbname)}`,
  ];
  if (profile.sslmode) parts.push(`sslmode=${quoteConninfoValue(profile.sslmode)}`);
  return parts.join(" ");
}

function envFor(profile) {
  const env = { ...process.env };
  if (profile.password !== null && profile.password !== undefined) {
    env.PGPASSWORD = profile.password;
  }
  env.PGCONNECT_TIMEOUT = "10";
  env.PGCLIENTENCODING = "UTF8";
  return env;
}

function tail(text) {
  return (text || "").trim().slice(-STDERR_TAIL);
}

// コマンドを実行し、失敗時は stderr を含めて ToolError を投げる
function run(cmd, args, env) {
  return new Promise((resolve, reject) => {
    const proc = spawn(cmd, args, { env, stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    proc.stderr.setEncoding("utf-8");
    proc.stderr.on("data", (d) => {
      if (stderr.length < STDERR_TAIL * 4) stderr += d;
    });
    proc.on("error", (e) => reject(new ToolError(`${path.basename(cmd)} を起動できません: ${e.message}`)));
    proc.on("close", (code) => {
      if (code === 0) resolve();
      else reject(new ToolError(`${path.parse(cmd).name} が失敗しました (exit=${code}):\n${tail(stderr)}`));
    });
  });
}

export function pgDumpVersion() {
  const r = spawnSync(toolPath("pg_dump"), ["--version"], { encoding: "utf-8" });
  return (r.stdout || "").trim();
}

// pg_dump でファイルへ出力する。fmt: 'plain' (SQL) または 'custom' (.dump)。
// plain 形式は psql でそのまま流し込むため、keepOwner=false なら
// ダンプ時点で所有者/権限文を除いて移植可能にする
// (custom 形式は pg_restore 側の --no-owner で制御するため常に含めてよい)。
export async function dumpToFile(profile, dbname, outPath, fmt = "plain", keepOwner = true) {
  const fmtFlag = fmt === "plain" ? "p" : "c";
  const args = [
    "--no-password",
    `--dbname=${makeConninfo(profile, dbname)}`,
    `--format=${fmtFlag}`,
    "--file",
    String(outPath),
  ];
  if (fmt === "plain" && !keepOwner) args.push("--no-owner", "--no-privileges");
  fs.mkdirSync(path.dirname(String(outPath)), { recursive: true });
  await run(toolPath("pg_dump"), args, envFor(profile));
}

// カスタム形式 (.dump) を pg_restore で dbname に復元する
export async function restoreCustom(profile, dbname, dumpPath, keepOwner = false) {
  const args = ["--no-password", "-d", makeConninfo(profile, dbname)];
  if (!keepOwner) args.push("--no-owner", "--no-privileges");
  args.push(String(dumpPath));
  await run(toolPath("pg_restore"), args, envFor(profile));
}

// プレーン SQL ダンプを psql で dbname に流し込む。
// 注意: psql はダンプ内のメタコマンド (\! 等) も実行する。
// 信頼できるファイルのみ復元すること (README のセキュリティ注意を参照)。
export async function restorePlain(profile, dbname, sqlPath) {
  const args = [
    "--no-password",
    "--no-psqlrc",
    "-v",
    "ON_ERROR_STOP=1",
    "--quiet",
    "-d",
    makeConninfo(profile, dbname),
    "-f",
    String(sqlPath),
  ];
  await run(toolPath("psql"), args, envFor(profile));
}

// pg_dump -Fc | pg_restore で DB を直接複製する (中間ファイルなし)
export function pipeDumpRestore(src, srcDb, dest, destDb, keepOwner = false) {
  const dumpArgs = ["--no-password", `--dbname=${makeConninfo(src, srcDb)}`, "--format=c"];
  const restoreArgs = ["--no-password", "-d", makeConninfo(dest, destDb)];
  if (!keepOwner) restoreArgs.push("--no-owner", "--no-privileges");

  return new Promise((resolve, reject) => {
    const dump = spawn(toolPath("pg_dump"), dumpArgs, {
      env: envFor(src), stdio: ["ignore", "pipe", "pipe"],
    });
    const restore = spawn(toolPath("pg_restore"), restoreArgs, {
      env: envFor(dest), stdio: ["pipe", "ignore", "pipe"],
    });

    let dumpErr = "", restoreErr = "";
    dump.stderr.setEncoding("utf-8");
    restore.stderr.setEncoding("utf-8");
    dump.stderr.on("data", (d) => { if (dumpErr.length < STDERR_TAIL * 4) dumpErr += d; });
    restore.stderr.on("data", (d) => { if (restoreErr.length < STDERR_TAIL * 4) restoreErr += d; });

    dump.stdout.pipe(restore.stdin);
    // pg_restore が先に死ぬと pipe 書き込みが EPIPE になる
    restore.stdin.on("error", () => {});

    let dumpCode = null, restoreCode = null, settled = false;
    const finish = () => {
      if (settled || dumpCode === null || restoreCode === null) return;
      settled = true;
      if (dumpCode === 0 && restoreCode === 0) return resolve();
      // 根本原因を見せるため両方のエラーをまとめ、pg_restore を先に報告する
      const parts = [];
      if (restoreCode !== 0) parts.push(`pg_restore (exit=${restoreCode}):\n${tail(restoreErr)}`);
      if (dumpCode !== 0) parts.push(`pg_dump (exit=${dumpCode}):\n${tail(dumpErr)}`);
      reject(new ToolError("複製に失敗しました:\n" + parts.join("\n\n")));
    };
    dump.on("error", (e) => {
      if (settled) return;
      settled = true;
      restore.kill(); // 相方を待たせたまま残さない
      reject(new ToolError(`pg_dump を起動できません: ${e.message}`));
    });
    restore.on("error", (e) => {
      if (settled) return;
      settled = true;
      dump.kill();
      reject(new ToolError(`pg_restore を起動できません: ${e.message}`));
    });
    dump.on("close", (code) => { dumpCode = code ?? -1; finish(); });
    restore.on("close", (code) => {
      restoreCode = code ?? -1;
      if (restoreCode !== 0 && dumpCode === null) {
        // pg_restore が先に失敗すると pipe() が unpipe/pause して pg_dump が
        // パイプ詰まりで永久ブロックする。読み捨てに切替えて確実に終了させる。
        dump.stdout.resume();
        dump.kill();
      }
      finish();
    });
  });
}
