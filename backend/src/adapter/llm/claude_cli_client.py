"""Claude Code CLI（`claude -p`）による `AIClient` 実装（TASKS.md T-15）。

⚠️ **これは試作段階の手段であり、本番の想定ではない。** CLI がインストールされ、
**ログイン済みの PC が起動していること**が前提なので、本番（将来の AWS 展開時）の
無人運用には持ち込めない。本番では Anthropic API 実装を追加してここを差し替える
（切り替えのトリガーは AWS 展開、または無人での定期実行が要件になった時点。
TASKS.md §1.1「備考：AI呼び出し方式」／`ai_client.py` 冒頭）。

**APIキーは使わない。** 認証は会社の Team 契約でログイン済みの CLI セッション。
そのため子プロセスの環境から `ANTHROPIC_API_KEY` を**取り除いて**渡す
（`Settings.anthropic_api_key` は本番の API 実装用に残してあるので、環境に値が
入っていることがある。渡すと課金経路が黙って API 側へ移る）。

---

**手元での実測（claude 2.1.232 / Team 契約ログイン済み / macOS。2026-08-14）**

`claude -p "<プロンプト>" --output-format json` は成功時に終了コード 0 を返し、
標準出力に**単一の JSON オブジェクト（封筒）**を出す。主なフィールドは
`result`（応答本文が**文字列で**入る）/ `is_error` / `subtype`（`"success"`）/
`stop_reason` / `api_error_status` / `total_cost_usd` / `modelUsage` /
`session_id` / `duration_ms`。既定モデルは `claude-opus-5` が使われた（`modelUsage`）。

したがって **パースは2段階**になる:

1. 標準出力 → 封筒（`ClaudeCliEnvelope`）
2. 封筒の `result` 文字列 → 目的の Pydantic スキーマ

⚠️ **些細なプロンプト（`1+1`）でも `duration_ms=131497`（約131秒）かかった。**
CLI の起動・初期化のオーバーヘッドが大きい。**タイムアウトの既定を短くしないこと**
（短い既定は、本番相当の実行を途中で殺す形で現れる）。

**エラー時の出力形式・終了コードは未実測。** そのため「終了コード0＝成功」とは
見なさず、封筒の `is_error` / `subtype` / `api_error_status` も確認し、矛盾があれば
例外にする。読めない出力を推測で補わない（`_parse_envelope` / `_ensure_success`）。

**構造化出力は「指示＋検証＋リトライ」で担保する。** CLI に API の structured
outputs 相当（`output_config.format`）があるかは未確認なので、JSON Schema を添えて
「JSON だけを出せ」と指示し、`result` を Pydantic で検証し、失敗したらパースエラーの
内容をプロンプトへ載せて再依頼する。**リトライするのはスキーマ不一致だけ**
（プロセス失敗・タイムアウトは1回あたり数分かかるうえ、再実行で直るかを判別する
材料が無い。ジョブ単位の再実行＝呼び出し元の判断に委ねる）。

---

**追加の実測（2026-08-14。同じ CLI・同じ環境）**

⚠️ **ツールの許可が無いと、封筒は「成功」のまま中身が拒否記録になる。**
`--allowedTools` を付けずに web 検索の要る指示を出すと、`is_error=false` /
`subtype="success"` / `api_error_status=null` のまま **`permission_denials` 配列に
拒否記録が入り**、`result` には「権限がないので実行できない」旨の日本語文が入った。
つまり **`is_error` / `subtype` / `api_error_status` / `stop_reason` の4つだけでは
すり抜ける**。`_ensure_success()` は `permission_denials` が空でないことも失敗として
扱う（拒否されたツール名を `reasons` に載せる）。⚠️ **この検査を外さないこと。**
外すと「検索していない収集結果」＝モデルの記憶からの推測が、成功として後段へ流れる。

⚠️ **「JSON のみ出力」と指示しても、`result` が ```json のコードフェンスで包まれ、
後ろに説明文や `Sources:` が付くことがある**（実測で発生）。そのため `result` は
そのまま Pydantic へ渡さず、**最初の完全な JSON 値だけを取り出してから**検証する
（`_extract_json_payload()`）。取り出せなければ従来どおりリトライ経路へ落ちる。

**web 検索は `--allowedTools "WebSearch"` で有効化できる**（→ `extra_args`。
プロトコルには出さない）。**実施されたかどうかは封筒トップの `server_tool_use`
ではなく `modelUsage[].webSearchRequests` に出る。**
"""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from adapter.llm.ai_client import (
    AICallMeta,
    AIOutputParseError,
    AIProcessError,
    AIProtocolError,
    AIResponseError,
    AIResult,
    AITimeoutError,
    AIUnavailableError,
    OutputSchema,
    resolve_output_adapter,
    summarize_stream,
)
from config import Settings, get_settings
from enterprise.entities.json_document import (
    DocumentParseError,
    parse_json_document,
)

