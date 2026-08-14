"""監査ログ書き込みサービス（T-10。設計書 §4.4 ／ 仕様書 §6.1・§14）。

重点は「破ると監査ログが証拠として使えなくなる」性質:

- **秘密を書かない**（平文パスワード・bcrypt ハッシュ・**セッショントークン**）
- **commit しない**＝呼び出し元のトランザクションに乗る。本処理が失敗したのに
  「変更した」記録だけが残る（またはその逆）を作らない
- **握り潰さない**＝書き込みが失敗したら例外が呼び出し元へ伝播する
- `actor` が `role:subject` 形式（誰の操作か後から追える）
- `at` は tz 付き＝UTC で保存（naive は `UtcDateTime` が拒否する）

加えて、直書きから寄せた3経路（T-41 / T-42 / T-13）と新規の `user_registered`
（T-40）が、**サービス経由でも従来と同じ行を書く**ことを確認する。
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapter.database.base import Base
from adapter.database.models.audit_log import AuditEventType, AuditLog
from application.usecases.audit import AuditService
from enterprise.entities.principal import Principal, Role

AT = datetime(2026, 8, 14, 3, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def audit(db: AsyncSession) -> AuditService:
    return AuditService(db)


async def rows(db: AsyncSession) -> list[AuditLog]:
    return list((await db.execute(select(AuditLog).order_by(AuditLog.at))).scalars())


# --- 基本 -------------------------------------------------------------------


async def test_it_records_the_fields_of_the_design_4_4_schema(
    db: AsyncSession, audit: AuditService
) -> None:
    """設計書 §4.4 のスキーマどおりの行が1件できる。"""
    audit.record(
        event_type=AuditEventType.RUN_START,
        actor="system:cron",
        at=AT,
        revision=3,
        diff=None,
        target="weekly_ai_intelligence_report.xlsx",
        period="2026-W31",
    )
    await db.commit()

    (entry,) = await rows(db)
    assert entry.audit_id.startswith("aud_")
    assert entry.event_type == AuditEventType.RUN_START
    assert entry.actor == "system:cron"
    assert entry.at == AT
    assert entry.revision == 3
    assert entry.target == "weekly_ai_intelligence_report.xlsx"
    assert entry.period == "2026-W31"


async def test_each_entry_gets_its_own_id(
    db: AsyncSession, audit: AuditService
) -> None:
    for _ in range(3):
        audit.record(event_type=AuditEventType.RUN_FINISH, actor="system:cron", at=AT)
    await db.commit()

    assert len({entry.audit_id for entry in await rows(db)}) == 3


async def test_it_does_not_commit(db: AsyncSession, audit: AuditService) -> None:
    """⚠️ **サービスは commit しない。**

    commit すると「config の書き込みは失敗したのに、変更した記録だけが残る」
    （T-13 の順序が壊れる）。rollback で消えることで、呼び出し元の
    トランザクションに乗っていることを示す。
    """
    audit.record(event_type=AuditEventType.RUN_START, actor="system:cron", at=AT)

    await db.rollback()

    assert await rows(db) == []


async def test_a_failure_is_not_swallowed(
    db: AsyncSession, audit: AuditService
) -> None:
    """⚠️ **握り潰さない。** 不正な入力は例外として呼び出し元へ伝播する。

    ここで静かに握って処理を続けると、「本処理は成功したが誰がやったか
    分からない」という追跡不能な状態が生まれる。
    """
    with pytest.raises(ValueError):
        audit.record(event_type=AuditEventType.RUN_START, actor="cron", at=AT)

    assert await rows(db) == []


async def test_a_naive_timestamp_is_rejected(audit: AuditService) -> None:
    """⚠️ tz 無しの時刻を受け付けない（`UtcDateTime` は naive を拒否する）。

    保存は UTC、表示は Asia/Tokyo（設計書 §14）。ここで naive を通すと、
    どのタイムゾーンの時刻か分からない行が残る。
    """
    with pytest.raises(ValueError):
        audit.record(
            event_type=AuditEventType.RUN_START,
            actor="system:cron",
            at=datetime(2026, 8, 14, 3, 0),  # noqa: DTZ001
        )


@pytest.mark.parametrize("actor", ["cron", "", ":subject"])
async def test_the_actor_must_name_who_did_it(audit: AuditService, actor: str) -> None:
    """`role:subject` 形式でなければ拒否（設計書 §4.4）。

    形式が崩れると「誰が」を機械的に取り出せなくなる。
    """
    with pytest.raises(ValueError):
        audit.record(event_type=AuditEventType.RUN_START, actor=actor, at=AT)


async def test_the_principal_actor_is_accepted(audit: AuditService) -> None:
    """`Principal.actor` がそのまま通ること（呼び出し側はこれを渡す）。"""
    audit.record(
        event_type=AuditEventType.CONFIG_UPDATE,
        actor=Principal(subject="usr_abc", role=Role.ADMIN).actor,
        at=AT,
        revision=2,
    )


# --- 秘密の非露出 -------------------------------------------------------------

PLAINTEXT = "correct horse battery staple"
SESSION_TOKEN = "9xQe2fLpTn4YbWc7Rk1sVu8mZaHdJg0oXyPiNvEtCr6"


async def test_the_convenience_recorders_write_no_secrets(
    db: AsyncSession, audit: AuditService
) -> None:
    """⚠️ **平文・ハッシュ・セッショントークンを監査ログに書かない。**

    3種すべてを検査するのが要点。従来のテスト（T-41・T-42）は平文と bcrypt
    ハッシュしか見ておらず、**セッショントークンは未検証だった**（2026-08-14 の
    調査で判明。TASKS.md T-10）。

    監査ログの参照経路は admin 限定にする想定だが、それでも置く理由がない。
    """
    audit.record_user_registered(
        user_id="usr_1", email="a@sapeet.com", role=Role.VIEWER, at=AT
    )
    audit.record_user_role_change(
        actor="admin:usr_0",
        at=AT,
        user_id="usr_1",
        email="a@sapeet.com",
        before=Role.VIEWER,
        after=Role.EDITOR,
    )
    audit.record_user_status_change(
        actor="admin:usr_0",
        at=AT,
        user_id="usr_1",
        email="a@sapeet.com",
        before=True,
        after=False,
    )
    await db.commit()

    serialized = json.dumps(
        [
            {
                "actor": entry.actor,
                "target": entry.target,
                "diff": entry.diff,
                "period": entry.period,
            }
            for entry in await rows(db)
        ],
        ensure_ascii=False,
    )
    assert PLAINTEXT not in serialized
    assert "$2b$" not in serialized
    assert SESSION_TOKEN not in serialized
    assert "password" not in serialized.lower()
    assert "token" not in serialized.lower()


async def test_the_registration_recorder_takes_no_password_argument() -> None:
    """⚠️ **署名の時点でパスワードを受け取らない。**

    「渡さないよう気をつける」ではなく「渡せない」形にしておく。
    """
    import inspect

    parameters = inspect.signature(AuditService.record_user_registered).parameters
    assert set(parameters) == {"self", "user_id", "email", "role", "at"}


# --- イベント種別 -------------------------------------------------------------


def test_the_event_types_cover_the_design_and_the_added_ones() -> None:
    """⚠️ 設計書 §4.4 の4種＋方針変更で増えた3種。

    増えた分（`user_registered` / `user_role_change` / `user_status_change`）は
    **§4.4 の enum に対する差分**なので、設計書の表を更新する必要がある（→ T-38）。
    """
    assert {event.value for event in AuditEventType} == {
        # 設計書 §4.4
        "config_update",
        "run_start",
        "run_finish",
        "artifact_created",
        # 2026-08-13 の方針変更（自前 ID/PW 認証）で増えた分
        "user_registered",
        "user_role_change",
        "user_status_change",
    }


def test_login_events_are_not_audit_events() -> None:
    """⚠️ ログイン成功・失敗は監査ログに入れない（アプリログの担当）。

    件数が多く `audit_log` の粒度と合わない（TASKS.md T-10）。ここに
    `login_*` が現れたら、方針が変わったか取り違えたかのどちらか。
    """
    assert not [event for event in AuditEventType if "login" in event.value]


# --- 便宜メソッドが書く内容 ---------------------------------------------------


async def test_a_config_update_carries_the_revision_and_the_diff(
    db: AsyncSession, audit: AuditService
) -> None:
    """`config_update` は **revision と before→after の差分**を伴う（§4.4）。"""
    diff = {
        "tunable_thresholds.min_total_score_to_publish": {"before": 60, "after": 62}
    }
    audit.record_config_update(
        actor="admin:usr_a", at=AT, revision=2, diff=diff, target="config.json"
    )
    await db.commit()

    (entry,) = await rows(db)
    assert entry.event_type == AuditEventType.CONFIG_UPDATE
    assert entry.revision == 2
    assert entry.diff == diff
    assert entry.target == "config.json"


async def test_a_registration_records_the_new_account(
    db: AsyncSession, audit: AuditService
) -> None:
    """自己登録は **viewer として作られたこと**が残る（T-40）。"""
    audit.record_user_registered(
        user_id="usr_new", email="new@sapeet.com", role=Role.VIEWER, at=AT
    )
    await db.commit()

    (entry,) = await rows(db)
    assert entry.event_type == AuditEventType.USER_REGISTERED
    assert entry.target == "usr_new"
    # 行為者は本人（まだセッションは無いが、登録したのは本人）。
    assert entry.actor == "viewer:usr_new"
    assert entry.diff == {"email": "new@sapeet.com", "role": "viewer"}


async def test_a_role_change_keeps_both_sides(
    db: AsyncSession, audit: AuditService
) -> None:
    """before→after の両方を残す（片方だけでは何が起きたか分からない）。"""
    audit.record_user_role_change(
        actor="admin:usr_a",
        at=AT,
        user_id="usr_b",
        email="b@sapeet.com",
        before=Role.VIEWER,
        after=Role.ADMIN,
    )
    await db.commit()

    (entry,) = await rows(db)
    assert entry.diff == {
        "role": {"before": "viewer", "after": "admin"},
        "email": "b@sapeet.com",
    }


async def test_a_creation_is_recorded_as_a_role_change_from_nothing(
    db: AsyncSession, audit: AuditService
) -> None:
    """`before=None` は「その操作が作成した」の意（T-41 の初期 admin）。"""
    audit.record_user_role_change(
        actor="cli:create-admin",
        at=AT,
        user_id="usr_first",
        email="first@sapeet.com",
        before=None,
        after=Role.ADMIN,
    )
    await db.commit()

    (entry,) = await rows(db)
    assert entry.actor == "cli:create-admin"
    assert entry.diff == {
        "role": {"before": None, "after": "admin"},
        "email": "first@sapeet.com",
    }


async def test_a_status_change_keeps_both_sides(
    db: AsyncSession, audit: AuditService
) -> None:
    """admin の停止は実質的な権限剥奪なので、降格と同じ重みで残す（T-42）。"""
    audit.record_user_status_change(
        actor="admin:usr_a",
        at=AT,
        user_id="usr_b",
        email="b@sapeet.com",
        before=True,
        after=False,
    )
    await db.commit()

    (entry,) = await rows(db)
    assert entry.event_type == AuditEventType.USER_STATUS_CHANGE
    assert entry.diff == {
        "is_active": {"before": True, "after": False},
        "email": "b@sapeet.com",
    }


# --- 直書きが残っていないこと -------------------------------------------------


def test_no_usecase_builds_an_audit_log_directly() -> None:
    """⚠️ **`AuditLog(...)` を直接組み立てる経路がこのサービス以外に無いこと。**

    直書きが復活すると、`actor` の表記・`diff` の形・「何を書かないか」の約束が
    箇所ごとにずれる（実際 T-41・T-42・T-13 の3箇所でずれていた。TASKS.md T-10）。
    """
    from pathlib import Path as _Path

    usecases = _Path(__file__).parents[2] / "src" / "application" / "usecases"
    offenders = [
        path.name
        for path in usecases.glob("*.py")
        if path.name != "audit.py" and "AuditLog(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
