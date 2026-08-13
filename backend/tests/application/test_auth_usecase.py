"""認証ユースケース（T-40）。

application 層の業務規則を直接検証する。HTTP 経由の確認は
`tests/adapter/test_auth_router.py`。

重点は「破ると認証が壊れる」性質:

- ログイン失敗の理由を区別しない（アカウント列挙を防ぐ）
- ログアウトが**サーバー側**で失効させる（Cookie 削除だけにしない）
- セッション ID が推測困難で、**生トークンが DB に無い**
- ロールをセッションに焼き込まない（昇格が即時に効く）
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from adapter.database.base import Base
from adapter.database.models.session import Session
from adapter.database.models.user import User
from application.usecases import auth as auth_module
from application.usecases.auth import (
    LOGIN_FAILED_MESSAGE,
    AuthError,
    AuthErrorCode,
    AuthUsecase,
    LoginPolicy,
    SessionPolicy,
    hash_session_token,
)
from enterprise.entities.principal import Role
from enterprise.services.password import PasswordPolicyError, verify_password

PASSWORD = "correct horse battery staple"
EMAIL = "viewer@sapeet.com"

SESSION_POLICY = SessionPolicy(
    absolute_lifetime=timedelta(days=7),
    idle_timeout=timedelta(hours=8),
)
LOGIN_POLICY = LoginPolicy(
    max_failed_attempts=5,
    lockout_duration=timedelta(minutes=15),
)


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
def usecase(db: AsyncSession) -> AuthUsecase:
    return AuthUsecase(
        db=db,
        session_policy=SESSION_POLICY,
        login_policy=LOGIN_POLICY,
        allowed_email_domains=["sapeet.com"],
    )


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """`_now()` を差し替えて時間を進められるようにする。

    戻り値のリストの 0 番目を書き換えると、以降の `_now()` がそれを返す。
    """
    current = [datetime(2026, 8, 13, 10, 0, tzinfo=UTC)]
    monkeypatch.setattr(auth_module, "_now", lambda: current[0])
    return current


async def register_user(usecase: AuthUsecase, email: str = EMAIL) -> User:
    return await usecase.register(
        email=email, display_name="テスト 太郎", password=PASSWORD
    )


# --- 登録 -----------------------------------------------------------------


async def test_registration_always_creates_a_viewer(usecase: AuthUsecase) -> None:
    """⚠️ TASKS.md §1.1「登録直後は全員 viewer」の実体。"""
    user = await register_user(usecase)

    assert user.role is Role.VIEWER


async def test_register_takes_no_role_argument() -> None:
    """⚠️ ロールを引数で渡せないこと自体を固定する。

    引数が足されると、HTTP 層の検証を通り抜けて昇格できる経路ができる。
    """
    import inspect

    signature = inspect.signature(AuthUsecase.register)

    assert set(signature.parameters) == {"self", "email", "display_name", "password"}


async def test_registration_normalizes_the_email(usecase: AuthUsecase) -> None:
    user = await register_user(usecase, email="  Viewer@Sapeet.COM  ")

    assert user.email == "viewer@sapeet.com"


async def test_registration_stores_a_hash_not_the_plaintext(
    usecase: AuthUsecase,
) -> None:
    user = await register_user(usecase)

    assert PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$2b$")
    assert verify_password(PASSWORD, user.password_hash) is True


async def test_registration_rejects_a_disallowed_domain(usecase: AuthUsecase) -> None:
    """2026-08-13 決定：既定で `sapeet.com` のみ（要確認事項 #6）。"""
    with pytest.raises(AuthError) as excinfo:
        await register_user(usecase, email="outsider@example.com")

    assert excinfo.value.code is AuthErrorCode.EMAIL_DOMAIN_NOT_ALLOWED


async def test_registration_allows_any_domain_when_the_list_is_empty(
    db: AsyncSession,
) -> None:
    unrestricted = AuthUsecase(
        db=db,
        session_policy=SESSION_POLICY,
        login_policy=LOGIN_POLICY,
        allowed_email_domains=[],
    )

    user = await register_user(unrestricted, email="outsider@example.com")

    assert user.role is Role.VIEWER


