"""ユーザー管理エンドポイント（T-42）。**すべて admin のみ**。

`GET /users` / `PATCH /users/{user_id}/role` / `PATCH /users/{user_id}/status`。
未認証は 401、認証済みだが admin でなければ 403（`require_admin`）。

⚠️ **`password_hash` を返さない。** レスポンスは `UserSummary` に詰め替える形に
してあり、ORM の `User` をそのまま返す経路を作らないこと。

⚠️ **`role` を上げられる API はここだけ。** 自己登録（T-40）は `role` を受け取らず
必ず `viewer` を作る。この非対称が「昇格は admin のみ」（TASKS.md §1.1）の実体。

⚠️ **これらのパスは設計書 §3.2 のエンドポイント表に無い**（2026-08-13 の方針変更で
増えた分）。§3.2 の表と §4.4 の監査イベント enum の更新が必要（→ T-38）。
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from adapter.database.models.user import User
from adapter.http.fastapi.auth.dependencies import (
    get_manage_users_usecase,
    require_admin,
)
from application.usecases.manage_users import (
    ManageUsersError,
    ManageUsersErrorCode,
    ManageUsersUsecase,
)
from enterprise.entities.principal import Principal, Role

router = APIRouter(prefix="/users", tags=["users"])

# 業務エラー → HTTP ステータス。ここに無いコードは 422 に落ちる。
_STATUS_BY_CODE = {
    ManageUsersErrorCode.USER_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ManageUsersErrorCode.LAST_ADMIN: status.HTTP_409_CONFLICT,
    ManageUsersErrorCode.ROLE_NOT_ASSIGNABLE: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


class _StrictModel(BaseModel):
    """未知のキーを拒否する（余計なフィールドを黙って無視しない）。"""

    model_config = ConfigDict(extra="forbid")


class ChangeRoleRequest(_StrictModel):
    """⚠️ `system` を**型で**弾く（OpenAPI にも3値しか出ない＝T-31 の型生成に効く）。

    `enterprise.entities.principal.ASSIGNABLE_ROLES` と同じ集合であることを
    テストで固定してある（片方だけ増えると `system` が通る）。
    """

    role: Literal[Role.ADMIN, Role.EDITOR, Role.VIEWER]


class ChangeStatusRequest(_StrictModel):
    is_active: bool


class UserSummary(BaseModel):
    """⚠️ `password_hash` を**絶対に含めない**（T-08 のモデル docstring と対）。"""

    user_id: str
    email: str
    display_name: str
    role: Role
    is_active: bool
    created_at: datetime


class UserListResponse(BaseModel):
    items: list[UserSummary]


def _to_summary(user: User) -> UserSummary:
    return UserSummary(
        user_id=user.user_id,
        email=user.email,
        display_name=user.display_name,
        role=Role(user.role),
        is_active=user.is_active,
        created_at=user.created_at,
    )


def _to_http_error(exc: ManageUsersError) -> HTTPException:
    return HTTPException(
        status_code=_STATUS_BY_CODE.get(
            exc.code, status.HTTP_422_UNPROCESSABLE_CONTENT
        ),
        detail={"error": exc.code.value, "message": exc.message},
    )


@router.get("")
async def list_users(
    _admin: Annotated[Principal, Depends(require_admin)],
    usecase: Annotated[ManageUsersUsecase, Depends(get_manage_users_usecase)],
) -> UserListResponse:
    """ユーザー一覧（admin のみ）。管理画面のユーザー管理タブ（T-32）の入力。"""
    users = await usecase.list_users()
    return UserListResponse(items=[_to_summary(user) for user in users])


@router.patch("/{user_id}/role")
async def change_role(
    user_id: str,
    body: ChangeRoleRequest,
    admin: Annotated[Principal, Depends(require_admin)],
    usecase: Annotated[ManageUsersUsecase, Depends(get_manage_users_usecase)],
) -> UserSummary:
    """ロールの昇格・降格（admin のみ）。

    - 対象が居なければ **404**
    - `system` 指定は **422**（リクエストモデルの型で先に弾かれる）
    - 最後の admin の降格は **409**（自分自身を含む）

    降格は**再ログインを待たずに次のリクエストから効く**（ロールは毎回
    `users` 行から解決するため。TASKS.md §1.1「ログイン状態の保持」）。
    """
    try:
        user = await usecase.change_role(
            actor=admin, user_id=user_id, new_role=Role(body.role)
        )
    except ManageUsersError as exc:
        raise _to_http_error(exc) from exc

    return _to_summary(user)


@router.patch("/{user_id}/status")
async def change_status(
    user_id: str,
    body: ChangeStatusRequest,
    admin: Annotated[Principal, Depends(require_admin)],
    usecase: Annotated[ManageUsersUsecase, Depends(get_manage_users_usecase)],
) -> UserSummary:
    """アカウントの停止・再開（admin のみ）。

    停止時は**そのユーザーのセッションを全失効**させる。最後の admin の停止は
    **409**（降格と同じ理由：admin が0人になると CLI 以外で復旧できない）。
    """
    try:
        user = await usecase.change_status(
            actor=admin, user_id=user_id, is_active=body.is_active
        )
    except ManageUsersError as exc:
        raise _to_http_error(exc) from exc

    return _to_summary(user)
