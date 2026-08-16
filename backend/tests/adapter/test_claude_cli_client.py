"""Claude Code CLI 実装（T-15）。

⚠️ **このテストは実際に `claude` を起動しない。** サブプロセスの実行は
`CommandRunner` で差し替え、標準出力には**手元で実測した封筒 JSON** を流し込む。
CI に CLI のインストールとログインを要求しないため（T-15 完了条件）。

重点:

- 封筒 → `result` の**2段階パース**が通ること
- 成功判定を**終了コード0だけに頼らない**こと（`is_error` / `subtype` /
  `api_error_status` / `stop_reason=refusal`）
- 失敗の原因が**呼び出し元で判別できる別個の例外**になること
- スキーマ不一致は**指摘をプロンプトへ載せて**リトライすること
- 実際に使われたモデル（`modelUsage`）と `prompt_version` がメタに載ること
"""

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter

from adapter.llm.ai_client import (
    AIOutputParseError,
    AIProcessError,
    AIProtocolError,
    AIResponseError,
    AITimeoutError,
    AIUnavailableError,
)
from adapter.llm.claude_cli_client import (
    API_KEY_ENV,
    ClaudeCliClient,
    CommandResult,
    SubprocessCommandRunner,
)

# 2026-08-14 に手元で実測した封筒（claude 2.1.232 / Team 契約ログイン済み / macOS）。
# `1+1` という些細なプロンプトでも duration_ms が約131秒だったことも含めて残す。
# `type` / `num_turns` / `usage` は封筒の余分なフィールドで、extra="ignore" が
# これらを無視できることも同時に固定している。
MEASURED_ENVELOPE: dict[str, Any] = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 131497,
    "duration_api_ms": 4231,
    "num_turns": 1,
    "result": "2",
    "session_id": "b0f5e0d2-1f6a-4a1e-9a53-2f0c4b8d1e77",
    "total_cost_usd": 0.08213,
    "stop_reason": None,
    "api_error_status": None,
    "usage": {"input_tokens": 12, "output_tokens": 5},
    "modelUsage": {
        "claude-opus-5": {
            "inputTokens": 12,
            "outputTokens": 5,
            "costUSD": 0.08213,
        }
    },
}


# 2026-08-14 の追加実測: **`--allowedTools` を付けずに**ツールの要る指示を出した場合。
# ⚠️ **`is_error` / `subtype` / `api_error_status` / `stop_reason` は成功時と同じ。**
# 違うのは `permission_denials` が非空になることと、`result` が「権限が無いので
# 実行できない」旨の日本語文になることだけ。**この2件が既存の成功判定をすり抜けた**
# のがこの封筒をここへ残す理由。
# （ID・所要時間・文面の細部は記録から起こしたもので、判定には使っていない。
#  判定に使うのは「`permission_denials` が空でないこと」だけ。）
MEASURED_DENIAL_ENVELOPE: dict[str, Any] = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 24518,
    "duration_api_ms": 9042,
    "num_turns": 2,
    "result": (
        "申し訳ありませんが、WebSearch ツールの使用権限が与えられていないため、"
        "検索を実行できませんでした。"
    ),
    "session_id": "6a4b1c77-2b0e-4c39-9c8e-3d51a0f9b204",
    "total_cost_usd": 0.04127,
    "stop_reason": None,
    "api_error_status": None,
    "permission_denials": [
        {
            "tool_name": "WebSearch",
            "tool_use_id": "toolu_01B9xQ7mS2vK4dP6fL8nR3wY",
            "tool_input": {"query": "AI ニュース 2026-W31"},
        }
    ],
    "usage": {"input_tokens": 1843, "output_tokens": 96},
    "modelUsage": {
        "claude-opus-5": {
            "inputTokens": 1843,
            "outputTokens": 96,
            "costUSD": 0.04127,
            "webSearchRequests": 0,
        }
    },
}

