"""サービストークン発行 CLI（T-41）。

重点:

- `.env` へ案内するのは**ハッシュ**で、生トークンではない
- 生トークンは「再表示できない」ことを利用者に伝える
- 毎回違うトークンが出る
"""

from adapter.cli import create_service_token as cli
from enterprise.services.service_token import (
    hash_service_token,
    looks_like_service_token_hash,
    verify_service_token,
)


def test_the_env_line_carries_the_hash_not_the_raw_token() -> None:
    """⚠️ ここが逆になると、設定ファイルの漏洩がそのまま system の乗っ取りになる。"""
    raw_token = "raw-token-value"
    token_hash = hash_service_token(raw_token)

    text = cli.format_instructions(raw_token, token_hash)

    assert f"SERVICE_TOKEN_HASH={token_hash}" in text
    assert f"SERVICE_TOKEN_HASH={raw_token}" not in text


def test_the_raw_token_is_shown_for_the_cron_side() -> None:
    text = cli.format_instructions("raw-token-value", hash_service_token("x"))

    assert "raw-token-value" in text
    assert "Authorization: Bearer raw-token-value" in text
    assert "再表示できません" in text


def test_the_generated_pair_verifies(capsys) -> None:  # noqa: ANN001
    assert cli.main([]) == 0

    printed = capsys.readouterr().out
    token_hash = _extract_hash(printed)
    raw_token = _extract_raw_token(printed)

    assert looks_like_service_token_hash(token_hash)
    assert verify_service_token(raw_token, token_hash) is True


def test_each_run_issues_a_different_token(capsys) -> None:  # noqa: ANN001
    cli.main([])
    first = capsys.readouterr().out
    cli.main([])
    second = capsys.readouterr().out

    assert _extract_hash(first) != _extract_hash(second)


def _extract_hash(printed: str) -> str:
    for line in printed.splitlines():
        if "SERVICE_TOKEN_HASH=" in line:
            return line.split("SERVICE_TOKEN_HASH=", 1)[1].strip()
    raise AssertionError("SERVICE_TOKEN_HASH の行が出力されていない")


def _extract_raw_token(printed: str) -> str:
    marker = "Authorization: Bearer "
    for line in printed.splitlines():
        if marker in line:
            return line.split(marker, 1)[1].strip().rstrip('"')
    raise AssertionError("Bearer トークンの行が出力されていない")
