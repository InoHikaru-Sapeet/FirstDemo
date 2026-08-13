"""アプリの利用者（TASKS.md §1.1「認証」「ユーザー登録とロール付与」／T-08）。

**ID（メールアドレス）／パスワード認証を自前で実装する**方針（2026-08-13 変更、
TASKS.md §1.1「備考：SSO 前提からの差分」）に伴い、ID の発行元は外部 IdP ではなく
このテーブルになった。

ロールの正もここ。認証（T-40）は**リクエストごとにこの行から `role` を解決する**。
セッション側にロールを焼き込まないことで、admin による昇格・降格が
**再ログインなしで次のリクエストから効く**（§1.1「ログイン状態の保持」の根拠）。

⚠️ `password_hash` は bcrypt ハッシュであって平文ではない。とはいえ
**API レスポンス・ログ・監査ログに出してはならない**（T-42 の `GET /users` は
この列を返さない）。`__repr__` もそれを踏まえて定義してある。
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from adapter.database.base import Base
from adapter.database.types import UtcDateTime
from enterprise.entities.principal import ASSIGNABLE_ROLES, Role

# メールアドレスの最大長。慣例的に使われる上限値。
EMAIL_MAX_LENGTH = 254


def normalize_email(email: str) -> str:
    """メールアドレスを比較・保存用に正規化する。

    前後の空白を落とし、**小文字化**する。`Admin@example.com` と
    `admin@example.com` を別アカウントとして登録できてしまうと、
    「どちらが admin か」が運用上わからなくなるため。

    ⚠️ 登録・ログイン・CLI の**すべての入口でこれを通す**こと。一箇所でも
    素の値を使うと一意制約をすり抜ける。
    """
    return email.strip().lower()


def is_valid_email_format(normalized_email: str) -> bool:
    """メールアドレスの形（`local@domain.tld`）を満たすか。

    ⚠️ **厳密な RFC 検証はしない。** 打ち間違いを弾くための最低限の形式検査で、
    到達性は「そのアドレスでログインできるか」で確かめる運用。正規表現で
    RFC 5322 を追うと、正しいアドレスを誤って弾く事故のほうが増える。

    入口が2つ（自己登録 T-40 / ブートストラップ CLI T-41）あるので、
    判定はここ1箇所に置く。**`normalize_email()` を通した値**を渡すこと。
    """
    local, separator, domain = normalized_email.partition("@")
    return bool(separator and local and domain and "." in domain)


def email_domain(normalized_email: str) -> str:
    """メールアドレスのドメイン部（許可リスト判定用）。"""
    return normalized_email.partition("@")[2]


class User(Base):
    """利用者1人。

    自己登録された直後は必ず `viewer`（`DEFAULT_SELF_REGISTERED_ROLE`）で、
    `editor` / `admin` への昇格は admin のみが行える（T-42）。
    """

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # 正規化済み（小文字）で保存する。ログインの検索キーなので一意かつ索引付き。
    email: Mapped[str] = mapped_column(
        String(EMAIL_MAX_LENGTH), nullable=False, unique=True, index=True
    )

    display_name: Mapped[str] = mapped_column(String(128), nullable=False)

    # bcrypt ハッシュ（`$2b$12$...`）。平文は保存しない。
    # bcrypt の出力は 60 文字だが、コスト変更やアルゴリズム移行の余地を見て広く取る。
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[Role] = mapped_column(String(16), nullable=False)

    # 停止されたユーザーはセッションが残っていても弾く（T-40）。
    # 退職者を削除せず無効化するための列（監査ログの actor が参照先を失わない）。
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    # パスワード変更時刻。変更時に**他セッションを全失効**させる（T-40）ため、
    # 「このセッションはパスワード変更より前に発行されたか」を判定できるようにする。
    password_updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    # 総当たり対策（T-40）。連続失敗を数え、閾値に達したら `locked_until` まで
    # ログインを拒否する。成功したら 0 に戻す。
    # ⚠️ ロック中であることを**利用者に伝えない**（エラー文言は常に同一）。
    # 「ロックされました」と返すと、そのアドレスが実在することを教えてしまう。
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        # ⚠️ `system` は**ログインするユーザーではない**。cron 等の非対話クライアント
        # の種別であり、サービストークンから直接 Principal を組み立てる（T-41）。
        # `users` に system 行があると、パスワードで system 権限を取れる経路が
        # できてしまうため DB 制約で塞ぐ。
        CheckConstraint(
            "role IN ('"
            + "', '".join(sorted(r.value for r in ASSIGNABLE_ROLES))
            + "')",
            name="ck_users_role_is_assignable",
        ),
    )

    def __repr__(self) -> str:
        """⚠️ `password_hash` を**含めない**。repr はログや例外に紛れ込むため。"""
        return (
            f"User(user_id={self.user_id!r}, email={self.email!r}, "
            f"role={self.role!r}, is_active={self.is_active!r})"
        )