# 2026-08-16 の実測（初の通し実行 `make run-weekly PERIOD=2026-W33` / claude 2.1.232）:
# **`--allowedTools "WebSearch"` だけを渡した crawl** は、約735秒走ったあと
# `permission_denials` に **`WebFetch` の拒否3件**を載せて返り、この検査で落ちた。
# ⚠️ **検索そのものは実施されている**（`webSearchRequests` は非0）ので、
# T-16 の「検索したか」の検査だけでは素通りする。**捉えたのはこちらの検査だけ。**
# → 対処は許可へ `WebFetch` を足すこと（`adapter.llm.WEB_SEARCH_TOOLS`）。
# （ID・回数・費用の細部は記録から起こしたもので、判定には使っていない。
#  判定に使うのは「`permission_denials` が空でないこと」だけ。）
MEASURED_WEBFETCH_DENIAL_ENVELOPE: dict[str, Any] = {
    **MEASURED_DENIAL_ENVELOPE,
    "duration_ms": 735412,
    "result": (
        "WebFetch ツールの使用権限が無いため、記事本文を読んで要約することが"
        "できませんでした。"
    ),
    "permission_denials": [
        {
            "tool_name": "WebFetch",
            "tool_use_id": f"toolu_01WebFetchDenied{index}",
            "tool_input": {"url": f"https://example.com/news/{index}"},
        }
        for index in (1, 2, 3)
    ],
    "modelUsage": {
        "claude-opus-5": {
            "inputTokens": 24180,
            "outputTokens": 1420,
            "costUSD": 0.61,
            "webSearchRequests": 7,
        }
    },
}

# 2026-08-14 の追加実測: 「JSON のみ出力」と指示しても、```json のコードフェンスで
# 包まれ、**後ろに説明文と `Sources:` が付く**ことがある（実測で発生）。
# 1回の呼び出しが数分〜30分かかるので、この逸脱はリトライを消費せず吸収する。
MEASURED_FENCED_RESULT = """```json
{"label": "AI", "score": 7}
```

上記が対象期間の収集結果です。件数は1件でした。

Sources:
- https://example.com/news/1
"""


class Answer(BaseModel):
    """テスト用の出力スキーマ。"""

    model_config = ConfigDict(extra="forbid")

    label: str
    score: int


ANSWER_JSON = '{"label": "AI", "score": 7}'


@dataclass
class RecordedCall:
    argv: list[str]
    env: dict[str, str]
    timeout: float | None


@dataclass
class FakeRunner:
    """`claude` を起動しない差し替え。

    `outcomes` を順に返し、尽きたら最後のものを繰り返す（同じ失敗が続く
    リトライの検証用）。`Exception` を入れるとその場で raise する。
    """

    outcomes: Sequence[CommandResult | Exception]
    calls: list[RecordedCall] = field(default_factory=list)

    async def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: float | None,
    ) -> CommandResult:
        self.calls.append(RecordedCall(argv=list(argv), env=dict(env), timeout=timeout))
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def envelope(result_text: str | None = ANSWER_JSON, **overrides: Any) -> str:
    """実測した封筒をベースに、一部だけ差し替えた標準出力を作る。"""
    payload = dict(MEASURED_ENVELOPE)
    payload["result"] = result_text
    for key, value in overrides.items():
        if value is _ABSENT:
            payload.pop(key, None)
        else:
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False)


def denial_envelope(**overrides: Any) -> str:
    """実測した**拒否つき**封筒をベースに、一部だけ差し替えた標準出力を作る。"""
    payload = dict(MEASURED_DENIAL_ENVELOPE)
    for key, value in overrides.items():
        if value is _ABSENT:
            payload.pop(key, None)
        else:
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False)


class _Absent:
    """`envelope()` でフィールドを**消す**ための印。"""


_ABSENT = _Absent()


def stdout_of(text: str, *, exit_code: int = 0, stderr: str = "") -> CommandResult:
    return CommandResult(exit_code=exit_code, stdout=text, stderr=stderr)


