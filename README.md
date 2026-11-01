# pgbackup — PostgreSQL バックアップ / リストア / バージョン管理ツール

PostgreSQL データベースのバックアップ・複製・リストアを **CLI** と **ローカル Web UI** の
両方から操作できるツールです。ローカル⇔リモートの任意の組合せに対応します。

2 つの実装を同梱しています (機能・データ形式は互換):

| 実装 | 場所 | 実行 |
|---|---|---|
| **Python 版** (このドキュメント) | リポジトリ直下 | `pgbackup ...` / `python -m pgbackup ...` |
| **Node.js 版** | [`nodejs/`](nodejs/README.md) | `node nodejs/bin/pgbackup.js ...` |

どちらも同じ `PGBACKUP_HOME` (カタログ・接続プロファイル・アーカイブ形式) を共有できるため、
片方で取ったバックアップをもう片方で一覧・復元できます。

## 主な機能

| 機能 | 説明 |
|---|---|
| DB 一覧表示 | ローカル / リモートサーバー上のデータベース一覧 (所有者・エンコーディング・サイズ) |
| 構造の確認 | スキーマ / テーブル / カラム定義 / インデックス / データのサンプル / 保存されているデータ型の内訳 |
| 視覚的なデータ編集 | Webの型別フォームで行の追加・更新・削除。主キー/一意キーとETagで対象・競合を検証 |
| DB間比較・結合・上書き | 型互換テーブルをキーで比較し、`merge` (追加＋更新) または `sync` (完全同期) をプレビュー後に実行 |
| ファイルバックアップ | pg_dump で SQL ファイル (`.sql`) またはカスタム形式 (`.dump`) に出力 |
| アーカイブバックアップ | 指定サーバー上に **BACK_UP_DB** を自動作成し、すべてのバックアップを 1 つの DB に蓄積 |
| HTTPS バックアップエージェント | PostgreSQL のポートを公開せず、一時起動した専用 HTTPS サーバー経由で別端末へ保存・取得・任意で復元 |
| 複製 (クローン) | DB をそのまま別 DB として複製。**リモート→ローカル / ローカル→リモート / ローカル→ローカル / リモート→リモート** の全方向に対応 |
| 対象の選択 | 指定した DB のみ、複数 DB、または全 DB (`--all`) をバックアップ |
| リストア | バージョン ID・アーカイブ・ダンプファイルのいずれからでも、任意のサーバー / 任意の DB 名へ復元 |
| バージョン管理 | すべてのバックアップに `プロファイル/DB名@v連番` のバージョン ID を付与。sha256 による整合性検証つき |
| 操作履歴 | backup / restore / clone の全操作を記録 |

## 必要環境

- Python 3.10+
- PostgreSQL クライアントツール (`pg_dump` / `pg_restore` / `psql`)
  - 自動検出されます。見つからない場合は環境変数 `PGBACKUP_PG_BIN` に bin ディレクトリを指定
- 接続先 PostgreSQL サーバー (ローカル / リモート)

## インストール

```bash
git clone https://github.com/makamaika/backup-program-postgres.git
cd backup-program-postgres
python -m venv .venv
```

仮想環境を**有効化してから**インストールします (有効化を忘れると、システムの
Python にはライブラリが入っていないため `ModuleNotFoundError: No module named
'psycopg'` になります):

```powershell
# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
pip install -e .
```

```bash
# Linux / macOS
source .venv/bin/activate
pip install -e .
```

有効化中はプロンプトの先頭に `(.venv)` が付き、`pgbackup` コマンド
(または `python -m pgbackup`) が使えます。新しいターミナルを開いたときは
毎回 有効化が必要です。

有効化せずに使いたい場合は、venv 内の実行ファイルをフルパスで指定します:

```powershell
.\.venv\Scripts\pgbackup.exe --help
```

> **注意**: `python -m pgbackup` を仮想環境の外で実行すると `psycopg` が
> 見つからずエラーになります。`python` がどこを指しているかは
> `python -c "import sys; print(sys.executable)"` で確認できます
> (末尾が `.venv\Scripts\python.exe` になっていれば有効化済みです)。

## クイックスタート (CLI)

