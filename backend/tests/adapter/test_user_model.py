"""利用者テーブル（T-08）。

ID/PW 認証を自前実装する方針（TASKS.md §1.1）に伴い、ID の発行元とロールの正が
このテーブルになった。守るべき性質:

- 平文パスワードを保存しない／ハッシュを露出しない
- 同じメールアドレスで2つのアカウントを作れない（大文字小文字を含めて）
- `system` ロールの行を作れない（パスワードで system 権限を取れる経路を塞ぐ）
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateTable

from adapter.database.base import Base
from adapter.database.models import User, normalize_email
from enterprise.entities.principal import Principal, Role
from enterprise.services.password import hash_password, verify_password

NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
PASSWORD = "correct horse battery staple"


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


def make_user(
    user_id: str = "usr_0001",
    email: str = "viewer@sapeet.com",
    role: Role = Role.VIEWER,
    password: str = PASSWORD,
) -> User:
    return User(
        user_id=user_id,
        email=normalize_email(email),
        display_name="テスト 太郎",
        password_hash=hash_password(password),
        role=role,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
        password_updated_at=NOW,
    )


# --- 保存と読み出し -------------------------------------------------------


async def test_user_round_trips(session: AsyncSession) -> None:
    session.add(make_user())
    await session.commit()

    stored = (await session.execute(select(User))).scalar_one()
    assert stored.user_id == "usr_0001"
    assert stored.email == "viewer@sapeet.com"
    assert stored.display_name == "テスト 太郎"
    assert stored.role == Role.VIEWER
    assert stored.is_active is True
    assert stored.created_at == NOW
    assert stored.password_updated_at == NOW


async def test_the_stored_password_is_a_hash_not_the_plaintext(
    session: AsyncSession,
) -> None:
    session.add(make_user())
    await session.commit()

    stored = (await session.execute(select(User))).scalar_one()

    assert PASSWORD not in stored.password_hash
    assert stored.password_hash.startswith("$2b$")
    assert verify_password(PASSWORD, stored.password_hash) is True


async def test_a_principal_can_be_built_from_a_stored_user(
    session: AsyncSession,
) -> None:
    """ロールの正はこの行。認証（T-40）はここから Principal を組み立てる。"""
    session.add(make_user(role=Role.EDITOR))
    await session.commit()

    stored = (await session.execute(select(User))).scalar_one()
    principal = Principal(subject=stored.user_id, role=stored.role)

    assert principal.actor == "editor:usr_0001"


# --- メールアドレスの一意性 ----------------------------------------------


def test_normalize_email_lowercases_and_strips() -> None:
    assert normalize_email("  Admin@Sapeet.COM ") == "admin@sapeet.com"


async def test_duplicate_email_is_rejected(session: AsyncSession) -> None:
    session.add(make_user(user_id="usr_0001", email="dup@sapeet.com"))
    await session.commit()

    session.add(make_user(user_id="usr_0002", email="dup@sapeet.com"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_case_differing_emails_are_the_same_account(
    session: AsyncSession,
) -> None:
    """`Admin@…` と `admin@…` が別アカウントになると、どちらが admin か分からない。"""
    session.add(make_user(user_id="usr_0001", email="Admin@Sapeet.com"))
    await session.commit()

    session.add(make_user(user_id="usr_0002", email="admin@sapeet.com"))
    with pytest.raises(IntegrityError):
        await session.commit()


# --- `system` ロールを作れないこと ---------------------------------------


async def test_a_system_user_row_cannot_be_created(session: AsyncSession) -> None:
    """⚠️ system 行があると、パスワードで system 権限を取れてしまう。

    `system` は cron 等の非対話クライアント用で、サービストークンから
    直接 Principal を組み立てる（T-41）。DB 制約で塞いでいる。
    """
    session.add(make_user(role=Role.SYSTEM))

    with pytest.raises(IntegrityError, match="ck_users_role_is_assignable"):
        await session.commit()


@pytest.mark.parametrize("role", [Role.ADMIN, Role.EDITOR, Role.VIEWER])
async def test_assignable_roles_are_accepted(session: AsyncSession, role: Role) -> None:
    session.add(make_user(role=role))
    await session.commit()

    stored = (await session.execute(select(User))).scalar_one()
    assert stored.role == role


# --- ハッシュを露出しないこと --------------------------------------------


def test_repr_does_not_leak_the_password_hash() -> None:
    """repr はログ・例外・デバッガ出力に紛れ込む。"""
    user = make_user()

    rendered = repr(user)

    assert user.password_hash not in rendered
    assert "password" not in rendered
    assert "usr_0001" in rendered  # 追跡に必要な情報は残す


# --- DB 差し替え可能性の担保 ---------------------------------------------


def test_schema_compiles_for_both_backends() -> None:
    """PostgreSQL 固有型に依存していないこと（TASKS.md §1 備考・T-39）。"""
    table = Base.metadata.tables["users"]
    postgres_ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    sqlite_ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))

    assert "JSONB" not in postgres_ddl
    assert "ck_users_role_is_assignable" in postgres_ddl
    assert "ck_users_role_is_assignable" in sqlite_ddl


async def test_naive_datetime_is_rejected(session: AsyncSession) -> None:
    """日時は必ず tz 付き（設計書 §14 ／ T-03 と同じ約束）。"""
    user = make_user()
    user.created_at = datetime(2026, 8, 13, 10, 0)
    session.add(user)

    with pytest.raises(Exception, match="タイムゾーンなし"):
        await session.commit()
