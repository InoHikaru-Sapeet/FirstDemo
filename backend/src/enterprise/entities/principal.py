"""呼び出し元の同一性（誰か）とロール（設計書 §4.1 ／ 仕様書 §2・§6.1）。

**認可（T-09）が参照してよいのはこのモジュールの型だけ**にする。
`Principal` はパスワード・セッション・トークンといった「認証の実現方法」を
一切知らない。この境界を保つことで、認証方式を差し替えても認可側
（§6.2 権限マトリクス）に手を入れずに済む（TASKS.md §1.1「認可」）。

⚠️ 2026-08-13 の方針変更（TASKS.md §1.1「備考：SSO 前提からの差分」）で、
認証は SSO 前提から **自前の ID/PW 認証** に変わった。この分離は、将来 SSO を
足す余地を残すためのもので、**SSO 対応済みという意味ではない**。
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Role(StrEnum):
    """権限ロール（設計書 §4.1、仕様書 §2 アクター表）。

    値は §6.2 権限マトリクスのキーとしてそのまま使う。
    """

    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"
    SYSTEM = "system"


# 自己登録で付与されるロール（TASKS.md §1.1「ユーザー登録とロール付与」）。
# 登録経路はロールを一切受け取らず、必ずこの値になる（T-40）。
DEFAULT_SELF_REGISTERED_ROLE = Role.VIEWER

# 人間のユーザーが取りうるロール。`system` は**ログインするユーザーではなく**
# cron 等の非対話クライアントの種別なので、`users` テーブルに行を持たない
# （サービストークンから直接 Principal を組み立てる。T-41）。
ASSIGNABLE_ROLES: frozenset[Role] = frozenset(
    {Role.ADMIN, Role.EDITOR, Role.VIEWER},
)


class Principal(BaseModel):
    """認証済みの呼び出し元。認可判定の唯一の入力。

    `subject` はユーザ識別子。人間なら `users.user_id`、`system` なら
    呼び出し元の名前（例 `cron`）。監査ログの `actor`（設計書 §4.4）は
    `role:subject` 形式なので `actor` プロパティで組み立てる。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str
    role: Role

    @property
    def actor(self) -> str:
        """監査ログの `actor` 表記（設計書 §4.4 の例 `admin:admin_a`）。"""
        return f"{self.role.value}:{self.subject}"

    @property
    def is_internal(self) -> bool:
        """内部呼び出し（`system`）か。§4.2 の `internal_only` 判定に使う。"""
        return self.role is Role.SYSTEM
