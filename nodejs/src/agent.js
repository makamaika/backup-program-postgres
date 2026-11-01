// Python 版 pgbackup-agent と互換の HTTPS クライアント。
// 新しい npm 依存は使わず、Node 標準の https/crypto/stream だけで実装する。

import crypto from "node:crypto";
import fs from "node:fs";
import https from "node:https";
import path from "node:path";
import { Transform } from "node:stream";
import { pipeline } from "node:stream/promises";

import { AgentError } from "./errors.js";

const JSON_LIMIT = 8 * 1024 * 1024;

function makeUrl(profile, endpoint, query = null) {
  const url = new URL(endpoint, profile.url + "/");
  for (const [key, value] of Object.entries(query || {})) {
    if (value !== null && value !== undefined) url.searchParams.set(key, String(value));
  }
  return url;
}

function optionsFor(profile, method, headers = {}) {
  return {
    method,
    ca: profile.ca_cert,
    rejectUnauthorized: true,
    minVersion: "TLSv1.2",
    headers: {
      Authorization: `Bearer ${profile.token}`,
      "User-Agent": "pgbackup-node-agent-client/1",
      ...headers,
    },
  };
}

function collectResponse(res) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    res.on("data", (chunk) => {
      size += chunk.length;
      if (size > JSON_LIMIT) {
        res.destroy(new AgentError("エージェントの応答が大きすぎます"));
        return;
      }
      chunks.push(chunk);
    });
    res.on("end", () => resolve(Buffer.concat(chunks)));
    res.on("error", reject);
  });
}

async function responseError(res) {
  const raw = await collectResponse(res);
  let detail = raw.toString("utf-8").trim();
  try { detail = JSON.parse(detail).detail || detail; } catch { /* テキストのまま */ }
  return new AgentError(`エージェントが HTTP ${res.statusCode} を返しました: ${detail || res.statusMessage}`);
}

function open(profile, method, endpoint, { query = null, headers = {}, timeout = 30000 } = {}) {
  if (!["GET", "DELETE"].includes(method)) {
    throw new AgentError(`内部エラー: body なしの ${method} は未対応です`);
  }
  return new Promise((resolve, reject) => {
    const req = https.request(makeUrl(profile, endpoint, query), optionsFor(profile, method, headers), (res) => {
      if (res.statusCode < 200 || res.statusCode >= 300) {
        responseError(res).then(reject, reject);
      } else {
        resolve({ req, res });
      }
    });
    req.setTimeout(timeout, () => req.destroy(new AgentError("エージェント通信がタイムアウトしました")));
    req.on("error", (err) => reject(err instanceof AgentError ? err
      : new AgentError(`エージェントへ接続できません: ${err.message}`)));
    req.end();
  });
}

function requestWithBody(profile, method, endpoint, body, { query = null, timeout = 30000 } = {}) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(body), "utf-8");
    const req = https.request(makeUrl(profile, endpoint, query), optionsFor(profile, method, {
      "Content-Type": "application/json",
      "Content-Length": data.length,
      Accept: "application/json",
    }), async (res) => {
      try {
        if (res.statusCode < 200 || res.statusCode >= 300) throw await responseError(res);
        const raw = await collectResponse(res);
        try { resolve(JSON.parse(raw.toString("utf-8"))); }
        catch { reject(new AgentError("エージェントから不正な JSON 応答を受信しました")); }
      } catch (err) { reject(err); }
    });
    req.setTimeout(timeout, () => req.destroy(new AgentError("エージェント通信がタイムアウトしました")));
    req.on("error", (err) => reject(err instanceof AgentError ? err
      : new AgentError(`エージェントへ接続できません: ${err.message}`)));
    req.end(data);
  });
}

async function requestJson(profile, method, endpoint, query = null) {
  const { res } = await open(profile, method, endpoint, { query });
  const raw = await collectResponse(res);
  try { return JSON.parse(raw.toString("utf-8")); }
  catch { throw new AgentError("エージェントから不正な JSON 応答を受信しました"); }
}

export async function saveResponse(res, destination, expectedSha = null, replaceReserved = false) {
  if (fs.existsSync(destination) && !replaceReserved) throw new AgentError(`出力先は既に存在します: ${destination}`);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const temp = `${destination}.${crypto.randomBytes(6).toString("hex")}.part`;
  const hash = crypto.createHash("sha256");
  let size = 0;
  let tempCreated = false;
  try {
    const meter = new Transform({
      transform(chunk, encoding, callback) {
        size += chunk.length;
        hash.update(chunk);
        callback(null, chunk);
      },
    });
    const output = fs.createWriteStream(temp, { flags: "wx" });
    output.on("open", () => { tempCreated = true; });
    await pipeline(res, meter, output);
    const digest = hash.digest("hex");
    const expected = expectedSha || res.headers["x-pgbackup-sha256"];
    if (expected && expected !== digest) {
      throw new AgentError(`SHA-256 が一致しません: expected=${expected}, actual=${digest}`);
    }
    fs.renameSync(temp, destination);
    return { size, sha256: digest };
  } finally {
    if (tempCreated) fs.rmSync(temp, { force: true });
  }
}

export class AgentClient {
  constructor(profile) {
    this.profile = profile;
  }

