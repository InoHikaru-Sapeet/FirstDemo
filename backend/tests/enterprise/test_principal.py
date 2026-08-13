"""ロール定義と Principal（T-08）。

認可（T-09）が参照する唯一の型なので、**認証の実現方法を知らない**ことを
ここで固定する。この境界が保たれている限り、認証方式を差し替えても
§6.2 の権限マトリクスに手を入れずに済む。
"""

import pytest
from pydantic import ValidationError

from enterprise.entities.principal import (
    ASSIGNABLE_ROLES,
    DEFAULT_SELF_REGISTERED_ROLE,
    Principal,
    Role,
)


def test_roles_match_the_design() -> None:
    """設計書 §4.1・仕様書 §2 のアクター表そのまま。"""
    assert {r.value for r in Role} == {"admin", "editor", "viewer", "system"}


def test_self_registration_defaults_to_viewer() -> None:
    """TASKS.md §1.1「登録直後は全員 viewer」。"""
    assert DEFAULT_SELF_REGISTERED_ROLE is Role.VIEWER


def test_system_is_not_assignable_to_a_user() -> None:
    """`system` は cron 用の呼び出し元種別で、ログインする利用者ではない（T-41）。"""
    assert Role.SYSTEM not in ASSIGNABLE_ROLES
    assert ASSIGNABLE_ROLES == {Role.ADMIN, Role.EDITOR, Role.VIEWER}


def test_actor_matches_the_audit_log_format() -> None:
    """設計書 §4.4 の `actor` は `ロール:ユーザ識別子`（例 `admin:admin_a`）。"""
    principal = Principal(subject="admin_a", role=Role.ADMIN)

    assert principal.actor == "admin:admin_a"


def test_only_system_is_internal() -> None:
    """§4.2 の `internal_only` 判定に使う。"""
    assert Principal(subject="cron", role=Role.SYSTEM).is_internal is True
    assert Principal(subject="u1", role=Role.ADMIN).is_internal is False


def test_principal_is_immutable() -> None:
    """解決後にロールを書き換えられると、認可判定の前提が崩れる。"""
    principal = Principal(subject="u1", role=Role.VIEWER)

    with pytest.raises(ValidationError):
        principal.role = Role.ADMIN  # type: ignore[misc]


def test_principal_carries_no_credentials() -> None:
    """パスワード・トークン・セッションを持ち込めないこと。

    ⚠️ 認可の入力に資格情報が混ざると、ログや例外に載る経路が増える。
    """
    assert set(Principal.model_fields) == {"subject", "role"}

    with pytest.raises(ValidationError):
        Principal(subject="u1", role=Role.ADMIN, password="x")  # type: ignore[call-arg]
