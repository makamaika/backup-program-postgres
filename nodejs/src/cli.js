// pgbackup CLI (Node.js 版)。
//
// 使用例:
//   pgbackup-js conn add local --host localhost --port 5432 --user postgres --ask-password
//   pgbackup-js dbs local
//   pgbackup-js inspect local shop_db
//   pgbackup-js backup local --db shop_db --mode file --fmt plain
//   pgbackup-js backup local --db shop_db --mode archive --dest remote
//   pgbackup-js backup local --db shop_db --mode clone --dest remote --dest-db shop_db_copy
//   pgbackup-js versions
//   pgbackup-js restore local/shop_db@v1 --dest remote --db shop_db_restored
//   pgbackup-js web

import readline from "node:readline";

import { Command, InvalidArgumentError } from "commander";

import * as archiveMod from "./archive.js";
import * as backupMod from "./backup.js";
import * as inspectMod from "./inspect.js";
import * as restoreMod from "./restore.js";
import { Catalog } from "./catalog.js";
import {
  DEFAULT_ARCHIVE_DB, ProfileStore, cleanupTmp, makeProfile, profileDisplay, profileMasked,
} from "./config.js";
import { serverVersion } from "./db.js";
import { PgBackupError } from "./errors.js";

const VERSION = "0.1.0";

// ---------------------------------------------------------------- 表示ヘルパー

function displayWidth(s) {
  // 全角文字を 2 桁として数える (表の桁揃え用の簡易判定)
  let w = 0;
  for (const ch of String(s)) {
    w += /[ᄀ-ᅟ⺀-꓏가-힣豈-﫿＀-｠￠-￦]/.test(ch) ? 2 : 1;
  }
  return w;
}

function padDisplay(s, width) {
  return String(s) + " ".repeat(Math.max(0, width - displayWidth(s)));
}

// rows を列定義 [[キー, 見出し], ...] に従って整形表示する
function printTable(rows, columns) {
  if (!rows.length) {
    console.log("(なし)");
    return;
  }
  const data = rows.map((r) => columns.map(([k]) => (r[k] === null || r[k] === undefined ? "" : String(r[k]))));
  const headers = columns.map(([, h]) => h);
  const widths = headers.map((h, i) => Math.max(displayWidth(h), ...data.map((row) => displayWidth(row[i]))));
  const headerLine = headers.map((h, i) => padDisplay(h, widths[i])).join("  ");
  console.log(headerLine);
  console.log("-".repeat(displayWidth(headerLine)));
  for (const row of data) {
    console.log(row.map((c, i) => padDisplay(c, widths[i])).join("  "));
  }
}

function sizeFmt(n) {
  if (n === null || n === undefined) return "";
  let x = Number(n);
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (x < 1024 || u === "TB") return u === "B" ? `${Math.round(x)}B` : `${x.toFixed(1)}${u}`;
    x /= 1024;
  }
  return "";
}

// パスワードの非表示入力。
// - マスクは stdin が TTY かどうかで判定する (stdout リダイレクト時も平文表示しない)
// - Ctrl+C / EOF では未解決 Promise で黙って終了せず、エラーとして中断する
function askHidden(promptText) {
  return new Promise((resolve, reject) => {
    const rl = readline.createInterface({
      input: process.stdin,
      output: process.stderr,
      terminal: process.stdin.isTTY === true,
    });
    let answered = false;
    if (rl.terminal) {
      const orig = rl._writeToOutput.bind(rl);
      rl._writeToOutput = (str) => {
        if (str.includes(promptText)) orig(promptText);
      };
    }
    rl.on("SIGINT", () => rl.close());
    rl.on("close", () => {
      if (!answered) reject(new PgBackupError("パスワード入力が中断されました"));
    });
    rl.question(promptText, (answer) => {
      answered = true;
      rl.close();
      process.stderr.write("\n");
      resolve(answer);
    });
  });
}

// commander 用の数値バリデータ
function intArg(label, min, max) {
  return (value) => {
    const n = Number(value);
    if (!/^\d+$/.test(value) || !Number.isInteger(n) || n < min || n > max) {
      throw new InvalidArgumentError(`${label} は ${min}〜${max} の整数で指定してください`);
    }
    return n;
  };
}

function collect(value, previous) {
  return previous.concat([value]);
}

// ---------------------------------------------------------------- コマンド本体