```bash
# 1. 接続プロファイルを登録 (パスワードは対話入力)
pgbackup conn add local  --host localhost      --port 5432 --user postgres --ask-password
pgbackup conn add remote --host db.example.com --port 5432 --user postgres --ask-password
pgbackup conn test remote

# 2. DB 一覧
pgbackup dbs remote

# 3. 構造・データの確認
pgbackup inspect remote shop_db
pgbackup inspect remote shop_db --table customers --sample 5

# 4. バックアップ
#    (a) SQL ファイルとして出力
pgbackup backup remote --db shop_db --mode file --fmt plain
#    (b) カスタム形式 (圧縮・pg_restore 用)
pgbackup backup remote --db shop_db --mode file --fmt custom --out C:\backups\
#    (c) アーカイブ: ローカルサーバーの BACK_UP_DB に蓄積 (リモート→ローカル)
pgbackup backup remote --db shop_db --mode archive --dest local
#    (d) 複製: リモート DB をローカルにそのままコピー (リモート→ローカル)
pgbackup backup remote --db shop_db --mode clone --dest local
#    (e) 同一サーバー内の別 DB へ複製 (ローカル→ローカル)
pgbackup backup local --db shop_db --mode clone --dest local --dest-db shop_db_copy
#    (f) 全 DB を一括バックアップ
pgbackup backup remote --all --mode archive --dest local
#    (g) PostgreSQL を外部公開せず、別端末のエージェントへ保存
pgbackup backup local --db shop_db --mode agent --agent backup-pc

# 5. バージョン一覧
pgbackup versions

# 6. リストア (任意の環境へ)
pgbackup restore remote/shop_db@v3 --dest local --db shop_db_restored
pgbackup restore remote/shop_db@v3 --dest remote --drop      # 元の場所を上書き
pgbackup restore-file C:\backups\shop_db_v1.sql --dest local --db shop_db_from_file

# 7. アーカイブ DB の直接操作 (別マシンで作ったバックアップも見える)
pgbackup archive list local
pgbackup archive restore local remote/shop_db@v3 --dest local --db shop_db_x

# 8. 操作履歴
pgbackup history

# 9. 行の表示・編集
pgbackup data rows local shop_db customers --limit 20 --json
pgbackup data insert local shop_db customers --values '{"name":"Alice","email":"alice@example.com"}'
pgbackup data update local shop_db customers \
  --key '{"id":1}' --values '{"name":"Alice Updated"}' --etag ETAG

# 10. テーブル比較 (データ変更なし)
pgbackup data compare local shop_db edit_source \
  --target-profile local --target-db shop_db --target-table edit_target --json

# 11. compareで得たplan_hashと確認文字列を使って適用
pgbackup data apply local shop_db edit_source \
  --target-profile local --target-db shop_db --target-table edit_target \
  --strategy merge --plan-hash PLAN_HASH \
  --confirm 'local@localhost:5432/shop_db/public.edit_target'
```

復元・複製は同じ `PGBACKUP_HOME` を使う job 間で直列化します。
別のデータ領域や別ホストから同じ PostgreSQL クラスタを操作する場合は、この排他の対象外です。

## Web UI

```bash
pgbackup web            # http://127.0.0.1:8500/
pgbackup web --port 9000
```

ブラウザから以下が行えます:

- **接続**: プロファイルの追加 / 削除 / 接続テスト
- **バックアップエージェント**: pairing bundle の登録、リモート保管庫、
  エージェント端末の DB 一覧・取得・スナップショット・復元
- **データベース**: DB 一覧 → 構造ビューア
  (スキーマツリー、カラム定義・型・PK・インデックス、データのサンプル表示、
  DB に保存されているデータ型の内訳)
- **データ編集・比較**: テーブル選択 → ページングされた行グリッド → 型別フォームで
  追加・編集・削除。別DB/別接続のテーブルとの差分プレビュー、merge、sync
- **バックアップ**: file / archive / clone / agent を画面から実行 (進捗は右下のジョブパネル)
- **バージョン・リストア**: バージョン一覧・アーカイブの中身・ダンプファイルからのリストア
- **履歴**: 全操作の記録

## バックアップモードの使い分け

| モード | 保存先 | 用途 |
|---|---|---|
| `file` (plain) | `.sql` ファイル | 可読性・他ツールとの連携・長期保管 |
| `file` (custom) | `.dump` ファイル | 圧縮・高速リストア |
| `archive` | 指定サーバーの `BACK_UP_DB` | バックアップを 1 箇所の DB に集約管理 (チャンク分割 bytea + sha256) |
| `clone` | 新しい DB | 検証環境の複製・移行。DB をそのままコピー |
| `agent` | 別端末のファイル保管庫 | PostgreSQL のリモート接続を有効にせず、HTTPS でバックアップを転送 |

