"""サービストークン認証バックエンドと認証方式の合成（T-41）。

重点:

- `Authorization: Bearer` だけが system 経路（Cookie では system になれない）
- 設定にハッシュが無ければ **system 経路そのものが無効**
- `users` テーブルを引かずに `Principal` を組み立てる（system は行を持たない）
- 合成の順序が固定されている（Bearer が先。Cookie が system を上書きしない）
"""

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from adapter.database.base import Base
from adapter.http.fastapi.auth.chain import ChainedAuthenticationBackend
from adapter.http.fastapi.auth.dependencies import get_authentication_backend
from adapter.http.fastapi.auth.service_token import (
    ServiceTokenAuthenticationBackend,
    extract_bearer_token,
)
from config import get_settings
from enterprise.entities.principal import Principal, Role
from enterprise.services.service_token import (
    generate_service_token,
    hash_service_token,
)

TOKEN = "cron-token-for-tests"
TOKEN_HASH = hash_service_token(TOKEN)


def make_request(headers: dict[str, str] | None = None) -> Request:
    """ヘッダだけを持つ最小の Request（DB もアプリも不要）。"""
    raw_headers = [
        (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
    ]
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": raw_headers}
    )


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


# --- ヘッダの取り出し -----------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",  # 値が無い
        "Bearer    ",  # 空白だけ
        "Basic dXNlcjpwYXNz",  # 別スキーム
        "Token abc",
        "abc",  # スキーム無し
    ],
)
def test_non_bearer_headers_yield_no_token(header: str | None) -> None:
    assert extract_bearer_token(header) is None


@pytest.mark.parametrize(
    "header",
    ["Bearer abc", "bearer abc", "BEARER abc", "Bearer   abc  "],
)
def test_the_bearer_token_is_extracted_case_insensitively(header: str) -> None:
    assert extract_bearer_token(header) == "abc"


# --- 有効化 / 無効化 ------------------------------------------------------


async def test_an_unset_hash_disables_the_system_path() -> None:
    """⚠️ 未設定で system になれてしまうと認可（§6.2）が根本から崩れる。"""
    backend = ServiceTokenAuthenticationBackend(expected_hash="")

    assert backend.is_enabled is False
    assert await backend.resolve(make_request(bearer(TOKEN))) is None


async def test_a_matching_token_resolves_to_the_system_principal() -> None:
    backend = ServiceTokenAuthenticationBackend(expected_hash=TOKEN_HASH)

    principal = await backend.resolve(make_request(bearer(TOKEN)))

    assert principal == Principal(subject="cron", role=Role.SYSTEM)
    assert principal is not None
    assert principal.is_internal is True
    assert principal.actor == "system:cron"


async def test_a_wrong_token_is_not_authenticated() -> None:
    backend = ServiceTokenAuthenticationBackend(expected_hash=TOKEN_HASH)

    assert await backend.resolve(make_request(bearer(generate_service_token()))) is None


async def test_a_request_without_a_bearer_header_is_not_authenticated() -> None:
    backend = ServiceTokenAuthenticationBackend(expected_hash=TOKEN_HASH)

    assert await backend.resolve(make_request()) is None
    assert await backend.resolve(make_request({"cookie": f"sid={TOKEN}"})) is None


async def test_the_backend_needs_no_database(db: AsyncSession) -> None:
    """`system` は `users` に行を持てない（T-08 の CHECK 制約）。

    したがってこの経路は DB を引かずに解決する。行が1つも無い DB でも成立する
    ことで、「users を見ていない」ことを示す。
    """
    backend = ServiceTokenAuthenticationBackend(expected_hash=TOKEN_HASH)

    assert await backend.resolve(make_request(bearer(TOKEN))) is not None


async def test_a_raw_token_pasted_into_the_hash_setting_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """生トークンを `SERVICE_TOKEN_HASH` に貼る運用ミスを警告して無効化する。"""
    raw_token = generate_service_token()

    with caplog.at_level(logging.WARNING):
        backend = ServiceTokenAuthenticationBackend(expected_hash=raw_token)

    assert backend.is_enabled is False
    assert await backend.resolve(make_request(bearer(raw_token))) is None
    assert "SERVICE_TOKEN_HASH" in caplog.text
    # ⚠️ 設定値そのものはログに出さない。
    assert raw_token not in caplog.text


# --- 合成（順序） ---------------------------------------------------------


class _FixedBackend:
    """常に同じ結果を返すダミー（呼ばれた回数を数える）。"""

    def __init__(self, principal: Principal | None) -> None:
        self._principal = principal
        self.calls = 0

    async def resolve(self, request: Request) -> Principal | None:
        self.calls += 1
        return self._principal


VIEWER = Principal(subject="usr_1", role=Role.VIEWER)
SYSTEM = Principal(subject="cron", role=Role.SYSTEM)


async def test_the_first_backend_that_resolves_wins() -> None:
    first = _FixedBackend(SYSTEM)
    second = _FixedBackend(VIEWER)

    principal = await ChainedAuthenticationBackend(first, second).resolve(
        make_request()
    )

    assert principal == SYSTEM
    assert second.calls == 0  # 確定したら後続を呼ばない


async def test_the_chain_falls_through_when_a_backend_cannot_resolve() -> None:
    first = _FixedBackend(None)
    second = _FixedBackend(VIEWER)

    principal = await ChainedAuthenticationBackend(first, second).resolve(
        make_request()
    )

    assert principal == VIEWER
    assert first.calls == 1


async def test_the_chain_returns_none_when_nothing_resolves() -> None:
    chain = ChainedAuthenticationBackend(_FixedBackend(None), _FixedBackend(None))

    assert await chain.resolve(make_request()) is None


async def test_a_cookie_session_cannot_override_a_service_token() -> None:
    """⚠️ 順序が逆になると、Bearer を提示した cron が人のロールで通りうる。"""
    service = ServiceTokenAuthenticationBackend(expected_hash=TOKEN_HASH)
    cookie_session = _FixedBackend(VIEWER)

    principal = await ChainedAuthenticationBackend(service, cookie_session).resolve(
        make_request(bearer(TOKEN) | {"cookie": "sid=whatever"})
    )

    assert principal == SYSTEM


# --- DI の配線 ------------------------------------------------------------


async def test_the_di_puts_the_service_token_before_the_cookie_session(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`get_authentication_backend()`（差し替え口）が合成を返していること。"""
    monkeypatch.setenv("SERVICE_TOKEN_HASH", TOKEN_HASH)
    get_settings.cache_clear()
    try:
        backend = get_authentication_backend(db)

        assert isinstance(backend, ChainedAuthenticationBackend)
        assert await backend.resolve(make_request(bearer(TOKEN))) == SYSTEM
    finally:
        get_settings.cache_clear()


async def test_the_di_disables_the_system_path_by_default(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既定（`SERVICE_TOKEN_HASH` 未設定）では system 経路が無い。

    ⚠️ 空文字を明示するのは、開発者の手元の `.env` に値があってもこの検証が
    「未設定のときの挙動」を見るため（環境変数は `.env` より優先される）。
    """
    monkeypatch.setenv("SERVICE_TOKEN_HASH", "")
    get_settings.cache_clear()
    try:
        backend = get_authentication_backend(db)

        assert await backend.resolve(make_request(bearer(TOKEN))) is None
    finally:
        get_settings.cache_clear()
