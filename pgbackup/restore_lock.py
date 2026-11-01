"""同じデータ領域を使う Python / Node job 間の復元排他。"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

from .config import home_dir


@contextmanager
def replacement_lock():
    """復元全体を直列化する。接続終了時に OS がロックを解放する。"""
    conn = sqlite3.connect(home_dir() / "restore-lock.sqlite3", timeout=0, isolation_level=None)
    try:
        while True:
            try:
                conn.execute("BEGIN EXCLUSIVE")
                break
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                time.sleep(0.05)
        yield
    finally:
        conn.close()
