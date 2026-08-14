"""認証の DI（T-40）。**差し替えはこのファイルの1箇所で完結する。**

将来 SSO 等へ差し替える場合は `get_authentication_backend()` の戻り値を
変えるだけでよい（`backend.py` の説明どおり）。テストでは
`app.dependency_overrides[get_authentication_backend]` で差し替えられる。

⚠️ **未認証は 401、認証済みだが権限なしは 403。** 両者を混ぜると、フロントが
「ログインへ誘導すべきか」「権限不足を表示すべきか」を判断できなくなる（T-43）。

**認可の判定そのものは `auth/rbac.py` の権限マトリクスが持つ**（T-09。2026-08-14
実施）。このモジュールがやるのは「リクエスト → オペレーション」の解決と、判定結果を
HTTP ステータスへ変換することだけ。**ここでロールを見て分岐しないこと。**
"""

import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.database.database import db_manager
from adapter.http.fastapi.auth.backend import AuthenticationBackend
from adapter.http.fastapi.auth.chain import ChainedAuthenticationBackend
from adapter.http.fastapi.auth.rbac import (
    Operation,
    Outcome,
    authorize,
    resolve_operation,
)
from adapter.http.fastapi.auth.service_token import ServiceTokenAuthenticationBackend
from adapter.http.fastapi.auth.session_backend import (
    SESSION_COOKIE_NAME,
    SessionAuthenticationBackend,
)
from application.usecases.auth import AuthUsecase, LoginPolicy, SessionPolicy
from application.usecases.manage_users import ManageUsersUsecase
from config import Settings, get_settings
from enterprise.entities.principal import Principal

logger = logging.getLogger(__name__)

# 認可の失敗で返す文言。**config の存在・中身をほのめかさない**（仕様書 §2・§6.1）。
# 権限の話だけをする＝ revision も項目名も enum 値も出さない。
UNAUTHENTICATED_MESSAGE = "ログインが必要です。"
FORBIDDEN_MESSAGE = "この操作には管理者権限が必要です。"


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


def _resolve_request_operation(request: Request) -> Operation | None:
    """リクエストが叩いたルートを `Operation` へ解決する。

    **実パス（`/users/usr_123/role`）ではなくルートのテンプレート
    （`/users/{user_id}/role`）で引く。** FastAPI が routing 時に
    `scope["route"]` へ APIRoute を入れる（`fastapi/routing.py` の
    `APIRoute.matches`）ので、そこから `path` を取れば prefix 込みの
    テンプレートが得られる。実パスで引くと ID ごとに別のキーになってしまう。
    """
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if not isinstance(route_path, str):
        return None
    return resolve_operation(request.method, route_path)


async def require_principal(
    principal: Annotated[Principal | None, Depends(get_current_principal)],
) -> Principal:
    """認証必須の入口。未認証は **401**。ロールは見ない。

    §6.2 の「認証済みの全ロール可」に相当する経路（`GET /auth/me` /
    `POST /auth/password`）が使う。マトリクス上もこの2つは4ロールすべて `allow`
    なので、`require_permission` を通しても結果は同じになる
    （`test_rbac.py::test_the_authenticated_auth_routes_allow_every_role` で固定）。
    """
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=UNAUTHENTICATED_MESSAGE,
        )
    return principal


async def require_permission(
    request: Request,
    principal: Annotated[Principal | None, Depends(get_current_principal)],
) -> Principal:
    """**認可の入口。判定は `auth/rbac.py` の権限マトリクスに委ねる**（T-09）。

    リクエストが叩いたルートから `Operation` を引き、`authorize()` の結果を
    HTTP ステータスへ変換する。**この関数にロール別の分岐を書かないこと**
    （書いた瞬間、判定の正がマトリクスとここの2箇所に分かれる）。

    ⚠️ **マトリクスに行の無いルートは 403 で落とす（fail-closed）。**
    「まだ登録していないだけ」と「許可されていない」を実行時に区別できない以上、
    通す側へ倒すと、認可を書き忘れた新エンドポイントが素通りする。登録漏れは
    `test_rbac.py::test_every_route_is_covered_by_the_matrix` が検出する。
    """
    operation = _resolve_request_operation(request)
    if operation is None:
        logger.warning(
            "権限マトリクスに行の無いルートへの要求を拒否した: %s %s",
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=FORBIDDEN_MESSAGE,
        )

    outcome = authorize(operation, principal.role if principal else None)
    if outcome is Outcome.UNAUTHENTICATED:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=UNAUTHENTICATED_MESSAGE,
        )
    if outcome is Outcome.FORBIDDEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=FORBIDDEN_MESSAGE,
        )

    if principal is None:
        # public なオペレーション（未認証で到達可）にこの依存を付けた場合だけ
        # 到達する。認可の失敗ではなく**ルーターの設定ミス**なので隠さない。
        raise RuntimeError(
            f"public なオペレーション {operation} に require_permission を"
            "付けている。未認証の呼び出し元には Principal が存在しない。"
        )
    return principal


async def require_admin(
    principal: Annotated[Principal, Depends(require_permission)],
) -> Principal:
    """admin 限定オペレーション用の別名。**判定の実体は `require_permission`。**

    呼び出し側（config 3本・users 3本）が「ここは admin 限定」と読めるように
    名前だけ残してある。未認証は **401**、認証済みだが許可されなければ **403**。

    ⚠️ `system`（cron）も **403**。ユーザー管理も config も人が行う操作で、
    §6.2 の `internal_only`（パイプラインの内部読込）には当てはまらない。

    ⚠️ **この名前が実態と合っているかはテストが検査する。** マトリクス上 admin
    限定でないルートにこれを付けると
    `test_rbac.py::test_require_admin_is_only_used_on_admin_only_routes` が落ちる。

    （2026-08-14 実施）T-42 着手時に先取りしていた独自のロール判定は、この形で
    権限マトリクス由来へ置き換えた。呼び出し側の import と使い方は変えていない。
    """
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
