"""セッションテーブル（T-40）。

有効性の判定ロジックは application 層（`tests/application/test_auth_usecase.py`）。
ここは**永続化の性質**だけを見る。
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateTable

from adapter.database.base import Base
from adapter.database.models.session import SESSION_ID_LENGTH, Session
from adapter.database.models.user import User
from enterprise.entities.principal import Role
from enterprise.services.password import hash_password

NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


async def add_user(db: AsyncSession, user_id: str = "usr_0001") -> User:
    user = User(
        user_id=user_id,
        email=f"{user_id}@sapeet.com",
        display_name="テスト 太郎",
        password_hash=hash_password("correct horse battery staple"),
        role=Role.VIEWER,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
        password_updated_at=NOW,
        failed_login_attempts=0,
        locked_until=None,
    )
    db.add(user)
    await db.commit()
    return user


def make_session(user_id: str = "usr_0001", session_id: str = "a" * 64) -> Session:
    return Session(
        session_id=session_id,
        user_id=user_id,
        created_at=NOW,
        expires_at=NOW + timedelta(days=7),
        last_seen_at=NOW,
        revoked_at=None,
    )


async def test_session_round_trips(session: AsyncSession) -> None:
    await add_user(session)
    session.add(make_session())
    await session.commit()

    stored = (await session.execute(select(Session))).scalar_one()
    assert stored.user_id == "usr_0001"
    assert stored.expires_at == NOW + timedelta(days=7)
    assert stored.revoked_at is None


async def test_a_session_requires_an_existing_user(session: AsyncSession) -> None:
    """孤児セッションを作れないこと（外部キー）。"""
    # SQLite は既定で外部キーを検査しないので、明示的に有効化する。
    await session.execute(text("PRAGMA foreign_keys=ON"))
    session.add(make_session(user_id="usr_missing"))

    with pytest.raises(IntegrityError):
        await session.commit()


def test_the_session_id_column_fits_a_sha256_hex() -> None:
    """`session_id` は SHA-256 の16進表現（64文字）。"""
    assert SESSION_ID_LENGTH == 64
    assert Base.metadata.tables["sessions"].c.session_id.type.length == 64


def test_repr_does_not_leak_the_session_id() -> None:
    """repr はログ・例外に紛れ込む。DB の値を出す価値がない。"""
    rendered = repr(make_session(session_id="b" * 64))

    assert "b" * 64 not in rendered
    assert "usr_0001" in rendered


def test_schema_compiles_for_both_backends() -> None:
    """PostgreSQL 固有型に依存していないこと（TASKS.md §1 備考・T-39）。"""
    table = Base.metadata.tables["sessions"]
    postgres_ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    sqlite_ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))

    assert "JSONB" not in postgres_ddl
    assert postgres_ddl and sqlite_ddl
