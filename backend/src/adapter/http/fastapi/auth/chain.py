"""複数の認証方式を順に試す `AuthenticationBackend`（T-41）。

人（Cookie セッション）と cron（サービストークン）は資格情報の持ち方が違うが、
**認可（T-09）にはどちらも `Principal` として同じ形で渡す**。その合流点がここ。

差し替え口は増やさない：`get_authentication_backend()`（`dependencies.py`）が
返すのは相変わらず1つの `AuthenticationBackend` で、その中身がこの合成になる。

⚠️ **順序に意味がある。** サービストークンを先に試す:

- cron は Cookie を持たないので、セッション解決を先に走らせても外れる（DB を1回
  無駄に引く）
- Bearer ヘッダが明示的に提示されているなら、それが呼び出し元の意図
- 逆順にすると「ブラウザの Cookie と Bearer が同時にある」場合に Cookie が勝ち、
  cron 用トークンで意図せず人のロールになりうる

⚠️ **最初に `Principal` を返した実装で確定する。** 複数を突き合わせて権限を
足し合わせるようなことはしない（認可が2箇所に散る）。
"""

from fastapi import Request

from adapter.http.fastapi.auth.backend import AuthenticationBackend
from enterprise.entities.principal import Principal


class ChainedAuthenticationBackend:
    """渡された順に `resolve()` を試し、最初の成功を返す。

    どれも解決できなければ `None`（未認証）。**理由は区別しない。**
    """

    def __init__(self, *backends: AuthenticationBackend) -> None:
        self._backends = backends

    async def resolve(self, request: Request) -> Principal | None:
        for backend in self._backends:
            principal = await backend.resolve(request)
            if principal is not None:
                return principal
        return None
