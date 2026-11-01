// PostgreSQL and dump operations are mocked; SQLite locks use synthetic test files.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Readable, Writable } from "node:stream";
import { test } from "node:test";
import { DatabaseSync } from "node:sqlite";
import pg from "pg";

process.env.PGBACKUP_HOME = fs.mkdtempSync(path.join(os.tmpdir(), "pgbackup-lock-test-"));

let exists = false;
let failCreate = false;
let drops = [];
let renames = [];
let existsChecks = 0;

class FakeClient {
  constructor(config) { this.config = config; }
  async connect() {}
  async end() {}
  async query(sql) {
    if (sql.startsWith("SET TIME ZONE")) return { rows: [] };
    if (sql.startsWith("SELECT 1 FROM pg_database")) {
      existsChecks++;
      return { rowCount: exists ? 1 : 0 };
    }
    if (sql.startsWith("CREATE DATABASE")) {
      if (failCreate) {
        exists = true; // another job created it after our existence check
        throw Error("duplicate database");
      }
      if (exists) throw Error("duplicate database");
      exists = true;
      return { rows: [] };
    }
    if (sql.startsWith("DROP DATABASE")) {
      const name = /"([^"]+)"$/.exec(sql)[1];
      drops.push(name);
      if (name === "target") exists = false;
      return { rows: [] };
    }
    if (sql.startsWith("ALTER DATABASE")) {
      const names = [...sql.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
      renames.push(names);
      exists = names[1] === "target";
      return { rows: [] };
    }
    return { rows: [] };
  }
}

pg.Client = FakeClient;
const { withReplaceDatabase } = await import("../src/db.js");
const { reserveOutPath, backupToFile, backupAgentDatabaseToFile } = await import("../src/backup.js");
const { AgentClient, saveResponse } = await import("../src/agent.js");
const profile = { name: "profile", host: "unused", port: 5432, user: "unused", dbname: "postgres", sslmode: "disable" };

function reset() {
  exists = false;
  failCreate = false;
  drops = [];
  renames = [];
  existsChecks = 0;
}

test("concurrent restore observes the database created by the first job", async () => {
  reset();
  const originalExec = DatabaseSync.prototype.exec;
  let beginAttempts = 0;
  DatabaseSync.prototype.exec = function (sql) {
    if (sql === "BEGIN EXCLUSIVE") beginAttempts++;
    return originalExec.call(this, sql);
  };
  let entered;
  let release;
  const inside = new Promise((resolve) => { entered = resolve; });
  const hold = new Promise((resolve) => { release = resolve; });
  try {
    const first = withReplaceDatabase(profile, "target", {}, async () => { entered(); await hold; });
    await inside;
    const second = withReplaceDatabase(profile, "target", {}, async () => {});
    assert.ok(beginAttempts >= 2, "second job attempted the SQLite lock");
    assert.equal(existsChecks, 1);
    assert.deepEqual(drops, []);
    release();
    await first;
    await assert.rejects(second, /既に存在/);
    assert.equal(exists, true);
    assert.deepEqual(drops, []);
  } finally {
    release();
    DatabaseSync.prototype.exec = originalExec;
  }
});

test("failed CREATE does not drop a database owned by another job", async () => {
  reset();
  failCreate = true;
  await assert.rejects(withReplaceDatabase(profile, "target", {}, async () => {}), /duplicate database/);
  assert.equal(exists, true);
  assert.deepEqual(drops, []);
});

test("restore failure rolls back the new database and restores the old one", async () => {
  reset();
  exists = true;
  await assert.rejects(withReplaceDatabase(profile, "target", { drop: true }, async () => {
    throw Error("restore failed");
  }), /restore failed/);
  assert.deepEqual(drops, ["target"]);
  assert.equal(renames.length, 2);
  assert.equal(renames[1][1], "target");
  assert.equal(exists, true);
});

test("successful replacement drops the old database only", async () => {
  reset();
  exists = true;
  await withReplaceDatabase(profile, "target", { drop: true }, async () => {});
  assert.deepEqual(drops, [renames[0][1]]);
  assert.equal(exists, true);
});

test("maintenance database names remain valid targets", async () => {
  reset();
  exists = true;
  await withReplaceDatabase(profile, "postgres", { drop: true }, async () => {});
  reset();
  exists = true;
  await withReplaceDatabase(profile, "template1", { drop: true }, async () => {});
});

