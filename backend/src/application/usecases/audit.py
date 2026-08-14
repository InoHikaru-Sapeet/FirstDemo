"""監査ログの書き込み（T-10。設計書 §4.4 ／ 仕様書 §6.1・§14）。

**監査ログを積む経路はこのモジュール1つ。** `AuditLog` を直接 `session.add()` する
コードを他所に置かないこと。直書きが散ると、`actor` の表記・`diff` の形・
「何を書かないか」の約束が箇所ごとにずれる（実際 T-41・T-42・T-13 の3箇所で
先行実装され、`at` の渡し方も `diff` の形もばらついていた）。

---

⚠️ **書き込み失敗を握り潰さない。**

このサービスは例外を捕まえない。`add()` が失敗すれば呼び出し元へ伝播し、
呼び出し元のトランザクションごと rollback される。「本処理は成功したが監査ログだけ
静かに落ちた」を作らないため＝**誰が何を変えたか分からない変更を残さない**ため。

`try/except` で握って握り潰す実装をここへ足さないこと。ログだけ出して処理を続けると、
config が書き換わったのに監査ログに行が無い、という追跡不能な状態が生まれる。

---

⚠️ **commit しない。呼び出し元のトランザクションに乗る。**

`record()` は `session.add()` までで、commit は呼び出し元が行う。これは
「config の書き込みが失敗したら監査ログも残らない」（T-13）を成立させるための
設計で、**サービス側で commit するとこの対応が壊れる**（config は保存されて
いないのに「変更した」記録だけが残る、またはその逆）。

---

⚠️ **秘密を書かない。**

平文パスワード・パスワードハッシュ・セッショントークンを `diff` / `target` /
`actor` に入れない。監査ログの参照経路は admin 限定にする想定だが、それでも
置く理由がない（漏れたときの被害だけが増える）。
`tests/application/test_audit.py` が3種すべての非露出を固定している。

---

⚠️ **`at` は UTC で保存する。**

カラムは `UtcDateTime`（T-03）で、naive な datetime は保存時に拒否される。
表示・出力時に `Asia/Tokyo` へ変換する（設計書 §14）。設計書 §4.4 の表は
`at` を「when（Asia/Tokyo）」と書いているが、これは**表示の話**で、保存は UTC。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from adapter.database.models.audit_log import AuditEventType, AuditLog
from enterprise.entities.principal import Role


class AuditService:
    """監査ログを1件積む。**commit はしない**（モジュール docstring 参照）。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # --- 汎用 -------------------------------------------------------------

    def record(
        self,
        *,
        event_type: AuditEventType,
        actor: str,
        at: datetime,
        revision: int | None = None,
        diff: dict[str, Any] | None = None,
        target: str | None = None,
        period: str | None = None,
    ) -> AuditLog:
        """1件積む。

        Args:
            actor: **`role:subject` 形式**（設計書 §4.4。例 `admin:usr_abc`）。
                人以外の実行者は種別を接頭辞にする（例 `cli:create-admin`）。
                `Principal.actor` がこの形を組み立てる。
            at: **tz 付き**の時刻。naive は `UtcDateTime` が拒否する。

        Raises:
            ValueError: `actor` が `役割:識別子` の形でない／`at` が naive。
                どちらも「後から誰の操作か追えない行」を作らないための門番。
        """
        if ":" not in actor or not actor.split(":", 1)[0]:
            raise ValueError(
                f"actor は '役割:識別子' 形式にすること（設計書 §4.4）: {actor!r}"
            )
        if at.tzinfo is None:
            raise ValueError("at は tz 付きの datetime にすること（UTC 保存）。")

        entry = AuditLog(
            audit_id=f"aud_{uuid.uuid4().hex}",
            event_type=event_type,
            actor=actor,
            at=at,
            revision=revision,
            diff=diff,
            target=target,
            period=period,
        )
        self._db.add(entry)
        return entry

    # --- config（T-13）----------------------------------------------------

    def record_config_update(
        self,
        *,
        actor: str,
        at: datetime,
        revision: int,
        diff: dict[str, dict[str, Any]],
        target: str,
    ) -> AuditLog:
        """`config_update`。**`revision` と before→after の差分を必ず伴う**。

        `diff` は `{path: {"before", "after"}}`（T-11 の `diff_configs()`）で、
        設計書 §4.4 の例と同じ形。`meta.*` は呼び出し元で除いてある
        （保存のたびに必ず3件出て、実際に変わった判断基準が埋もれるため）。

        ⚠️ **`diff` には config の中身が入る。** 監査ログの参照経路を admin 限定に
        しないと、「config を admin 以外に露出しない」（仕様書 §2・§6.1）を
        監査ログ経由で破ることになる。現時点で監査ログを返す API は存在しない。
        """
        return self.record(
            event_type=AuditEventType.CONFIG_UPDATE,
            actor=actor,
            at=at,
            revision=revision,
            diff=diff,
            target=target,
        )

    # --- 認証・ユーザー（T-40 / T-41 / T-42）-------------------------------

    def record_user_registered(
        self, *, user_id: str, email: str, role: Role, at: datetime
    ) -> AuditLog:
        """`user_registered`。自己登録（T-40）。

        `actor` は登録した本人。まだセッションが無い（登録直後にログインする
        設計）が、行為者は本人なので `role:user_id` で表す。

        ⚠️ **パスワードは平文もハッシュも渡さない**（引数に取っていない）。

        ⚠️ **ログイン成功・失敗はここへ書かない**（アプリログの担当。件数が多く
        `audit_log` の粒度と合わない。TASKS.md T-10）。
        """
        return self.record(
            event_type=AuditEventType.USER_REGISTERED,
            actor=f"{role.value}:{user_id}",
            at=at,
            diff={"email": email, "role": role.value},
            target=user_id,
        )

    def record_user_role_change(
        self,
        *,
        actor: str,
        at: datetime,
        user_id: str,
        email: str,
        before: Role | None,
        after: Role,
    ) -> AuditLog:
        """`user_role_change`。before→after のロールを `diff` に持つ。

        `before=None` は「その操作が作成した」の意（T-41 の初期 admin 作成）。
        `email` を併記するのは、`user_id` が不透明で人が読めないため。
        """
        return self.record(
            event_type=AuditEventType.USER_ROLE_CHANGE,
            actor=actor,
            at=at,
            diff={
                "role": {
                    "before": before.value if before is not None else None,
                    "after": after.value,
                },
                "email": email,
            },
            target=user_id,
        )

    def record_user_status_change(
        self,
        *,
        actor: str,
        at: datetime,
        user_id: str,
        email: str,
        before: bool,
        after: bool,
    ) -> AuditLog:
        """`user_status_change`。停止・再開（T-42）。

        ⚠️ admin の停止は**実質的な権限剥奪**（ログインできない＝管理者として
        機能しない）なので、降格と同じ重みで残す。これが無いと「誰が admin を
        無力化したか」が追えない。
        """
        return self.record(
            event_type=AuditEventType.USER_STATUS_CHANGE,
            actor=actor,
            at=at,
            diff={
                "is_active": {"before": before, "after": after},
                "email": email,
            },
            target=user_id,
        )


__all__ = ["AuditService"]
