"""共通例外定義。"""


class PgBackupError(Exception):
    """pgbackup のすべての想定内エラーの基底クラス。"""


class ProfileError(PgBackupError):
    """接続プロファイルが見つからない・不正。"""


class ToolError(PgBackupError):
    """pg_dump / pg_restore / psql の実行失敗。"""


class BackupError(PgBackupError):
    """バックアップ処理の失敗。"""


class RestoreError(PgBackupError):
    """リストア処理の失敗。"""