def build_client(
    outcomes: Sequence[CommandResult | Exception],
    *,
    max_attempts: int = 3,
    **kwargs: Any,
) -> tuple[ClaudeCliClient, FakeRunner, list[float]]:
    """クライアントと、記録用のランナー／バックオフ待ち時間を返す。"""
    runner = FakeRunner(outcomes=outcomes)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = ClaudeCliClient(
        runner=runner,
        sleep=sleep,
        max_attempts=max_attempts,
        **kwargs,
    )
    return client, runner, sleeps


# --- 正常系（実測した封筒）------------------------------------------------


async def test_the_measured_envelope_is_parsed_in_two_stages() -> None:
    """封筒 → `result` 文字列 → 出力スキーマ。応答本文は封筒の中にある。"""
    client, _, _ = build_client([stdout_of(envelope())])

    result = await client.complete(prompt="質問", output_schema=Answer)

    assert result.value == Answer(label="AI", score=7)


async def test_the_model_actually_used_comes_from_model_usage() -> None:
    """⚠️ 「指定した」ではなく「使われた」モデルを載せる（監査／validation 用）。"""
    client, _, _ = build_client(
        [stdout_of(envelope(modelUsage={"claude-opus-5": {"costUSD": 0.1}}))],
        model="claude-sonnet-5",
    )

    meta = (await client.complete(prompt="q", output_schema=Answer)).meta

    assert meta.requested_model == "claude-sonnet-5"
    assert meta.models_used == ("claude-opus-5",)


async def test_the_web_search_count_comes_from_model_usage() -> None:
    """⚠️ **実測**: 実施回数は封筒トップの `server_tool_use` ではなく
    `modelUsage[].webSearchRequests` に出る。crawl（T-16）がここを見る。"""
    client, _, _ = build_client(
        [
            stdout_of(
                envelope(
                    modelUsage={
                        "claude-opus-5": {"costUSD": 0.1, "webSearchRequests": 7},
                        "claude-haiku-4-5-20251001": {
                            "costUSD": 0.01,
                            "webSearchRequests": 2,
                        },
                    }
                )
            )
        ]
    )

    meta = (await client.complete(prompt="q", output_schema=Answer)).meta

    assert meta.web_search_requests == 9  # モデルをまたいで合計する


async def test_a_reported_zero_search_count_is_kept_as_zero() -> None:
    """許可はあるが検索しなかった場合。⚠️ `None`（報告なし）と混ぜない。"""
    client, _, _ = build_client(
        [stdout_of(envelope(modelUsage={"claude-opus-5": {"webSearchRequests": 0}}))]
    )

    meta = (await client.complete(prompt="q", output_schema=Answer)).meta

    assert meta.web_search_requests == 0


@pytest.mark.parametrize(
    "usage",
    [
        {"claude-opus-5": {"costUSD": 0.1}},  # キーが無い（実測の成功封筒がこの形）
        {"claude-opus-5": {"webSearchRequests": True}},  # bool は回数ではない
        {"claude-opus-5": {"webSearchRequests": "3"}},  # 文字列も読み替えない
        {"claude-opus-5": "使えません"},  # 要素が dict ですらない
    ],
)
async def test_an_unreported_search_count_stays_unknown(usage: dict[str, Any]) -> None:
    """⚠️ 報告が無いことを 0 と書かない（「していない」と「分からない」は別）。"""
    client, _, _ = build_client([stdout_of(envelope(modelUsage=usage))])

    meta = (await client.complete(prompt="q", output_schema=Answer)).meta

    assert meta.web_search_requests is None


async def test_the_measured_success_envelope_reports_no_search_count() -> None:
    """実測の成功封筒（`1+1`）には `webSearchRequests` が無い＝不明。"""
    client, _, _ = build_client([stdout_of(envelope())])

    meta = (await client.complete(prompt="q", output_schema=Answer)).meta

    assert meta.web_search_requests is None