  async health() {
    const result = await requestJson(this.profile, "GET", "/v1/health");
    if (result.protocol !== 1) throw new AgentError(`未対応のプロトコルです: ${result.protocol}`);
    return result;
  }

  listBackups(limit = 200) {
    return requestJson(this.profile, "GET", "/v1/backups", { limit });
  }

  getBackup(id) {
    return requestJson(this.profile, "GET", `/v1/backups/${encodeURIComponent(id)}`);
  }

  deleteBackup(id) {
    return requestJson(this.profile, "DELETE", `/v1/backups/${encodeURIComponent(id)}`);
  }

  async uploadBackup(filePath, { versionId, sourceProfile, sourceDb, fmt, sha256, note = null }) {
    if (note !== null && String(note).length > 1000) {
      throw new AgentError("エージェントへ送るメモは 1000 文字以内にしてください");
    }
    const stat = fs.statSync(filePath);
    const health = await this.health();
    if (health.max_upload_bytes !== null && health.max_upload_bytes !== undefined
        && stat.size > Number(health.max_upload_bytes)) {
      throw new AgentError(
        `バックアップ (${stat.size} bytes) がエージェントのアップロード上限 ` +
        `(${health.max_upload_bytes} bytes) を超えています`
      );
    }
    return new Promise((resolve, reject) => {
      const metadata = Buffer.from(JSON.stringify({
        version_id: versionId, source_profile: sourceProfile, source_db: sourceDb, fmt, note,
      }), "utf-8").toString("base64url");
      const req = https.request(makeUrl(this.profile, "/v1/backups"), optionsFor(this.profile, "POST", {
        "Content-Type": "application/octet-stream",
        "Content-Length": stat.size,
        "X-Pgbackup-Sha256": sha256,
        "X-Pgbackup-Metadata": metadata,
        Accept: "application/json",
      }), async (res) => {
        try {
          if (res.statusCode < 200 || res.statusCode >= 300) throw await responseError(res);
          const raw = await collectResponse(res);
          try { resolve(JSON.parse(raw.toString("utf-8"))); }
          catch { reject(new AgentError("アップロード後の応答が不正です")); }
        } catch (err) { reject(err); }
      });
      req.setTimeout(3600000, () => req.destroy(new AgentError("アップロードがタイムアウトしました")));
      req.on("error", (err) => reject(err instanceof AgentError ? err : new AgentError(err.message)));
      fs.createReadStream(filePath).on("error", reject).pipe(req);
    });
  }

  async downloadBackup(id, destination) {
    const record = await this.getBackup(id);
    const { res } = await open(this.profile, "GET", `/v1/backups/${encodeURIComponent(id)}/content`, {
      timeout: 3600000,
    });
    const downloaded = await saveResponse(res, destination, record.sha256);
    if (Number(record.size) !== downloaded.size) {
      fs.rmSync(destination, { force: true });
      throw new AgentError(`ダウンロードサイズが一致しません: ${record.size} != ${downloaded.size}`);
    }
    return { ...record, ...downloaded };
  }

  databases(remoteProfile, templates = false) {
    return requestJson(this.profile, "GET", "/v1/databases", {
      profile: remoteProfile, templates: String(templates),
    });
  }

  structure(remoteProfile, db) {
    return requestJson(this.profile, "GET", "/v1/structure", { profile: remoteProfile, db });
  }

  table(remoteProfile, db, schema, table) {
    return requestJson(this.profile, "GET", "/v1/table", { profile: remoteProfile, db, schema, table });
  }

  sample(remoteProfile, db, schema, table, limit = 10) {
    return requestJson(this.profile, "GET", "/v1/sample", {
      profile: remoteProfile, db, schema, table, limit,
    });
  }

  async downloadDatabase(remoteProfile, db, destination, {
    fmt = "custom", keepOwner = false, replaceReserved = false,
  } = {}) {
    const body = Buffer.from(JSON.stringify({ profile: remoteProfile, db, fmt, keep_owner: keepOwner }));
    const result = await new Promise((resolve, reject) => {
      const req = https.request(makeUrl(this.profile, "/v1/dump"), optionsFor(this.profile, "POST", {
        "Content-Type": "application/json", "Content-Length": body.length,
      }), async (res) => {
        try {
          if (res.statusCode < 200 || res.statusCode >= 300) throw await responseError(res);
          resolve(await saveResponse(res, destination, null, replaceReserved));
        } catch (err) { reject(err); }
      });
      req.setTimeout(3600000, () => req.destroy(new AgentError("DB 取得がタイムアウトしました")));
      req.on("error", reject);
      req.end(body);
    });
    return { ...result, fmt };
  }

  createSnapshot(remoteProfile, db, options = {}) {
    return requestWithBody(this.profile, "POST", "/v1/snapshots", {
      profile: remoteProfile,
      db,
      version_id: options.versionId ?? null,
      fmt: options.fmt ?? "custom",
      keep_owner: Boolean(options.keepOwner),
      note: options.note ?? null,
    }, { timeout: 3600000 });
  }

  restoreRemoteBackup(id, remoteProfile, options = {}) {
    return requestWithBody(this.profile, "POST", `/v1/backups/${encodeURIComponent(id)}/restore`, {
      profile: remoteProfile,
      db: options.destDb ?? null,
      drop: Boolean(options.drop),
      keep_owner: Boolean(options.keepOwner),
    }, { timeout: 3600000 });
  }
}
