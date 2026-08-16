"""AIクライアント層のプロトコルと DI（T-15）。

重点:

- **差し替え口は `get_ai_client()` の1箇所**（`get_authentication_backend()` と同じ形）
- 上位は `AIClient` プロトコルだけに依存でき、CLI か API かを知らない
- 呼び出しメタ（使用モデル・`prompt_version`）が監査／validation に載せられる形で返る
"""

import pytest
from pydantic import BaseModel, TypeAdapter

from adapter.llm import (
    ALLOWED_TOOLS_FLAG,
    WEB_SEARCH_CLI_ARGS,
    WEB_SEARCH_TOOLS,
    AICallMeta,
    AIClient,
    AIClientError,
    AIOutputParseError,
    AIProcessError,
    AIProtocolError,
    AIResponseError,
    AIResult,
    AITimeoutError,
    AIUnavailableError,
    ClaudeCliClient,
    describe_models,
    get_ai_client,
    meta_to_audit_payload,
)
from adapter.llm.ai_client import (
    DIAGNOSTIC_LIMIT,
    EMPTY_STREAM,
    OutputSchema,
    resolve_output_adapter,
    summarize_stream,
)
from config import Settings
from tests.adapter.test_claude_cli_client import (
    Answer,
    FakeRunner,
    envelope,
    stdout_of,
)


def test_the_di_hook_returns_the_claude_cli_implementation() -> None:
    """現行の実装は CLI ひとつだけ（本番は API 実装へ差し替える。§1.1）。"""
    client = get_ai_client()

    assert isinstance(client, ClaudeCliClient)


def test_the_cli_client_satisfies_the_protocol() -> None:
    assert isinstance(get_ai_client(), AIClient)


async def test_the_web_search_capability_is_resolved_by_this_layer() -> None:
    """⚠️ 上位（T-16 crawl）が言うのは「web 検索を使う」までで、
    `--allowedTools` という CLI 固有の書き方はこの層から出さない。"""
    runner = FakeRunner(outcomes=[stdout_of(envelope())])
    client = ClaudeCliClient.from_settings(
        Settings(_env_file=None), extra_args=WEB_SEARCH_CLI_ARGS, runner=runner
    )

    await client.complete(prompt="q", output_schema=Answer)

    assert runner.calls[0].argv[-3:] == ["--allowedTools", "WebSearch", "WebFetch"]


def test_reading_the_article_body_is_part_of_the_web_search_capability() -> None:
    """⚠️ **実測（2026-08-16 / `make run-weekly`）**: `WebSearch` だけを許可すると
    `permission_denials` に **`WebFetch` の拒否**が入って落ちた。PROMPT-1 は本文からの
    2〜4文の客観要約（§13.2）を求めるので、モデルは本文を読むために `WebFetch` を使う。
    """
    assert WEB_SEARCH_TOOLS == ("WebSearch", "WebFetch")
    assert WEB_SEARCH_CLI_ARGS == ("--allowedTools", "WebSearch", "WebFetch")


def test_only_the_two_web_tools_are_allowed() -> None:
    """⚠️ **crawl の子プロセスへ実行系のツールを渡さない**（混入経路を作らない）。

    許可した名前はそのまま `claude` へ渡る。収集に要るのは「検索する」「読む」の
    2つだけで、`Bash` やファイル書き込みは要らない。**足すときはここが落ちる。**
    """
    assert len(WEB_SEARCH_TOOLS) == 2
    assert WEB_SEARCH_CLI_ARGS.count(ALLOWED_TOOLS_FLAG) == 1

    forbidden = ("Bash", "Write", "Edit", "NotebookEdit", "Read", "Task", "WebSearch(")
    for name in forbidden:
        assert not any(tool.startswith(name) for tool in WEB_SEARCH_TOOLS)


async def test_the_crawl_client_gets_no_permission_bypass_flag() -> None:
    """⚠️ 許可は**列挙**で渡す。「全部許可」のフラグへ逃げない
    （逃げると、拒否が起きない＝`permission_denials` の歯止めごと無効になる）。"""
    runner = FakeRunner(outcomes=[stdout_of(envelope())])
    client = ClaudeCliClient.from_settings(
        Settings(_env_file=None), extra_args=WEB_SEARCH_CLI_ARGS, runner=runner
    )

    await client.complete(prompt="q", output_schema=Answer)

    argv = runner.calls[0].argv
    assert "--dangerously-skip-permissions" not in argv
    assert "--permission-mode" not in argv


async def test_without_the_capability_no_tool_is_allowed() -> None:
    """既定では許可を足さない（分類・採点はツールを使わない）。"""
    runner = FakeRunner(outcomes=[stdout_of(envelope())])
    client = ClaudeCliClient.from_settings(Settings(_env_file=None), runner=runner)

    await client.complete(prompt="q", output_schema=Answer)

    assert "--allowedTools" not in runner.calls[0].argv


