"""パスワードのハッシュ化と照合（T-08）。

このテストが守っているのは主に3つ:

1. **平文が保存・露出されない**（ハッシュ・ログ・例外メッセージ）
2. **bcrypt の 72 バイト制限で認証が壊れない**（黙って切り詰めない）
3. **ハッシュ処理を自前で書いていない**（Passlib へ委譲している）
"""

import logging

import pytest

from enterprise.services import password as password_module
from enterprise.services.password import (
    BCRYPT_ROUNDS,
    MAX_PASSWORD_BYTES,
    MIN_PASSWORD_LENGTH,
    PasswordIssueCode,
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    validate_password_policy,
    verify_and_migrate_password,
    verify_password,
)

VALID_PASSWORD = "correct horse battery staple"


# --- ハッシュ化 -----------------------------------------------------------


def test_hash_is_not_the_plaintext() -> None:
    hashed = hash_password(VALID_PASSWORD)

    assert VALID_PASSWORD not in hashed
    assert hashed != VALID_PASSWORD


def test_hash_is_bcrypt_with_the_configured_cost() -> None:
    """bcrypt を使っていること・コストが効いていることを形式から確認する。"""
    hashed = hash_password(VALID_PASSWORD)

    assert hashed.startswith("$2b$")
    assert hashed.split("$")[2] == f"{BCRYPT_ROUNDS:02d}"


def test_same_password_yields_different_hashes() -> None:
    """ソルトが効いている＝同じパスワードでも保存値が一致しない。

    一致してしまうと、DB を見ただけで「同じパスワードの利用者」が分かる。
    """
    assert hash_password(VALID_PASSWORD) != hash_password(VALID_PASSWORD)


def test_verify_accepts_the_original_and_rejects_others() -> None:
    hashed = hash_password(VALID_PASSWORD)

    assert verify_password(VALID_PASSWORD, hashed) is True
    assert verify_password("wrong password here", hashed) is False
    assert verify_password(VALID_PASSWORD.upper(), hashed) is False


def test_verify_returns_false_for_a_malformed_hash() -> None:
    """DB 破損・移行ミスで壊れたハッシュが来ても例外にしない。

    例外にすると「その利用者のハッシュが壊れている」ことが応答の差から漏れる。
    """
    assert verify_password(VALID_PASSWORD, "not-a-bcrypt-hash") is False
    assert verify_password(VALID_PASSWORD, "") is False


# --- ポリシー（長さのみ）--------------------------------------------------


def test_policy_rejects_a_short_password() -> None:
    issues = validate_password_policy("a" * (MIN_PASSWORD_LENGTH - 1))

    assert [i.code for i in issues] == [PasswordIssueCode.TOO_SHORT]


def test_policy_accepts_the_minimum_length() -> None:
    assert validate_password_policy("a" * MIN_PASSWORD_LENGTH) == []


def test_policy_rejects_an_empty_password() -> None:
    issues = validate_password_policy("")

    assert [i.code for i in issues] == [PasswordIssueCode.EMPTY]


def test_policy_does_not_require_symbols_or_digits() -> None:
    """複雑性要件は課さない（長いパスフレーズを許す）。"""
    assert validate_password_policy("すべてひらがなのぱすふれーず") == []
    assert validate_password_policy("all lowercase letters only") == []


def test_hash_password_raises_on_policy_violation() -> None:
    with pytest.raises(PasswordPolicyError) as excinfo:
        hash_password("short")

    assert [i.code for i in excinfo.value.issues] == [PasswordIssueCode.TOO_SHORT]


def test_verify_does_not_enforce_the_policy() -> None:
    """ポリシーを厳しくしても既存利用者がログインできなくならないこと。

    照合時にポリシーを見ると、`MIN_PASSWORD_LENGTH` を引き上げた瞬間に
    既存パスワードが一斉に無効化される。
    """
    weak = "short"
    hashed = password_module._context.hash(weak)  # ポリシーを迂回して直接ハッシュ化

    assert verify_password(weak, hashed) is True


# --- ⚠️ bcrypt の 72 バイト制限 -------------------------------------------


def test_password_over_72_bytes_is_rejected_not_truncated() -> None:
    """**これを許すと認証が壊れる。**

    bcrypt は 73 バイト目以降を無視する。passlib / bcrypt 4.x はエラーにせず
    黙って切り詰めるため、拒否しないと「先頭 72 バイトが同じ別のパスワード」で
    ログインできてしまう。
    """
    too_long = "a" * (MAX_PASSWORD_BYTES + 1)

    with pytest.raises(PasswordPolicyError) as excinfo:
        hash_password(too_long)

    assert [i.code for i in excinfo.value.issues] == [
        PasswordIssueCode.TOO_LONG_IN_BYTES
    ]


def test_truncation_would_have_collapsed_distinct_passwords() -> None:
    """上限を課さなかった場合に何が起きるかを固定しておく（回帰の説明用）。

    ポリシーを迂回して直接ハッシュ化すると、**72 バイト以降が違うだけの
    別パスワードで照合が通ってしまう**。上のテストはこれを防いでいる。
    """
    shared_prefix = "a" * MAX_PASSWORD_BYTES
    hashed = password_module._context.hash(shared_prefix + "-ORIGINAL-SUFFIX")

    assert password_module._context.verify(shared_prefix + "-OTHER-SUFFIX", hashed)


