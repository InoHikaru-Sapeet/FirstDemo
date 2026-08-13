"""認証エンドポイント（T-40）。

HTTP 層の約束を検証する:

- Cookie が `HttpOnly` / `SameSite` 付きで、JS から読めない形で発行される
- ログアウトが Cookie 削除だけでなく**サーバー側の失効**を伴う
- ログイン失敗が理由を問わず 401 ＋ 同一文言
- **未認証は 401**（権限なしの 403 と混ぜない）
- 登録リクエストに `role` を混ぜられない
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
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
from adapter.http.fastapi.auth.csrf import build_csrf_middleware, is_origin_allowed
from adapter.http.fastapi.auth.dependencies import get_db_session
from adapter.http.fastapi.auth.session_backend import SESSION_COOKIE_NAME
from adapter.http.fastapi.main import app
from application.usecases.auth import LOGIN_FAILED_MESSAGE, hash_session_token
from config import get_settings

PASSWORD = "correct horse battery staple"
EMAIL = "viewer@sapeet.com"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """テスト用 DB に差し替えた API クライアント。

    ⚠️ `session_cookie_secure` を false にしている。TestClient は http で
    アクセスするため、Secure Cookie は**送り返されない**（本番の既定は true）。
    """
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    # NullPool: 接続を使い回さないので、fixture と TestClient の
    # イベントループが違っても問題にならない。
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


def register(client: TestClient, email: str = EMAIL) -> None:
    response = client.post(
        "/auth/register",
        json={"email": email, "display_name": "テスト 太郎", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text


def login(client: TestClient, email: str = EMAIL, password: str = PASSWORD):  # noqa: ANN201
    return client.post("/auth/login", json={"email": email, "password": password})


# --- 登録 -----------------------------------------------------------------


def test_register_creates_a_viewer(client: TestClient) -> None:
    response = client.post(
        "/auth/register",
        json={"email": EMAIL, "display_name": "テスト 太郎", "password": PASSWORD},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "viewer"
    assert body["email"] == EMAIL


def test_register_never_returns_the_password_hash(client: TestClient) -> None:
    response = client.post(
        "/auth/register",
        json={"email": EMAIL, "display_name": "テスト 太郎", "password": PASSWORD},
    )

    assert "password" not in response.text
    assert "$2b$" not in response.text


def test_register_rejects_a_role_field(client: TestClient) -> None:
    """⚠️ 自己登録で admin を名乗れないこと（TASKS.md §1.1）。"""
    response = client.post(
        "/auth/register",
        json={
            "email": EMAIL,
            "display_name": "テスト 太郎",
            "password": PASSWORD,
            "role": "admin",
        },
    )

    assert response.status_code == 422


def test_register_rejects_a_disallowed_domain(client: TestClient) -> None:
    response = client.post(
        "/auth/register",
        json={
            "email": "outsider@example.com",
            "display_name": "外部 太郎",
            "password": PASSWORD,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "email_domain_not_allowed"


def test_register_reports_a_weak_password_with_issues(client: TestClient) -> None:
    response = client.post(
        "/auth/register",
        json={"email": EMAIL, "display_name": "テスト 太郎", "password": "short"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "validation_failed"
    assert detail["issues"][0]["code"] == "password_too_short"


def test_register_rejects_a_duplicate_email(client: TestClient) -> None:
    register(client)

    response = client.post(
        "/auth/register",
        json={"email": EMAIL, "display_name": "別 太郎", "password": PASSWORD},
    )

    assert response.status_code == 409


# --- Cookie の属性 --------------------------------------------------------


def test_login_sets_an_httponly_samesite_cookie(client: TestClient) -> None:
    """⚠️ `HttpOnly` が無いと JS から盗める。`SameSite` が無いと CSRF 面が広がる。"""
    register(client)

    response = login(client)

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE_NAME}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.replace("samesite", "SameSite")
    assert "Path=/" in set_cookie


def test_the_cookie_value_is_not_stored_in_the_database(client: TestClient) -> None:
    """⚠️ DB には SHA-256 だけ。生トークンは残さない。"""
    register(client)
    login(client)
    raw_token = client.cookies[SESSION_COOKIE_NAME]

    async def read_sessions() -> list[Session]:
        async with client._maker() as session:  # type: ignore[attr-defined]
            return list((await session.execute(select(Session))).scalars().all())

    stored = asyncio.run(read_sessions())

    assert len(stored) == 1
    assert stored[0].session_id != raw_token
    assert stored[0].session_id == hash_session_token(raw_token)


# --- ログイン失敗 ---------------------------------------------------------


def test_login_failures_are_indistinguishable_over_http(client: TestClient) -> None:
    register(client)

    wrong_password = login(client, password="wrong password here")
    unknown_account = login(client, email="nobody@sapeet.com")

    assert wrong_password.status_code == unknown_account.status_code == 401
    assert wrong_password.json() == unknown_account.json()
    assert wrong_password.json()["detail"]["message"] == LOGIN_FAILED_MESSAGE


def test_a_failed_login_sets_no_cookie(client: TestClient) -> None:
    register(client)

    response = login(client, password="wrong password here")

    assert SESSION_COOKIE_NAME not in response.cookies


# --- /auth/me と 401 ------------------------------------------------------


def test_me_requires_authentication(client: TestClient) -> None:
    """⚠️ 未認証は **401**（権限なしの 403 と混ぜない）。"""
    response = client.get("/auth/me")

    assert response.status_code == 401


def test_me_returns_the_logged_in_user(client: TestClient) -> None:
    register(client)
    login(client)

    response = client.get("/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == EMAIL
    assert body["role"] == "viewer"
    assert body["display_name"] == "テスト 太郎"
    assert "password_hash" not in body


def test_an_invalid_cookie_is_treated_as_unauthenticated(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, "forged-token-value")

    response = client.get("/auth/me")

    assert response.status_code == 401


# --- ログアウト -----------------------------------------------------------


def test_logout_invalidates_the_session_server_side(client: TestClient) -> None:
    """⚠️ Cookie を消すだけにしない。

    ログアウト後に**同じ Cookie を再提示**しても通らないことを確認する
    （攻撃者が Cookie のコピーを持っていた場合の想定）。
    """
    register(client)
    login(client)
    stolen_token = client.cookies[SESSION_COOKIE_NAME]

    logout_response = client.post("/auth/logout")
    assert logout_response.status_code == 204

    # 盗まれた Cookie を手で再セットしても無効。
    client.cookies.set(SESSION_COOKIE_NAME, stolen_token)
    assert client.get("/auth/me").status_code == 401


def test_logout_clears_the_cookie(client: TestClient) -> None:
    register(client)
    login(client)

    response = client.post("/auth/logout")

    assert f'{SESSION_COOKIE_NAME}=""' in response.headers["set-cookie"]


def test_logout_is_idempotent_without_a_session(client: TestClient) -> None:
    assert client.post("/auth/logout").status_code == 204
    assert client.post("/auth/logout").status_code == 204


# --- パスワード変更 -------------------------------------------------------


def test_change_password_requires_authentication(client: TestClient) -> None:
    response = client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": "a new passphrase!"},
    )

    assert response.status_code == 401


def test_change_password_revokes_the_current_session(client: TestClient) -> None:
    register(client)
    login(client)

    response = client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": "a new passphrase!"},
    )

    assert response.status_code == 204
    assert client.get("/auth/me").status_code == 401


def test_change_password_rejects_a_wrong_current_password(client: TestClient) -> None:
    register(client)
    login(client)

    response = client.post(
        "/auth/password",
        json={
            "current_password": "wrong password here",
            "new_password": "a new passphrase!",
        },
    )

    assert response.status_code == 401


# --- CSRF（Origin 検証）---------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "allowed", "expected"),
    [
        (None, ["https://app.example.com"], True),  # 非ブラウザ（cron 等）
        ("https://app.example.com", ["https://app.example.com"], True),
        ("https://evil.example.com", ["https://app.example.com"], False),
        ("https://evil.example.com", ["*"], True),  # 既定は素通り
    ],
)
def test_origin_allowlist(
    origin: str | None, allowed: list[str], expected: bool
) -> None:
    assert is_origin_allowed(origin, allowed) is expected


def test_state_changing_requests_from_a_foreign_origin_are_rejected() -> None:
    """更新系のみを弾き、参照系は通すこと。"""
    isolated = FastAPI()
    isolated.middleware("http")(build_csrf_middleware(["https://app.example.com"]))

    @isolated.get("/thing")
    def read() -> dict[str, str]:
        return {"status": "ok"}

    @isolated.post("/thing")
    def write() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(isolated) as test_client:
        evil = {"Origin": "https://evil.example.com"}
        good = {"Origin": "https://app.example.com"}

        assert test_client.post("/thing", headers=evil).status_code == 403
        assert test_client.post("/thing", headers=good).status_code == 200
        # 参照系は Origin を見ない（副作用が無いため）。
        assert test_client.get("/thing", headers=evil).status_code == 200
