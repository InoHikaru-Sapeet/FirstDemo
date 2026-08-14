"""ユーザー管理のユースケース（T-42）。

重点は「破るとロール付与・締め出し防止の建前が壊れる」性質:

- **`system` を人に割り当てられない**（T-08 の DB CHECK 制約より手前で弾く）
- **最後の有効な admin を降格・停止できない**（自分自身を含む）
- 存在しないユーザーの操作は明確なエラー
- ロール変更・停止が監査ログに残り、そこに**パスワードが入らない**
- 停止でセッションが全失効する
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
from adapter.database.models.session import Session
from adapter.database.models.user import User
from application.usecases.manage_users import (
    ManageUsersError,
    ManageUsersErrorCode,
    ManageUsersUsecase,
)
from enterprise.entities.principal import ASSIGNABLE_ROLES, Principal, Role
from enterprise.services.password import hash_password

PASSWORD = "correct horse battery staple"


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
def usecase(db: AsyncSession) -> ManageUsersUsecase:
    return ManageUsersUsecase(db)


async def add_user(
    db: AsyncSession,
    email: str,
    role: Role = Role.VIEWER,
    is_active: bool = True,
    created_at: datetime | None = None,
) -> User:
    now = created_at or datetime(2026, 8, 1, tzinfo=UTC)
    user = User(
        user_id=f"usr_{email}",
        email=email,
        display_name="テスト 花子",
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


def actor_for(user: User) -> Principal:
    return Principal(subject=user.user_id, role=Role(user.role))


async def audit_entries(db: AsyncSession) -> list[AuditLog]:
    return list((await db.execute(select(AuditLog))).scalars().all())


async def add_session(db: AsyncSession, user_id: str) -> str:
    """有効なセッションを1つ作り、その `session_id` を返す。

    `session_id` は生トークンの SHA-256（T-40）。実物と同じ64桁16進にしておく。
    """
    now = datetime(2026, 8, 1, tzinfo=UTC)
    session_id = "9f" * 32
    db.add(
        Session(
            session_id=session_id,
            user_id=user_id,
            created_at=now,
            expires_at=datetime(2026, 8, 8, tzinfo=UTC),
            last_seen_at=now,
            revoked_at=None,
        )
    )
    await db.commit()
    return session_id


# --- 一覧 -----------------------------------------------------------------


async def test_list_users_returns_everyone_in_a_stable_order(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    await add_user(db, "b@sapeet.com", created_at=datetime(2026, 8, 2, tzinfo=UTC))
    await add_user(db, "a@sapeet.com", created_at=datetime(2026, 8, 1, tzinfo=UTC))

    users = await usecase.list_users()

    assert [u.email for u in users] == ["a@sapeet.com", "b@sapeet.com"]


async def test_list_users_includes_deactivated_accounts(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    """停止済みも見えないと、admin が再開させられない。"""
    await add_user(db, "stopped@sapeet.com", is_active=False)

    assert len(await usecase.list_users()) == 1


# --- ロール変更 -----------------------------------------------------------


async def test_change_role_promotes_a_viewer_to_editor(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    target = await add_user(db, "viewer@sapeet.com")

    updated = await usecase.change_role(actor_for(admin), target.user_id, Role.EDITOR)

    assert updated.role == Role.EDITOR


async def test_change_role_demotes_an_admin_when_another_admin_remains(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    other = await add_user(db, "admin2@sapeet.com", role=Role.ADMIN)

    updated = await usecase.change_role(actor_for(admin), other.user_id, Role.VIEWER)

    assert updated.role == Role.VIEWER


async def test_change_role_rejects_an_unknown_user(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)

    with pytest.raises(ManageUsersError) as excinfo:
        await usecase.change_role(actor_for(admin), "usr_does_not_exist", Role.EDITOR)

    assert excinfo.value.code is ManageUsersErrorCode.USER_NOT_FOUND


async def test_change_role_rejects_the_system_role(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    """⚠️ `system` は cron の呼び出し元種別で、ログインする人ではない（T-08）。

    割り当てられると、パスワードで `system` を名乗れる経路ができる。
    DB の CHECK 制約が最後の砦だが、その手前で業務エラーとして弾く。
    """
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    target = await add_user(db, "viewer@sapeet.com")

    with pytest.raises(ManageUsersError) as excinfo:
        await usecase.change_role(actor_for(admin), target.user_id, Role.SYSTEM)

    assert excinfo.value.code is ManageUsersErrorCode.ROLE_NOT_ASSIGNABLE
    await db.refresh(target)
    assert target.role == Role.VIEWER


async def test_the_rejected_roles_are_exactly_the_non_assignable_ones(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    """`ASSIGNABLE_ROLES` 以外はすべて拒否されること（列挙の取りこぼし防止）。"""
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    target = await add_user(db, "viewer@sapeet.com")

    for role in Role:
        if role in ASSIGNABLE_ROLES:
            continue
        with pytest.raises(ManageUsersError) as excinfo:
            await usecase.change_role(actor_for(admin), target.user_id, role)
        assert excinfo.value.code is ManageUsersErrorCode.ROLE_NOT_ASSIGNABLE


async def test_change_role_is_idempotent(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    """同じロールなら何も書かない（監査ログも増やさない。T-41 と同じ方針）。"""
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    target = await add_user(db, "viewer@sapeet.com")

    await usecase.change_role(actor_for(admin), target.user_id, Role.VIEWER)

    assert await audit_entries(db) == []


# --- 最後の admin を守る --------------------------------------------------


async def test_the_last_admin_cannot_demote_themselves(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    """⚠️ 通ると admin が0人になり、CLI 以外で復旧できなくなる。"""
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    await add_user(db, "viewer@sapeet.com")

    with pytest.raises(ManageUsersError) as excinfo:
        await usecase.change_role(actor_for(admin), admin.user_id, Role.VIEWER)

    assert excinfo.value.code is ManageUsersErrorCode.LAST_ADMIN
    await db.refresh(admin)
    assert admin.role == Role.ADMIN


async def test_the_last_admin_cannot_be_demoted_by_another_admin_account(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    """自分自身に限らない。「最後の1人」であることが条件。"""
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    # actor が admin ロールを名乗っていても、users 行を持たない場合がある
    # （CLI・将来の内部呼び出し）。守るのは DB 上の人数。
    ghost = Principal(subject="usr_not_in_db", role=Role.ADMIN)

    with pytest.raises(ManageUsersError) as excinfo:
        await usecase.change_role(ghost, admin.user_id, Role.EDITOR)

    assert excinfo.value.code is ManageUsersErrorCode.LAST_ADMIN


async def test_a_deactivated_admin_does_not_count_as_a_remaining_admin(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    """⚠️ 停止中の admin はログインできない＝「居る」ことにならない。

    全 admin を数えると、停止済みの admin を口実に最後の有効な admin を
    降格できてしまい、誰も管理画面へ入れなくなる。
    """
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    await add_user(db, "stopped-admin@sapeet.com", role=Role.ADMIN, is_active=False)

    with pytest.raises(ManageUsersError) as excinfo:
        await usecase.change_role(actor_for(admin), admin.user_id, Role.VIEWER)

    assert excinfo.value.code is ManageUsersErrorCode.LAST_ADMIN


async def test_the_last_admin_cannot_be_deactivated(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)

    with pytest.raises(ManageUsersError) as excinfo:
        await usecase.change_status(actor_for(admin), admin.user_id, is_active=False)

    assert excinfo.value.code is ManageUsersErrorCode.LAST_ADMIN
    await db.refresh(admin)
    assert admin.is_active is True


async def test_re_asserting_the_last_admins_own_role_is_allowed(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    """`role=admin` への「変更」（＝無変更）まで 409 にしない。"""
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)

    updated = await usecase.change_role(actor_for(admin), admin.user_id, Role.ADMIN)

    assert updated.role == Role.ADMIN


async def test_demoting_an_admin_is_allowed_once_a_replacement_is_promoted(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    """守っているのは「0人になること」だけで、交代そのものは妨げない。"""
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    successor = await add_user(db, "next@sapeet.com")

    await usecase.change_role(actor_for(admin), successor.user_id, Role.ADMIN)
    updated = await usecase.change_role(actor_for(admin), admin.user_id, Role.VIEWER)

    assert updated.role == Role.VIEWER


# --- 停止・再開 -----------------------------------------------------------


async def test_deactivating_a_user_revokes_all_of_their_sessions(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    target = await add_user(db, "viewer@sapeet.com")
    now = datetime(2026, 8, 1, tzinfo=UTC)
    db.add(
        Session(
            session_id="a" * 64,
            user_id=target.user_id,
            created_at=now,
            expires_at=datetime(2026, 8, 8, tzinfo=UTC),
            last_seen_at=now,
            revoked_at=None,
        )
    )
    await db.commit()

    await usecase.change_status(actor_for(admin), target.user_id, is_active=False)

    sessions = list((await db.execute(select(Session))).scalars().all())
    assert all(s.revoked_at is not None for s in sessions)


async def test_deactivating_does_not_touch_other_users_sessions(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    target = await add_user(db, "viewer@sapeet.com")
    bystander = await add_user(db, "other@sapeet.com")
    now = datetime(2026, 8, 1, tzinfo=UTC)
    for index, user in enumerate((target, bystander)):
        db.add(
            Session(
                session_id=str(index) * 64,
                user_id=user.user_id,
                created_at=now,
                expires_at=datetime(2026, 8, 8, tzinfo=UTC),
                last_seen_at=now,
                revoked_at=None,
            )
        )
    await db.commit()

    await usecase.change_status(actor_for(admin), target.user_id, is_active=False)

    remaining = (
        await db.execute(select(Session).where(Session.user_id == bystander.user_id))
    ).scalar_one()
    assert remaining.revoked_at is None


async def test_reactivating_a_user_restores_the_flag(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    target = await add_user(db, "viewer@sapeet.com", is_active=False)

    updated = await usecase.change_status(
        actor_for(admin), target.user_id, is_active=True
    )

    assert updated.is_active is True


async def test_change_status_is_idempotent(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    target = await add_user(db, "viewer@sapeet.com")

    await usecase.change_status(actor_for(admin), target.user_id, is_active=True)

    assert await audit_entries(db) == []


async def test_change_status_rejects_an_unknown_user(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)

    with pytest.raises(ManageUsersError) as excinfo:
        await usecase.change_status(
            actor_for(admin), "usr_does_not_exist", is_active=False
        )

    assert excinfo.value.code is ManageUsersErrorCode.USER_NOT_FOUND


# --- 監査ログ -------------------------------------------------------------


async def test_a_role_change_records_who_changed_whom_to_what(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    """§4.4 の「誰が・誰を・いつ・何に」。"""
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    target = await add_user(db, "viewer@sapeet.com")

    await usecase.change_role(actor_for(admin), target.user_id, Role.EDITOR)

    entry = (await audit_entries(db))[0]
    assert entry.event_type == AuditEventType.USER_ROLE_CHANGE
    assert entry.actor == f"admin:{admin.user_id}"  # 誰が
    assert entry.target == target.user_id  # 誰を
    assert entry.at is not None  # いつ
    assert entry.diff is not None
    assert entry.diff["role"] == {"before": "viewer", "after": "editor"}  # 何に
    assert entry.diff["email"] == target.email


async def test_a_demotion_records_the_previous_role(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    other = await add_user(db, "admin2@sapeet.com", role=Role.ADMIN)

    await usecase.change_role(actor_for(admin), other.user_id, Role.VIEWER)

    entry = (await audit_entries(db))[0]
    assert entry.diff is not None
    assert entry.diff["role"] == {"before": "admin", "after": "viewer"}


async def test_a_status_change_is_recorded(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    target = await add_user(db, "viewer@sapeet.com")

    await usecase.change_status(actor_for(admin), target.user_id, is_active=False)

    entry = (await audit_entries(db))[0]
    assert entry.event_type == AuditEventType.USER_STATUS_CHANGE
    assert entry.diff is not None
    assert entry.diff["is_active"] == {"before": True, "after": False}


async def test_a_rejected_change_writes_no_audit_entry(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    """拒否された操作を「変更した」と記録しない。"""
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)

    with pytest.raises(ManageUsersError):
        await usecase.change_role(actor_for(admin), admin.user_id, Role.VIEWER)

    assert await audit_entries(db) == []


async def test_the_audit_log_contains_no_password_material(
    db: AsyncSession, usecase: ManageUsersUsecase
) -> None:
    """⚠️ 平文もハッシュも監査ログに入れない（T-41 と同じ約束）。

    （2026-08-14 追加）**セッショントークンも検査する。** 停止（`change_status`）は
    そのユーザーのセッションを全失効させるので、失効した `session_id` を
    「何を消したか」として `diff` に載せたくなるが、載せてはいけない。
    `session_id` は生トークンの SHA-256（T-40）で、それ自体は乗っ取りに使えない
    ものの、監査ログに置く理由がない（従来このケースは未検証だった）。
    """
    admin = await add_user(db, "admin@sapeet.com", role=Role.ADMIN)
    target = await add_user(db, "viewer@sapeet.com")
    session_id = await add_session(db, target.user_id)

    await usecase.change_role(actor_for(admin), target.user_id, Role.EDITOR)
    await usecase.change_status(actor_for(admin), target.user_id, is_active=False)

    for entry in await audit_entries(db):
        serialized = json.dumps(
            {"actor": entry.actor, "target": entry.target, "diff": entry.diff},
            ensure_ascii=False,
        )
        assert PASSWORD not in serialized
        assert "$2b$" not in serialized
        assert "password" not in serialized
        assert session_id not in serialized
        assert "token" not in serialized.lower()