def test_length_limit_is_measured_in_bytes_not_characters() -> None:
    """日本語は1文字3バイト。**24文字でちょうど上限**に達する。

    文字数で検証していたら、この境界を見逃して切り詰めが起きる。
    """
    assert len(("あ" * 24).encode("utf-8")) == MAX_PASSWORD_BYTES

    assert validate_password_policy("あ" * 24) == []

    issues = validate_password_policy("あ" * 25)
    assert [i.code for i in issues] == [PasswordIssueCode.TOO_LONG_IN_BYTES]


def test_a_japanese_password_at_the_boundary_round_trips() -> None:
    at_limit = "あ" * 24
    hashed = hash_password(at_limit)

    assert verify_password(at_limit, hashed) is True
    assert verify_password("あ" * 23, hashed) is False


def test_short_and_too_long_can_never_both_apply() -> None:
    """現行ポリシーでは2つの違反が同時に起きない。

    UTF-8 は1文字あたり最大4バイトなので、`MIN_PASSWORD_LENGTH` 未満
    （11文字以下）の入力は最大でも 44 バイトにしかならず、72 バイト超に
    ならない。**「両方の違反が同時に返る」ケースは作れない。**

    `validate_password_policy` を早期 return なしで書いているのは、
    将来ポリシーを増やしたときにこの前提が崩れるため（T-05 と同じ方針）。
    ここではその前提のほうを固定しておく。
    """
    max_bytes_for_a_short_password = (MIN_PASSWORD_LENGTH - 1) * 4

    assert max_bytes_for_a_short_password < MAX_PASSWORD_BYTES


# --- コスト変更への追随 ---------------------------------------------------


def test_verify_and_migrate_returns_a_new_hash_for_an_outdated_cost() -> None:
    """`BCRYPT_ROUNDS` を上げた後、ログイン時に静かに移行できること。"""
    outdated = password_module.CryptContext(
        schemes=["bcrypt"], bcrypt__rounds=BCRYPT_ROUNDS - 2
    ).hash(VALID_PASSWORD)

    assert needs_rehash(outdated) is True

    is_valid, new_hash = verify_and_migrate_password(VALID_PASSWORD, outdated)

    assert is_valid is True
    assert new_hash is not None
    assert new_hash.split("$")[2] == f"{BCRYPT_ROUNDS:02d}"
    assert verify_password(VALID_PASSWORD, new_hash) is True


def test_verify_and_migrate_returns_no_new_hash_when_current() -> None:
    current = hash_password(VALID_PASSWORD)

    assert needs_rehash(current) is False

    is_valid, new_hash = verify_and_migrate_password(VALID_PASSWORD, current)

    assert is_valid is True
    assert new_hash is None


def test_verify_and_migrate_rejects_a_wrong_password() -> None:
    outdated = password_module.CryptContext(
        schemes=["bcrypt"], bcrypt__rounds=BCRYPT_ROUNDS - 2
    ).hash(VALID_PASSWORD)

    is_valid, new_hash = verify_and_migrate_password("wrong password here", outdated)

    assert is_valid is False
    assert new_hash is None


# --- 平文・ハッシュが漏れないこと ---------------------------------------


def test_policy_error_message_does_not_contain_the_password() -> None:
    """例外はログに出る。`str(e)` に平文が入っていてはいけない。"""
    secret = "s3cret-but-too-short"[:8]

    with pytest.raises(PasswordPolicyError) as excinfo:
        hash_password(secret)

    assert secret not in str(excinfo.value)
    assert all(secret not in issue.reason for issue in excinfo.value.issues)


def test_hashing_and_verifying_emit_no_logs_containing_the_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        hashed = hash_password(VALID_PASSWORD)
        verify_password(VALID_PASSWORD, hashed)
        verify_password("wrong password here", hashed)

    assert VALID_PASSWORD not in caplog.text
    assert hashed not in caplog.text


def test_importing_the_module_does_not_warn_about_the_bcrypt_version(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """passlib 1.7.4 + bcrypt 4.x の既知の警告を初期化時に握っていること。

    実害はないが、トレースバック付きの WARNING が本番ログに出ると障害に見える。
    """
    with caplog.at_level(logging.WARNING):
        password_module._build_context()

    assert "error reading bcrypt version" not in caplog.text


# --- 「自前でハッシュ処理を書かない」---------------------------------------


def test_hashing_is_delegated_to_passlib() -> None:
    """TASKS.md §1.1「認証」の「自前でハッシュ処理を書かない」を固定する。

    ソルト生成・ストレッチ・定数時間比較を自作すると事故る。このモジュールに
    `hashlib` / `secrets` が現れたら、委譲から逸脱した合図。
    """
    module_globals = vars(password_module)

    assert "hashlib" not in module_globals
    assert "secrets" not in module_globals
    assert isinstance(password_module._context, password_module.CryptContext)
    assert password_module._context.schemes() == ("bcrypt",)
