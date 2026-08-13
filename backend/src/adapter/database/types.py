"""DB 非依存の共通カラム型。

SQLite と PostgreSQL で挙動が変わる箇所をここで吸収し、
「DB を差し替えても同じように動く」状態を保つ（TASKS.md §1 備考・T-39）。
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator[datetime]):
    """タイムゾーン付き日時を、必ず UTC で保存し UTC で返す。

    SQLite の DATETIME はオフセットを保持しない（`+09:00` を渡しても naive で
    返る）が、PostgreSQL の TIMESTAMPTZ は保持する。素の `DateTime(timezone=True)`
    を使うと**バックエンドによって読み出し結果が変わる**ため、保存前に UTC へ
    正規化し、読み出し時に UTC を付け直して差を消す。

    表示は呼び出し側で `Settings.tzinfo`（Asia/Tokyo）へ変換する（設計書 §14）。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "タイムゾーンなしの datetime は保存できません。"
                "Settings.tzinfo などで明示してください。"
            )
        return value.astimezone(UTC)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite はオフセットを落とすため、保存時の約束（UTC）を付け直す。
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
