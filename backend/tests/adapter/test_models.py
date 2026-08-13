"""監査ログ・config改訂履歴の ORM モデル。

DB を SQLite から PostgreSQL へ差し替えられる状態を保つことが要件なので
（TASKS.md §1 備考・T-39）、往復とあわせて「PostgreSQL 固有型を使っていない」
「バックエンドで日時の扱いが変わらない」ことも検証する。
"""

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateTable

from adapter.database.base import Base
from adapter.database.models import AuditEventType, AuditLog, ConfigRevision

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


# --- 監査ログ -------------------------------------------------------------


async def test_config_update_round_trips(session: AsyncSession) -> None:
    session.add(
        AuditLog(
            audit_id="aud_0001",
            event_type=AuditEventType.CONFIG_UPDATE,
            actor="admin:admin_a",
            at=datetime(2026, 8, 13, 10, 0, tzinfo=JST),
            revision=2,
            diff={
                "tunable_thresholds.min_total_score_to_publish": {
                    "before": 60,
                    "after": 62,
                }
            },
            target="config.json",
        )
    )
    await session.commit()

    stored = (await session.execute(select(AuditLog))).scalar_one()
    assert stored.event_type == AuditEventType.CONFIG_UPDATE
    assert stored.actor == "admin:admin_a"
    assert stored.revision == 2
    assert stored.diff == {
        "tunable_thresholds.min_total_score_to_publish": {"before": 60, "after": 62}
    }
    assert stored.target == "config.json"
    assert stored.period is None


async def test_run_event_round_trips(session: AsyncSession) -> None:
    session.add(
        AuditLog(
            audit_id="aud_0002",
            event_type=AuditEventType.RUN_START,
            actor="system:scheduler",
            at=datetime(2026, 8, 13, 8, 0, tzinfo=JST),
            revision=2,
            period="2026-W31",
        )
    )
    await session.commit()

    stored = (await session.execute(select(AuditLog))).scalar_one()
    assert stored.event_type == AuditEventType.RUN_START
    assert stored.period == "2026-W31"
    assert stored.diff is None


def test_event_types_match_the_design() -> None:
    assert {e.value for e in AuditEventType} == {
        "config_update",
        "run_start",
        "run_finish",
        "artifact_created",
    }


# --- config 改訂履歴 ------------------------------------------------------


async def test_config_revision_round_trips(session: AsyncSession) -> None:
    snapshot = {"schema_version": "1.0", "meta": {"revision": 1}, "scoring_total": 100}
    session.add(
        ConfigRevision(
            revision=1,
            updated_at=datetime(2026, 8, 13, 0, 0, tzinfo=JST),
            updated_by=None,  # 初期マイグレーション投入時は null（設計書 §10.3）
            config_snapshot=snapshot,
            diff_summary=None,
        )
    )
    await session.commit()

    stored = (await session.execute(select(ConfigRevision))).scalar_one()
    assert stored.revision == 1
    assert stored.updated_by is None
    assert stored.config_snapshot == snapshot


# --- DB 差し替え可能性の担保 ---------------------------------------------


async def test_datetime_keeps_the_same_instant_across_backends(
    session: AsyncSession,
) -> None:
    """SQLite はオフセットを落とすため、UTC 正規化で挙動を揃えている。"""
    at = datetime(2026, 8, 13, 10, 0, tzinfo=JST)
    session.add(
        AuditLog(
            audit_id="aud_tz",
            event_type=AuditEventType.RUN_FINISH,
            actor="system:scheduler",
            at=at,
        )
    )
    await session.commit()

    stored = (await session.execute(select(AuditLog))).scalar_one()
    assert stored.at.tzinfo is not None
    assert stored.at == at  # 同じ瞬間を指す
    assert stored.at.astimezone(JST).hour == 10


async def test_naive_datetime_is_rejected(session: AsyncSession) -> None:
    """タイムゾーンの取り違えを黙って通さない（設計書 §14）。"""
    session.add(
        AuditLog(
            audit_id="aud_naive",
            event_type=AuditEventType.RUN_FINISH,
            actor="system:scheduler",
            at=datetime(2026, 8, 13, 10, 0),
        )
    )
    with pytest.raises(Exception, match="タイムゾーンなし"):
        await session.commit()


@pytest.mark.parametrize("table_name", ["audit_logs", "config_revisions"])
def test_schema_compiles_for_both_backends(table_name: str) -> None:
    """PostgreSQL 固有型に依存していないこと（移行の道を塞がない）。"""
    table = Base.metadata.tables[table_name]
    postgres_ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    sqlite_ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))

    assert "JSONB" not in postgres_ddl
    assert postgres_ddl and sqlite_ddl


def test_no_leftover_scaffold_tables() -> None:
    assert set(Base.metadata.tables) == {"audit_logs", "config_revisions"}
