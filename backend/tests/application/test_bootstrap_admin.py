"""最初の admin のブートストラップ（T-41）。

重点は「破るとロール付与の建前が壊れる」性質:

- **admin が居るなら CLI では作らせない**（常用させない）
- 再実行しても行を二重に作らない・既存ユーザーを黙って書き換えない
- 昇格は**べき等**（既に admin なら何も書かない）
- ロール変更が監査ログに残り、そこに**パスワードが入らない**
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from adapter.database.base import Base
from adapter.database.models.audit_log import AuditEventType, AuditLog
from adapter.database.models.user import User
from application.usecases import bootstrap_admin as bootstrap_module
from application.usecases.bootstrap_admin import (
    CLI_ACTOR,
    BootstrapAdminError,
    BootstrapAdminUsecase,
    BootstrapErrorCode,
    BootstrapOutcome,
)
from enterprise.entities.principal import Role
from enterprise.services.password import (
    PasswordPolicyError,
    hash_password,
    verify_password,
)

PASSWORD = "correct horse battery staple"
EMAIL = "admin@sapeet.com"
DISPLAY_NAME = "管理 太郎"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def usecase(db: AsyncSession) -> BootstrapAdminUsecase:
    return BootstrapAdminUsecase(db)


async def add_user(
    db: AsyncSession,
    email: str,
    role: Role = Role.VIEWER,
    is_active: bool = True,
) -> User:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    user = User(
        user_id=f"usr_{email}",
        email=email,
        display_name="既存 花子",
        password_hash=hash_password(PASSWORD),
        role=role,
        is_active=is_active,
        created_at=now,
        updated_at=now,
        password_updated_at=now,
        failed_login_attempts=0,
        locked_until=None,
    )
    db.add(user)
    await db.commit()
    return user


async def audit_rows(db: AsyncSession) -> list[AuditLog]:
    return list((await db.execute(select(AuditLog))).scalars())


async def user_count(db: AsyncSession) -> int:
    return len(list((await db.execute(select(User))).scalars()))


# --- 作成 -----------------------------------------------------------------


async def test_the_first_admin_is_created(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    user = await usecase.create_initial_admin(
        email=EMAIL, display_name=DISPLAY_NAME, password=PASSWORD
    )

    stored = (await db.execute(select(User))).scalar_one()
    assert stored.user_id == user.user_id
    assert stored.role == Role.ADMIN
    assert stored.is_active is True
    assert stored.display_name == DISPLAY_NAME


async def test_the_created_admin_can_authenticate_with_the_given_password(
    usecase: BootstrapAdminUsecase,
) -> None:
    user = await usecase.create_initial_admin(
        email=EMAIL, display_name=DISPLAY_NAME, password=PASSWORD
    )

    assert verify_password(PASSWORD, user.password_hash) is True
    # 平文は保存されない（ハッシュだけ）。
    assert PASSWORD not in user.password_hash


async def test_the_email_is_normalised(usecase: BootstrapAdminUsecase) -> None:
    """`Admin@…` / `admin@…` の二重登録を防ぐ（T-08 備考の約束）。"""
    user = await usecase.create_initial_admin(
        email="  Admin@Sapeet.com  ", display_name=DISPLAY_NAME, password=PASSWORD
    )

    assert user.email == "admin@sapeet.com"


async def test_display_name_whitespace_is_trimmed(
    usecase: BootstrapAdminUsecase,
) -> None:
    user = await usecase.create_initial_admin(
        email=EMAIL, display_name=f"  {DISPLAY_NAME}  ", password=PASSWORD
    )

    assert user.display_name == DISPLAY_NAME


# --- 「最初の1人だけ」の担保 ----------------------------------------------


async def test_a_second_admin_cannot_be_created(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    """⚠️ CLI をロール付与の常用手段にしない（正規の昇格は T-42 の API）。"""
    await add_user(db, "first@sapeet.com", role=Role.ADMIN)

    with pytest.raises(BootstrapAdminError) as exc_info:
        await usecase.create_initial_admin(
            email=EMAIL, display_name=DISPLAY_NAME, password=PASSWORD
        )

    assert exc_info.value.code is BootstrapErrorCode.ADMIN_ALREADY_EXISTS
    assert await user_count(db) == 1
    # 代替手段を案内する（利用者が次の行動を取れるように）。
    assert "--promote" in exc_info.value.message


async def test_a_deactivated_admin_still_counts_as_an_admin(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    """停止中の admin を「居ない」と扱うと、停止するだけで2人目を作れてしまう。"""
    await add_user(db, "first@sapeet.com", role=Role.ADMIN, is_active=False)

    with pytest.raises(BootstrapAdminError) as exc_info:
        await usecase.create_initial_admin(
            email=EMAIL, display_name=DISPLAY_NAME, password=PASSWORD
        )

    assert exc_info.value.code is BootstrapErrorCode.ADMIN_ALREADY_EXISTS


async def test_editors_and_viewers_do_not_block_the_bootstrap(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    """自己登録済みの利用者が居るだけなら、最初の admin は作れる。"""
    await add_user(db, "viewer@sapeet.com", role=Role.VIEWER)
    await add_user(db, "editor@sapeet.com", role=Role.EDITOR)

    user = await usecase.create_initial_admin(
        email=EMAIL, display_name=DISPLAY_NAME, password=PASSWORD
    )

    assert user.role == Role.ADMIN


# --- 再実行 ---------------------------------------------------------------


async def test_an_existing_email_is_not_overwritten(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    """再実行しても行を二重に作らず、既存ユーザーを黙って書き換えない。"""
    existing = await add_user(db, EMAIL, role=Role.VIEWER)
    original_hash = existing.password_hash

    with pytest.raises(BootstrapAdminError) as exc_info:
        await usecase.create_initial_admin(
            email=EMAIL, display_name=DISPLAY_NAME, password="another passphrase!!"
        )

    assert exc_info.value.code is BootstrapErrorCode.USER_ALREADY_EXISTS
    assert "--promote" in exc_info.value.message
    assert await user_count(db) == 1
    stored = (await db.execute(select(User))).scalar_one()
    assert stored.role == Role.VIEWER  # 昇格していない
    assert stored.password_hash == original_hash  # パスワードも変えていない


async def test_an_existing_email_is_matched_after_normalisation(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    await add_user(db, EMAIL, role=Role.VIEWER)

    with pytest.raises(BootstrapAdminError) as exc_info:
        await usecase.create_initial_admin(
            email="ADMIN@SAPEET.COM", display_name=DISPLAY_NAME, password=PASSWORD
        )

    assert exc_info.value.code is BootstrapErrorCode.USER_ALREADY_EXISTS


# --- 入力の検証 -----------------------------------------------------------


@pytest.mark.parametrize(
    "email", ["", "admin", "admin@", "@sapeet.com", "admin@localhost"]
)
async def test_a_malformed_email_is_refused(
    usecase: BootstrapAdminUsecase, db: AsyncSession, email: str
) -> None:
    with pytest.raises(BootstrapAdminError) as exc_info:
        await usecase.create_initial_admin(
            email=email, display_name=DISPLAY_NAME, password=PASSWORD
        )

    assert exc_info.value.code is BootstrapErrorCode.EMAIL_INVALID
    assert await user_count(db) == 0


async def test_an_empty_display_name_is_refused(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    with pytest.raises(BootstrapAdminError) as exc_info:
        await usecase.create_initial_admin(
            email=EMAIL, display_name="   ", password=PASSWORD
        )

    assert exc_info.value.code is BootstrapErrorCode.DISPLAY_NAME_REQUIRED
    assert await user_count(db) == 0


async def test_a_password_below_the_policy_creates_no_user(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    """ポリシー（長さのみ・T-08）は CLI 経路でも同じように効く。"""
    with pytest.raises(PasswordPolicyError):
        await usecase.create_initial_admin(
            email=EMAIL, display_name=DISPLAY_NAME, password="short"
        )

    assert await user_count(db) == 0


async def test_the_policy_error_does_not_leak_the_password(
    usecase: BootstrapAdminUsecase,
) -> None:
    """例外はログに出うるので、平文を含めない（T-08 と同じ約束）。"""
    # ⚠️ 違反理由の定型文（「パスワードは…」）に含まれない語を選ぶ。含まれる語だと
    # 「漏れていない」ことを検証できているのか判別できない。
    weak = "みじかい秘密"

    with pytest.raises(PasswordPolicyError) as exc_info:
        await usecase.create_initial_admin(
            email=EMAIL, display_name=DISPLAY_NAME, password=weak
        )

    assert weak not in str(exc_info.value)


# --- 事前チェック（パスワードを聞く前に呼べる）---------------------------


async def test_the_precheck_writes_nothing(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    normalized = await usecase.ensure_can_create_initial_admin(
        "  Admin@Sapeet.com  ", DISPLAY_NAME
    )

    assert normalized == "admin@sapeet.com"
    assert await user_count(db) == 0
    assert await audit_rows(db) == []


async def test_the_precheck_refuses_what_creation_would_refuse(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    await add_user(db, "first@sapeet.com", role=Role.ADMIN)

    with pytest.raises(BootstrapAdminError) as exc_info:
        await usecase.ensure_can_create_initial_admin(EMAIL, DISPLAY_NAME)

    assert exc_info.value.code is BootstrapErrorCode.ADMIN_ALREADY_EXISTS


# --- 昇格 -----------------------------------------------------------------


async def test_an_existing_user_is_promoted(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    await add_user(db, EMAIL, role=Role.VIEWER)

    outcome, user = await usecase.promote_to_admin(EMAIL)

    assert outcome is BootstrapOutcome.PROMOTED
    assert user.role == Role.ADMIN
    stored = (await db.execute(select(User))).scalar_one()
    assert stored.role == Role.ADMIN


async def test_promotion_keeps_the_password(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    """昇格はロールだけを変える（パスワードを聞かない・変えない）。"""
    existing = await add_user(db, EMAIL, role=Role.EDITOR)
    original_hash = existing.password_hash

    await usecase.promote_to_admin(EMAIL)

    stored = (await db.execute(select(User))).scalar_one()
    assert stored.password_hash == original_hash


async def test_promoting_an_admin_changes_nothing(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    """べき等。再実行しても監査ログを増やさない（変えていないものを記録しない）。"""
    await add_user(db, EMAIL, role=Role.ADMIN)

    outcome, _ = await usecase.promote_to_admin(EMAIL)

    assert outcome is BootstrapOutcome.ALREADY_ADMIN
    assert await audit_rows(db) == []


async def test_promoting_an_unknown_email_is_refused(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    with pytest.raises(BootstrapAdminError) as exc_info:
        await usecase.promote_to_admin("nobody@sapeet.com")

    assert exc_info.value.code is BootstrapErrorCode.USER_NOT_FOUND
    assert await user_count(db) == 0


async def test_promotion_matches_the_email_after_normalisation(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    await add_user(db, EMAIL, role=Role.VIEWER)

    outcome, _ = await usecase.promote_to_admin("  ADMIN@sapeet.com ")

    assert outcome is BootstrapOutcome.PROMOTED


async def test_promotion_works_even_when_an_admin_exists(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    """⚠️ `--promote` は復旧手段でもあるので、admin が居ても拒否しない。"""
    await add_user(db, "first@sapeet.com", role=Role.ADMIN)
    await add_user(db, EMAIL, role=Role.VIEWER)

    outcome, user = await usecase.promote_to_admin(EMAIL)

    assert outcome is BootstrapOutcome.PROMOTED
    assert user.role == Role.ADMIN


# --- 監査ログ -------------------------------------------------------------


async def test_creation_is_recorded_in_the_audit_log(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    user = await usecase.create_initial_admin(
        email=EMAIL, display_name=DISPLAY_NAME, password=PASSWORD
    )

    row = (await db.execute(select(AuditLog))).scalar_one()
    assert row.event_type == AuditEventType.USER_ROLE_CHANGE
    assert row.actor == CLI_ACTOR == "cli:create-admin"
    assert row.target == user.user_id
    # before=None は「このコマンドが作成した」ことを表す。
    assert row.diff == {
        "role": {"before": None, "after": "admin"},
        "email": EMAIL,
    }


async def test_promotion_records_the_previous_role(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    await add_user(db, EMAIL, role=Role.EDITOR)

    await usecase.promote_to_admin(EMAIL)

    row = (await db.execute(select(AuditLog))).scalar_one()
    assert row.diff is not None
    assert row.diff["role"] == {"before": "editor", "after": "admin"}


async def test_the_audit_timestamp_is_timezone_aware_utc(
    usecase: BootstrapAdminUsecase, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`UtcDateTime` は naive を拒否する（T-03）。"""
    fixed = datetime(2026, 8, 13, 1, 30, tzinfo=UTC)
    monkeypatch.setattr(bootstrap_module, "_now", lambda: fixed)

    await usecase.create_initial_admin(
        email=EMAIL, display_name=DISPLAY_NAME, password=PASSWORD
    )

    row = (await db.execute(select(AuditLog))).scalar_one()
    assert row.at == fixed
    assert row.at.tzinfo is not None


async def test_the_audit_log_contains_no_password_material(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    """⚠️ 平文もハッシュも監査ログに置かない（T-10 と同じ約束）。"""
    user = await usecase.create_initial_admin(
        email=EMAIL, display_name=DISPLAY_NAME, password=PASSWORD
    )

    row = (await db.execute(select(AuditLog))).scalar_one()
    serialised = json.dumps(
        {
            "actor": row.actor,
            "target": row.target,
            "diff": row.diff,
            "event_type": row.event_type,
        },
        ensure_ascii=False,
    )

    assert PASSWORD not in serialised
    assert user.password_hash not in serialised
    assert "$2b$" not in serialised


# --- 人数の数え方 ---------------------------------------------------------


async def test_count_admins_counts_only_admins(
    usecase: BootstrapAdminUsecase, db: AsyncSession
) -> None:
    await add_user(db, "viewer@sapeet.com", role=Role.VIEWER)
    await add_user(db, "editor@sapeet.com", role=Role.EDITOR)

    assert await usecase.count_admins() == 0

    await add_user(db, EMAIL, role=Role.ADMIN)

    assert await usecase.count_admins() == 1
