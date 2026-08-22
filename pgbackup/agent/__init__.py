"""HTTPS バックアップエージェント。

PostgreSQL のポートを外部公開せず、対象端末上で一時的に起動する専用 API を通じて
ダンプの保管・取得と、明示的に許可されたローカル PostgreSQL 操作を行う。
"""

PROTOCOL_VERSION = 1
