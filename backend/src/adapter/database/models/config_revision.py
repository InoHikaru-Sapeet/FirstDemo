"""config.json の改訂履歴。

`config.json` 自体はファイルが正だが、改訂履歴は DB で管理する
（設計書 §6.3・§4.3）。用途は2つ:

- `GET /config/history`（設計書 §3.3）が返す一覧
- 実行中ジョブが「開始時点の revision」を固定参照するためのスナップショット
  （設計書 §6.3・§14。実行中に config が変わっても切り替わらないこと）

⚠️ `config_snapshot` は config の中身そのもの。参照経路は admin 限定にすること。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from adapter.database.base import Base
from adapter.database.types import UtcDateTime


class ConfigRevision(Base):
    __tablename__ = "config_revisions"

    # config.json の meta.revision と一致する。楽観ロックの比較対象（設計書 §4.3）。
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)

    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    # 初期マイグレーション投入時は null（設計書 §10.3 手順6）
    updated_by: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # この revision 時点の config 全体。DB 非依存にするため汎用 JSON。
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # 一覧表示用の要約（例: "min_total_score_to_publish 60→62"）
    diff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
