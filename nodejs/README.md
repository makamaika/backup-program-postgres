# pgbackup (Node.js 版)

PostgreSQL バックアップ / リストア / バージョン管理ツールの **Node.js 実装**です。
機能・CLI 体系・Web UI・REST API は [Python 版](../README.md) と同等で、
データディレクトリ (`PGBACKUP_HOME`) の形式も完全互換です。
**Python 版で取ったバックアップを Node 版で復元する (逆も) ことができます。**

## 必要環境

- Node.js 22.5 以上 (`node:sqlite` を使用。動作確認は Node 24)
- PostgreSQL クライアントツール (`pg_dump` / `pg_restore` / `psql`)
  - 自動検出。見つからない場合は環境変数 `PGBACKUP_PG_BIN` に bin ディレクトリを指定

## インストール

```bash
cd nodejs
npm install
# 直接実行
node bin/pgbackup.js --help
# またはグローバルコマンド化 (pgbackup-js)
npm link
```

## 使い方

CLI は Python 版と同じコマンド体系です:

```bash
node bin/pgbackup.js conn add local --host localhost --port 5432 --user postgres --ask-password
node bin/pgbackup.js dbs local                                        # DB 一覧
node bin/pgbackup.js inspect local shop_db                            # 構造・データ型
node bin/pgbackup.js backup local --db shop_db --mode file --fmt plain    # SQL ファイル出力
node bin/pgbackup.js backup local --db shop_db --mode archive --dest remote  # BACK_UP_DB へ蓄積
node bin/pgbackup.js backup local --db shop_db --mode clone --dest remote    # DB 複製
node bin/pgbackup.js versions                                         # バージョン一覧
node bin/pgbackup.js restore local/shop_db@v1 --dest remote --db shop_db_restored
node bin/pgbackup.js web                                              # Web UI (http://127.0.0.1:8501)
```

### HTTPS バックアップエージェント

エージェントサーバーの初期化・起動はPython版の `pgbackup agent init / serve` を使います。
pairing後の保存・取得・復元はNode版からも同じ `agents.json` とプロトコルで操作できます。

```bash
node bin/pgbackup.js agent add pairing-backup-pc.json
node bin/pgbackup.js agent test backup-pc
node bin/pgbackup.js backup local --db shop_db --mode agent --agent backup-pc
node bin/pgbackup.js agent pull backup-pc --remote-profile local --db shop_db
node bin/pgbackup.js agent backups backup-pc
```

TLS証明書固定、トークン、権限、ファイアウォール等の注意事項は
[ルートREADMEのエージェント節](../README.md#postgresql-を公開しない-https-バックアップエージェント)を参照してください。

Web UI のフロントエンドは Python 版と共通の SPA (`src/web/static/index.html`) で、
REST API も互換です。既定ポートのみ異なります (Python: 8500 / Node: 8501)。
両方を同時に起動して同じカタログを操作することもできます。

## モジュール構成

```
nodejs/
├── bin/pgbackup.js      # エントリポイント
├── src/
│   ├── config.js        # PGBACKUP_HOME・接続プロファイル (connections.json)
│   ├── db.js            # pg 接続・CREATE/DROP/RENAME DATABASE・クラスタ同一性判定
│   ├── pgtools.js       # pg_dump / pg_restore / psql の検出と実行、パイプ複製
│   ├── inspect.js       # DB 一覧・構造・データ型内訳・サンプル取得
│   ├── catalog.js       # バージョンカタログ (node:sqlite、Python 版とスキーマ互換)
│   ├── archive.js       # BACK_UP_DB への格納 (16MB チャンク bytea + sha256)
│   ├── agent.js         # Python版エージェント互換 HTTPS クライアント
│   ├── backup.js        # file / archive / clone / agent の各バックアップモード
│   ├── restore.js       # バージョン / ファイル / アーカイブからのリストア
│   ├── cli.js           # CLI (commander)
│   └── web/
│       ├── server.js    # Express + バックグラウンドジョブ
│       └── static/index.html  # SPA (Python 版と共通)
└── tests/               # E2E テスト (CLI 35 件 + Web 19 件)
```

## テスト

Python 版と同じテストクラスタ・シードデータを使います
(セットアップは [../tests/setup_clusters.ps1](../tests/setup_clusters.ps1) を参照):

```powershell
$env:PGBACKUP_HOME = "C:\tmp\pghome-node"
node tests\e2e_cli.test.js   # CLI  : 35 テスト
node tests\e2e_web.test.js   # Web  : 19 テスト
```

セキュリティ・安全性・既知の制限は [Python 版 README](../README.md) の記載と共通です。

## ライセンス

MIT License (リポジトリルートの [LICENSE](../LICENSE) を参照)。