async def test_the_upper_layers_can_depend_on_the_protocol_alone() -> None:
    """CLI を知らない差し替え（本番の API 実装／テストダブル）が成り立つこと。"""

    class StubAIClient:
        async def complete[T](
            self,
            *,
            prompt: str,
            output_schema: OutputSchema[T],
            prompt_version: str | None = None,
            timeout: float | None = None,
        ) -> AIResult[T]:
            adapter = resolve_output_adapter(output_schema)
            return AIResult(
                value=adapter.validate_python({"label": "stub", "score": 1}),
                meta=AICallMeta(requested_model="stub-model"),
            )

    client: AIClient = StubAIClient()

    result = await client.complete(prompt="q", output_schema=Answer)

    assert isinstance(client, AIClient)
    assert result.value == Answer(label="stub", score=1)


async def test_from_settings_takes_the_command_model_and_timeout() -> None:
    settings = Settings(
        _env_file=None,
        ai_cli_command="/opt/homebrew/bin/claude",
        anthropic_model="claude-opus-5",
        ai_timeout_seconds=600,
    )
    runner = FakeRunner(outcomes=[stdout_of(envelope())])
    client = ClaudeCliClient.from_settings(settings, runner=runner)

    await client.complete(prompt="q", output_schema=Answer)

    argv = runner.calls[0].argv
    assert argv[0] == "/opt/homebrew/bin/claude"
    assert argv[argv.index("--model") + 1] == "claude-opus-5"
    assert runner.calls[0].timeout == pytest.approx(600.0)


def test_the_timeout_defaults_are_long_enough_for_the_measured_overhead() -> None:
    """⚠️ 些細なプロンプトでも実測 約131秒。短い既定は本番相当の実行を殺す。"""
    settings = Settings(_env_file=None)

    assert settings.ai_timeout_seconds >= 600
    assert settings.ai_crawl_timeout_seconds >= 1800


# --- 出力スキーマの受け取り方 ---------------------------------------------


def test_a_model_type_and_a_type_adapter_are_both_accepted() -> None:
    class Item(BaseModel):
        name: str

    adapter = TypeAdapter(list[Item])

    assert resolve_output_adapter(Item).validate_python({"name": "x"}) == Item(name="x")
    assert resolve_output_adapter(adapter) is adapter


# --- 監査／validation メタ（T-30 連携）------------------------------------


def test_the_model_label_prefers_what_was_actually_used() -> None:
    meta = AICallMeta(requested_model="claude-sonnet-5", models_used=("claude-opus-5",))

    assert describe_models(meta) == "claude-opus-5"


def test_an_unconfirmed_model_is_labelled_as_such() -> None:
    """「指定した」と「使われた」を混ぜない（監査で取り違えないため）。"""
    meta = AICallMeta(requested_model="claude-opus-5")

    assert describe_models(meta) == "claude-opus-5(未確認)"


def test_the_audit_payload_carries_the_reproducibility_fields() -> None:
    """設計書 §9.2「使用した `prompt_version` を記録し再現性を確保」。"""
    meta = AICallMeta(
        requested_model="claude-opus-5",
        models_used=("claude-opus-5",),
        prompt_version="1.0.0",
        attempts=2,
        duration_ms=131497,
        total_cost_usd=0.08213,
        session_id="sess-1",
    )

    payload = meta_to_audit_payload(meta)

    assert payload["model"] == "claude-opus-5"
    assert payload["prompt_version"] == "1.0.0"
    assert payload["attempts"] == 2
    assert payload["duration_ms"] == 131497


def test_the_audit_payload_does_not_carry_the_prompt_or_the_response() -> None:
    """本文は成果物ファイル側にある。監査に積むのは再現の手がかりだけ。"""
    payload = meta_to_audit_payload(AICallMeta(requested_model="claude-opus-5"))

    assert "prompt" not in payload
    assert "result" not in payload


# --- 例外（呼び出し元が原因で分岐できること）------------------------------


def test_every_failure_shares_one_base_exception() -> None:
    """ジョブ側は基底でまとめて捕まえ、原因で分岐したいときだけ下位を見る。"""
    for error in (
        AIUnavailableError("x", command="claude"),
        AITimeoutError("x", timeout_seconds=1.0),
        AIProcessError("x", exit_code=1, stderr=""),
        AIProtocolError("x", stdout="", stderr=""),
        AIResponseError("x", reasons=(), stderr=""),
        AIOutputParseError("x", attempts=1, issues=(), payload=""),
    ):
        assert isinstance(error, AIClientError)


def test_the_failures_are_distinguishable_from_each_other() -> None:
    """⚠️ どれかが他の親になっていると `except` の順序で取り違える。"""
    types = (
        AIUnavailableError,
        AITimeoutError,
        AIProcessError,
        AIProtocolError,
        AIResponseError,
        AIOutputParseError,
    )

    for error_type in types:
        others = [other for other in types if other is not error_type]
        assert not issubclass(error_type, tuple(others))


# --- 診断出力の切り詰め ---------------------------------------------------


def test_an_empty_stream_is_shown_as_empty() -> None:
    assert summarize_stream("   \n ") == EMPTY_STREAM


def test_a_short_stream_is_kept_as_is() -> None:
    assert summarize_stream("  Invalid API key  ") == "Invalid API key"


def test_a_long_stream_says_that_it_was_truncated() -> None:
    """黙って切り詰めない（載っている分が全部だと誤解させない）。"""
    summary = summarize_stream("a" * (DIAGNOSTIC_LIMIT + 10))

    assert summary.startswith("a" * DIAGNOSTIC_LIMIT)
    assert "10 文字省略" in summary