## PostgreSQL を公開しない HTTPS バックアップエージェント

通常、別端末から PostgreSQL へ直接接続するには `listen_addresses` と `pg_hba.conf`、
OS ファイアウォールを変更する必要があります。エージェント方式では PostgreSQL は
`localhost` のままにし、対象端末上の `pgbackup agent serve` だけを必要な時間だけ起動します。

### 1. バックアップ先／DB端末で初期化

`--public-url` のホスト名または IP は、操作元端末から実際に接続する名前と一致させます。

```bash
pgbackup agent init \
  --name backup-pc \
  --public-url https://192.168.1.50:8510 \
  --max-upload-gib 100 \
  --max-storage-gib 500
```

生成物:

- `~/.pgbackup/agent/server.json`: サーバー設定。トークンは SHA-256 ハッシュだけを保持
- `agent-cert.pem` / `agent-key.pem`: 自己署名 TLS 証明書と秘密鍵
- `pairing-backup-pc.json`: 接続 URL・秘密トークン・信頼する証明書を含む一時ファイル
- `storage/agent.db`: 保管メタデータだけを保持する SQLite
- `storage/objects/*.dump`: ダンプ本体。巨大 BLOB を SQLite に入れず、ファイルとして保存

リモート側 PostgreSQL の一覧・ダンプ取得も許可する場合だけ `--allow-db-read`、
保管済みダンプをリモート側 PostgreSQL へ戻す場合だけ `--allow-db-restore` を追加します。
**復元権限は既定で無効**です。

### 2. 一時サーバーを起動

```bash
# 127.0.0.1 はローカルテスト用。LAN の別端末から接続するときだけ 0.0.0.0 を明示
pgbackup agent serve --host 0.0.0.0 --port 8510
```

### 3. 操作元へ pairing bundle を安全に渡して登録

USB、既存の SSH/SCP、暗号化済みメッセンジャーなどで `pairing-backup-pc.json` を一度だけ渡します。

```bash
pgbackup agent add pairing-backup-pc.json
pgbackup agent test backup-pc

# 手元の PostgreSQL → 別端末の保管庫
pgbackup backup local --db shop_db --mode agent --agent backup-pc

# エージェント端末の PostgreSQL → 手元のファイル
pgbackup agent dbs backup-pc --remote-profile local
pgbackup agent pull backup-pc --remote-profile local --db shop_db

# エージェント端末内で直接 snapshot (ネットワークへダンプ本体を往復させない)
pgbackup agent snapshot backup-pc --remote-profile local --db shop_db

# 保管済みバックアップをエージェント端末の PostgreSQL へ戻す
pgbackup agent backups backup-pc
pgbackup agent restore backup-pc BACKUP_ID --remote-profile local --db shop_db_restored
```

登録後は `pairing-backup-pc.json` を安全に削除し、作業終了後はサーバーを `Ctrl+C` で停止します。
通常の `pgbackup restore <version-id>` は、`agent` モードのバージョンも自動取得して復元できます。

## データ編集・テーブル比較・merge/sync

### 行編集

- 実テーブルの行を最大200件ずつページ表示します
- `INSERT` は権限があれば利用できます。デフォルト値・NULL・値を列ごとに選択できます
- `UPDATE` / `DELETE` は、主キーまたは「全列 NOT NULL の非遅延 UNIQUE 制約」を
  行キーとして使える場合だけ有効になります。キーがないテーブルは自動的に読み取り専用です
- 表示時に行全体から `etag` を生成し、保存直前に `SELECT ... FOR UPDATE` で再取得して比較します。
  表示後に別ユーザー/処理が変更した行は上書きせず、再読み込みを要求します
- 列名・テーブル名は識別子としてクォートし、入力値はすべてパラメータバインドします。
  Web/CLIから任意SQLを実行する機能はありません
- PostgreSQLの権限、CHECK、FOREIGN KEY、トリガー、Row Level Securityはそのまま適用されます

### 比較と適用

比較キーは両テーブルで非NULLのPRIMARY KEY/UNIQUEである必要があります。列名が同じものを対象に、
同一型のほか、数値型同士、文字列型同士、`json`/`jsonb`を互換として扱います。生成列は
比較・上書き対象外です。既定の比較上限は10,000行、設定可能な最大は50,000行です。

