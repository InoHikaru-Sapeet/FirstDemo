"""最初の admin を作る（TASKS.md §1.1「ユーザー登録とロール付与」／T-41）。

**なぜ API ではなく DB へ直接書く経路が必要なのか**

自己登録（`POST /auth/register`）は**常に `viewer`** を作り、`editor` / `admin`
への昇格は **admin だけ**が実行できる（`PATCH /users/{id}/role`。T-42）。
この2つを守る限り、**最初の1人は API 経由では作れない**（昇格させる admin が
まだ居ない）。そこで DB へ直接書ける経路を **CLI 1本に限って**正式化する。

---

**設計上、動かしてはいけない点**

1. **このモジュールはパスワードを受け取るが、入力手段を知らない。** 対話プロンプト
   （`getpass`）は CLI（`adapter/cli/create_admin.py`）の責務。**コマンドライン引数・
   環境変数から平文を渡す経路を作らないこと**（`ps` / シェル履歴 / `.env` に残る）。
2. **admin が既に居るなら作らない。** ブートストラップ以外での常用を防ぐ。
   2人目以降は admin 自身が API で昇格させるのが正規の経路（T-42）。
3. **同じメールの行を二重に作らない。** 既存ユーザーは昇格（`promote_to_admin`）で
   扱う。メールは必ず `normalize_email()` を通す（T-08 備考）。
4. **ロール変更は監査ログに残す**（actor は `cli:create-admin`）。CLI からの変更が
   記録に残らないと、「誰が admin を作ったか」が追えなくなる。
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.database.models.audit_log import AuditEventType, AuditLog
from adapter.database.models.user import (
    User,
    is_valid_email_format,
    normalize_email,
)
from enterprise.entities.principal import Role
from enterprise.services.password import hash_password

# 監査ログの actor（設計書 §4.4 の `role:subject` 形式に合わせる）。
# 人ではなく CLI が実行主体なので、ロールの位置に `cli` を置く。
CLI_ACTOR = "cli:create-admin"


class BootstrapOutcome(StrEnum):
    """CLI が利用者に伝える結果。**何もしなかった**ことも結果として返す。"""

    CREATED = "created"
    PROMOTED = "promoted"
    ALREADY_ADMIN = "already_admin"


class BootstrapErrorCode(StrEnum):
    """ブートストラップを拒否した理由。"""

    ADMIN_ALREADY_EXISTS = "admin_already_exists"
    EMAIL_INVALID = "email_invalid"
    DISPLAY_NAME_REQUIRED = "display_name_required"
    USER_ALREADY_EXISTS = "user_already_exists"
    USER_NOT_FOUND = "user_not_found"


class BootstrapAdminError(Exception):
    """ブートストラップの業務エラー。CLI が終了コードへ変換する。

    ⚠️ `message` はそのまま端末に出る。**平文パスワードを含めないこと。**
    """

    def __init__(self, code: BootstrapErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _now() -> datetime:
    """現在時刻（UTC・tz付き）。テストが差し替えられるよう1箇所に集約する。"""
    return datetime.now(UTC)


class BootstrapAdminUsecase:
    """最初の admin の作成と、既存ユーザーの admin 昇格。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # --- 参照 -------------------------------------------------------------

    async def count_admins(self) -> int:
        """admin の人数。停止中（`is_active=false`）も数える。

        ⚠️ 停止中を除外してはいけない。除外すると「admin を停止しただけ」の DB に
        2人目の admin を新規作成できてしまい、"最初の1人だけ" の建前が崩れる。
        再開すれば admin は戻るので、居ないことにはならない。
        """
        return (
            await self._db.execute(
                select(func.count())
                .select_from(User)
                .where(User.role == Role.ADMIN.value)
            )
        ).scalar_one()

    async def find_by_email(self, email: str) -> User | None:
        return (
            await self._db.execute(
                select(User).where(User.email == normalize_email(email))
            )
        ).scalar_one_or_none()

    # --- 作成 -------------------------------------------------------------

    async def ensure_can_create_initial_admin(
        self, email: str, display_name: str
    ) -> str:
        """新規作成が許されるかを検査し、正規化済みメールを返す。

        **パスワードを受け取る前に呼べる**ように分けてある。拒否されるとわかって
        いる操作のために、利用者に長いパスワードを2回入力させないため
        （CLI は `create_initial_admin()` の前にこれを呼ぶ）。

        Raises:
            BootstrapAdminError: admin が既に居る / メールが不正 / 表示名が空 /
                同一メールが既存
        """
        normalized = normalize_email(email)
        self._ensure_email_format(normalized)

        if not display_name.strip():
            raise BootstrapAdminError(
                BootstrapErrorCode.DISPLAY_NAME_REQUIRED,
                "表示名を入力してください。",
            )

        if await self.count_admins() > 0:
            raise BootstrapAdminError(
                BootstrapErrorCode.ADMIN_ALREADY_EXISTS,
                "admin が既に存在します。ブートストラップ用の CLI は最初の1人"
                "だけに使います。既存ユーザーを admin にするには "
                "`--promote <email>` を、通常の昇格は admin としてログインして "
                "`PATCH /users/{user_id}/role` を使ってください。",
            )

        if await self.find_by_email(normalized) is not None:
            # ⚠️ 再実行しても行を二重に作らない。既存ユーザーを admin にするのは
            # 「作成」ではなく「昇格」なので、明示フラグ（--promote）を要求する。
            raise BootstrapAdminError(
                BootstrapErrorCode.USER_ALREADY_EXISTS,
                f"{normalized} は既に登録されています。"
                "admin へ昇格させるなら `--promote` を付けて実行してください"
                "（このコマンドは既存ユーザーを黙って書き換えません）。",
            )

        return normalized

    async def create_initial_admin(
        self, email: str, display_name: str, password: str
    ) -> User:
        """admin ロールのユーザーを新規作成する。

        ⚠️ **admin が1人でも居たら拒否する。** 2人目以降は admin 自身が API で
        昇格させる（T-42）のが正規の経路で、CLI はブートストラップ専用。

        パスワードのポリシー違反は `PasswordPolicyError` がそのまま伝播する
        （長さのみ・72 バイト上限。T-08）。

        Args:
            email: 作成するユーザーのメールアドレス（正規化前でよい）
            display_name: 表示名
            password: 平文パスワード（**呼び出し元は対話入力で受け取ること**）

        Returns:
            作成した `User`

        Raises:
            BootstrapAdminError: admin が既に居る / メールが不正 / 同一メールが既存
        """
        normalized = await self.ensure_can_create_initial_admin(email, display_name)

        # ポリシー違反はここで弾く（PasswordPolicyError が伝播する）。
        password_hash = hash_password(password)

        now = _now()
        user = User(
            user_id=f"usr_{uuid.uuid4().hex}",
            email=normalized,
            display_name=display_name.strip(),
            password_hash=password_hash,
            role=Role.ADMIN,
            is_active=True,
            created_at=now,
            updated_at=now,
            password_updated_at=now,
            failed_login_attempts=0,
            locked_until=None,
        )
        self._db.add(user)
        self._record_role_change(user, before=None, at=now)
        await self._db.commit()
        return user

    # --- 昇格 -------------------------------------------------------------

    async def promote_to_admin(self, email: str) -> tuple[BootstrapOutcome, User]:
        """既存ユーザーを admin へ昇格させる。**べき等**。

        既に admin なら何も書かずに `ALREADY_ADMIN` を返す（監査ログも増やさない。
        変更していないものを「変更した」と記録しないため）。

        ⚠️ これは**復旧手段**でもある（admin が全員ログイン不能になった場合）。
        そのため admin が既に居ても拒否しない。日常の昇格は API（T-42）で行う。

        Returns:
            `(結果, 対象ユーザー)`

        Raises:
            BootstrapAdminError: メールが不正 / 該当ユーザーが居ない
        """
        normalized = normalize_email(email)
        self._ensure_email_format(normalized)

        user = await self.find_by_email(normalized)
        if user is None:
            raise BootstrapAdminError(
                BootstrapErrorCode.USER_NOT_FOUND,
                f"{normalized} のユーザーが見つかりません。"
                "先に本人が `POST /auth/register` で登録するか、"
                "`--promote` なしで新規作成してください。",
            )

        if user.role == Role.ADMIN:
            return BootstrapOutcome.ALREADY_ADMIN, user

        before = Role(user.role)
        now = _now()
        user.role = Role.ADMIN
        user.updated_at = now
        self._record_role_change(user, before=before, at=now)
        await self._db.commit()
        return BootstrapOutcome.PROMOTED, user

    # --- 内部 -------------------------------------------------------------

    def _ensure_email_format(self, normalized_email: str) -> None:
        if not is_valid_email_format(normalized_email):
            raise BootstrapAdminError(
                BootstrapErrorCode.EMAIL_INVALID,
                "メールアドレスの形式が正しくありません。",
            )

    def _record_role_change(
        self, user: User, before: Role | None, at: datetime
    ) -> None:
        """ロール変更を監査ログへ積む（commit は呼び出し元）。

        `before=None` は「このコマンドが作成した」ことを意味する。

        ⚠️ **パスワードハッシュ・平文を書かない。** 監査ログは admin が閲覧する
        ものだが、それでもハッシュを置く理由がない（T-10 も同じ約束）。

        ⚠️ T-10 が監査ログ書き込みサービスを作ったら、**この直書きをそちらへ
        寄せること**（現時点では T-10 が未着手のためモデルへ直接積んでいる）。
        """
        self._db.add(
            AuditLog(
                audit_id=f"aud_{uuid.uuid4().hex}",
                event_type=AuditEventType.USER_ROLE_CHANGE,
                actor=CLI_ACTOR,
                at=at,
                revision=None,
                diff={
                    "role": {
                        "before": before.value if before is not None else None,
                        "after": Role.ADMIN.value,
                    },
                    # 対象を人間が識別できるように残す（user_id は不透明なため）。
                    "email": user.email,
                },
                target=user.user_id,
                period=None,
            )
        )


__all__ = [
    "CLI_ACTOR",
    "BootstrapAdminError",
    "BootstrapAdminUsecase",
    "BootstrapErrorCode",
    "BootstrapOutcome",
]
