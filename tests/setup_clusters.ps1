# E2E テスト用の PostgreSQL クラスタ 2 つを作成・起動・シードする (Windows)
#
# 使い方:
#   powershell -File tests\setup_clusters.ps1 -Root C:\tmp\pgbackup-e2e
#   $env:PGBACKUP_HOME = "C:\tmp\pgbackup-e2e\pghome"
#   python tests\e2e_cli.py
#   python tests\e2e_web.py
#
# 後片付け:
#   pg_ctl -D <Root>\pgA stop; pg_ctl -D <Root>\pgB stop

param(
    [string]$Root = "$env:TEMP\pgbackup-e2e",
    [string]$PgBin = "",
    [int]$PortA = 55432,
    [int]$PortB = 55433
)

$ErrorActionPreference = "Stop"

if (-not $PgBin) {
    $candidates = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\pg_ctl.exe" -ErrorAction SilentlyContinue
    if (-not $candidates) { throw "PostgreSQL が見つかりません。-PgBin を指定してください。" }
    $PgBin = ($candidates | Sort-Object FullName -Descending | Select-Object -First 1).DirectoryName
}
Write-Host "PostgreSQL bin: $PgBin"

New-Item -ItemType Directory -Force $Root | Out-Null
$seed = Join-Path $PSScriptRoot "seed_shop.sql"

foreach ($c in @(@{dir = "pgA"; port = $PortA }, @{dir = "pgB"; port = $PortB })) {
    $data = Join-Path $Root $c.dir
    if (-not (Test-Path "$data\PG_VERSION")) {
        & "$PgBin\initdb.exe" -D $data -U postgres -A trust -E UTF8 --no-locale | Out-Null
    }
    & "$PgBin\pg_ctl.exe" -D $data -l "$data.log" -o "-p $($c.port)" -w start
}

$env:PGCLIENTENCODING = "UTF8"
& "$PgBin\psql.exe" -h localhost -p $PortA -U postgres -c "CREATE DATABASE shop_db" -c "CREATE DATABASE inventory_db"
& "$PgBin\psql.exe" -h localhost -p $PortA -U postgres -d shop_db -v ON_ERROR_STOP=1 -q -f $seed
& "$PgBin\psql.exe" -h localhost -p $PortA -U postgres -d inventory_db -c "CREATE TABLE items(id serial primary key, sku text, qty int); INSERT INTO items(sku, qty) SELECT 'SKU-'||i, i*3 FROM generate_series(1,100) i;"
& "$PgBin\psql.exe" -h localhost -p $PortB -U postgres -c "CREATE DATABASE analytics_db"
& "$PgBin\psql.exe" -h localhost -p $PortB -U postgres -d analytics_db -c "CREATE TABLE events(id bigserial primary key, kind text, payload jsonb, at timestamptz default now()); INSERT INTO events(kind, payload) SELECT 'click', jsonb_build_object('n', i) FROM generate_series(1,300) i;"

Write-Host ""
Write-Host "準備完了: クラスタ A = localhost:$PortA (shop_db, inventory_db), クラスタ B = localhost:$PortB (analytics_db)"
Write-Host "次に実行:  `$env:PGBACKUP_HOME = '$Root\pghome'; python tests\e2e_cli.py"
