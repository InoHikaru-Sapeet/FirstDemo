"""CSRF 対策：`Origin` ヘッダの検証（T-40）。

Cookie 認証は「ブラウザが自動で資格情報を送る」仕組みなので、悪意あるサイトから
の POST でも Cookie が付く可能性がある。これを2段構えで防ぐ:

1. **`SameSite=Lax`**（Cookie 側）— クロスサイトの POST に Cookie を載せない
2. **`Origin` の検証**（このモジュール）— 許可外オリジンからの更新系を 403

---

⚠️ **既定の `cors_allowed_origins` は `*` で、その場合この検証は素通りする。**
本番では実際のオリジンを設定すること。設定しない限り、CSRF 対策は
`SameSite=Lax` の1枚だけになる。

⚠️ **`Origin` が無いリクエストは通す。** cron や curl のような非ブラウザ
クライアント（T-41 のサービストークン経由）は `Origin` を送らないため。
ブラウザは更新系リクエストに `Origin` を付けるので、この緩和は
「ブラウザからの CSRF」には効かない。
"""

from collections.abc import Awaitable, Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse

# 本体を変えうるメソッドだけを対象にする（GET/HEAD/OPTIONS は対象外）。
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

ALLOW_ANY_ORIGIN = "*"


def is_origin_allowed(origin: str | None, allowed_origins: list[str]) -> bool:
    """`Origin` が許可されているか。

    - `None`（ヘッダ無し）→ 許可（非ブラウザクライアント。上記⚠️）
    - 許可リストに `*` が含まれる → 許可
    """
    if origin is None:
        return True
    if ALLOW_ANY_ORIGIN in allowed_origins:
        return True
    return origin in allowed_origins


def build_csrf_middleware(
    allowed_origins: list[str],
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    """`app.middleware("http")` に登録する検証関数を作る。"""

    async def verify_origin(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method in STATE_CHANGING_METHODS and not is_origin_allowed(
            request.headers.get("origin"), allowed_origins
        ):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "許可されていないオリジンからのリクエストです。"},
            )
        return await call_next(request)

    return verify_origin
