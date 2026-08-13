"""サービストークンの発行と照合（T-41）。

重点は「破ると system を騙られる」性質:

- 平文を保存しない（設定に入るのはハッシュで、そこから生トークンは戻せない）
- 比較が**定数時間**（応答時間差から1バイトずつ当てられない）
- **未設定なら常に不一致**（空のハッシュを「何にでも一致」にしない）
"""

import secrets

import pytest

from enterprise.services import service_token as service_token_module
from enterprise.services.service_token import (
    SERVICE_TOKEN_HASH_LENGTH,
    SERVICE_TOKEN_SUBJECT,
    generate_service_token,
    hash_service_token,
    looks_like_service_token_hash,
    verify_service_token,
)

# --- 発行 -----------------------------------------------------------------


def test_generated_tokens_are_long_and_url_safe() -> None:
    token = generate_service_token()

    # token_urlsafe(32) は 256 ビットを base64url で表すので 43 文字。
    assert len(token) >= 43
    assert all(c.isalnum() or c in "-_" for c in token)


def test_generated_tokens_do_not_repeat() -> None:
    tokens = {generate_service_token() for _ in range(50)}

    assert len(tokens) == 50


# --- ハッシュ -------------------------------------------------------------


def test_the_hash_is_a_sha256_hex_digest() -> None:
    token_hash = hash_service_token("some-token")

    assert len(token_hash) == SERVICE_TOKEN_HASH_LENGTH
    assert token_hash == hash_service_token("some-token")  # 決定的


def test_the_raw_token_cannot_be_read_out_of_the_hash() -> None:
    """設定ファイルが漏れても、そのままでは system を騙れない。"""
    token = generate_service_token()

    token_hash = hash_service_token(token)

    assert token not in token_hash
    assert token_hash != token


def test_different_tokens_hash_differently() -> None:
    assert hash_service_token("token-a") != hash_service_token("token-b")


# --- 照合 -----------------------------------------------------------------


def test_the_matching_token_verifies() -> None:
    token = generate_service_token()

    assert verify_service_token(token, hash_service_token(token)) is True


def test_a_wrong_token_does_not_verify() -> None:
    expected = hash_service_token(generate_service_token())

    assert verify_service_token(generate_service_token(), expected) is False


def test_a_token_sharing_a_prefix_does_not_verify() -> None:
    """先頭が一致していても通らない（前方一致で当てられない）。"""
    token = generate_service_token()

    assert verify_service_token(token[:-1] + "x", hash_service_token(token)) is False


def test_an_unset_hash_disables_the_system_path() -> None:
    """⚠️ 未設定を「誰でも system」にしない。ここが逆になると認可が崩壊する。"""
    assert verify_service_token(generate_service_token(), "") is False
    assert verify_service_token("", "") is False


def test_an_empty_token_never_verifies() -> None:
    expected = hash_service_token(generate_service_token())

    assert verify_service_token("", expected) is False


def test_the_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ `==` へ書き換えられていないことを固定する。

    `==` は一致した先頭バイト数だけ時間がかかるため、応答時間の差から
    ハッシュを1バイトずつ復元できる。
    """
    calls: list[tuple[str, str]] = []
    # 差し替え前の実装を捕まえてから包む（包んだ関数から `secrets.compare_digest` を
    # 呼ぶと自分自身を呼び出して無限再帰になる）。
    original_compare_digest = secrets.compare_digest

    def recording_compare_digest(a: str, b: str) -> bool:
        calls.append((a, b))
        return original_compare_digest(a, b)

    monkeypatch.setattr(
        service_token_module.secrets, "compare_digest", recording_compare_digest
    )

    token = generate_service_token()
    assert verify_service_token(token, hash_service_token(token)) is True
    assert len(calls) == 1


def test_configuration_whitespace_and_case_are_tolerated() -> None:
    """`.env` の値は空白が混ざりやすく、16進は大文字でも同じ値。"""
    token = generate_service_token()
    token_hash = hash_service_token(token)

    assert verify_service_token(token, f"  {token_hash}  ") is True
    assert verify_service_token(token, token_hash.upper()) is True


# --- 運用ミスの検出 -------------------------------------------------------


def test_a_hash_is_recognised_as_a_hash() -> None:
    assert looks_like_service_token_hash(hash_service_token("t")) is True
    assert looks_like_service_token_hash(f"  {hash_service_token('t').upper()}  ")


def test_a_raw_token_is_not_mistaken_for_a_hash() -> None:
    """最も起きやすい運用ミス（生トークンを SERVICE_TOKEN_HASH に貼る）を検出する。"""
    assert looks_like_service_token_hash(generate_service_token()) is False


def test_short_or_non_hex_values_are_not_hashes() -> None:
    assert looks_like_service_token_hash("") is False
    assert looks_like_service_token_hash("abc123") is False
    assert looks_like_service_token_hash("z" * SERVICE_TOKEN_HASH_LENGTH) is False


# --- 呼び出し元の識別 -----------------------------------------------------


def test_the_subject_is_stable() -> None:
    """監査ログの actor が `system:cron` で安定していること（設計書 §4.4）。"""
    assert SERVICE_TOKEN_SUBJECT == "cron"
