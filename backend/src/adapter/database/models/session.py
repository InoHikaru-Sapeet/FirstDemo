"""ログインセッション（TASKS.md §1.1「ログイン状態の保持」／T-40）。

**なぜ JWT ではなくサーバー側セッションなのか**（§1.1 の根拠列より）:

admin による昇格・降格が**即時に効く**必要がある。JWT はロールをトークンへ
焼き込むため、降格しても有効期限まで旧ロールで通ってしまう。セッションなら
失効（ログアウト・パスワード変更・アカウント停止）をサーバー側で即座に行え、
ロールは毎リクエスト `users` 行から引き直せる。**この判断を覆さないこと。**

⚠️ **`session_id` は Cookie の値そのものではない。**
Cookie に入るのは推測困難な生トークンで、DB に保存するのはその SHA-256 ハッシュ。
こうしておくと、**DB が漏れてもセッションを乗っ取れない**（ハッシュから生トークンを
逆算できないため）。パスワードを平文で保存しないのと同じ理由。
"""

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from adapter.database.base import Base
from adapter.database.types import UtcDateTime

# SHA-256 を16進で表した長さ。`session_id` はこの固定長になる。
SESSION_ID_LENGTH = 64


class Session(Base):
    """発行済みセッション1件。

    有効性は次の**すべて**を満たすこと（判定は application 層）:

    - `revoked_at` が null（ログアウト・パスワード変更・停止で埋まる）
    - 現在時刻 < `expires_at`（絶対期限。延長しない）
    - 現在時刻 < `last_seen_at` + アイドル期限（アクセスのたびに延長する）
    """

    __tablename__ = "sessions"

    # ⚠️ Cookie の生トークンではなく、その SHA-256（16進）。
    session_id: Mapped[str] = mapped_column(String(SESSION_ID_LENGTH), primary_key=True)

    # 利用者が消えたらセッションも消す（孤児セッションを残さない）。
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    # 絶対期限。ログイン時刻 + `session_absolute_lifetime_days` で固定し、
    # **アクセスしても延ばさない**（乗っ取られたセッションが無期限に生き残らない）。
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    # 最終アクセス時刻。アイドル期限の起点で、アクセスのたびに更新する。
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    # 失効時刻。null なら有効。**ログアウトは行を消さずここを埋める**
    # （いつ失効したかが監査で追える）。
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    def __repr__(self) -> str:
        """⚠️ `session_id` を**含めない**。

        生トークンではないとはいえ、ログに出す価値がないうえ、
        DB の値が漏れる経路を1つ増やすだけになる。
        """
        return (
            f"Session(user_id={self.user_id!r}, expires_at={self.expires_at!r}, "
            f"revoked={self.revoked_at is not None!r})"
        )
