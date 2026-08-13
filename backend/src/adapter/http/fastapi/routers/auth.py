"""認証エンドポイント（T-40）。

`POST /auth/register` / `POST /auth/login` は**未認証で到達可**（public）。
`POST /auth/logout` / `GET /auth/me` / `POST /auth/password` は認証済みの全ロール可
（T-09 の権限マトリクスに追加済み）。

⚠️ **Cookie の属性を緩めないこと。** `HttpOnly` を外すと JS から盗める。
`SameSite` を外すと CSRF 面が広がる。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from adapter.http.fastapi.auth.dependencies import (
    get_auth_usecase,
    get_session_token,
    require_principal,
)
from adapter.http.fastapi.auth.session_backend import SESSION_COOKIE_NAME
from application.usecases.auth import (
    AuthError,
    AuthErrorCode,
    AuthUsecase,
    IssuedSession,
)
from config import get_settings
from enterprise.entities.principal import Principal, Role
from enterprise.services.password import PasswordPolicyError

router = APIRouter(prefix="/auth", tags=["auth"])

# 登録の失敗のうち、409（重複）として返すもの。それ以外は 422。
_CONFLICT_CODES = frozenset({AuthErrorCode.EMAIL_ALREADY_REGISTERED})


class _StrictModel(BaseModel):
    """未知のキーを拒否する。

    ⚠️ これが **`role` を送り込めないこと**の実体。`{"role": "admin"}` を
    足したリクエストは 422 になる（TASKS.md §1.1「昇格は admin のみ」）。
    """

    model_config = ConfigDict(extra="forbid")


class RegisterRequest(_StrictModel):
    # ⚠️ **ロールのフィールドを足さないこと。** 自己登録は常に viewer。
    email: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1)


class LoginRequest(_StrictModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1)


class ChangePasswordRequest(_StrictModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


class UserResponse(BaseModel):
    """⚠️ `password_hash` を**絶対に含めない**。"""

    user_id: str
    email: str
    display_name: str
    role: Role


def _set_session_cookie(response: Response, issued: IssuedSession) -> None:
    """セッション Cookie を付与する。

    - `httponly=True`  … JS（`document.cookie`）から読めない＝XSS で盗まれない
    - `samesite="lax"` … クロスサイトの POST に載らない＝CSRF の主要面を塞ぐ
    - `secure`         … 設定値。本番（HTTPS）では必ず true
    - `path="/"`       … API 全体へ送る
    """
    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=issued.raw_token,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
        expires=issued.expires_at,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    usecase: Annotated[AuthUsecase, Depends(get_auth_usecase)],
) -> UserResponse:
    """自己登録。**作成されるのは必ず `viewer`**。

    昇格は admin のみ（T-42）。ここでセッションは発行しない（登録後に
    改めてログインする）。
    """
    try:
        user = await usecase.register(
            email=body.email,
            display_name=body.display_name,
            password=body.password,
        )
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "validation_failed",
                "issues": [issue.model_dump() for issue in exc.issues],
            },
        ) from exc
    except AuthError as exc:
        status_code = (
            status.HTTP_409_CONFLICT
            if exc.code in _CONFLICT_CODES
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(
            status_code=status_code,
            detail={"error": exc.code.value, "message": exc.message},
        ) from exc

    return UserResponse(
        user_id=user.user_id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
    )


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    usecase: Annotated[AuthUsecase, Depends(get_auth_usecase)],
) -> dict[str, str]:
    """ログイン。成功でセッションを発行し Cookie を付与する。

    ⚠️ **失敗は理由を問わず 401 ＋ 同一文言**。存在しないアドレスか
    パスワード違いかを区別させない。
    """
    try:
        issued = await usecase.login(email=body.email, password=body.password)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": exc.code.value, "message": exc.message},
        ) from exc

    _set_session_cookie(response, issued)
    return {"status": "ok"}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    usecase: Annotated[AuthUsecase, Depends(get_auth_usecase)],
    raw_token: Annotated[str | None, Depends(get_session_token)],
) -> None:
    """ログアウト。**べき等**（未ログインで叩いても 204）。

    ⚠️ Cookie の削除だけで済ませない。**サーバー側で失効させる**のが本体で、
    Cookie 削除は利用者の手元を片付ける補助にすぎない。
    """
    if raw_token:
        await usecase.logout(raw_token)

    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


@router.get("/me")
async def me(
    principal: Annotated[Principal, Depends(require_principal)],
    usecase: Annotated[AuthUsecase, Depends(get_auth_usecase)],
) -> UserResponse:
    """ログイン中の利用者。フロントのロール出し分けの入力（T-32・T-36・T-43）。"""
    user = await usecase.get_user(principal.subject)
    if user is None:
        # セッションは有効だがユーザーが消えている（削除直後など）。
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ログインが必要です。",
        )

    return UserResponse(
        user_id=user.user_id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
    )


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    response: Response,
    principal: Annotated[Principal, Depends(require_principal)],
    usecase: Annotated[AuthUsecase, Depends(get_auth_usecase)],
) -> None:
    """自分のパスワードを変更する。

    現在のパスワードを要求し、成功時は**そのユーザーの全セッションを失効**させる
    （乗っ取られていた場合に追い出すのが目的なので、操作中のセッションも切る）。
    """
    try:
        await usecase.change_password(
            user_id=principal.subject,
            current_password=body.current_password,
            new_password=body.new_password,
        )
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "validation_failed",
                "issues": [issue.model_dump() for issue in exc.issues],
            },
        ) from exc
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": exc.code.value, "message": exc.message},
        ) from exc

    # 全セッションを切ったので、手元の Cookie も無効。片付けておく。
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
