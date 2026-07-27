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
| ファイルバックアップ | pg_dump で SQL ファイル (`.sql`) またはカスタム形式 (`.dump`) に出力 |
| アーカイブバックアップ | 指定サーバー上に **BACK_UP_DB** を自動作成し、すべてのバックアップを 1 つの DB に蓄積 |
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
```

## Web UI

```bash
pgbackup web            # http://127.0.0.1:8500/
pgbackup web --port 9000
```

ブラウザから以下が行えます:

- **接続**: プロファイルの追加 / 削除 / 接続テスト
- **データベース**: DB 一覧 → 構造ビューア
  (スキーマツリー、カラム定義・型・PK・インデックス、データのサンプル表示、
  DB に保存されているデータ型の内訳)
- **バックアップ**: file / archive / clone を画面から実行 (進捗は右下のジョブパネル)
- **バージョン・リストア**: バージョン一覧・アーカイブの中身・ダンプファイルからのリストア
- **履歴**: 全操作の記録

## バックアップモードの使い分け

| モード | 保存先 | 用途 |
|---|---|---|
| `file` (plain) | `.sql` ファイル | 可読性・他ツールとの連携・長期保管 |
| `file` (custom) | `.dump` ファイル | 圧縮・高速リストア |
| `archive` | 指定サーバーの `BACK_UP_DB` | バックアップを 1 箇所の DB に集約管理 (チャンク分割 bytea + sha256) |
| `clone` | 新しい DB | 検証環境の複製・移行。DB をそのままコピー |

## バージョン管理の仕組み

- すべての file / archive バックアップは `PGBACKUP_HOME/catalog.db` (SQLite) に登録されます
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
├── catalog.db         # バージョンカタログ + 操作履歴 (SQLite)
├── backups/           # file モードの既定出力先 (プロファイル/DB別)
└── tmp/               # アーカイブ転送時の一時ファイル
```

## セキュリティ・安全性に関する注意

- `connections.json` にはパスワードが平文で保存されます。リポジトリにコミットしない
  でください (`.gitignore` 済み)。パスワードを保存したくない場合は `--password` を
  省略し、`PGPASSWORD` 環境変数や `~/.pgpass` を利用してください
- Web UI は既定で `127.0.0.1` のみにバインドされ、認証はありません。
  `--host 0.0.0.0` での公開はしないでください
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
python tests\e2e_cli.py   # CLI  : 27 テスト
python tests\e2e_web.py   # Web  : 14 テスト
```

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
├── catalog.py   # バージョンカタログ + 操作履歴 (SQLite)
├── archive.py   # BACK_UP_DB への格納 (16MB チャンク bytea + sha256)
├── backup.py    # file / archive / clone の各バックアップモード
├── restore.py   # バージョン / ファイル / アーカイブからのリストア
├── cli.py       # CLI (argparse)
└── web/
    ├── server.py        # FastAPI + バックグラウンドジョブ
    └── static/index.html # SPA (vanilla JS)
```