logger = logging.getLogger(__name__)

ENCODING = "utf-8"

# 実測した呼び出し方（`--output-format json` で封筒が出る）。
OUTPUT_FORMAT = "json"
PROMPT_FLAG = "-p"
OUTPUT_FORMAT_FLAG = "--output-format"
MODEL_FLAG = "--model"

# 封筒が成功を表す `subtype`。
SUCCESS_SUBTYPE = "success"

# モデルが応答を拒否したときの `stop_reason`（API の契約由来。CLI 経由では未実測）。
# ⚠️ **握り潰さない**（T-15 備考「`stop_reason` が `refusal` の場合を握り潰さない」）。
REFUSAL_STOP_REASON = "refusal"

# 子プロセスへ渡さない環境変数。**Team 契約のログインセッションで呼ぶ**ため。
API_KEY_ENV = "ANTHROPIC_API_KEY"

# 拒否記録（`permission_denials[]`）からツール名を取るキー。実測ではこの名前だった。
# ⚠️ **要素の形に依存しないこと。** 取れなければ要素そのものを載せる（拒否があった
# 事実の方が重要で、名前が読めないことを理由に成功へ倒さない）。
PERMISSION_DENIAL_TOOL_KEY = "tool_name"

# 拒否記録の形が想定と違ったときに例外メッセージへ載せる長さの上限
# （`tool_input` に長い本文が入りうるので、原因が読める範囲だけ載せる）。
DENIAL_LABEL_LIMIT = 200

# `result` から JSON を取り出すときに試す開始位置の上限（`_extract_json_payload`）。
# 途中で切れた巨大な出力に対して「開始位置 × 末尾まで走査」の総当たりになるのを
# 防ぐだけの安全弁。実際の出力は先頭かフェンス直後から始まるので、通常は1回で済む。
JSON_SCAN_LIMIT = 64

# 実施された web 検索の回数が入るキー（`modelUsage[<model>]` の中）。
# ⚠️ **封筒トップの `server_tool_use` ではなくここに出る**（2026-08-14 実測）。
WEB_SEARCH_REQUESTS_KEY = "webSearchRequests"

# JSON 値の開始文字。⚠️ オブジェクトと配列だけを見る（`raw_articles.json` は
# トップレベルが array。設計書 §2.3）。裸の数値・文字列は本文中の数字を拾うので見ない。
JSON_VALUE_STARTS = "{["

# リトライ時にプロンプトへ載せる「前回の出力」の上限。プロンプトは argv で渡すので
# 際限なく膨らませない（下の ⚠️ ARG_MAX 参照）。
RETRY_PAYLOAD_LIMIT = 2000

# 出力スキーマの名前が取れないときのラベル（例外メッセージ用）。
DEFAULT_SCHEMA_LABEL = "AI 出力"

OUTPUT_INSTRUCTIONS = """
---
出力形式（厳守）:

- 次の JSON Schema に一致する **JSON だけ**を出力する。
- 説明文・前置き・後書き・見出しを付けない。コードフェンス（```）で囲まない。
- スキーマに無いキーを足さない。値を推測で埋めず、分からない項目はスキーマの
  定めどおりに扱う。

JSON Schema:
{schema}
"""

