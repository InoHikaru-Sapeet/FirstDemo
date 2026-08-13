"""ユーザー管理エンドポイント（T-42）。

HTTP 層の約束を検証する:

- **admin のみ**。viewer / editor は 403、未認証は 401（401 と 403 を混ぜない）
- `password_hash` を返さない
- 存在しないユーザーは 404、最後の admin の降格・停止は 409、`system` 指定は 422
- **昇格が再ログインなしで効く**（TASKS.md §1.1「ログイン状態の保持」の実体）
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from adapter.database.base import Base
from adapter.database.models.session import Session
from adapter.database.models.user import User
from adapter.http.fastapi.auth.dependencies import get_db_session
from adapter.http.fastapi.auth.session_backend import SESSION_COOKIE_NAME
from adapter.http.fastapi.main import app
from adapter.http.fastapi.routers.users import ChangeRoleRequest
from application.usecases.auth import hash_session_token
from config import get_settings
from enterprise.entities.principal import ASSIGNABLE_ROLES, Role
from enterprise.services.password import hash_password

PASSWORD = "correct horse battery staple"
ADMIN_EMAIL = "admin@sapeet.com"
VIEWER_EMAIL = "viewer@sapeet.com"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """テスト用 DB に差し替えた API クライアント（T-40 のものと同じ作り）。"""
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'api.db'}", poolclass=NullPool
    )

    async def create_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())

    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_db
    with TestClient(app) as test_client:
        test_client._maker = maker  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def seed_user(
    client: TestClient,
    email: str,
    role: Role = Role.VIEWER,
    is_active: bool = True,
) -> str:
    """DB へ直接ユーザーを作る（admin は API では作れないため）。"""

    async def insert() -> str:
        now = datetime(2026, 8, 1, tzinfo=UTC)
        async with client._maker() as session:  # type: ignore[attr-defined]
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
            session.add(user)
            await session.commit()
            return user.user_id

    return asyncio.run(insert())


def login(client: TestClient, email: str) -> None:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


def login_as_admin(client: TestClient) -> str:
    user_id = seed_user(client, ADMIN_EMAIL, role=Role.ADMIN)
    login(client, ADMIN_EMAIL)
    return user_id


# --- 認可（admin のみ）----------------------------------------------------


@pytest.mark.parametrize("role", [Role.VIEWER, Role.EDITOR])
def test_non_admins_are_forbidden(client: TestClient, role: Role) -> None:
    """⚠️ viewer / editor は **403**（TASKS.md T-09・T-42）。"""
    target = seed_user(client, VIEWER_EMAIL)
    seed_user(client, "caller@sapeet.com", role=role)
    login(client, "caller@sapeet.com")

    assert client.get("/users").status_code == 403
    assert (
        client.patch(f"/users/{target}/role", json={"role": "admin"}).status_code == 403
    )
    assert (
        client.patch(f"/users/{target}/status", json={"is_active": False}).status_code
        == 403
    )


def test_unauthenticated_requests_are_401_not_403(client: TestClient) -> None:
    """⚠️ 401 と 403 を混ぜない。フロントの出し分けが崩れる（T-43）。"""
    target = seed_user(client, VIEWER_EMAIL)

    assert client.get("/users").status_code == 401
    assert (
        client.patch(f"/users/{target}/role", json={"role": "admin"}).status_code == 401
    )


def test_a_forbidden_response_does_not_leak_the_user_list(
    client: TestClient,
) -> None:
    seed_user(client, "secret-person@sapeet.com", role=Role.EDITOR)
    seed_user(client, "caller@sapeet.com", role=Role.VIEWER)
    login(client, "caller@sapeet.com")

    response = client.get("/users")

    assert response.status_code == 403
    assert "secret-person@sapeet.com" not in response.text


def test_a_demoted_admin_loses_access_without_re_login(client: TestClient) -> None:
    """降格は**次のリクエストから**効く（ロールを毎回 users 行から解決するため）。"""
    admin_id = login_as_admin(client)
    successor = seed_user(client, "next@sapeet.com", role=Role.ADMIN)

    assert client.get("/users").status_code == 200
    demote = client.patch(f"/users/{admin_id}/role", json={"role": "viewer"})
    assert demote.status_code == 200

    # Cookie はそのまま。再ログインしていない。
    assert client.get("/users").status_code == 403
    assert successor  # 後任が居るので降格自体は許される


# --- 一覧 -----------------------------------------------------------------


def test_list_users_returns_the_expected_fields(client: TestClient) -> None:
    login_as_admin(client)
    seed_user(client, VIEWER_EMAIL)

    response = client.get("/users")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    assert set(items[0]) == {
        "user_id",
        "email",
        "display_name",
        "role",
        "is_active",
        "created_at",
    }


def test_list_users_never_returns_the_password_hash(client: TestClient) -> None:
    """⚠️ ハッシュを返すと、オフラインで総当たりできる材料を配ることになる。"""
    login_as_admin(client)
    seed_user(client, VIEWER_EMAIL)

    response = client.get("/users")

    assert "password_hash" not in response.text
    assert "$2b$" not in response.text


# --- ロール変更 -----------------------------------------------------------


def test_an_admin_can_promote_a_viewer(client: TestClient) -> None:
    login_as_admin(client)
    target = seed_user(client, VIEWER_EMAIL)

    response = client.patch(f"/users/{target}/role", json={"role": "editor"})

    assert response.status_code == 200
    assert response.json()["role"] == "editor"


def test_an_unknown_user_is_404(client: TestClient) -> None:
    login_as_admin(client)

    response = client.patch("/users/usr_does_not_exist/role", json={"role": "editor"})

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "user_not_found"


def test_the_system_role_is_rejected(client: TestClient) -> None:
    """⚠️ `system` を人に割り当てられると、パスワードで system を名乗れる。"""
    login_as_admin(client)
    target = seed_user(client, VIEWER_EMAIL)

    response = client.patch(f"/users/{target}/role", json={"role": "system"})

    assert response.status_code == 422


def test_an_unknown_role_is_rejected(client: TestClient) -> None:
    login_as_admin(client)
    target = seed_user(client, VIEWER_EMAIL)

    response = client.patch(f"/users/{target}/role", json={"role": "superuser"})

    assert response.status_code == 422


def test_the_request_model_allows_exactly_the_assignable_roles() -> None:
    """⚠️ リクエストの型と `ASSIGNABLE_ROLES` がずれると `system` が通る。"""
    annotation = ChangeRoleRequest.model_fields["role"].annotation
    assert set(get_args(annotation)) == ASSIGNABLE_ROLES


def test_unknown_fields_are_rejected(client: TestClient) -> None:
    login_as_admin(client)
    target = seed_user(client, VIEWER_EMAIL)

    response = client.patch(
        f"/users/{target}/role", json={"role": "editor", "is_active": False}
    )

    assert response.status_code == 422


# --- 最後の admin を守る --------------------------------------------------


def test_the_last_admin_cannot_demote_themselves_over_http(
    client: TestClient,
) -> None:
    """⚠️ 通ると admin が0人になり、CLI 以外で復旧できなくなる。"""
    admin_id = login_as_admin(client)

    response = client.patch(f"/users/{admin_id}/role", json={"role": "viewer"})

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "last_admin"
    # 権限は残っている。
    assert client.get("/users").status_code == 200


def test_the_last_admin_cannot_be_deactivated_over_http(client: TestClient) -> None:
    admin_id = login_as_admin(client)

    response = client.patch(f"/users/{admin_id}/status", json={"is_active": False})

    assert response.status_code == 409
    assert client.get("/users").status_code == 200


def test_promoting_a_successor_unblocks_the_demotion(client: TestClient) -> None:
    admin_id = login_as_admin(client)
    successor = seed_user(client, "next@sapeet.com")

    assert (
        client.patch(f"/users/{successor}/role", json={"role": "admin"}).status_code
        == 200
    )
    assert (
        client.patch(f"/users/{admin_id}/role", json={"role": "viewer"}).status_code
        == 200
    )


# --- 停止・再開 -----------------------------------------------------------


def test_deactivating_a_user_revokes_their_sessions(client: TestClient) -> None:
    """停止された利用者は、手元の Cookie を持っていても入れない。"""
    login_as_admin(client)
    target = seed_user(client, VIEWER_EMAIL)

    victim = TestClient(app)
    victim.post("/auth/login", json={"email": VIEWER_EMAIL, "password": PASSWORD})
    stolen_token = victim.cookies[SESSION_COOKIE_NAME]

    response = client.patch(f"/users/{target}/status", json={"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False

    async def read_session() -> Session | None:
        async with client._maker() as session:  # type: ignore[attr-defined]
            return (
                await session.execute(
                    select(Session).where(
                        Session.session_id == hash_session_token(stolen_token)
                    )
                )
            ).scalar_one_or_none()

    stored = asyncio.run(read_session())
    assert stored is not None
    assert stored.revoked_at is not None

    victim.cookies.set(SESSION_COOKIE_NAME, stolen_token)
    assert victim.get("/auth/me").status_code == 401


def test_a_deactivated_user_cannot_log_in_again(client: TestClient) -> None:
    login_as_admin(client)
    target = seed_user(client, VIEWER_EMAIL)
    client.patch(f"/users/{target}/status", json={"is_active": False})

    other = TestClient(app)
    response = other.post(
        "/auth/login", json={"email": VIEWER_EMAIL, "password": PASSWORD}
    )

    assert response.status_code == 401


def test_reactivating_a_user_lets_them_log_in(client: TestClient) -> None:
    login_as_admin(client)
    target = seed_user(client, VIEWER_EMAIL, is_active=False)

    assert (
        client.patch(f"/users/{target}/status", json={"is_active": True}).status_code
        == 200
    )

    other = TestClient(app)
    response = other.post(
        "/auth/login", json={"email": VIEWER_EMAIL, "password": PASSWORD}
    )
    assert response.status_code == 200


# --- 昇格の即時反映 -------------------------------------------------------


def test_a_promotion_takes_effect_without_re_login(client: TestClient) -> None:
    """⚠️ T-42 の完了条件そのもの。

    viewer としてログイン中のセッションが、admin による昇格のあと
    **再ログインなしに** editor の権限で通ること。JWT を採らなかった理由
    （TASKS.md §1.1「ログイン状態の保持」）の実体。

    `POST /run` は未実装（T-26）なので、代わりに **admin へ昇格させて
    `GET /users` が 403 → 200 に変わる**ことで確認する。
    """
    login_as_admin(client)
    target = seed_user(client, VIEWER_EMAIL)

    promoted = TestClient(app)
    promoted.post("/auth/login", json={"email": VIEWER_EMAIL, "password": PASSWORD})
    assert promoted.get("/users").status_code == 403

    assert (
        client.patch(f"/users/{target}/role", json={"role": "admin"}).status_code == 200
    )

    # Cookie はそのまま。再ログインしていない。
    assert promoted.get("/users").status_code == 200


def test_the_literal_type_is_a_literal_of_roles() -> None:
    """`Literal[Role.X]` であること（`str` に緩めると `system` が通りうる）。"""
    annotation = ChangeRoleRequest.model_fields["role"].annotation
    assert all(isinstance(value, Role) for value in get_args(annotation))
    assert annotation is not Literal[str]