@pytest.mark.parametrize(
    "email", ["not-an-email", "@sapeet.com", "user@", "user@localhost"]
)
async def test_registration_rejects_malformed_emails(
    usecase: AuthUsecase, email: str
) -> None:
    with pytest.raises(AuthError) as excinfo:
        await register_user(usecase, email=email)

    assert excinfo.value.code in {
        AuthErrorCode.EMAIL_INVALID,
        AuthErrorCode.EMAIL_DOMAIN_NOT_ALLOWED,
    }


async def test_registration_rejects_a_duplicate_email(usecase: AuthUsecase) -> None:
    await register_user(usecase)

    with pytest.raises(AuthError) as excinfo:
        await register_user(usecase, email="VIEWER@sapeet.com")

    assert excinfo.value.code is AuthErrorCode.EMAIL_ALREADY_REGISTERED


async def test_registration_enforces_the_password_policy(
    usecase: AuthUsecase,
) -> None:
    with pytest.raises(PasswordPolicyError):
        await usecase.register(email=EMAIL, display_name="X", password="short")


async def test_registration_rejects_a_blank_display_name(
    usecase: AuthUsecase,
) -> None:
    with pytest.raises(AuthError) as excinfo:
        await usecase.register(email=EMAIL, display_name="   ", password=PASSWORD)

    assert excinfo.value.code is AuthErrorCode.DISPLAY_NAME_REQUIRED


# --- ログインとセッション -------------------------------------------------


async def test_login_issues_a_session(usecase: AuthUsecase) -> None:
    await register_user(usecase)

    issued = await usecase.login(email=EMAIL, password=PASSWORD)

    assert issued.raw_token
    principal = await usecase.resolve_session(issued.raw_token)
    assert principal is not None
    assert principal.role is Role.VIEWER


async def test_the_raw_token_is_never_stored(
    usecase: AuthUsecase, db: AsyncSession
) -> None:
    """⚠️ DB が漏れてもセッションを乗っ取れないこと。"""
    await register_user(usecase)
    issued = await usecase.login(email=EMAIL, password=PASSWORD)

    stored = (await db.execute(select(Session))).scalar_one()

    assert stored.session_id != issued.raw_token
    assert stored.session_id == hash_session_token(issued.raw_token)
    assert len(stored.session_id) == 64  # SHA-256 の16進表現


async def test_session_tokens_are_unpredictable(usecase: AuthUsecase) -> None:
    """推測困難な乱数であること（長さと重複しないことで確認する）。"""
    await register_user(usecase)

    tokens = set()
    for _ in range(5):
        issued = await usecase.login(email=EMAIL, password=PASSWORD)
        tokens.add(issued.raw_token)

    assert len(tokens) == 5
    # secrets.token_urlsafe(32) は 256 ビット由来で 43 文字になる。
    assert all(len(token) >= 40 for token in tokens)


async def test_resolve_returns_none_for_an_unknown_token(
    usecase: AuthUsecase,
) -> None:
    assert await usecase.resolve_session("not-a-real-token") is None
    assert await usecase.resolve_session("") is None


# --- ⚠️ 失敗理由を区別しない ---------------------------------------------


async def test_login_failures_are_indistinguishable(usecase: AuthUsecase) -> None:
    """存在しないアカウントとパスワード違いで**同じ**エラーになること。

    区別すると、どのアドレスが実在するかを外部から列挙できる。
    """
    await register_user(usecase)

    with pytest.raises(AuthError) as wrong_password:
        await usecase.login(email=EMAIL, password="wrong password here")

    with pytest.raises(AuthError) as unknown_account:
        await usecase.login(email="nobody@sapeet.com", password=PASSWORD)

    assert wrong_password.value.code == unknown_account.value.code
    assert wrong_password.value.message == unknown_account.value.message
    assert wrong_password.value.message == LOGIN_FAILED_MESSAGE


async def test_an_inactive_user_gets_the_same_error(usecase: AuthUsecase) -> None:
    """停止済みであることも漏らさない。"""
    user = await register_user(usecase)
    user.is_active = False

    with pytest.raises(AuthError) as excinfo:
        await usecase.login(email=EMAIL, password=PASSWORD)

    assert excinfo.value.message == LOGIN_FAILED_MESSAGE


async def test_a_locked_account_gets_the_same_error(
    usecase: AuthUsecase, frozen_clock: list[datetime]
) -> None:
    """ロック中であることも漏らさない（「ロックされました」と言わない）。"""
    await register_user(usecase)

    for _ in range(LOGIN_POLICY.max_failed_attempts):
        with pytest.raises(AuthError):
            await usecase.login(email=EMAIL, password="wrong password here")

    # 正しいパスワードでもロック中は入れない。文言は同一。
    with pytest.raises(AuthError) as excinfo:
        await usecase.login(email=EMAIL, password=PASSWORD)

    assert excinfo.value.message == LOGIN_FAILED_MESSAGE


