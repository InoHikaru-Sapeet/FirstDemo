"""ユーザー管理のユースケース（TASKS.md §1.1「ユーザー登録とロール付与」／T-42）。

**このモジュールがロールを上げられる唯一の API 経路**である。自己登録（T-40）は
`role` を一切受け取らず必ず `viewer` を作るので、`editor` / `admin` へ上がる道は
ここ（admin 限定）とブートストラップ CLI（T-41）の2本しかない。

---

**設計上、動かしてはいけない点**

1. **`system` をユーザーに割り当てない。** `system` は cron 等の呼び出し元の種別で
   あって「ログインする人」ではない（T-08・T-41）。ここで割り当てられると、
   パスワードで `system` を名乗れる経路ができる。DB の CHECK 制約
   （`ck_users_role_is_assignable`）が最後の砦だが、その手前で 422 として弾く。
2. **最後の admin を降格・停止させない。** admin が0人になると、昇格させられる
   人が居なくなり **CLI（T-41）でしか復旧できない**。自分自身の降格も同じ扱い。
3. **件数の確認は変更と同じトランザクション内で行う。** 2人の admin が同時に
   相手を降格させると、それぞれが「相手が居るから大丈夫」と判断して 0 人に
   なりうる（`_count_other_active_admins()` の説明を参照）。
4. **ロール変更・停止は監査ログに残す。** 誰が・誰を・いつ・何に変えたか。
   ⚠️ ここでも**パスワードハッシュ・平文を書かない**（T-41 と同じ約束）。
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.database.models.session import Session
from adapter.database.models.user import User
from application.usecases.audit import AuditService
from enterprise.entities.principal import ASSIGNABLE_ROLES, Principal, Role


class ManageUsersErrorCode(StrEnum):
    """ユーザー管理の失敗の種類。HTTP 層がステータスコードへ変換する。"""

    # 対象が存在しない → 404
    USER_NOT_FOUND = "user_not_found"
    # `system` など人に割り当ててはいけないロール → 422
    ROLE_NOT_ASSIGNABLE = "role_not_assignable"
    # 最後の admin を降格・停止しようとした → 409
    LAST_ADMIN = "last_admin"


class ManageUsersError(Exception):
    """ユーザー管理の業務エラー。

    ⚠️ `message` は利用者にそのまま見せる文言。**対象ユーザーの
    パスワード情報を含めないこと。**
    """

    def __init__(self, code: ManageUsersErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _now() -> datetime:
    """現在時刻（UTC・tz付き）。テストが差し替えられるよう1箇所に集約する。"""
    return datetime.now(UTC)


class ManageUsersUsecase:
    """ユーザー一覧と、ロール・有効状態の変更。

    **呼び出し元が admin であることの確認はここではしない**（HTTP 層の
    `require_admin` が行う）。ここは「admin だと確定した呼び出し元」を
    `Principal` として受け取り、監査ログの `actor` に使うだけ。
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._audit = AuditService(db)

    # --- 参照 -------------------------------------------------------------

    async def list_users(self) -> list[User]:
        """全ユーザー。作成順（同時刻は user_id 順）で安定させる。

        ⚠️ 返すのは ORM の `User` そのものなので、**HTTP 層で
        `password_hash` を含まないレスポンスモデルへ詰め替えること**。
        """
        return list(
            (
                await self._db.execute(
                    select(User).order_by(User.created_at, User.user_id)
                )
            )
            .scalars()
            .all()
        )

    # --- ロール変更 -------------------------------------------------------

    async def change_role(self, actor: Principal, user_id: str, new_role: Role) -> User:
        """ロールを変更する。**べき等**（同じロールなら何も書かない）。

        Args:
            actor: 実行者（admin であることは HTTP 層で確認済み）
            user_id: 対象ユーザーの ID
            new_role: `admin` / `editor` / `viewer` のいずれか

        Returns:
            変更後の `User`

        Raises:
            ManageUsersError: 対象が居ない（404）/ `system` 指定（422）/
                最後の admin を降格しようとした（409）
        """
        if new_role not in ASSIGNABLE_ROLES:
            # ⚠️ DB の CHECK 制約より手前で弾く。制約に頼ると 500 になり、
            # 「なぜ弾かれたか」が利用者に伝わらない。
            assignable = " / ".join(sorted(r.value for r in ASSIGNABLE_ROLES))
            raise ManageUsersError(
                ManageUsersErrorCode.ROLE_NOT_ASSIGNABLE,
                f"指定できるロールは次のいずれかです：{assignable}"
                "（system は cron 用の呼び出し元種別で、ユーザーには割り当てません）。",
            )

        user = await self._require_user(user_id)
        before = Role(user.role)
        if before is new_role:
            # 変えていないものを「変更した」と記録しない（T-41 の昇格と同じ方針）。
            return user

        await self._ensure_an_active_admin_remains(
            user, next_role=new_role, next_is_active=user.is_active
        )

        now = _now()
        user.role = new_role
        user.updated_at = now
        self._record_role_change(
            actor=actor, user=user, before=before, after=new_role, at=now
        )
        await self._db.commit()

        # ⚠️ セッションは失効させない。ロールは毎リクエスト `users` 行から
        # 引き直されるので（T-40）、降格は**次のリクエストから即座に効く**。
        # ここで失効させると、降格された人が「ログアウトされた」ことから
        # 降格に気づく、という副次的な情報漏れが増えるだけで得がない。
        return user

    # --- 停止・再開 -------------------------------------------------------

    async def change_status(
        self, actor: Principal, user_id: str, is_active: bool
    ) -> User:
        """アカウントを停止／再開する。**べき等**。

        停止時は**そのユーザーの有効なセッションをすべて失効**させる。
        `resolve_session()` は `is_active=false` を弾く（T-40）ので停止だけでも
        入れなくなるが、行を残しておく理由がない。

        Raises:
            ManageUsersError: 対象が居ない（404）/ 最後の admin を停止しようと
                した（409）
        """
        user = await self._require_user(user_id)
        if user.is_active == is_active:
            return user

        await self._ensure_an_active_admin_remains(
            user, next_role=Role(user.role), next_is_active=is_active
        )

        now = _now()
        user.is_active = is_active
        user.updated_at = now
        if not is_active:
            await self._revoke_sessions(user_id, now)

        self._record_status_change(
            actor=actor, user=user, before=not is_active, after=is_active, at=now
        )
        await self._db.commit()
        return user

    async def _revoke_sessions(self, user_id: str, now: datetime) -> None:
        """そのユーザーの有効なセッションをすべて失効させる。

        行は消さずに `revoked_at` を埋める（いつ失効したかが監査で追える。
        T-40 のログアウトと同じ扱い）。
        """
        await self._db.execute(
            update(Session)
            .where(Session.user_id == user_id, Session.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    # --- 内部 -------------------------------------------------------------

    async def _require_user(self, user_id: str) -> User:
        user = await self._db.get(User, user_id)
        if user is None:
            raise ManageUsersError(
                ManageUsersErrorCode.USER_NOT_FOUND,
                "対象のユーザーが見つかりません。",
            )
        return user

    async def _ensure_an_active_admin_remains(
        self, user: User, next_role: Role, next_is_active: bool
    ) -> None:
        """変更後も **有効な admin が1人以上残る**ことを確認する。

        「有効な（`is_active=true`）」で数えるのが要点。停止中の admin は
        ログインできないので、admin が居ることにならない。全 admin を数えて
        しまうと、**最後の1人を停止する操作が通ってしまい**、誰も管理画面へ
        入れなくなる（復旧は CLI のみ）。

        ⚠️ T-41 の `count_admins()` が停止中も数えるのとは**意図的に異なる**。
        あちらは「2人目の初期 admin を CLI で作らせない」ための判定で、
        こちらは「締め出されない」ための判定。目的が違うので数え方も違う。

        Raises:
            ManageUsersError: 変更後に有効な admin が0人になる場合
        """
        others = await self._count_other_active_admins(user.user_id)
        stays_admin = next_role is Role.ADMIN and next_is_active
        if others + (1 if stays_admin else 0) > 0:
            return

        raise ManageUsersError(
            ManageUsersErrorCode.LAST_ADMIN,
            "最後の管理者を降格・停止することはできません。"
            "先に別のユーザーを admin へ昇格させてください"
            "（管理者が0人になると、復旧は CLI（make create-admin）だけになります）。",
        )

    async def _count_other_active_admins(self, exclude_user_id: str) -> int:
        """対象**以外**の有効な admin の人数。

        ⚠️ `SELECT count(*)` ではなく行そのものを **`FOR UPDATE` で読む**。
        数えるだけだと、2人の admin が同時に相手を降格させたときに
        両方が「相手が居る」と判断して 0 人になりうる（それぞれ相手の
        未コミットの変更を見られないため）。admin 行に行ロックを取れば、
        後続のトランザクションは先行がコミットするまで待たされ、
        待った側は更新後の状態を見て正しく 409 を返せる。

        SQLite では `FOR UPDATE` は無視される（SQLAlchemy が出力しない）が、
        SQLite は書き込みトランザクション自体を直列化するので同じ結果になる。
        PostgreSQL へ移行しても成立させるための書き方（§1「DB 固有機能を
        使わない」からは外れない。`FOR UPDATE` は標準 SQL）。
        """
        rows = (
            await self._db.execute(
                select(User.user_id)
                .where(
                    User.role == Role.ADMIN.value,
                    User.is_active.is_(True),
                    User.user_id != exclude_user_id,
                )
                .with_for_update()
            )
        ).all()
        return len(rows)

    def _record_role_change(
        self, *, actor: Principal, user: User, before: Role, after: Role, at: datetime
    ) -> None:
        """ロール変更を監査ログへ積む（commit は呼び出し元）。

        （2026-08-14）T-10 の `AuditService` へ寄せた。`diff` の形も `actor` の
        表記も従来と同一。約束（秘密を書かない・握り潰さない・commit しない）は
        `application/usecases/audit.py` のモジュール docstring を参照。
        """
        self._audit.record_user_role_change(
            actor=actor.actor,
            at=at,
            user_id=user.user_id,
            email=user.email,
            before=before,
            after=after,
        )

    def _record_status_change(
        self, *, actor: Principal, user: User, before: bool, after: bool, at: datetime
    ) -> None:
        """停止・再開を監査ログへ積む（commit は呼び出し元）。"""
        self._audit.record_user_status_change(
            actor=actor.actor,
            at=at,
            user_id=user.user_id,
            email=user.email,
            before=before,
            after=after,
        )


__all__ = [
    "ManageUsersError",
    "ManageUsersErrorCode",
    "ManageUsersUsecase",
]