| 操作 | 動作 |
|---|---|
| `compare` | 追加候補・更新候補・比較先だけにある行・同一行を表示。データは変更しない |
| `merge` | 比較元だけにある行を追加し、同じキーで内容が異なる行を更新。比較先だけの行は残す |
| `sync` | mergeに加え、比較先だけにある行も削除し、比較先を比較元と同じ状態にする |

`compare`は元データ・先データ・列構成・接続先ホスト/ポートから`plan_hash`を生成します。
適用時は以下をすべて行います:

1. 比較元をREPEATABLE READの読み取り専用トランザクションで固定
2. 比較先をSERIALIZABLEトランザクションにし、対象テーブルをロック
3. 差分を再計算し、`plan_hash`が変わっていれば中止
4. 接続名・ホスト・ポート・DB・テーブルを含む確認文字列を検証
5. 全INSERT/UPDATE/DELETEを1トランザクションで実行し、1件でも失敗すれば全ロールバック

実効的なRow Level Securityがある場合は全行を確認できないため、行編集はポリシーで見える範囲に
限定し、テーブル全体のcompare/merge/syncは拒否します。

> **重要**: `sync`は比較先だけにある行を削除します。先にバックアップを取得し、Webの
> 差分プレビューまたはCLIの`--json`を確認してください。編集用接続には必要なテーブルだけの
> `SELECT/INSERT/UPDATE/DELETE`を与え、スーパーユーザーを日常編集に使わないことを推奨します。

## バージョン管理の仕組み

- すべての file / archive / agent バックアップは `PGBACKUP_HOME/catalog.db` (SQLite) に登録されます
- バージョン ID: `<プロファイル>/<DB名>@v<連番>` (DB ごとに v1, v2, … と増加)
- 各バックアップに sha256 / サイズ / pg_dump バージョン / メモを記録し、
  リストア時に sha256 で破損チェックを行います
- アーカイブは格納先の `BACK_UP_DB.pgbackup_archive` にもメタデータを持つため、
  カタログのない別マシンからも `pgbackup archive list / restore` で復元できます

## データの保存場所

既定は `~/.pgbackup` (環境変数 `PGBACKUP_HOME` で変更可):

```
~/.pgbackup/
├── connections.json   # 接続プロファイル (パスワードを含む — 権限に注意)
├── agents.json        # pairing 済みエージェント (秘密トークン + 固定 CA 証明書)
├── catalog.db         # バージョンカタログ + 操作履歴 (SQLite)
├── backups/           # file モードの既定出力先 (プロファイル/DB別)
└── tmp/               # アーカイブ転送時の一時ファイル
```

## セキュリティ・安全性に関する注意

- `connections.json` にはパスワードが平文で保存されます。リポジトリにコミットしない
  でください (`.gitignore` 済み)。パスワードを保存したくない場合は `--password` を
  省略し、`PGPASSWORD` 環境変数や `~/.pgpass` を利用してください
- Web UI は既定で `127.0.0.1` のみにバインドされ、認証はありません。
  `--host 0.0.0.0` での公開はしないでください。DNS rebinding / CSRF 対策として Host は
  `127.0.0.1,localhost,::1`、変更系APIは同一Originだけを既定で許可します。特殊な構成で
  追加Hostが必要な場合のみ `PGBACKUP_WEB_ALLOWED_HOSTS` をカンマ区切りで設定してください
- バックアップエージェントは PostgreSQL より狭い専用 API ですが、ネットワークサービスである
  ことに変わりはありません。インターネットへ直接ポート開放せず、同一 LAN、VPN
  (WireGuard/Tailscale 等)、または SSH トンネル内で利用し、ファイアウォールで操作元 IP のみに
  制限してください。必要な時間だけ起動し、終了後は停止してください
- エージェント通信は TLS 1.2 以上、pairing bundle に含まれる証明書の固定、ランダムな
  384bit 相当の Bearer token、認証失敗レート制限で保護します。HTTP URL、別証明書、
  不正トークン、容量超過、SHA-256 不一致は拒否します
- `pairing-*.json` と `agents.json` は秘密トークンを含みます。共有・コミットせず、OS の
  ユーザー権限で保護してください。エージェント側の `agent-key.pem` も秘密情報です
