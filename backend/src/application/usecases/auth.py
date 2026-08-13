"""認証のユースケース（TASKS.md §1.1「認証」「ログイン状態の保持」／T-40）。

登録・ログイン・ログアウト・パスワード変更と、セッションの発行・検証を行う。
HTTP の都合（Cookie・ステータスコード）は adapter 層に置き、ここには**業務規則**
だけを置く。

---

**設計上、動かしてはいけない点**

1. **ログイン失敗は理由を区別しない。** 「アドレスが存在しない」「パスワードが違う」
   「ロック中」「停止済み」のすべてで**同一のエラー**を返す。区別すると、
   どのアドレスが実在するかを外部から列挙できてしまう。
2. **存在しないアカウントでもハッシュ照合を実行する。** 早期 return すると
   応答時間の差でアカウントの存在が分かる（bcrypt の照合は約0.2秒かかるので
   差が顕著に出る）。
3. **ロールはセッションに焼き込まない。** 毎回 `users` 行から読む。これにより
   admin による昇格・降格が再ログインなしで効く（§1.1 の JWT を採らなかった理由）。
4. **Cookie の生トークンを DB に保存しない。** 保存するのは SHA-256 ハッシュだけ。
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.database.models.session import Session
from adapter.database.models.user import User, normalize_email
from enterprise.entities.principal import (
    DEFAULT_SELF_REGISTERED_ROLE,
    Principal,
    Role,
)
from enterprise.services.password import (
    PasswordPolicyError,
    hash_password,
    verify_and_migrate_password,
    verify_password,
)

# ⚠️ ログイン失敗時に返す**唯一の**文言。理由ごとに変えないこと。
LOGIN_FAILED_MESSAGE = "メールアドレスまたはパスワードが違います。"

# 生トークンのバイト数。`secrets.token_urlsafe(32)` は 256 ビットの乱数から
# 43文字の URL-safe 文字列を作る。推測は現実的に不可能。
SESSION_TOKEN_BYTES = 32


def _now() -> datetime:
    """現在時刻（UTC・tz付き）。

    テストが時間を進められるよう、時刻取得をこの1箇所に集約する
    （`UtcDateTime` は naive な datetime を拒否するので必ず tz 付きで返す）。
    """
    return datetime.now(UTC)


def hash_session_token(raw_token: str) -> str:
    """Cookie の生トークンから、DB に保存する `session_id` を作る。

    ⚠️ ここはパスワードと違い **bcrypt を使わない**。理由は2つ:

    - 生トークンは 256 ビットの乱数で、辞書攻撃・総当たりの対象にならない
      （ソルトとストレッチが必要なのは「人間が選んだ低エントロピーな秘密」）
    - セッション検証は**全リクエストで走る**ため、1回0.2秒の bcrypt は使えない

    SHA-256 は「DB が漏れても生トークンを逆算できない」という目的には十分。
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class AuthErrorCode(StrEnum):
    """認証系の失敗の種類。HTTP 層がステータスコードへ変換する。"""

    # 登録
    EMAIL_INVALID = "email_invalid"
    EMAIL_DOMAIN_NOT_ALLOWED = "email_domain_not_allowed"
    EMAIL_ALREADY_REGISTERED = "email_already_registered"
    DISPLAY_NAME_REQUIRED = "display_name_required"
    # ログイン・パスワード変更
    INVALID_CREDENTIALS = "invalid_credentials"