async def test_an_unknown_model_is_not_filled_in_with_the_requested_one() -> None:
    """`modelUsage` が無いときに指定値で埋めない（別の事実を混ぜない）。"""
    client, _, _ = build_client([stdout_of(envelope(modelUsage=_ABSENT))])

    meta = (await client.complete(prompt="q", output_schema=Answer)).meta

    assert meta.models_used == ()


async def test_the_envelope_metadata_is_carried_into_the_result() -> None:
    client, _, _ = build_client([stdout_of(envelope())])

    meta = (
        await client.complete(prompt="q", output_schema=Answer, prompt_version="1.2.0")
    ).meta

    assert meta.prompt_version == "1.2.0"
    assert meta.attempts == 1
    assert meta.duration_ms == 131497
    assert meta.total_cost_usd == pytest.approx(0.08213)
    assert meta.session_id == MEASURED_ENVELOPE["session_id"]


async def test_a_type_adapter_can_be_passed_as_the_output_schema() -> None:
    """トップレベルが配列のスキーマ（`raw_articles.json` は array。§2.3）用。"""
    adapter = TypeAdapter(list[Answer])
    client, _, _ = build_client([stdout_of(envelope('[{"label":"a","score":1}]'))])

    result = await client.complete(prompt="q", output_schema=adapter)

    assert result.value == [Answer(label="a", score=1)]


# --- CLI の呼び方 ---------------------------------------------------------


async def test_the_cli_is_called_the_way_it_was_measured() -> None:
    client, runner, _ = build_client(
        [stdout_of(envelope())], command="/usr/local/bin/claude", model="claude-opus-5"
    )

    await client.complete(prompt="質問本文", output_schema=Answer)

    argv = runner.calls[0].argv
    assert argv[0] == "/usr/local/bin/claude"
    assert argv[1] == "-p"
    assert argv[2].startswith("質問本文")
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-opus-5"


async def test_the_prompt_carries_the_schema_and_forbids_prose() -> None:
    """構造化出力の担保は「JSON のみ出力させる指示 ＋ 検証 ＋ リトライ」。"""
    client, runner, _ = build_client([stdout_of(envelope())])

    await client.complete(prompt="質問本文", output_schema=Answer)

    prompt = runner.calls[0].argv[2]
    assert "JSON Schema" in prompt
    assert '"score"' in prompt
    assert "JSON だけ" in prompt


