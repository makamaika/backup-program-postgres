// 設定・接続プロファイル管理。
//
// データディレクトリ (PGBACKUP_HOME, 既定 ~/.pgbackup) は Python 版と共通:
//   - connections.json : 接続プロファイル (パスワードを含むため git 管理外に置くこと)
//   - catalog.db       : バックアップのバージョンカタログ (SQLite)
//   - backups/         : ファイルバックアップの保存先
// スキーマ・形式も Python 版と互換なので、どちらの版で取ったバックアップも
// もう一方の版から一覧・復元できる。

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { ProfileError } from "./errors.js";

// アーカイブモードの既定の格納先データベース名
export const DEFAULT_ARCHIVE_DB = "BACK_UP_DB";

// プロファイル名: パス・URL・バージョン ID ("profile/db@vN") に安全に埋め込める文字のみ
const PROFILE_NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/;

export function homeDir() {
  const root = process.env.PGBACKUP_HOME || path.join(os.homedir(), ".pgbackup");
  fs.mkdirSync(root, { recursive: true });
  return root;
}

export function backupsDir() {
  const d = path.join(homeDir(), "backups");
  fs.mkdirSync(d, { recursive: true });
  return d;
}

export function tmpDir() {
  const d = path.join(homeDir(), "tmp");
  fs.mkdirSync(d, { recursive: true });
  return d;
}

export function catalogPath() {
  return path.join(homeDir(), "catalog.db");
}

// 異常終了で残った一時ファイルを掃除する (CLI / Web の起動時に呼ぶ)
export function cleanupTmp(maxAgeHours = 24) {
  const cutoff = Date.now() - maxAgeHours * 3600 * 1000;
  let entries;
  try {
    entries = fs.readdirSync(tmpDir());
  } catch {
    return;
  }
  for (const name of entries) {
    const p = path.join(tmpDir(), name);
    try {
      const st = fs.statSync(p);
      if (st.isFile() && st.mtimeMs < cutoff) fs.unlinkSync(p);
    } catch {
      // 使用中・権限なし等は無視
    }
  }
}

export function makeProfile(name, attrs = {}) {
  return {
    name,
    host: attrs.host ?? "localhost",
    port: Number(attrs.port ?? 5432),
    user: attrs.user ?? "postgres",
    password: attrs.password ?? null,
    dbname: attrs.dbname ?? "postgres",
    sslmode: attrs.sslmode ?? null,
  };
}

export function profileDisplay(p) {
  return `${p.user}@${p.host}:${p.port}`;
}

export function profileMasked(p) {
  return { ...p, password: p.password ? "********" : null };
}

// connections.json の読み書き。
// 同一プロセス内の書き込みは Node がシングルスレッドのため自然に直列化される。
// 別プロセス同時書き込みの保護はない (最後の書き込みが勝つ)。
export class ProfileStore {
  constructor(filePath = null) {
    this.path = filePath || path.join(homeDir(), "connections.json");
  }

  _loadRaw() {
    if (!fs.existsSync(this.path)) return { profiles: Object.create(null) };
    const raw = JSON.parse(fs.readFileSync(this.path, "utf-8"));
    // '__proto__' や 'constructor' のような名前をデータとして安全に扱えるよう
    // プロトタイプなしオブジェクトへ移し替える
    raw.profiles = Object.assign(Object.create(null), raw.profiles ?? {});
    return raw;
  }

  _saveRaw(data) {
    fs.mkdirSync(path.dirname(this.path), { recursive: true });
    const tmp = this.path + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2), "utf-8");
    // Windows では対象を開いているプロセスがいると rename が一時的に失敗し得る。
    // 同期 API のため待ちも同期だが、競合時のみ・最大 250ms に抑えている。
    for (let attempt = 0; ; attempt++) {
      try {
        fs.renameSync(tmp, this.path);
        return;
      } catch (e) {
        if (attempt >= 4 || (e.code !== "EPERM" && e.code !== "EACCES" && e.code !== "EBUSY")) throw e;
        const until = Date.now() + 50;
        while (Date.now() < until) { /* 短い同期待ち */ }
      }
    }
  }

  list() {
    const raw = this._loadRaw();
    return Object.keys(raw.profiles).sort().map((name) => makeProfile(name, raw.profiles[name]));
  }

  get(name) {
    const raw = this._loadRaw();
    if (!Object.hasOwn(raw.profiles, name)) {
      const known = Object.keys(raw.profiles).sort().join(", ") || "(なし)";
      throw new ProfileError(`接続プロファイル '${name}' が見つかりません。登録済み: ${known}`);
    }
    return makeProfile(name, raw.profiles[name]);
  }

  add(profile, overwrite = false) {
    if (!PROFILE_NAME_RE.test(profile.name)) {
      throw new ProfileError(
        `プロファイル名 '${profile.name}' は使用できません。` +
        "英数字・アンダースコアで始まり、英数字・「_ . -」のみ 64 文字以内で指定してください。"
      );
    }
    if (!Number.isInteger(profile.port) || profile.port < 1 || profile.port > 65535) {
      throw new ProfileError(`ポート番号が不正です: ${profile.port} (1〜65535 の整数)`);
    }
    const raw = this._loadRaw();
    if (Object.hasOwn(raw.profiles, profile.name) && !overwrite) {
      throw new ProfileError(`接続プロファイル '${profile.name}' は既に存在します (--overwrite で上書き)`);
    }
    const { name, ...attrs } = profile;
    raw.profiles[name] = attrs;
    this._saveRaw(raw);
  }

  remove(name) {
    const raw = this._loadRaw();
    if (!Object.hasOwn(raw.profiles, name)) {
      throw new ProfileError(`接続プロファイル '${name}' が見つかりません`);
    }
    delete raw.profiles[name];
    this._saveRaw(raw);
  }
}
