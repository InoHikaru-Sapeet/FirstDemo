"""認証バックエンドの差し替え口（設計書 §4.1 ／ TASKS.md T-08）。

「リクエスト → `Principal`」の解決だけをこのプロトコルの内側に閉じ込める。
認可（T-09）は `Principal` しか見ないので、**認証方式を変えても §6.2 の
権限マトリクスに手を入れずに済む**。

差し替え方は1箇所だけ:

    # src/adapter/http/fastapi/auth/dependencies.py（T-40 で作成）
    def get_authentication_backend() -> AuthenticationBackend:
        return SessionAuthenticationBackend(...)   # ← ここを差し替える

FastAPI の依存として注入するため、テストでは
`app.dependency_overrides[get_authentication_backend]` で差し替えられる。

---

⚠️ **この差し替え口があること＝SSO 対応済み、ではない。**

2026-08-13 の方針変更（TASKS.md §1.1「備考：SSO 前提からの差分」）で
**SSO 連携はやらない**ことが確定し、開発用の認証スタブも廃止した。
現行の実装は ID/PW 認証（T-40 の `SessionAuthenticationBackend`）ひとつだけ。

将来 SSO を足す場合、このプロトコルの実装を1つ増やすだけでは終わらない。
少なくとも「ロールの正は本アプリの `users.role` か IdP のクレームか」を
決め直す必要がある（docs/future-roadmap.md 構想3 の表を参照）。
"""

from typing import Protocol, runtime_checkable

from fastapi import Request

from enterprise.entities.principal import Principal


@runtime_checkable
class AuthenticationBackend(Protocol):
    """リクエストから呼び出し元を解決する。

    ⚠️ **認証だけを行い、認可を判定しない。** 「このユーザーがこの操作を
    してよいか」は T-09 の RBAC の責務。ここで権限を見ると、判定が2箇所に
    散って §6.2 のマトリクスとテストの1:1 対応が崩れる。
    """

    async def resolve(self, request: Request) -> Principal | None:
        """認証済みなら `Principal`、未認証なら `None` を返す。

        **例外で「未認証」を表現しないこと。** 未認証（401）と権限なし（403）は
        HTTP 層で区別して返す必要があり（T-09）、その判断材料は呼び出し側が持つ。

        資格情報が提示されているが不正な場合（期限切れセッション・不正な
        サービストークン）も `None` を返す。**理由を呼び出し元に区別させない**
        ことで、アカウントの存在有無が漏れる経路を作らない（T-40）。
        """
        ...