RETRY_INSTRUCTIONS = """
---
⚠️ 直前の試行（{attempt} 回目）の出力は上のスキーマに一致しなかった。
指摘は次のとおり（すべて直すこと）:

{issues}

前回出力（そのまま。長い場合は先頭のみ）:
{payload}

指摘を修正し、**JSON だけ**を出力し直すこと。
"""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """サブプロセスの実行結果（終了コードと両ストリーム）。"""

    exit_code: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """サブプロセスの実行だけを担う差し替え口。

    **テストが実際に `claude` を起動しないための境目**でもある（CI に CLI と
    ログインを要求しない。T-15 完了条件）。ここが「起動できたか・終了コードは
    いくつか」までを扱い、出力の解釈は `ClaudeCliClient` が行う。
    """

    async def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: float | None,
    ) -> CommandResult:
        """コマンドを実行して結果を返す。

        Raises:
            AIUnavailableError: コマンドが見つからない／実行できない
            AITimeoutError: 制限時間を超えた（プロセスは kill する）
        """
        ...


class SubprocessCommandRunner:
    """`asyncio` のサブプロセスで実行する既定の実装。

    **同期の `subprocess.run` は使わない。** 1回の呼び出しが数分〜30分に及ぶため
    （実測で些細なプロンプトでも約131秒）、イベントループを塞ぐと API サーバーが
    その間応答できなくなる。
    """

    async def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: float | None,
    ) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                # ⚠️ 標準入力は閉じる。プロンプトは argv で渡しているので入力は
                # 不要で、親の端末を継いだままにすると入力待ちで固まりうる。
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(env),
            )
        except FileNotFoundError as exc:
            raise AIUnavailableError(
                f"コマンド {argv[0]!r} が見つかりません。Claude Code CLI を"
                "インストールし、ログイン済みの環境で実行してください"
                "（TASKS.md §1.1「AI呼び出し方式」）",
                command=argv[0],
            ) from exc
        except PermissionError as exc:
            raise AIUnavailableError(
                f"コマンド {argv[0]!r} を実行できません（実行権限を確認してください）",
                command=argv[0],
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError as exc:
            # ⚠️ kill したら必ず wait する。回収しないとゾンビが残り、
            # 30分級のジョブを繰り返す運用でプロセステーブルを食い潰す。
            process.kill()
            await process.wait()
            raise AITimeoutError(
                f"{argv[0]!r} が {timeout} 秒以内に終わりませんでした"
                "（プロセスは停止済み）",
                timeout_seconds=timeout if timeout is not None else 0.0,
            ) from exc

        return CommandResult(
            exit_code=-1 if process.returncode is None else process.returncode,
            stdout=stdout.decode(ENCODING, errors="replace"),
            stderr=stderr.decode(ENCODING, errors="replace"),
        )


class ClaudeCliEnvelope(BaseModel):
    """`--output-format json` が標準出力へ出す封筒（2026-08-14 実測）。

    ⚠️ **`extra="ignore"`。** CLI のバージョンが上がってフィールドが増えても
    落ちないようにする（成果物スキーマの `extra="forbid"` とは狙いが違う。
    こちらは自分が定義したファイルではなく、外部ツールの出力）。

    ⚠️ **成功判定に使うフィールドを `| None` にしてある**のは、値が無いことを
    「成功」と解釈させないため。判定は `_ensure_success()` が明示的に行う。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    result: str | None = None
    """応答本文。**文字列で入る**（ここをさらにパースする＝2段階）。"""

    is_error: bool | None = None
    subtype: str | None = None
    stop_reason: str | None = None

    api_error_status: Any = None
    """API 側のエラー状態。⚠️ **型を決めつけない**（未実測。数値か文字列か不明）。"""

    permission_denials: list[Any] | None = None
    """許可されていないツールを使おうとして拒否された記録（2026-08-14 実測）。

    ⚠️ **ここが非空でも封筒は `is_error=false` / `subtype="success"` のまま**。
    要素の形は `{"tool_name": ..., ...}` だったが、**形に依存しない**（`list[Any]`）。
    判定に使うのは「空でないこと」で、名前は診断のために best-effort で取る。
    """

    total_cost_usd: float | None = None
    duration_ms: int | None = None
    session_id: str | None = None

    usage_by_model: dict[str, Any] | None = Field(default=None, alias="modelUsage")
    """モデルごとの使用量。キーが**実際に使われたモデル名**。

    フィールド名を `model_usage` にしないのは、Pydantic の保護名前空間
    （`model_`）と衝突するため。JSON 側の名前は alias で受ける。
    """

    @property
    def models_used(self) -> tuple[str, ...]:
        """実際に使われたモデル名。取れなければ空タプル。"""
        return tuple(self.usage_by_model or ())

    @property
    def web_search_requests(self) -> int | None:
        """実施された web 検索の回数（`modelUsage[].webSearchRequests` の合計）。

        ⚠️ **報告が無いときは `None` を返す（`0` にしない）。** 「検索していない」と
        「回数が分からない」は別の事実で、crawl（T-16）はどちらも失敗として扱うが、
        原因が違えば対処も違う（前者は許可・プロンプト、後者は CLI の版差）。
        """
        total = 0
        reported = False
        for entry in (self.usage_by_model or {}).values():
            if not isinstance(entry, Mapping):
                continue
            value = entry.get(WEB_SEARCH_REQUESTS_KEY)
            # ⚠️ `bool` は `int` の派生。`true` を1回と数えない。
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            reported = True
            total += value
        return total if reported else None

    @property
    def denied_tools(self) -> tuple[str, ...]:
        """拒否されたツールの名前（診断用）。拒否が無ければ空タプル。

        ⚠️ **名前が読めないことを「拒否が無かった」に読み替えない。** 形が想定と
        違う要素は、その内容をそのまま載せて1件として数える。
        """
        labels: list[str] = []
        for denial in self.permission_denials or ():
            name = (
                denial.get(PERMISSION_DENIAL_TOOL_KEY)
                if isinstance(denial, Mapping)
                else None
            )
            labels.append(
                str(name)
                if name is not None
                else summarize_stream(str(denial), limit=DENIAL_LABEL_LIMIT)
            )
        return tuple(labels)


class ClaudeCliClient:
    """`claude -p` をサブプロセスとして呼ぶ `AIClient` 実装。"""

    def __init__(
        self,
        *,
        command: str = "claude",
        model: str = "claude-opus-5",
        default_timeout_seconds: float = 600.0,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 2.0,
        extra_args: Sequence[str] = (),
        runner: CommandRunner | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """
        Args:
            command: 実行するコマンド（PATH 上の名前または絶対パス）
            model: `--model` へ渡す値（`Settings.anthropic_model`）
            default_timeout_seconds: `complete(timeout=None)` のときの制限時間
            max_attempts: **スキーマ不一致時**に呼ぶ回数の上限（1 ならリトライ無し）
            retry_backoff_seconds: リトライ前の待ち時間の基準（指数で伸ばす）
            extra_args: CLI へ足す引数。⚠️ **プロトコルに漏らさない**ための逃げ道で、
                T-16 が web 検索の許可等を足す場合はここを DI で設定する
            runner: サブプロセスの実行方法（テストで差し替える）
            sleep: バックオフの待ち方（テストで差し替える）
        """
        if max_attempts < 1:
            raise ValueError(f"max_attempts は1以上が必要です: {max_attempts}")
        self._command = command
        self._model = model
        self._default_timeout_seconds = default_timeout_seconds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._extra_args = tuple(extra_args)
        self._runner = runner or SubprocessCommandRunner()
        self._sleep = sleep or asyncio.sleep

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        extra_args: Sequence[str] = (),
        runner: CommandRunner | None = None,
    ) -> "ClaudeCliClient":
        """
        Args:
            settings: 実行設定。既定は `get_settings()`
            extra_args: CLI へ足す引数（web 検索の許可など）。⚠️ **上位が組み立てる
                ものではない**。呼ぶのは `adapter.llm.get_ai_client()` だけ
            runner: サブプロセスの実行方法（テストで差し替える）
        """
        settings = settings or get_settings()
        return cls(
            command=settings.ai_cli_command,
            model=settings.anthropic_model,
            default_timeout_seconds=float(settings.ai_timeout_seconds),
            max_attempts=settings.ai_max_attempts,
            retry_backoff_seconds=settings.ai_retry_backoff_seconds,
            extra_args=extra_args,
            runner=runner,
        )

    async def complete[T](
        self,
        *,
        prompt: str,
        output_schema: OutputSchema[T],
        prompt_version: str | None = None,
        timeout: float | None = None,
    ) -> AIResult[T]:
        """`AIClient.complete()` の CLI 実装（プロトコル側の docstring を参照）。"""
        adapter = resolve_output_adapter(output_schema)
        json_schema = adapter.json_schema()
        label = _schema_label(output_schema, json_schema)
        instructions = OUTPUT_INSTRUCTIONS.format(
            schema=json.dumps(json_schema, ensure_ascii=False, indent=2)
        )
        timeout_seconds = (
            self._default_timeout_seconds if timeout is None else float(timeout)
        )

        retry_note = ""
        for attempt in range(1, self._max_attempts + 1):
            envelope = await self._invoke(
                f"{prompt}\n{instructions}{retry_note}",
                timeout_seconds=timeout_seconds,
                prompt_version=prompt_version,
                attempt=attempt,
            )
            payload = _extract_json_payload(_require_payload(envelope))
            try:
                value = parse_json_document(adapter, payload, label=label)
            except DocumentParseError as exc:
                logger.warning(
                    "ai output did not match the schema (attempt %d/%d, schema=%s): %s",
                    attempt,
                    self._max_attempts,
                    label,
                    exc,
                )
                if attempt == self._max_attempts:
                    raise AIOutputParseError(
                        f"{label} を {attempt} 回試しても得られませんでした — {exc}",
                        attempts=attempt,
                        issues=exc.issues,
                        payload=summarize_stream(payload),
                    ) from exc
                # ⚠️ 試行ごとに CLI のセッションは別（会話が続いていない）。
                # そのため元のプロンプトとスキーマを毎回まるごと送り直し、
                # 指摘だけを追記する。
                retry_note = _retry_note(exc, payload, attempt=attempt)
                await self._sleep(self._backoff_for(attempt))
                continue

            return AIResult(
                value=value,
                meta=AICallMeta(
                    requested_model=self._model,
                    models_used=envelope.models_used,
                    prompt_version=prompt_version,
                    attempts=attempt,
                    duration_ms=envelope.duration_ms,
                    total_cost_usd=envelope.total_cost_usd,
                    session_id=envelope.session_id,
                    web_search_requests=envelope.web_search_requests,
                ),
            )

        # `max_attempts >= 1` と上の raise により到達しない。
        raise AssertionError("リトライループを抜けました")  # pragma: no cover

    def _backoff_for(self, attempt: int) -> float:
        return self._retry_backoff_seconds * (2 ** (attempt - 1))

    def _argv(self, prompt: str) -> list[str]:
        """実測した呼び出し方をそのまま組み立てる。

        ⚠️ **プロンプトは argv で渡している**（実測がこの形）。OS の引数長上限
        （macOS は合計 1MB 程度）があるため、プロンプトが際限なく育つ場合は
        標準入力経由へ変える必要がある。**その形は未実測なので今は採らない。**
        """
        return [
            self._command,
            PROMPT_FLAG,
            prompt,
            OUTPUT_FORMAT_FLAG,
            OUTPUT_FORMAT,
            MODEL_FLAG,
            self._model,
            *self._extra_args,
        ]

    def _child_env(self) -> dict[str, str]:
        """子プロセスへ渡す環境。**APIキーは渡さない。**

        認証は Team 契約のログイン済みセッション（TASKS.md §1.1）。環境に
        `ANTHROPIC_API_KEY` が入っていると課金経路が黙って API 側へ移るので、
        ここで取り除く。他の環境変数（`HOME` 等）はログインセッションの解決に
        必要なので、そのまま引き継ぐ。
        """
        env = dict(os.environ)
        env.pop(API_KEY_ENV, None)
        return env

    async def _invoke(
        self,
        prompt: str,
        *,
        timeout_seconds: float,
        prompt_version: str | None,
        attempt: int,
    ) -> ClaudeCliEnvelope:
        logger.info(
            "calling claude cli (model=%s, prompt_version=%s, attempt=%d, "
            "prompt_chars=%d, timeout=%.0fs)",
            self._model,
            prompt_version,
            attempt,
            len(prompt),
            timeout_seconds,
        )
        result = await self._runner.run(
            self._argv(prompt), env=self._child_env(), timeout=timeout_seconds
        )
        if result.exit_code != 0:
            # ⚠️ 封筒が「成功」と言っていても、終了コードが非0ならここで落とす
            # （どちらを信じるかを決められないので安全側＝失敗に倒す）。
            raise AIProcessError(
                f"{self._command} が終了コード {result.exit_code} で終了しました"
                f" — stderr: {summarize_stream(result.stderr)}",
                exit_code=result.exit_code,
                stderr=summarize_stream(result.stderr),
            )

        envelope = _parse_envelope(result.stdout, stderr=result.stderr)
        _ensure_success(envelope, stderr=result.stderr)
        logger.info(
            "claude cli returned (models_used=%s, duration_ms=%s, cost_usd=%s)",
            ",".join(envelope.models_used) or "unknown",
            envelope.duration_ms,
            envelope.total_cost_usd,
        )
        return envelope


def _parse_envelope(stdout: str, *, stderr: str) -> ClaudeCliEnvelope:
    """標準出力を封筒として読む（パースの1段目）。

    Raises:
        AIProtocolError: JSON として読めない／オブジェクトでない／型が合わない
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AIProtocolError(
            "標準出力を JSON（--output-format json の封筒）として読めません "
            f"— {exc.msg} (line {exc.lineno} column {exc.colno})"
            f" / stdout: {summarize_stream(stdout)}"
            f" / stderr: {summarize_stream(stderr)}",
            stdout=summarize_stream(stdout),
            stderr=summarize_stream(stderr),
        ) from exc

    if not isinstance(data, dict):
        raise AIProtocolError(
            "封筒が JSON オブジェクトではありません "
            f"(型: {type(data).__name__}) / stdout: {summarize_stream(stdout)}",
            stdout=summarize_stream(stdout),
            stderr=summarize_stream(stderr),
        )

    try:
        return ClaudeCliEnvelope.model_validate(data)
    except ValidationError as exc:
        raise AIProtocolError(
            f"封筒のフィールドが想定と違います — {exc}"
            f" / stdout: {summarize_stream(stdout)}",
            stdout=summarize_stream(stdout),
            stderr=summarize_stream(stderr),
        ) from exc


def _ensure_success(envelope: ClaudeCliEnvelope, *, stderr: str) -> None:
    """封筒が成功を申告しているかを確認する（終了コードだけに頼らない）。

    Raises:
        AIResponseError: 封筒が失敗を申告している
        AIProtocolError: 成功と判断するためのフィールドが無い（＝確認できない）
    """
    reasons: list[str] = []
    if envelope.is_error:
        reasons.append("is_error=true")
    if envelope.api_error_status is not None:
        reasons.append(f"api_error_status={envelope.api_error_status!r}")
    if envelope.subtype is not None and envelope.subtype != SUCCESS_SUBTYPE:
        reasons.append(f"subtype={envelope.subtype!r}（{SUCCESS_SUBTYPE!r} ではない）")
    if envelope.stop_reason == REFUSAL_STOP_REASON:
        reasons.append("stop_reason='refusal'（モデルが応答を拒否した）")
    if denied := envelope.denied_tools:
        # ⚠️ **実測でここだけが失敗を示していた。** 封筒は成功を申告したまま、
        # `result` には「権限がないので実行できない」旨の文が入っていた。
        # 許可の付け忘れ（`extra_args` の `--allowedTools`）はここで気づく。
        reasons.append(
            f"permission_denials={len(denied)}件（拒否されたツール: "
            + ", ".join(denied)
            + "）。CLI へツールの許可を渡していない疑い"
        )

    if reasons:
        raise AIResponseError(
            "終了コードは0でしたが、この応答は成功として扱えません — "
            + " / ".join(reasons)
            + f" / stderr: {summarize_stream(stderr)}",
            reasons=reasons,
            stderr=summarize_stream(stderr),
        )

    # ⚠️ 「失敗と言っていない」と「成功と言っている」は別。実測した封筒には
    # 両方あるので、無ければ CLI の仕様が変わったと見なして落とす。
    checked = (("is_error", envelope.is_error), ("subtype", envelope.subtype))
    missing = [name for name, value in checked if value is None]
    if missing:
        raise AIProtocolError(
            "封筒に成功判定のためのフィールドがありません: "
            + ", ".join(missing)
            + "（CLI のバージョン差の疑い。2026-08-14 時点の実測では両方存在した）",
            stdout="",
            stderr=summarize_stream(stderr),
        )


def _require_payload(envelope: ClaudeCliEnvelope) -> str:
    """封筒から応答本文を取り出す（パースの2段目の入力）。"""
    if envelope.result is None:
        raise AIProtocolError(
            "封筒に result（応答本文）がありません"
            f"（subtype={envelope.subtype!r} / is_error={envelope.is_error!r}）",
            stdout="",
            stderr="",
        )
    return envelope.result


def _extract_json_payload(payload: str) -> str:
    """`result` から**最初の完全な JSON 値**（オブジェクトか配列）を取り出す。

    プロンプトで「JSON だけ・フェンスで囲まない」と指示してはいるが、**実測では
    ```json のフェンスで包まれ、後ろに説明文と `Sources:` が付いて返った**。
    1回の呼び出しが数分〜30分かかるので、この程度の逸脱はここで吸収して
    リトライを節約する（フェンスの除去も、この走査が兼ねる）。

    ⚠️ **取り出せなければ元の文字列をそのまま返す**（＝従来どおりスキーマ検証で
    失敗し、指摘つきのリトライ経路へ落ちる）。ここで「それらしい断片」を継ぎ接ぎ
    して通すと、後段が黙って別の記事集合を扱うことになる。

    ⚠️ **取るのは「最初の」完全な値**。説明文の中に例示の JSON が先に出てくると
    そちらを拾う。拾った値がスキーマに合わなければリトライで気づける形にしてある。
    """
    stripped = payload.strip()
    decoder = json.JSONDecoder()
    attempts = 0
    for index, char in enumerate(stripped):
        if char not in JSON_VALUE_STARTS:
            continue
        attempts += 1
        if attempts > JSON_SCAN_LIMIT:
            break
        try:
            _, end = decoder.raw_decode(stripped, index)
        except ValueError:
            continue
        return stripped[index:end]
    return stripped


def _retry_note(exc: DocumentParseError, payload: str, *, attempt: int) -> str:
    issues = "\n".join(f"- {issue.path}: {issue.reason}" for issue in exc.issues)
    return RETRY_INSTRUCTIONS.format(
        attempt=attempt,
        issues=issues,
        payload=summarize_stream(payload, limit=RETRY_PAYLOAD_LIMIT),
    )


def _schema_label(output_schema: OutputSchema[Any], json_schema: dict[str, Any]) -> str:
    """例外メッセージに出す出力スキーマの名前。"""
    name = getattr(output_schema, "__name__", None)
    if isinstance(name, str):
        return name
    title = json_schema.get("title")
    if isinstance(title, str):
        return title
    return DEFAULT_SCHEMA_LABEL
