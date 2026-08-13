"""Cookie セッションによる `AuthenticationBackend` の実装（T-40）。

`backend.py` のプロトコルを満たす唯一の実装。リクエストの Cookie から生トークンを
取り出し、application 層に解決を任せる。

⚠️ **ここに認可（権限判定）を書かない。** 誰であるかを返すだけ。
「その操作をしてよいか」は T-09 の RBAC の責務。
"""

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from application.usecases.auth import AuthUsecase, LoginPolicy, SessionPolicy
from enterprise.entities.principal import Principal

# Cookie 名。フロント（T-43）は値を読めない（HttpOnly）ので、名前を知る必要はない。
SESSION_COOKIE_NAME = "sid"


class SessionAuthenticationBackend:
    """Cookie の生トークン → `Principal`。

    セッションが無い・期限切れ・失効済み・停止ユーザーのいずれでも `None` を返し、
    **理由を区別しない**（backend.py のプロトコルの約束）。
    """

    def __init__(
        self,
        db: AsyncSession,
        session_policy: SessionPolicy,
        login_policy: LoginPolicy,
    ) -> None:
        self._usecase = AuthUsecase(
            db=db,
            session_policy=session_policy,
            login_policy=login_policy,
            allowed_email_domains=[],  # 解決時にはドメイン制限を使わない
        )

    async def resolve(self, request: Request) -> Principal | None:
        raw_token = request.cookies.get(SESSION_COOKIE_NAME)
        if not raw_token:
            return None
        return await self._usecase.resolve_session(raw_token)