export async function main(argv) {
  const store = new ProfileStore();
  const catalog = new Catalog();
  cleanupTmp();

  const program = new Command();
  program
    .name("pgbackup-js")
    .description("PostgreSQL バックアップ / リストア / バージョン管理ツール (Node.js 版)")
    .version(`pgbackup-node ${VERSION}`);

  // ---- conn ----
  const conn = program.command("conn").description("接続プロファイルの管理");
  conn
    .command("add <name>")
    .description("プロファイルを追加")
    .option("--host <host>", "ホスト", "localhost")
    .option("--port <port>", "ポート", intArg("ポート", 1, 65535), 5432)
    .option("--user <user>", "ユーザー", "postgres")
    .option("--password <password>", "パスワード (履歴に残るため --ask-password 推奨)")
    .option("--ask-password", "対話的にパスワードを入力")
    .option("--dbname <dbname>", "メンテナンス接続用 DB", "postgres")
    .option("--sslmode <mode>", "require / verify-full など")
    .option("--overwrite", "既存プロファイルを上書き")
    .action(async (name, opts) => {
      let password = opts.password ?? null;
      if (opts.askPassword) password = await askHidden("パスワード: ");
      const profile = makeProfile(name, {
        host: opts.host, port: opts.port, user: opts.user,
        password, dbname: opts.dbname, sslmode: opts.sslmode ?? null,
      });
      store.add(profile, Boolean(opts.overwrite));
      console.log(`接続プロファイル '${name}' を保存しました (${profileDisplay(profile)})`);
      console.log(`  保存先: ${store.path} (パスワードを含むため取り扱いに注意)`);
    });
  conn.command("list").description("一覧").action(() => {
    printTable(store.list().map(profileMasked), [
      ["name", "名前"], ["host", "ホスト"], ["port", "ポート"],
      ["user", "ユーザー"], ["dbname", "メンテDB"], ["password", "パスワード"],
    ]);
  });
  conn.command("remove <name>").description("削除").action((name) => {
    store.remove(name);
    console.log(`接続プロファイル '${name}' を削除しました`);
  });
  conn.command("test <name>").description("接続テスト").action(async (name) => {
    const profile = store.get(name);
    const ver = await serverVersion(profile);
    console.log(`OK: ${profileDisplay(profile)} (PostgreSQL ${ver})`);
  });

  // ---- dbs ----
  program
    .command("dbs <profile>")
    .description("サーバー上のデータベース一覧")
    .option("--templates", "テンプレート DB も表示")
    .action(async (name, opts) => {
      const rows = await inspectMod.listDatabases(store.get(name), Boolean(opts.templates));
      printTable(rows, [
        ["name", "データベース"], ["owner", "所有者"], ["encoding", "エンコーディング"], ["size", "サイズ"],
      ]);
    });

  // ---- inspect ----
  program
    .command("inspect <profile> <db>")
    .description("DB の構造・データ型の確認")
    .option("--schema <schema>", "スキーマで絞り込み")
    .option("--table <table>", "テーブルの詳細 (カラム・インデックス) を表示")
    .option("--sample <n>", "--table 指定時にデータの先頭 N 行を表示", intArg("サンプル行数", 0, 200), 0)
    .action(async (name, db, opts) => {
      const profile = store.get(name);
      if (opts.table) {
        const schema = opts.schema || "public";
        console.log(`== ${db}.${schema}.${opts.table} のカラム ==`);
        printTable(await inspectMod.tableColumns(profile, db, schema, opts.table), [
          ["name", "カラム"], ["data_type", "型"], ["nullable", "NULL可"],
          ["default", "デフォルト"], ["is_primary_key", "PK"],
        ]);
        console.log("\n== インデックス ==");
        printTable(await inspectMod.tableIndexes(profile, db, schema, opts.table), [
          ["name", "名前"], ["definition", "定義"],
        ]);
        const n = opts.sample;
        if (n > 0) {
          console.log(`\n== 先頭 ${n} 行 ==`);
          const sample = await inspectMod.tableSample(profile, db, schema, opts.table, n);
          const rows = sample.rows.map((r) => Object.fromEntries(sample.columns.map((c, i) => [c, r[i]])));
          printTable(rows, sample.columns.map((c) => [c, c]));
          console.log(`(総行数: ${sample.total_rows}${sample.approx ? " [推定]" : ""})`);
        }
        return;
      }
      console.log(`== ${db} のテーブル一覧 ==`);
      printTable(await inspectMod.listTables(profile, db, opts.schema ?? null), [
        ["schema", "スキーマ"], ["name", "名前"], ["kind", "種別"],
        ["est_rows", "推定行数"], ["size", "サイズ"],
      ]);
      console.log("\n== 保存されているデータ型の内訳 ==");
      printTable(await inspectMod.datatypeSummary(profile, db), [
        ["data_type", "型"], ["column_count", "カラム数"], ["table_count", "テーブル数"],
      ]);
    });

  // ---- backup ----
  program
    .command("backup <profile>")
    .description("バックアップの実行 (file / archive / clone)")
    .option("--db <name>", "対象 DB (複数指定可)", collect, [])
    .option("--all", "テンプレート以外の全 DB を対象にする")
    .option("--mode <mode>", "file | archive | clone", "file")
    .option("--fmt <fmt>", "file モードの形式 (plain=SQL ファイル [既定], custom=pg_restore 用)")
    .option("--out <path>", "file モードの出力先 (ファイルまたはディレクトリ)")
    .option("--dest <profile>", "archive/clone モードの保存先プロファイル (archive の既定は元と同じ)")
    .option("--dest-db <name>", "clone モードの複製先 DB 名 (既定: 元と同名)")
    .option("--archive-db <name>", `archive モードの格納 DB 名 (既定: ${DEFAULT_ARCHIVE_DB})`, DEFAULT_ARCHIVE_DB)
    .option("--drop", "clone: 複製先に同名 DB があれば削除して作り直す")
    .option("--keep-owner", "clone / file(plain): 所有者・権限情報も含める (既定は移植性優先で除外)")
    .option("--note <note>", "バージョンに付けるメモ")
    .action(async (name, opts) => {
      const source = store.get(name);
      const dbs = await backupMod.resolveTargetDbs(source, opts.db, Boolean(opts.all), opts.archiveDb);
      const fmt = backupMod.checkModeOptions(opts.mode, {
        fmt: opts.fmt ?? null, out: opts.out ?? null, dest: opts.dest ?? null,
        destDb: opts.destDb ?? null, drop: Boolean(opts.drop),
        keepOwner: Boolean(opts.keepOwner), nDbs: dbs.length,
      });

      if (opts.mode === "clone") {
        const dest = store.get(opts.dest);
        for (const db of dbs) {
          const destDb = opts.destDb || db;
          await backupMod.cloneDatabase(catalog, source, db, dest, destDb, {
            drop: Boolean(opts.drop), keepOwner: Boolean(opts.keepOwner), note: opts.note ?? null,
          });
          console.log(`複製完了: ${source.name}:${db} -> ${dest.name}:${destDb}`);
        }
      } else if (opts.mode === "archive") {
        const dest = opts.dest ? store.get(opts.dest) : source;
        for (const db of dbs) {
          const rec = await backupMod.backupToArchive(catalog, source, db, dest, {
            archiveDb: opts.archiveDb, note: opts.note ?? null,
          });
          console.log(`アーカイブ保存: ${rec.version_id} -> ${dest.name}:${opts.archiveDb} (${sizeFmt(rec.size)})`);
        }
      } else {
        for (const db of dbs) {
          const rec = await backupMod.backupToFile(catalog, source, db, {
            fmt, out: opts.out ?? null, note: opts.note ?? null, keepOwner: Boolean(opts.keepOwner),
          });
          console.log(`バックアップ完了: ${rec.version_id}`);
          console.log(`  ファイル: ${rec.location} (${sizeFmt(rec.size)})`);
        }
      }
    });

  // ---- versions ----
  program
    .command("versions")
    .description("バックアップのバージョン一覧")
    .option("--profile <name>")
    .option("--db <name>")
    .action((opts) => {
      const rows = catalog.listBackups(opts.profile ?? null, opts.db ?? null).map((r) => ({
        ...r,
        size_h: sizeFmt(r.size),
        place: r.mode === "file" ? r.location : `${r.archive_profile}:${r.archive_db}`,
      }));
      printTable(rows, [
        ["version_id", "バージョン"], ["mode", "モード"], ["fmt", "形式"],
        ["size_h", "サイズ"], ["created_at", "作成日時 (UTC)"], ["place", "保存先"], ["note", "メモ"],
      ]);
    });

  // ---- restore ----
  program
    .command("restore <versionId>")
    .description("バージョン ID を指定してリストア (例: local/shop_db@v3)")
    .requiredOption("--dest <profile>", "復元先プロファイル")
    .option("--db <name>", "復元先 DB 名 (既定: バックアップ元と同名)")
    .option("--drop", "復元先に同名 DB があれば削除して作り直す")
    .option("--keep-owner")
    .action(async (versionId, opts) => {
      const dest = store.get(opts.dest);
      const r = await restoreMod.restoreVersion(catalog, store, versionId, dest, {
        destDb: opts.db ?? null, drop: Boolean(opts.drop), keepOwner: Boolean(opts.keepOwner),
      });
      console.log(`リストア完了: ${versionId} -> ${dest.name}:${r.dest_db}`);
    });

  // ---- restore-file ----
  program
    .command("restore-file <path>")
    .description("ダンプファイルを直接リストア (信頼できるファイルのみ)")
    .requiredOption("--dest <profile>")
    .requiredOption("--db <name>")
    .option("--drop")
    .option("--keep-owner")
    .action(async (filePath, opts) => {
      const dest = store.get(opts.dest);
      const r = await restoreMod.restoreDumpFile(catalog, filePath, dest, opts.db, {
        drop: Boolean(opts.drop), keepOwner: Boolean(opts.keepOwner),
      });
      console.log(`リストア完了: ${filePath} (${r.fmt}) -> ${dest.name}:${opts.db}`);
    });

  // ---- archive ----
  const arch = program.command("archive").description("アーカイブ DB (BACK_UP_DB) の直接操作");
  arch
    .command("list <profile>")
    .description("アーカイブ内のバックアップ一覧")
    .option("--archive-db <name>", "", DEFAULT_ARCHIVE_DB)
    .action(async (name, opts) => {
      const rows = (await archiveMod.listArchive(store.get(name), opts.archiveDb)).map((r) => ({
        ...r,
        size_h: sizeFmt(r.size),
        created_at: r.created_at instanceof Date ? r.created_at.toISOString() : r.created_at,
      }));
      printTable(rows, [
        ["version_id", "バージョン"], ["source_db", "元DB"], ["size_h", "サイズ"],
        ["created_at", "作成日時"], ["note", "メモ"],
      ]);
    });
  arch
    .command("restore <profile> <versionId>")
    .description("アーカイブから直接リストア (別マシンで作成した分も可)")
    .option("--archive-db <name>", "", DEFAULT_ARCHIVE_DB)
    .requiredOption("--dest <destProfile>")
    .option("--db <name>")
    .option("--drop")
    .option("--keep-owner")
    .action(async (name, versionId, opts) => {
      const host = store.get(name);
      const dest = store.get(opts.dest);
      const r = await restoreMod.restoreFromArchive(catalog, host, opts.archiveDb, versionId, dest, {
        destDb: opts.db ?? null, drop: Boolean(opts.drop), keepOwner: Boolean(opts.keepOwner),
      });
      console.log(`リストア完了: ${versionId} -> ${dest.name}:${r.dest_db}`);
    });
  arch
    .command("delete <profile> <versionId>")
    .description("アーカイブからバックアップを削除")
    .option("--archive-db <name>", "", DEFAULT_ARCHIVE_DB)
    .action(async (name, versionId, opts) => {
      const profile = store.get(name);
      await archiveMod.deleteArchiveRow(profile, opts.archiveDb, versionId);
      console.log(`アーカイブから削除しました: ${versionId}`);
      // ローカルカタログに対応するバージョンがあれば同期して消す (孤児エントリ防止)
      let rec = null;
      try {
        rec = catalog.getBackup(versionId);
      } catch { /* カタログ未登録 */ }
      if (rec && rec.mode === "archive"
          && rec.archive_profile === profile.name && rec.archive_db === opts.archiveDb) {
        catalog.deleteBackup(versionId);
        console.log(`カタログからも削除しました: ${versionId}`);
      }
    });

  // ---- history ----
  program
    .command("history")
    .description("操作履歴の表示")
    .option("--limit <n>", "表示件数", intArg("limit", 1, 10000), 50)
    .action((opts) => {
      const rows = catalog.listOperations(opts.limit).map((r) => ({
        ...r, ok_h: r.ok ? "OK" : "失敗",
      }));
      printTable(rows, [
        ["created_at", "日時 (UTC)"], ["kind", "操作"], ["ok_h", "結果"], ["message", "内容"],
      ]);
    });

  // ---- web ----
  program
    .command("web")
    .description("ローカル Web UI を起動")
    .option("--host <host>", "バインド先 (公開しないこと)", "127.0.0.1")
    .option("--port <port>", "ポート", intArg("ポート", 1, 65535), 8501)
    .action(async (opts) => {
      const { createApp } = await import("./web/server.js");
      const app = createApp();
      await new Promise((resolve, reject) => {
        const server = app.listen(opts.port, opts.host, () => {
          console.log(`Web UI: http://${opts.host}:${opts.port}/  (Ctrl+C で終了)`);
        });
        // ポート使用中などの listen 失敗を生スタックではなくメッセージで伝える
        server.on("error", (e) => reject(new PgBackupError(`Web サーバーを起動できません: ${e.message}`)));
      }); // サーバー稼働中は戻らない
    });

  try {
    await program.parseAsync(argv);
    catalog.close();
  } catch (e) {
    if (e instanceof PgBackupError) {
      console.error(`エラー: ${e.message}`);
      process.exitCode = 1;
    } else {
      throw e;
    }
  }
}