async def test_the_lock_expires(
    usecase: AuthUsecase, frozen_clock: list[datetime]
) -> None:
    await register_user(usecase)
    for _ in range(LOGIN_POLICY.max_failed_attempts):
        with pytest.raises(AuthError):
            await usecase.login(email=EMAIL, password="wrong password here")

    frozen_clock[0] += LOGIN_POLICY.lockout_duration + timedelta(seconds=1)

    issued = await usecase.login(email=EMAIL, password=PASSWORD)
    assert issued.raw_token


async def test_a_successful_login_resets_the_failure_counter(
    usecase: AuthUsecase, db: AsyncSession
) -> None:
    user = await register_user(usecase)
    for _ in range(LOGIN_POLICY.max_failed_attempts - 1):
        with pytest.raises(AuthError):
            await usecase.login(email=EMAIL, password="wrong password here")

    await usecase.login(email=EMAIL, password=PASSWORD)

    await db.refresh(user)
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


async def test_login_verifies_a_hash_even_for_an_unknown_account(
    usecase: AuthUsecase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ 応答時間差でアカウントの存在が漏れないこと。

    存在しないアカウントでも `verify_password` が呼ばれる（＝bcrypt の
    照合コストを必ず払う）ことを、呼び出し回数で確認する。
    """
    calls: list[str] = []
    original = auth_module.verify_password

    def counting_verify(password: str, password_hash: str) -> bool:
        calls.append(password_hash)
        return original(password, password_hash)

    monkeypatch.setattr(auth_module, "verify_password", counting_verify)

    with pytest.raises(AuthError):
        await usecase.login(email="nobody@sapeet.com", password=PASSWORD)

    assert len(calls) == 1
    assert calls[0].startswith("$2b$")


# --- ログアウト -----------------------------------------------------------


async def test_logout_revokes_the_session_on_the_server(
    usecase: AuthUsecase, db: AsyncSession
) -> None:
    """⚠️ Cookie を消すだけでは不十分。**サーバー側で無効化**すること。

    Cookie のコピーを持たれていても、失効済みなら通らないことを確認する。
    """
    await register_user(usecase)
    issued = await usecase.login(email=EMAIL, password=PASSWORD)

    await usecase.logout(issued.raw_token)

    # 同じトークンをそのまま提示しても通らない。
    assert await usecase.resolve_session(issued.raw_token) is None

    stored = (await db.execute(select(Session))).scalar_one()
    assert stored.revoked_at is not None


async def test_logout_is_idempotent(usecase: AuthUsecase) -> None:
    await register_user(usecase)
    issued = await usecase.login(email=EMAIL, password=PASSWORD)

    await usecase.logout(issued.raw_token)
    await usecase.logout(issued.raw_token)
    await usecase.logout("")

    assert await usecase.resolve_session(issued.raw_token) is None


async def test_logout_does_not_affect_other_sessions(usecase: AuthUsecase) -> None:
    """別端末のセッションは生きたまま（ログアウトは1セッション単位）。"""
    await register_user(usecase)
    first = await usecase.login(email=EMAIL, password=PASSWORD)
    second = await usecase.login(email=EMAIL, password=PASSWORD)

    await usecase.logout(first.raw_token)

    assert await usecase.resolve_session(first.raw_token) is None
    assert await usecase.resolve_session(second.raw_token) is not None


# --- 有効期限 -------------------------------------------------------------


async def test_the_idle_timeout_expires_a_session(
    usecase: AuthUsecase, frozen_clock: list[datetime]
) -> None:
    await register_user(usecase)
    issued = await usecase.login(email=EMAIL, password=PASSWORD)

    frozen_clock[0] += SESSION_POLICY.idle_timeout + timedelta(seconds=1)

    assert await usecase.resolve_session(issued.raw_token) is None


async def test_activity_extends_the_idle_timeout(
    usecase: AuthUsecase, frozen_clock: list[datetime]
) -> None:
    await register_user(usecase)
    issued = await usecase.login(email=EMAIL, password=PASSWORD)

    # アイドル期限の手前でアクセスし続ければ切れない。
    for _ in range(3):
        frozen_clock[0] += SESSION_POLICY.idle_timeout - timedelta(minutes=1)
        assert await usecase.resolve_session(issued.raw_token) is not None


async def test_the_absolute_lifetime_is_not_extended_by_activity(
    usecase: AuthUsecase, frozen_clock: list[datetime]
) -> None:
    """⚠️ 使い続けても7日で必ず切れる（乗っ取りが無期限に生き残らない）。"""
    await register_user(usecase)
    issued = await usecase.login(email=EMAIL, password=PASSWORD)

    # アイドル期限内で触り続ける。
    elapsed = timedelta()
    while elapsed < SESSION_POLICY.absolute_lifetime:
        step = SESSION_POLICY.idle_timeout - timedelta(minutes=1)
        frozen_clock[0] += step
        elapsed += step
        await usecase.resolve_session(issued.raw_token)

    assert await usecase.resolve_session(issued.raw_token) is None


async def test_expired_sessions_are_purged_on_login(
    usecase: AuthUsecase, db: AsyncSession, frozen_clock: list[datetime]
) -> None:
    """テーブルが単調増加しないこと。"""
    await register_user(usecase)
    await usecase.login(email=EMAIL, password=PASSWORD)

    frozen_clock[0] += SESSION_POLICY.absolute_lifetime + timedelta(seconds=1)
    await usecase.login(email=EMAIL, password=PASSWORD)

    remaining = (await db.execute(select(Session))).scalars().all()
    assert len(remaining) == 1


# --- ロールはセッションに焼き込まない ------------------------------------


async def test_a_role_change_takes_effect_without_re_login(
    usecase: AuthUsecase, db: AsyncSession
) -> None:
    """⚠️ §1.1 で JWT を採らなかった理由そのもの。

    admin が昇格させたら、**再ログインなしで**次のリクエストから効くこと。
    """
    user = await register_user(usecase)
    issued = await usecase.login(email=EMAIL, password=PASSWORD)

    assert (await usecase.resolve_session(issued.raw_token)).role is Role.VIEWER  # type: ignore[union-attr]

    user.role = Role.EDITOR
    await db.commit()

    principal = await usecase.resolve_session(issued.raw_token)
    assert principal is not None
    assert principal.role is Role.EDITOR


async def test_deactivating_a_user_kills_live_sessions(
    usecase: AuthUsecase, db: AsyncSession
) -> None:
    user = await register_user(usecase)
    issued = await usecase.login(email=EMAIL, password=PASSWORD)

    user.is_active = False
    await db.commit()

    assert await usecase.resolve_session(issued.raw_token) is None


# --- パスワード変更 -------------------------------------------------------


async def test_changing_the_password_revokes_all_sessions(
    usecase: AuthUsecase,
) -> None:
    user = await register_user(usecase)
    first = await usecase.login(email=EMAIL, password=PASSWORD)
    second = await usecase.login(email=EMAIL, password=PASSWORD)

    await usecase.change_password(
        user_id=user.user_id,
        current_password=PASSWORD,
        new_password="a brand new passphrase",
    )

    assert await usecase.resolve_session(first.raw_token) is None
    assert await usecase.resolve_session(second.raw_token) is None


async def test_the_new_password_works_and_the_old_one_does_not(
    usecase: AuthUsecase,
) -> None:
    user = await register_user(usecase)
    new_password = "a brand new passphrase"

    await usecase.change_password(
        user_id=user.user_id, current_password=PASSWORD, new_password=new_password
    )

    with pytest.raises(AuthError):
        await usecase.login(email=EMAIL, password=PASSWORD)

    assert await usecase.login(email=EMAIL, password=new_password)


async def test_changing_the_password_requires_the_current_one(
    usecase: AuthUsecase,
) -> None:
    user = await register_user(usecase)

    with pytest.raises(AuthError) as excinfo:
        await usecase.change_password(
            user_id=user.user_id,
            current_password="wrong password here",
            new_password="a brand new passphrase",
        )

    assert excinfo.value.code is AuthErrorCode.INVALID_CREDENTIALS


async def test_the_new_password_must_satisfy_the_policy(
    usecase: AuthUsecase,
) -> None:
    user = await register_user(usecase)

    with pytest.raises(PasswordPolicyError):
        await usecase.change_password(
            user_id=user.user_id, current_password=PASSWORD, new_password="short"
        )
