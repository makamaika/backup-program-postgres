// 同じ PGBACKUP_HOME を使う Python / Node job 間の復元排他。
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

import { homeDir } from "./config.js";

export async function withReplacementLock(fn) {
  const db = new DatabaseSync(path.join(homeDir(), "restore-lock.sqlite3"));
  try {
    db.exec("PRAGMA busy_timeout = 0");
    for (;;) {
      try {
        db.exec("BEGIN EXCLUSIVE");
        break;
      } catch (e) {
        if (!/database is locked|SQLITE_BUSY/i.test(e.message)) throw e;
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }
    return await fn();
  } finally {
    db.close();
  }
}