test("same-second names and sanitized names are distinct; reservation is exclusive", () => {
  const RealDate = Date;
  globalThis.Date = class extends RealDate {
    constructor(...args) { super(...(args.length ? args : ["2026-11-01T00:00:00Z"])); }
  };
  const original = {
    existsSync: fs.existsSync, statSync: fs.statSync, mkdirSync: fs.mkdirSync,
    openSync: fs.openSync, closeSync: fs.closeSync,
  };
  const opened = [];
  try {
    fs.existsSync = () => true;
    fs.statSync = () => ({ isDirectory: () => true });
    fs.mkdirSync = () => {};
    fs.openSync = (name, flags) => { opened.push([name, flags]); return 42; };
    fs.closeSync = () => {};
    const paths = [
      reserveOutPath("shared", profile, "a/b", 1, "plain"),
      reserveOutPath("shared", profile, "a:b", 1, "plain"),
      reserveOutPath("shared", { ...profile, name: "other" }, "a/b", 1, "plain"),
      reserveOutPath(null, profile, "a/b", 1, "plain"),
      reserveOutPath(null, profile, "a:b", 1, "plain"),
    ];
    assert.equal(new Set(paths).size, paths.length);
    assert.ok(opened.every(([, flags]) => flags === "wx"));
  } finally {
    globalThis.Date = RealDate;
    Object.assign(fs, original);
  }
});

test("failed reservation never cleans up an existing explicit path", async () => {
  const original = { existsSync: fs.existsSync, mkdirSync: fs.mkdirSync,
    openSync: fs.openSync, rmSync: fs.rmSync };
  let removed = false;
  const catalog = {
    reserveVersion: () => ({ id: 1, version_no: 1 }),
    abortBackup: () => {}, logOperation: () => {},
  };
  try {
    fs.existsSync = () => false;
    fs.mkdirSync = () => {};
    fs.openSync = () => { const e = Error("exists"); e.code = "EEXIST"; throw e; };
    fs.rmSync = () => { removed = true; };
    await assert.rejects(backupToFile(catalog, profile, "db", { out: "existing.sql" }), /既に存在/);
    assert.equal(removed, false);
  } finally {
    Object.assign(fs, original);
  }
});

test("agent pull uses its reserved output and cleans only that output on failure", async () => {
  const original = {
    existsSync: fs.existsSync, mkdirSync: fs.mkdirSync, openSync: fs.openSync,
    closeSync: fs.closeSync, rmSync: fs.rmSync,
    downloadDatabase: AgentClient.prototype.downloadDatabase,
  };
  let removed = [];
  let reservedOption = false;
  const catalog = {
    reserveVersion: () => ({ id: 1, version_no: 1 }),
    abortBackup: () => {}, logOperation: () => {},
  };
  try {
    fs.existsSync = () => false;
    fs.mkdirSync = () => {};
    fs.openSync = () => 42;
    fs.closeSync = () => {};
    fs.rmSync = (name) => { removed.push(name); };
    AgentClient.prototype.downloadDatabase = async (_remote, _db, _path, options) => {
      reservedOption = options.replaceReserved;
      throw Error("download failed");
    };
    await assert.rejects(backupAgentDatabaseToFile(catalog, profile, "remote", "db", {
      out: "reserved.dump",
    }), /download failed/);
    assert.equal(reservedOption, true);
    assert.deepEqual(removed, ["reserved.dump"]);
  } finally {
    const { downloadDatabase, ...fsOriginal } = original;
    Object.assign(fs, fsOriginal);
    AgentClient.prototype.downloadDatabase = original.downloadDatabase;
  }
});

test("agent response accepts a reserved output only when explicitly marked", async () => {
  const original = {
    existsSync: fs.existsSync, mkdirSync: fs.mkdirSync,
    createWriteStream: fs.createWriteStream, renameSync: fs.renameSync, rmSync: fs.rmSync,
  };
  const renamed = [];
  try {
    fs.existsSync = () => true;
    fs.mkdirSync = () => {};
    fs.createWriteStream = () => {
      const output = new Writable({ write(_chunk, _encoding, done) { done(); } });
      process.nextTick(() => output.emit("open", 42));
      return output;
    };
    fs.renameSync = (source, destination) => { renamed.push([source, destination]); };
    fs.rmSync = () => {};
    const rejectedResponse = Readable.from([Buffer.from("dump")]);
    rejectedResponse.headers = {};
    await assert.rejects(saveResponse(rejectedResponse, "reserved.dump"), /既に存在/);
    const acceptedResponse = Readable.from([Buffer.from("dump")]);
    acceptedResponse.headers = {};
    const result = await saveResponse(acceptedResponse, "reserved.dump", null, true);
    assert.equal(result.size, 4);
    assert.equal(renamed.length, 1);
    assert.equal(renamed[0][1], "reserved.dump");
  } finally {
    Object.assign(fs, original);
  }
});
