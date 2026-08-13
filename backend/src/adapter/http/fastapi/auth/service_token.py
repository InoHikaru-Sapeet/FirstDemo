"""サービストークンによる `AuthenticationBackend` の実装（T-41）。

cron（`system`）は Cookie を持てないため、`Authorization: Bearer <token>` を
別経路として受け付ける。トークンの発行・照合そのものは
`enterprise.services.service_token` にあり、ここは HTTP ヘッダの取り出しだけを担う。

⚠️ **ここに認可（権限判定）を書かない。** 返すのは「呼び出し元は system である」
という事実だけで、`system` が何をしてよいか（§6.2 では内部読込のみ）は
T-09 の RBAC が決める。

⚠️ **`users` テーブルを見ない。** `system` はログインする利用者ではないので
行を持たない（DB の CHECK 制約で禁止。T-08）。ここだけは DB を引かずに
`Principal` を組み立てる。
"""

from fastapi import Request

from common.logger import logger
from enterprise.entities.principal import Principal, Role
from enterprise.services.service_token import (
    SERVICE_TOKEN_SUBJECT,
    looks_like_service_token_hash,
    verify_service_token,
)

# RFC 6750 の Bearer スキーム。大文字小文字を区別せずに判定する。
BEARER_SCHEME = "bearer"


def extract_bearer_token(authorization: str | None) -> str | None:
    """`Authorization: Bearer <token>` から生トークンを取り出す。

    スキームが違う・値が空・ヘッダが無いのいずれでも `None` を返す
    （**理由を区別しない**＝`backend.py` のプロトコルの約束）。
    """
    if not authorization:
        return None

    scheme, separator, credentials = authorization.partition(" ")
    if not separator or scheme.lower() != BEARER_SCHEME:
        return None

    token = credentials.strip()
    return token or None


class ServiceTokenAuthenticationBackend:
    """Bearer トークン → `Principal(role=system)`。

    **設定にハッシュが無ければ、この経路は完全に無効**（常に `None`）。
    「未設定なら誰でも system」になるのを防ぐため、無効化を既定の側に置いている。
    """

    def __init__(self, expected_hash: str) -> None:
        self._expected_hash = expected_hash.strip()

        if self._expected_hash and not looks_like_service_token_hash(
            self._expected_hash
        ):
            # 最も起きやすい運用ミスは「生トークンをそのまま貼る」こと。
            # 照合は失敗する（安全側）が、気づけないと原因不明の 401 になる。
            # ⚠️ 設定値そのものはログに出さない（ハッシュでも秘密として扱う）。
            logger.warning(
                "SERVICE_TOKEN_HASH がハッシュの形式ではありません"
                "（64桁の16進を期待）。`make service-token` が出力した"
                "SERVICE_TOKEN_HASH= の行を設定してください。"
                "system 経路は無効のままです。"
            )
            self._expected_hash = ""

    @property
    def is_enabled(self) -> bool:
        """system 経路が有効か（設定にハッシュがあるか）。"""
        return bool(self._expected_hash)

    async def resolve(self, request: Request) -> Principal | None:
        if not self._expected_hash:
            return None

        token = extract_bearer_token(request.headers.get("authorization"))
        if token is None:
            return None

        if not verify_service_token(token, self._expected_hash):
            return None

        return Principal(subject=SERVICE_TOKEN_SUBJECT, role=Role.SYSTEM)