async def test_the_api_key_is_not_handed_to_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ 認証は Team 契約のログインセッション。APIキーを渡すと課金経路が移る。"""
    monkeypatch.setenv(API_KEY_ENV, "sk-must-not-leak")
    monkeypatch.setenv("HOME_MARKER_FOR_TEST", "kept")
    client, runner, _ = build_client([stdout_of(envelope())])

    await client.complete(prompt="q", output_schema=Answer)

    assert API_KEY_ENV not in runner.calls[0].env
    assert runner.calls[0].env["HOME_MARKER_FOR_TEST"] == "kept"


async def test_extra_args_are_appended_without_touching_the_protocol() -> None:
    """T-16 が web 検索の許可等を足すための逃げ道（プロトコルには出さない）。

    ⚠️ **`--allowedTools` は値を空白区切りで複数取る（可変長）ので、`extra_args` は
    argv の末尾に置く。** 後ろに別のフラグを足すと、その値まで許可の一覧として
    読まれる。
    """
    client, runner, _ = build_client(
        [stdout_of(envelope())],
        extra_args=("--allowedTools", "WebSearch", "WebFetch"),
    )

    await client.complete(prompt="q", output_schema=Answer)

    argv = runner.calls[0].argv
    assert argv[-3:] == ["--allowedTools", "WebSearch", "WebFetch"]
    assert argv.index("--allowedTools") == len(argv) - 3


async def test_the_default_timeout_is_used_when_the_caller_does_not_pass_one() -> None:
    client, runner, _ = build_client(
        [stdout_of(envelope())], default_timeout_seconds=600.0
    )

    await client.complete(prompt="q", output_schema=Answer)

    assert runner.calls[0].timeout == pytest.approx(600.0)


async def test_the_caller_can_lengthen_the_timeout_per_call() -> None:
    """crawl は 30分（`ai_crawl_timeout_seconds`）を渡す。"""
    client, runner, _ = build_client(
        [stdout_of(envelope())], default_timeout_seconds=600.0
    )

    await client.complete(prompt="q", output_schema=Answer, timeout=1800)

    assert runner.calls[0].timeout == pytest.approx(1800.0)


# --- 構造化出力のリトライ -------------------------------------------------


async def test_a_schema_mismatch_is_retried_with_the_parse_errors_in_the_prompt() -> (
    None
):
    client, runner, sleeps = build_client(
        [stdout_of(envelope('{"label": "AI"}')), stdout_of(envelope())]
    )

    result = await client.complete(prompt="質問本文", output_schema=Answer)

    assert result.value == Answer(label="AI", score=7)
    assert result.meta.attempts == 2
    retry_prompt = runner.calls[1].argv[2]
    assert "質問本文" in retry_prompt  # 試行ごとに別セッション＝毎回送り直す
    assert "JSON Schema" in retry_prompt
    assert "score" in retry_prompt
    assert '{"label": "AI"}' in retry_prompt  # 前回出力もそのまま載せる
    assert sleeps == [pytest.approx(2.0)]


async def test_the_retry_limit_is_respected_and_reports_the_last_issues() -> None:
    client, runner, sleeps = build_client(
        [stdout_of(envelope('{"label": "AI"}'))], max_attempts=2
    )

    with pytest.raises(AIOutputParseError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert caught.value.attempts == 2
    assert len(runner.calls) == 2
    assert [issue.path for issue in caught.value.issues] == ["score"]
    assert '{"label": "AI"}' in caught.value.payload
    assert len(sleeps) == 1  # 最後の試行の後は待たない


async def test_the_backoff_grows_between_attempts() -> None:
    client, _, sleeps = build_client(
        [stdout_of(envelope("not json"))],
        max_attempts=4,
        retry_backoff_seconds=1.5,
    )

    with pytest.raises(AIOutputParseError):
        await client.complete(prompt="q", output_schema=Answer)

    assert sleeps == [pytest.approx(1.5), pytest.approx(3.0), pytest.approx(6.0)]


async def test_retries_are_disabled_when_max_attempts_is_one() -> None:
    client, runner, sleeps = build_client(
        [stdout_of(envelope("not json"))], max_attempts=1
    )

    with pytest.raises(AIOutputParseError):
        await client.complete(prompt="q", output_schema=Answer)

    assert len(runner.calls) == 1
    assert sleeps == []


def test_a_zero_attempt_client_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        ClaudeCliClient(max_attempts=0)


async def test_a_code_fence_around_the_json_is_tolerated() -> None:
    """1回が数分かかるので、この程度の逸脱でリトライを消費しない。"""
    client, _, _ = build_client([stdout_of(envelope(f"```json\n{ANSWER_JSON}\n```"))])

    result = await client.complete(prompt="q", output_schema=Answer)

    assert result.value.score == 7


async def test_the_measured_fence_with_trailing_prose_is_extracted() -> None:
    """実測: フェンス＋後続の説明文・`Sources:` が付いて返ることがある。"""
    client, runner, _ = build_client([stdout_of(envelope(MEASURED_FENCED_RESULT))])

    result = await client.complete(prompt="q", output_schema=Answer)

    assert result.value == Answer(label="AI", score=7)
    assert len(runner.calls) == 1  # 逸脱の吸収にリトライを消費しない


async def test_prose_before_the_json_is_extracted() -> None:
    """前置きが付いた場合も最初の完全な JSON 値を取り出す。"""
    client, _, _ = build_client(
        [stdout_of(envelope(f"はい、こちらです: {ANSWER_JSON}"))]
    )

    result = await client.complete(prompt="q", output_schema=Answer)

    assert result.value.score == 7


async def test_a_top_level_array_is_extracted_from_prose() -> None:
    """`raw_articles.json` はトップレベルが array（設計書 §2.3）。"""
    adapter = TypeAdapter(list[Answer])
    client, _, _ = build_client(
        [stdout_of(envelope('収集結果です:\n[{"label":"a","score":1}]\n以上です。'))]
    )

    result = await client.complete(prompt="q", output_schema=adapter)

    assert result.value == [Answer(label="a", score=1)]


async def test_only_the_first_complete_json_value_is_taken() -> None:
    """⚠️ 断片を継ぎ接ぎしない。取るのは最初の完全な値ひとつだけ。"""
    result_text = ANSWER_JSON + '\nおまけ: {"label": "B", "score": 1}'
    client, _, _ = build_client([stdout_of(envelope(result_text))])

    result = await client.complete(prompt="q", output_schema=Answer)

    assert result.value == Answer(label="AI", score=7)


async def test_text_without_any_json_still_falls_back_to_the_retry_path() -> None:
    """取り出せなければ従来どおり（スキーマ検証で失敗 → 指摘つきリトライ）。"""
    client, runner, _ = build_client(
        [stdout_of(envelope("権限が無いため実行できませんでした。"))], max_attempts=2
    )

    with pytest.raises(AIOutputParseError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert len(runner.calls) == 2
    assert "権限が無いため" in caught.value.payload


# --- 異常系（原因ごとに別の例外）------------------------------------------


async def test_a_non_zero_exit_raises_a_process_error_with_stderr() -> None:
    client, runner, _ = build_client(
        [stdout_of("", exit_code=1, stderr="Invalid API key · Please run /login")]
    )

    with pytest.raises(AIProcessError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert caught.value.exit_code == 1
    assert "/login" in caught.value.stderr
    assert "/login" in str(caught.value)
    assert len(runner.calls) == 1  # 呼び出し失敗はリトライしない


async def test_a_non_zero_exit_wins_over_a_success_looking_envelope() -> None:
    """終了コードと封筒が矛盾したら安全側（失敗）へ倒す。"""
    client, _, _ = build_client([stdout_of(envelope(), exit_code=2, stderr="boom")])

    with pytest.raises(AIProcessError):
        await client.complete(prompt="q", output_schema=Answer)


async def test_stdout_that_is_not_an_envelope_raises_a_protocol_error() -> None:
    """エラー時の出力形式は未実測。封筒として読めなければ推測で補わない。"""
    client, _, _ = build_client(
        [stdout_of("Usage: claude [options]", stderr="unknown option")]
    )

    with pytest.raises(AIProtocolError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert "Usage: claude" in caught.value.stdout
    assert "unknown option" in caught.value.stderr


async def test_a_json_array_on_stdout_is_not_accepted_as_an_envelope() -> None:
    client, _, _ = build_client([stdout_of('[{"result": "x"}]')])

    with pytest.raises(AIProtocolError, match="オブジェクトではありません"):
        await client.complete(prompt="q", output_schema=Answer)


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("is_error", "maybe"),  # ⚠️ "yes"/"true" は Pydantic が bool へ寄せる
        ("result", {"label": "AI", "score": 7}),  # result は**文字列**で来る（実測）
        ("duration_ms", "しばらく"),
    ],
)
async def test_an_envelope_field_of_the_wrong_type_raises_a_protocol_error(
    field_name: str, value: Any
) -> None:
    client, _, _ = build_client([stdout_of(envelope(**{field_name: value}))])

    with pytest.raises(AIProtocolError):
        await client.complete(prompt="q", output_schema=Answer)


async def test_an_error_envelope_is_not_treated_as_success() -> None:
    """⚠️ 終了コード0でも `is_error=true` なら失敗（実測では成功時 0 が返る）。"""
    client, _, _ = build_client(
        [stdout_of(envelope(is_error=True), stderr="rate limited")]
    )

    with pytest.raises(AIResponseError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert "is_error=true" in caught.value.reasons
    assert "rate limited" in caught.value.stderr


async def test_an_api_error_status_is_not_treated_as_success() -> None:
    client, _, _ = build_client([stdout_of(envelope(api_error_status=529))])

    with pytest.raises(AIResponseError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert caught.value.reasons == ("api_error_status=529",)


async def test_a_subtype_other_than_success_is_not_treated_as_success() -> None:
    client, _, _ = build_client([stdout_of(envelope(subtype="error_max_turns"))])

    with pytest.raises(AIResponseError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert "error_max_turns" in caught.value.reasons[0]


async def test_the_measured_permission_denial_is_not_treated_as_success() -> None:
    """⚠️ **実測**: 許可の無いツールを使おうとすると、封筒は成功のまま
    `permission_denials` に拒否記録が入り、`result` は権限が無い旨の文になる。
    `is_error` / `subtype` / `api_error_status` / `stop_reason` の4つだけでは
    すり抜けるので、拒否記録そのものを見る。"""
    client, runner, _ = build_client([stdout_of(denial_envelope())])

    with pytest.raises(AIResponseError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert "WebSearch" in " ".join(caught.value.reasons)
    assert "WebSearch" in str(caught.value)
    assert len(runner.calls) == 1  # 許可の付け忘れはリトライで直らない


async def test_the_measured_web_fetch_denial_is_not_treated_as_success() -> None:
    """⚠️ **実測（2026-08-16 の通し実行）**: `WebSearch` だけを許可した crawl は
    `WebFetch` の拒否3件で落ちた。**検索は実施されている**（`webSearchRequests=7`）
    ので、T-16 の「検索したか」の検査は素通りする＝**この検査だけが捉えた**。

    許可へ `WebFetch` を足したあとも、**この経路は残す**（許可の付け忘れ・CLI 側の
    ツール名変更は、また同じ形で現れる）。
    """
    client, runner, _ = build_client(
        [stdout_of(json.dumps(MEASURED_WEBFETCH_DENIAL_ENVELOPE, ensure_ascii=False))]
    )

    with pytest.raises(AIResponseError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert "WebFetch" in " ".join(caught.value.reasons)
    assert "permission_denials=3件" in " ".join(caught.value.reasons)
    assert len(runner.calls) == 1  # 許可の付け忘れはリトライで直らない


async def test_a_denial_fails_even_when_the_result_parses() -> None:
    """拒否があったら、出力がスキーマに合っていても成功にしない。

    ⚠️ 検索していない収集結果＝モデルの記憶からの推測を後段へ流さないため。
    """
    client, _, _ = build_client([stdout_of(denial_envelope(result=ANSWER_JSON))])

    with pytest.raises(AIResponseError):
        await client.complete(prompt="q", output_schema=Answer)


async def test_an_empty_denial_list_is_still_a_success() -> None:
    """拒否が**無い**ことは成功の妨げにならない（実測の成功封筒には空配列が付く）。"""
    client, _, _ = build_client([stdout_of(envelope(permission_denials=[]))])

    result = await client.complete(prompt="q", output_schema=Answer)

    assert result.value.score == 7


@pytest.mark.parametrize(
    "denials",
    [
        [{"tool_use_id": "toolu_01", "tool_input": {"query": "q"}}],  # 名前が無い
        ["WebSearch"],  # 要素が dict ですらない
        [{"tool_name": "WebSearch"}, {"tool_name": "WebFetch"}],  # 複数
    ],
)
async def test_a_denial_of_an_unexpected_shape_still_fails(denials: list[Any]) -> None:
    """⚠️ 要素の形が想定と違っても「拒否が無かった」に読み替えない。"""
    client, _, _ = build_client(
        [stdout_of(denial_envelope(permission_denials=denials))]
    )

    with pytest.raises(AIResponseError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert f"permission_denials={len(denials)}件" in " ".join(caught.value.reasons)


async def test_a_refusal_stop_reason_is_not_swallowed() -> None:
    """T-15 備考「`stop_reason` が `refusal` の場合を握り潰さない」。"""
    client, _, _ = build_client([stdout_of(envelope(stop_reason="refusal"))])

    with pytest.raises(AIResponseError, match="refusal"):
        await client.complete(prompt="q", output_schema=Answer)


@pytest.mark.parametrize("missing", ["is_error", "subtype"])
async def test_a_missing_success_field_raises_a_protocol_error(missing: str) -> None:
    """「失敗と言っていない」を「成功」と読み替えない（CLI のバージョン差の検出）。"""
    client, _, _ = build_client([stdout_of(envelope(**{missing: _ABSENT}))])

    with pytest.raises(AIProtocolError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert missing in str(caught.value)


async def test_an_envelope_without_a_result_raises_a_protocol_error() -> None:
    client, _, _ = build_client([stdout_of(envelope(None))])

    with pytest.raises(AIProtocolError, match="result"):
        await client.complete(prompt="q", output_schema=Answer)


async def test_the_command_being_absent_surfaces_as_unavailable() -> None:
    """PATH に無い＝再実行では直らない。呼び出し元が他の失敗と区別できること。"""
    client, _, _ = build_client(
        [AIUnavailableError("claude が見つかりません", command="claude")]
    )

    with pytest.raises(AIUnavailableError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert caught.value.command == "claude"


async def test_a_timeout_surfaces_as_a_timeout_error() -> None:
    client, runner, _ = build_client(
        [AITimeoutError("時間切れ", timeout_seconds=600.0)]
    )

    with pytest.raises(AITimeoutError) as caught:
        await client.complete(prompt="q", output_schema=Answer)

    assert caught.value.timeout_seconds == pytest.approx(600.0)
    assert len(runner.calls) == 1  # タイムアウトもリトライしない


# --- 既定のランナー（`claude` ではなく python を起動して確かめる）---------


def _python_command(script: str) -> list[str]:
    """⚠️ ここでも `claude` は起動しない（CI に CLI を要求しない）。"""
    return [sys.executable, "-c", script]


async def test_the_runner_reports_the_exit_code_and_both_streams() -> None:
    result = await SubprocessCommandRunner().run(
        _python_command(
            "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(3)"
        ),
        env={"PATH": os.environ.get("PATH", "")},
        timeout=60,
    )

    assert result.exit_code == 3
    assert result.stdout == "out"
    assert result.stderr == "err"


async def test_the_runner_raises_unavailable_when_the_command_is_missing() -> None:
    with pytest.raises(AIUnavailableError) as caught:
        await SubprocessCommandRunner().run(
            ["claude-does-not-exist-in-this-environment"],
            env={"PATH": os.environ.get("PATH", "")},
            timeout=60,
        )

    assert caught.value.command == "claude-does-not-exist-in-this-environment"
    assert "ログイン" in str(caught.value)


async def test_the_runner_stops_a_process_that_overruns_the_timeout() -> None:
    with pytest.raises(AITimeoutError) as caught:
        await SubprocessCommandRunner().run(
            _python_command("import time; time.sleep(30)"),
            env={"PATH": os.environ.get("PATH", "")},
            timeout=0.2,
        )

    assert caught.value.timeout_seconds == pytest.approx(0.2)


async def test_the_runner_passes_only_the_given_environment() -> None:
    result = await SubprocessCommandRunner().run(
        _python_command("import os; print(os.environ.get('MARKER', 'none'))"),
        env={"PATH": os.environ.get("PATH", ""), "MARKER": "passed"},
        timeout=60,
    )

    assert result.stdout.strip() == "passed"
