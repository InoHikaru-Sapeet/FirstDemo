"""監査ログ。

config 変更・パイプライン実行・成果物生成を「誰が・いつ・何を」の形で残す
（設計書 §4.4、仕様書 §6.1・§14）。config 変更は before→after の差分を伴う。

⚠️ 参照経路は admin 限定にすること。`diff` には config の中身が入るため、
非 admin に見せると「config を admin 以外に露出しない」（仕様書 §2・§6.1）を
監査ログ経由で破ることになる。
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from adapter.database.base import Base
from adapter.database.types import UtcDateTime


class AuditEventType(StrEnum):
    """監査対象のイベント種別（設計書 §4.4）。

    ⚠️ `USER_ROLE_CHANGE` / `USER_STATUS_CHANGE` は **設計書 §4.4 の enum に
    対する追加分**。2026-08-13 の方針変更（自前の ID/PW 認証。TASKS.md §1.1）で
    ロール付与がアプリの操作になったため、T-41（ブートストラップ CLI）と
    T-42（ユーザー管理 API）で追加した。
    `event_type` は文字列カラムなのでマイグレーションは不要だが、**§4.4 の表は
    更新が必要**（→ T-38）。`user_registered` は T-10 が足す。
    """

    CONFIG_UPDATE = "config_update"
    RUN_START = "run_start"
    RUN_FINISH = "run_finish"
    ARTIFACT_CREATED = "artifact_created"
    USER_ROLE_CHANGE = "user_role_change"
    # アカウントの停止・再開（T-42）。admin の停止は実質的な権限剥奪なので、
    # ロール変更と同じ重みで残す。
    USER_STATUS_CHANGE = "user_status_change"


class AuditLog(Base):
    __tablename__ = "audit_logs"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    event_type: Mapped[AuditEventType] = mapped_column(String(32), nullable=False)

    # who: 「ロール:ユーザ識別子」形式（例 admin:admin_a）
    actor: Mapped[str] = mapped_column(String(256), nullable=False)

    # when: UTC で保存し、表示時に Asia/Tokyo へ変換する（設計書 §14）。
    # 時系列で引くのが主用途なので索引を張る。
    at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)

    # 対象 config の revision。実行系イベントでは固定参照していた revision。
    revision: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # config_update のときの before→after 差分。DB 非依存にするため汎用 JSON。
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # 対象成果物のパス（xlsx / HTML / config.json 等）
    target: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # 対象期間（2026-W31 / 2026-07）
    period: Mapped[str | None] = mapped_column(String(16), nullable=True)