class AuthError(Exception):
    """認証系の業務エラー。

    ⚠️ `message` は**利用者にそのまま見せる文言**。ログイン失敗では
    必ず `LOGIN_FAILED_MESSAGE` を使い、内部の理由を混ぜないこと。
    """

    def __init__(self, code: AuthErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class SessionPolicy:
    """セッションの寿命。`Settings` から組み立てて渡す。"""

    absolute_lifetime: timedelta
    idle_timeout: timedelta


@dataclass(frozen=True)
class LoginPolicy:
    """総当たり対策のしきい値。`Settings` から組み立てて渡す。"""

    max_failed_attempts: int
    lockout_duration: timedelta


@dataclass(frozen=True)
class IssuedSession:
    """発行したセッション。生トークンは**この瞬間しか存在しない**。

    DB にはハッシュしか残らないので、Cookie に載せ損ねると復元できない。
    """

    raw_token: str
    expires_at: datetime


def _dummy_password_hash() -> str:
    """存在しないアカウント用の照合先（タイミング差を消すため）。

    初回呼び出し時に1度だけ計算してキャッシュする（bcrypt は約0.2秒かかるので、
    import 時に走らせるとテスト全体が遅くなる）。
    """
    global _DUMMY_HASH_CACHE
    if _DUMMY_HASH_CACHE is None:
        _DUMMY_HASH_CACHE = hash_password("dummy password for timing equalization")
    return _DUMMY_HASH_CACHE


_DUMMY_HASH_CACHE: str | None = None


class AuthUsecase:
    """認証の入口。DB セッションと設定値を受け取って動く。"""

    def __init__(
        self,
        db: AsyncSession,
        session_policy: SessionPolicy,
        login_policy: LoginPolicy,
        allowed_email_domains: list[str],
    ) -> None:
        self._db = db
        self._session_policy = session_policy
        self._login_policy = login_policy
        self._allowed_email_domains = allowed_email_domains

    # --- 登録 -------------------------------------------------------------

    async def register(self, email: str, display_name: str, password: str) -> User:
        """自己登録。**常に viewer** で作成する。

        ⚠️ この関数はロールを引数に取らない。昇格の経路を admin 限定の
        `PATCH /users/{id}/role`（T-42）と CLI（T-41）だけに絞るため、
        **引数を足さないこと。**

        パスワードのポリシー違反は `PasswordPolicyError` がそのまま伝播する
        （HTTP 層が 422 の `issues` へ変換する）。
        """
        normalized = normalize_email(email)
        self._ensure_email_is_acceptable(normalized)

        if not display_name.strip():
            raise AuthError(
                AuthErrorCode.DISPLAY_NAME_REQUIRED, "表示名を入力してください。"
            )

        # ポリシー違反はここで弾く（先にハッシュ化して例外を伝播させる）。
        password_hash = hash_password(password)

        existing = await self._find_by_email(normalized)
        if existing is not None:
            # ⚠️ ここはアカウントの存在を明かす。ログインと違い、登録では
            # 「既に登録済み」を伝えないと利用者が次の行動を取れないため、
            # 利便性を優先した意図的な判断（TASKS.md T-40 備考に記録）。
            raise AuthError(
                AuthErrorCode.EMAIL_ALREADY_REGISTERED,
                "このメールアドレスは既に登録されています。",
            )

        now = _now()
        user = User(
            user_id=f"usr_{uuid.uuid4().hex}",
            email=normalized,
            display_name=display_name.strip(),
            password_hash=password_hash,
            role=DEFAULT_SELF_REGISTERED_ROLE,
            is_active=True,
            created_at=now,
            updated_at=now,
            password_updated_at=now,
            failed_login_attempts=0,
            locked_until=None,
        )
        self._db.add(user)
        await self._db.commit()
        return user

    def _ensure_email_is_acceptable(self, normalized_email: str) -> None:
        """形式とドメイン許可リストを検査する。"""
        local, separator, domain = normalized_email.partition("@")
        if not separator or not local or not domain or "." not in domain:
            raise AuthError(
                AuthErrorCode.EMAIL_INVALID,
                "メールアドレスの形式が正しくありません。",
            )

        # 空リスト＝無制限。既定は `sapeet.com`（要確認事項 #6 の決定）。
        if self._allowed_email_domains and domain not in self._allowed_email_domains:
            allowed = " / ".join(self._allowed_email_domains)
            raise AuthError(
                AuthErrorCode.EMAIL_DOMAIN_NOT_ALLOWED,
                f"登録できるのは次のドメインのみです：{allowed}",
            )

    # --- ログイン ---------------------------------------------------------

    async def login(self, email: str, password: str) -> IssuedSession:
        """認証してセッションを発行する。

        ⚠️ **失敗の理由を一切区別しない。** 存在しない・パスワード違い・
        ロック中・停止済みのすべてで `LOGIN_FAILED_MESSAGE` を返す。
        """
        normalized = normalize_email(email)
        user = await self._find_by_email(normalized)
        now = _now()

        if user is None:
            # ⚠️ 早期 return しない。ダミーハッシュに対して照合を走らせ、
            # 応答時間から「アドレスが存在しない」ことを推測されないようにする。
            verify_password(password, _dummy_password_hash())
            raise self._login_failed()

        if self._is_locked(user, now):
            # ロック中も同じだけ時間を使い、同じ文言を返す。
            verify_password(password, _dummy_password_hash())
            raise self._login_failed()

        is_valid, migrated_hash = verify_and_migrate_password(
            password, user.password_hash
        )

        if not is_valid:
            await self._record_failed_attempt(user, now)
            raise self._login_failed()

        if not user.is_active:
            # 停止済みは「パスワードは合っているが入れない」状態。これも
            # 区別させない（停止されたことを外部に知らせない）。
            raise self._login_failed()

        if migrated_hash is not None:
            # bcrypt のコストを上げた後、ログインのついでに静かに移行する。
            user.password_hash = migrated_hash

        user.failed_login_attempts = 0
        user.locked_until = None
        user.updated_at = now

        issued = await self._create_session(user, now)
        await self._purge_expired_sessions(now)
        await self._db.commit()
        return issued

    def _login_failed(self) -> AuthError:
        return AuthError(AuthErrorCode.INVALID_CREDENTIALS, LOGIN_FAILED_MESSAGE)

    def _is_locked(self, user: User, now: datetime) -> bool:
        return user.locked_until is not None and now < user.locked_until

    async def _record_failed_attempt(self, user: User, now: datetime) -> None:
        """失敗回数を数え、閾値に達したらロックする。"""
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= self._login_policy.max_failed_attempts:
            user.locked_until = now + self._login_policy.lockout_duration
            # 次のロック解除後にまた1回で再ロックされないよう、数えなおす。
            user.failed_login_attempts = 0
        await self._db.commit()

    # --- セッション -------------------------------------------------------

    async def _create_session(self, user: User, now: datetime) -> IssuedSession:
        """生トークンを発行し、そのハッシュだけを保存する。"""
        raw_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        expires_at = now + self._session_policy.absolute_lifetime

        self._db.add(
            Session(
                session_id=hash_session_token(raw_token),
                user_id=user.user_id,
                created_at=now,
                expires_at=expires_at,
                last_seen_at=now,
                revoked_at=None,
            )
        )
        return IssuedSession(raw_token=raw_token, expires_at=expires_at)

    async def resolve_session(self, raw_token: str) -> Principal | None:
        """Cookie の生トークンから呼び出し元を解決する。

        有効なら `last_seen_at` を更新（アイドル期限の延長）して `Principal` を返す。
        無効なら **理由を区別せず** `None`。

        ⚠️ ロールは**セッションではなく `users` 行**から読む。これが
        「昇格・降格が再ログインなしで効く」ことの実体（§1.1）。
        """
        if not raw_token:
            return None

        session_id = hash_session_token(raw_token)
        now = _now()

        row = (
            await self._db.execute(
                select(Session, User)
                .join(User, Session.user_id == User.user_id)
                .where(Session.session_id == session_id)
            )
        ).first()

        if row is None:
            return None

        session, user = row
        if not self._is_session_valid(session, now):
            return None
        if not user.is_active:
            # 停止されたユーザーは、セッションが残っていても即座に弾く。
            return None

        session.last_seen_at = now
        await self._db.commit()

        return Principal(subject=user.user_id, role=user.role)

    def _is_session_valid(self, session: Session, now: datetime) -> bool:
        if session.revoked_at is not None:
            return False
        if now >= session.expires_at:
            return False
        # アイドル期限。最終アクセスから一定時間で切れる。
        return now < session.last_seen_at + self._session_policy.idle_timeout

    async def logout(self, raw_token: str) -> None:
        """セッションをサーバー側で失効させる。**べき等**。

        ⚠️ Cookie を消すだけにしない。Cookie は利用者の手元にあり、
        削除を強制できない（コピーを取られていれば使い続けられる）。
        サーバー側で無効化して初めてログアウトが成立する。
        """
        if not raw_token:
            return

        await self._db.execute(
            update(Session)
            .where(
                Session.session_id == hash_session_token(raw_token),
                Session.revoked_at.is_(None),
            )
            .values(revoked_at=_now())
        )
        await self._db.commit()

    async def _purge_expired_sessions(self, now: datetime) -> None:
        """絶対期限を過ぎた行を掃除する（テーブルを単調増加させない）。

        ログインのついでに実行する。失効済み（`revoked_at`）の行は監査のため
        絶対期限が来るまで残す。
        """
        await self._db.execute(delete(Session).where(Session.expires_at <= now))

    # --- パスワード変更 ---------------------------------------------------

    async def change_password(
        self, user_id: str, current_password: str, new_password: str
    ) -> None:
        """本人のパスワードを変更する。

        現在のパスワードを要求し、成功したら**そのユーザーの全セッションを失効**
        させる（乗っ取られていた場合に追い出すのが目的なので、操作中の
        セッションも含めて切る）。
        """
        user = await self._db.get(User, user_id)
        if user is None or not verify_password(current_password, user.password_hash):
            raise AuthError(
                AuthErrorCode.INVALID_CREDENTIALS,
                "現在のパスワードが違います。",
            )

        # ポリシー違反は PasswordPolicyError として伝播（HTTP 層で 422）。
        new_hash = hash_password(new_password)

        now = _now()
        user.password_hash = new_hash
        user.password_updated_at = now
        user.updated_at = now
        user.failed_login_attempts = 0
        user.locked_until = None

        await self._db.execute(
            update(Session)
            .where(Session.user_id == user_id, Session.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await self._db.commit()

    # --- 参照 -------------------------------------------------------------

    async def get_user(self, user_id: str) -> User | None:
        return await self._db.get(User, user_id)

    async def _find_by_email(self, normalized_email: str) -> User | None:
        return (
            await self._db.execute(select(User).where(User.email == normalized_email))
        ).scalar_one_or_none()


__all__ = [
    "LOGIN_FAILED_MESSAGE",
    "AuthError",
    "AuthErrorCode",
    "AuthUsecase",
    "IssuedSession",
    "LoginPolicy",
    "PasswordPolicyError",
    "Role",
    "SessionPolicy",
    "hash_session_token",
]
