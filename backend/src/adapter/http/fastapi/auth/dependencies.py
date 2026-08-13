"""認証の DI（T-40）。**差し替えはこのファイルの1箇所で完結する。**

将来 SSO 等へ差し替える場合は `get_authentication_backend()` の戻り値を
変えるだけでよい（`backend.py` の説明どおり）。テストでは
`app.dependency_overrides[get_authentication_backend]` で差し替えられる。

⚠️ **未認証は 401、認証済みだが権限なしは 403。** 両者を混ぜると、フロントが
「ログインへ誘導すべきか」「権限不足を表示すべきか」を判断できなくなる（T-43）。

⚠️ 認可（403）の本体は T-09（`auth/rbac.py`）だが未着手のため、T-42 が必要と
する admin 限定の判定だけを `require_admin()` として先取りしてある。
**T-09 の完成時にそちらへ寄せること**（同関数の docstring 参照）。
"""

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.database.database import db_manager
from adapter.http.fastapi.auth.backend import AuthenticationBackend
from adapter.http.fastapi.auth.chain import ChainedAuthenticationBackend
from adapter.http.fastapi.auth.service_token import ServiceTokenAuthenticationBackend
from adapter.http.fastapi.auth.session_backend import (
    SESSION_COOKIE_NAME,
    SessionAuthenticationBackend,
)
from application.usecases.auth import AuthUsecase, LoginPolicy, SessionPolicy
from application.usecases.manage_users import ManageUsersUsecase
from config import Settings, get_settings
from enterprise.entities.principal import Principal, Role


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with db_manager.session() as session:
        yield session


def build_session_policy(settings: Settings) -> SessionPolicy:
    return SessionPolicy(
        absolute_lifetime=timedelta(days=settings.session_absolute_lifetime_days),
        idle_timeout=timedelta(hours=settings.session_idle_timeout_hours),
    )


def build_login_policy(settings: Settings) -> LoginPolicy:
    return LoginPolicy(
        max_failed_attempts=settings.login_max_failed_attempts,
        lockout_duration=timedelta(minutes=settings.login_lockout_minutes),
    )


def get_auth_usecase(
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> AuthUsecase:
    settings = get_settings()
    return AuthUsecase(
        db=db,
        session_policy=build_session_policy(settings),
        login_policy=build_login_policy(settings),
        allowed_email_domains=settings.allowed_email_domains,
    )


def get_authentication_backend(
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> AuthenticationBackend:
    """⚠️ **認証方式の差し替え口はここ1箇所。**

    別方式（SSO 等）を足す場合は、`AuthenticationBackend` を実装した
    クラスを返すよう、この関数の戻り値だけを変える。

    現在は2方式の合成（`chain.py` 参照）:

    1. サービストークン（`Authorization: Bearer`）— cron（`system`）。
       `SERVICE_TOKEN_HASH` 未設定なら無効
    2. Cookie セッション（`sid`）— 人（admin / editor / viewer）
    """
    settings = get_settings()
    return ChainedAuthenticationBackend(
        ServiceTokenAuthenticationBackend(expected_hash=settings.service_token_hash),
        SessionAuthenticationBackend(
            db=db,
            session_policy=build_session_policy(settings),
            login_policy=build_login_policy(settings),
        ),
    )


async def get_current_principal(
    request: Request,
    backend: Annotated[AuthenticationBackend, Depends(get_authentication_backend)],
) -> Principal | None:
    """認証済みなら `Principal`、未認証なら `None`（**例外にしない**）。

    「ログインしていれば出し分けるが、していなくても見せる」画面のための入口。
    """
    return await backend.resolve(request)


async def require_principal(
    principal: Annotated[Principal | None, Depends(get_current_principal)],
) -> Principal:
    """認証必須の入口。未認証は **401**。

    権限（ロール）は見ない。ロール判定は T-09 の RBAC が行う。
    """
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ログインが必要です。",
        )
    return principal


async def require_admin(
    principal: Annotated[Principal, Depends(require_principal)],
) -> Principal:
    """admin 限定の入口。未認証は **401**、認証済みだが admin でなければ **403**。

    ⚠️ `system`（cron）も **403**。ユーザー管理は人が行う操作で、
    §6.2 の `internal_only`（内部読込のみ）にも当てはまらない。

    ⚠️ **これは T-09（RBAC）の先取り。** T-42（ユーザー管理 API）は
    admin 限定の判定なしには成立しないが、T-09 が未着手のため最小の実装を
    ここへ置いた。**T-09 で `auth/rbac.py` を作ったら、この関数はそちらの
    権限マトリクスから導出する形へ寄せること**（判定の正が2箇所に分かれた
    ままだと、マトリクスを直しても API が追随しない）。
    """
    if principal.role is not Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="この操作には管理者権限が必要です。",
        )
    return principal


def get_manage_users_usecase(
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ManageUsersUsecase:
    return ManageUsersUsecase(db)


def get_session_token(
    sid: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> str | None:
    """Cookie の生トークン。ログアウトが自分のセッションを特定するために使う。"""
    return sid
