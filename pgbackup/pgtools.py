"""pg_dump / pg_restore / psql の検出と実行ラッパー。"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from psycopg import conninfo as _pg_conninfo

from .config import Profile
from .errors import ToolError

_IS_WIN = sys.platform == "win32"

# stderr が長大な場合にエラーメッセージへ含める最大文字数
_STDERR_TAIL = 4000


def _version_key(path: str) -> tuple:
    m = re.search(r"(\d+)(?:\.(\d+))?", Path(path).parent.name if Path(path).name.lower() == "bin" else path)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def find_pg_bindir() -> Path:
    """PostgreSQL クライアントツールのディレクトリを探す。

    優先順: 環境変数 PGBACKUP_PG_BIN > PATH 上の pg_dump > OS 標準のインストール先。
    """
    env = os.environ.get("PGBACKUP_PG_BIN")
    if env:
        p = Path(env)
        if (p / ("pg_dump.exe" if _IS_WIN else "pg_dump")).exists():
            return p
        raise ToolError(f"PGBACKUP_PG_BIN={env} に pg_dump が見つかりません")

    which = shutil.which("pg_dump")
    if which:
        return Path(which).parent

    candidates: list[str] = []
    if _IS_WIN:
        for base in (r"C:\Program Files\PostgreSQL", r"C:\Program Files (x86)\PostgreSQL"):
            candidates += glob.glob(os.path.join(base, "*", "bin"))
    else:
        candidates += glob.glob("/usr/lib/postgresql/*/bin")
        candidates += glob.glob("/usr/pgsql-*/bin")
        candidates += ["/opt/homebrew/opt/libpq/bin", "/usr/local/opt/libpq/bin"]
    candidates = [c for c in candidates if Path(c, "pg_dump.exe" if _IS_WIN else "pg_dump").exists()]
    if not candidates:
        raise ToolError(
            "pg_dump が見つかりません。PostgreSQL クライアントツールをインストールするか、"
            "環境変数 PGBACKUP_PG_BIN に bin ディレクトリを設定してください。"
        )
    return Path(max(candidates, key=_version_key))


def tool_path(name: str) -> str:
    exe = f"{name}.exe" if _IS_WIN else name
    return str(find_pg_bindir() / exe)


def _env_for(profile: Profile) -> dict:
    env = os.environ.copy()
    if profile.password is not None:
        env["PGPASSWORD"] = profile.password
    if profile.sslmode:
        env["PGSSLMODE"] = profile.sslmode
    env["PGCONNECT_TIMEOUT"] = "10"
    env["PGCLIENTENCODING"] = "UTF8"
    return env


def _conninfo(profile: Profile, dbname: str) -> str:
    """接続文字列を組み立てる。

    DB 名を pg_dump/psql の位置引数や -d に生で渡すと、'=' を含む名前や
    postgresql:// で始まる名前が conninfo として、'-' で始まる名前がオプションとして
    誤解釈される。make_conninfo で常に完全な conninfo を組み立てて防ぐ。
    パスワードはプロセス一覧に出さないため conninfo に含めず PGPASSWORD 環境変数で渡す。
    """
    kwargs: dict = dict(host=profile.host, port=profile.port, user=profile.user, dbname=dbname)
    if profile.sslmode:
        kwargs["sslmode"] = profile.sslmode
    return _pg_conninfo.make_conninfo(**kwargs)


def _run(cmd: list[str], env: dict) -> None:
    """コマンドを実行し、失敗時は stderr を含めて ToolError を送出する。"""
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-_STDERR_TAIL:]
        raise ToolError(f"{Path(cmd[0]).stem} が失敗しました (exit={proc.returncode}):\n{tail}")


def pg_dump_version() -> str:
    proc = subprocess.run([tool_path("pg_dump"), "--version"], capture_output=True, text=True)
    return proc.stdout.strip()


def dump_to_file(
    profile: Profile, dbname: str, out_path: Path, fmt: str = "plain", keep_owner: bool = True
) -> None:
    """pg_dump でファイルへ出力する。fmt: 'plain' (SQL) または 'custom' (.dump)。

    plain 形式は psql でそのまま流し込むため、keep_owner=False なら
    ダンプ時点で所有者/権限文を除いて移植可能にする
    (custom 形式は pg_restore 側の --no-owner で制御するため常に含めてよい)。
    """
    fmt_flag = "p" if fmt == "plain" else "c"
    cmd = [
        tool_path("pg_dump"),
        "--no-password",
        f"--dbname={_conninfo(profile, dbname)}",
        f"--format={fmt_flag}",
        "--file",
        str(out_path),
    ]
    if fmt == "plain" and not keep_owner:
        cmd += ["--no-owner", "--no-privileges"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run(cmd, _env_for(profile))


def restore_custom(profile: Profile, dbname: str, dump_path: Path, keep_owner: bool = False) -> None:
    """カスタム形式 (.dump) を pg_restore で dbname に復元する。"""
    cmd = [
        tool_path("pg_restore"),
        "--no-password",
        "-d",
        _conninfo(profile, dbname),
    ]
    if not keep_owner:
        cmd += ["--no-owner", "--no-privileges"]
    cmd.append(str(dump_path))
    _run(cmd, _env_for(profile))


def restore_plain(profile: Profile, dbname: str, sql_path: Path) -> None:
    """プレーン SQL ダンプを psql で dbname に流し込む。

    注意: psql はダンプ内のメタコマンド (\\! 等) も実行する。
    信頼できるファイルのみ復元すること (README のセキュリティ注意を参照)。
    """
    cmd = [
        tool_path("psql"),
        "--no-password",
        "--no-psqlrc",
        "-v",
        "ON_ERROR_STOP=1",
        "--quiet",
        "-d",
        _conninfo(profile, dbname),
        "-f",
        str(sql_path),
    ]
    _run(cmd, _env_for(profile))


def pipe_dump_restore(
    src: Profile, src_db: str, dest: Profile, dest_db: str, keep_owner: bool = False
) -> None:
    """pg_dump -Fc | pg_restore で DB を直接複製する (中間ファイルなし)。"""
    dump_cmd = [
        tool_path("pg_dump"), "--no-password",
        f"--dbname={_conninfo(src, src_db)}", "--format=c",
    ]
    restore_cmd = [
        tool_path("pg_restore"), "--no-password",
        "-d", _conninfo(dest, dest_db),
    ]
    if not keep_owner:
        restore_cmd += ["--no-owner", "--no-privileges"]

    # stderr はパイプ詰まりによるデッドロックを避けるため一時ファイルに逃がす
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err1, \
         tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err2:
        p_dump = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, stderr=err1, env=_env_for(src))
        p_restore = subprocess.Popen(restore_cmd, stdin=p_dump.stdout, stderr=err2, env=_env_for(dest))
        p_dump.stdout.close()  # p_restore 側が唯一の読み手になるように
        rc_restore = p_restore.wait()
        rc_dump = p_dump.wait()

        def _tail(f) -> str:
            f.seek(0)
            return f.read().strip()[-_STDERR_TAIL:]

        # pg_restore 側が先に死ぬと pg_dump はパイプ切断 (EPIPE) で巻き添え終了する。
        # 根本原因を見せるため両方のエラーをまとめ、pg_restore を先に報告する。
        if rc_dump != 0 or rc_restore != 0:
            parts = []
            if rc_restore != 0:
                parts.append(f"pg_restore (exit={rc_restore}):\n{_tail(err2)}")
            if rc_dump != 0:
                parts.append(f"pg_dump (exit={rc_dump}):\n{_tail(err1)}")
            raise ToolError("複製に失敗しました:\n" + "\n\n".join(parts))