- エージェントのダンプ本体は保存先ディスク上では暗号化されません。機密 DB の場合は
  BitLocker/LUKS/FileVault 等のディスク暗号化、アクセス権、物理保護を併用してください
- `--allow-db-restore` は有効なトークン保有者に DB 入替権限を与えるため、必要なときだけ有効にします
- エージェント側へのリモート復元は `custom` (`PGDMP`) 形式だけを許可します。plain SQL は
  `psql` のメタコマンドで OS コマンドを実行し得るため、ネットワーク越しには復元しません
- 複製および plain 形式のバックアップは既定で `--no-owner --no-privileges` 相当で
  実行します (復元先にロールがなくても失敗しない)。所有者情報も含める場合は
  `--keep-owner`
- `restore-file` で復元する SQL ファイルは信頼できるものだけにしてください
  (psql はファイル内のメタコマンド `\!` などを実行できます)
- 複製・リストアの安全対策:
  - 同一クラスタ判定は `system_identifier` で行うため、`localhost` と `127.0.0.1`
    のような表記ゆれでも「複製元 = 複製先」の事故を防ぎます
  - `--drop` 指定時、既存 DB は削除ではなく一時名に退避され、処理が失敗したら
    自動で元に戻ります (成功時に退避分を削除)
  - リストア時は sha256 でバックアップの破損・すり替えを検知します

## 既知の制限

- 複数マシンから同じアーカイブ DB へ同時にバックアップすると、まれにバージョン番号が
  衝突してどちらかが失敗します (再実行すれば成功します)
- `connections.json` は別プロセス同時書き込みに対しては最後の書き込みが勝ちます
- バックアップ実行中の DB 変更はダンプ開始時点のスナップショットになります
  (PostgreSQL の一貫性スナップショットによる)

## テスト

E2E テスト (要: テスト用 PostgreSQL クラスタ 2 つ):

```powershell
# テストクラスタの例 (Windows)
initdb -D C:\tmp\pgA -U postgres -A trust -E UTF8 --no-locale
initdb -D C:\tmp\pgB -U postgres -A trust -E UTF8 --no-locale
pg_ctl -D C:\tmp\pgA -o "-p 55432" start
pg_ctl -D C:\tmp\pgB -o "-p 55433" start
# (tests/e2e_cli.py 冒頭のコメント参照。shop_db 等のシードが必要)

$env:PGBACKUP_HOME = "C:\tmp\pghome"
python tests\e2e_cli.py   # Python CLI
python tests\e2e_web.py   # Python Web: 16 テスト
python tests\e2e_agent.py # HTTPS agent + Python/Node/Web 相互運用: 15 テスト
python tests\e2e_data.py  # CRUD/型/ETag/比較/merge/sync/Python-Node-Web: 17 テスト
```

Node CLI 35件・Node Web 19件を含め、全スイート合計135件です。

## ライセンス

MIT License (詳細は [LICENSE](LICENSE) を参照)。Python 版・Node.js 版とも共通です。
依存ライブラリのうち psycopg は LGPL-3.0 ですが、通常の依存として利用するのみで
本リポジトリには同梱していません。

## アーキテクチャ

```
pgbackup/
├── config.py    # PGBACKUP_HOME・接続プロファイル (connections.json)
├── db.py        # psycopg 接続・CREATE/DROP DATABASE ヘルパー
├── pgtools.py   # pg_dump / pg_restore / psql の検出と実行、パイプ複製
├── inspect.py   # DB 一覧・構造・データ型内訳・サンプル取得
├── dataedit.py  # 型対応CRUD・ETag・テーブル比較・merge/sync
├── catalog.py   # バージョンカタログ + 操作履歴 (SQLite)
├── archive.py   # BACK_UP_DB への格納 (16MB チャンク bytea + sha256)
├── backup.py    # file / archive / clone / agent の各バックアップモード
├── restore.py   # バージョン / ファイル / アーカイブからのリストア
├── cli.py       # CLI (argparse)
├── agent/
│   ├── certs.py  # 自己署名 TLS 証明書
│   ├── config.py # サーバー設定・pairing・agents.json
│   ├── store.py  # SQLite メタデータ + content-addressed ダンプ保管
│   ├── client.py # 証明書固定 HTTPS クライアント
│   └── server.py # 認証・容量制限付き FastAPI エージェント
└── web/
    ├── server.py        # FastAPI + バックグラウンドジョブ
    └── static/index.html # SPA (vanilla JS)
```
