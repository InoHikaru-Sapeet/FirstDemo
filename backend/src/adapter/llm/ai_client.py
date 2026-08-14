"""AIクライアント層の差し替え口（設計書 §6・§9 ／ TASKS.md T-15）。

上位（T-16 crawl / T-19 分類・採点）が AI を呼ぶ経路をここ1箇所に閉じ込める。
上位が渡すのは **プロンプトと出力スキーマだけ** で、呼び出し先が Claude Code CLI か
Anthropic API かを知らない。

差し替え方は1箇所だけ:

    # src/adapter/llm/__init__.py
    def get_ai_client() -> AIClient:
        return ClaudeCliClient.from_settings(get_settings())   # ← ここを差し替える

`AuthenticationBackend` の `get_authentication_backend()`（T-08）、`ArtifactStore`
（ローカル → S3）と同じ扱い方をする。

---

⚠️ **現行の実装（`ClaudeCliClient`）は試作段階の手段であり、本番の想定ではない。**

`claude -p` は **CLI がインストールされ、ログイン済みの PC が起動していること**が
前提で、本番（将来の AWS 展開時）の無人運用には向かない。切り替えのトリガーは
AWS への展開、または無人での定期実行が要件になった時点
（TASKS.md §1.1「備考：AI呼び出し方式」）。

**この層があること＝API 実装済み、ではない。** 差し替えるときは
`anthropic` 依存（`pyproject.toml`）と `Settings.anthropic_api_key` をそのまま使う
（どちらも本番切り替え用に**残してある**）。T-15 備考の「API 実装を書くときの注意」
（structured outputs は `output_config.format` / 長時間はストリーミング /
`stop_reason` が `refusal` を握り潰さない / `usage` を返す）を再確認すること。

---

**例外の設計**：失敗の原因を呼び出し元が判別できるように分けてある。
「AI が呼べなかった」（環境の問題＝人が直す）と「AI の出力が使えなかった」
（プロンプト・スキーマの問題）を混ぜると、ジョブの再実行判断ができない。

| 例外 | いつ |
|---|---|
| `AIUnavailableError` | コマンドが PATH に無い／実行できない（環境の是正が必要） |
| `AITimeoutError` | 制限時間内に終わらなかった（プロセスは停止済み） |
| `AIProcessError` | 終了コードが非0（未ログインもここに出る想定。stderr を見る） |
| `AIProtocolError` | 標準出力が想定の封筒として読めない（CLI の版差の疑い） |
| `AIResponseError` | 封筒自身が失敗を申告している（一時障害なら再実行） |
| `AIOutputParseError` | 出力がスキーマに合わない（上限まで再試行した後） |
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import TypeAdapter

from enterprise.entities.json_document import DocumentIssue

# 例外メッセージへ載せる標準出力・標準エラー出力の上限。CLI は長いログを吐くことが
# あるので、原因が読める範囲だけ載せて残りは省略する（ログを溢れさせない）。
DIAGNOSTIC_LIMIT = 2000

EMPTY_STREAM = "(空)"


def summarize_stream(text: str, *, limit: int = DIAGNOSTIC_LIMIT) -> str:
    """例外メッセージへ載せる形へ整える。

    ⚠️ **省略したことを明示する。** 黙って切り詰めると、載っている内容が全部だと
    誤解して原因を取り違える。
    """
    stripped = text.strip()
    if not stripped:
        return EMPTY_STREAM
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[:limit]}…(以下 {len(stripped) - limit} 文字省略)"


class AIClientError(Exception):
    """AI 呼び出しの失敗の基底。

    上位がまとめて捕まえたい場合（ジョブを失敗として記録する等）はこれを使い、
    原因で分岐したい場合は下のサブクラスを見る。**握り潰さないこと。**
    """


class AIUnavailableError(AIClientError):
    """AI を呼ぶ前提が満たされていない。

    現行の CLI 実装では `claude` が PATH に無い／実行権限が無い場合。
    **再実行では直らない**（実行環境の是正が必要）ので、リトライ対象にしない。

    ⚠️ **「未ログイン」はここに来ない。** ログイン状態は起動して初めて分かるので、
    終了コード非0（`AIProcessError`）か封筒のエラー（`AIResponseError`）として
    現れる（2026-08-14 時点では未実測。標準エラー出力を例外メッセージに載せてある）。
    """

    def __init__(self, message: str, *, command: str) -> None:
        self.command = command
        super().__init__(message)


class AITimeoutError(AIClientError):
    """制限時間内に終わらなかった（プロセスは kill 済み）。

    ⚠️ 既定のタイムアウトは長い。些細なプロンプトでも CLI の起動・初期化に
    約131秒かかることを実測している（TASKS.md T-15 備考）。**短くしないこと。**
    """

    def __init__(self, message: str, *, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(message)


class AIProcessError(AIClientError):
    """終了コードが非0だった。

    Attributes:
        exit_code: プロセスの終了コード
        stderr: 標準エラー出力（切り詰め済み）。原因はここに出る
    """

    def __init__(self, message: str, *, exit_code: int, stderr: str) -> None:
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(message)


class AIProtocolError(AIClientError):
    """応答の**形**が想定と違う（封筒として読めない・必要なフィールドが無い）。

    CLI のバージョン差でここへ落ちる可能性がある。`AIResponseError`（AI 側が
    失敗を申告した）とは対処が別なので分けてある。
    """

    def __init__(self, message: str, *, stdout: str, stderr: str) -> None:
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(message)


class AIResponseError(AIClientError):
    """応答を成功として扱えない（`is_error` / `subtype` / `api_error_status` ほか）。

    ⚠️ **終了コード0でもここへ落ちる。** 成功判定を終了コードだけに頼らないため
    （実測では成功時に 0 が返るが、失敗時の終了コードは未実測）。

    ⚠️ **封筒が「成功」を申告していてもここへ落ちることがある。** 実測では、ツールの
    許可が無いとき `is_error=false` / `subtype="success"` のまま `permission_denials`
    に拒否記録が入った（`claude_cli_client` 冒頭）。「失敗と言っていない」を
    「成功」と読み替えない。

    Attributes:
        reasons: 失敗と判断した根拠（`is_error=true`・拒否されたツール名 等）。
            全部載せる
    """

    def __init__(self, message: str, *, reasons: Sequence[str], stderr: str) -> None:
        self.reasons = tuple(reasons)
        self.stderr = stderr
        super().__init__(message)


class AIOutputParseError(AIClientError):
    """応答本文が出力スキーマに合わない（リトライ上限まで試した後）。

    Attributes:
        attempts: 実際に呼んだ回数
        issues: 最後の試行での違反（どのパスがなぜダメか。全件）
        payload: 最後の応答本文（切り詰め済み）
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        issues: Sequence[DocumentIssue],
        payload: str,
    ) -> None:
        self.attempts = attempts
        self.issues = tuple(issues)
        self.payload = payload
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AICallMeta:
    """1回の AI 呼び出しの出自。

    **監査ログ／validation メタに載せられる形**にしておく（設計書 §9.2 ＝
    「実行時に使用した `prompt_version` と `config.revision` を記録し再現性を確保」。
    `config.revision` は呼び出し元が持っているので、ここには含めない）。

    Attributes:
        requested_model: `--model` に渡した値（`Settings.anthropic_model`）
        models_used: **実際に使われた**モデル名。CLI の封筒 `modelUsage` のキー。
            空タプル＝取得できなかった。⚠️ 取得できないときに
            `requested_model` で埋めない（「指定した」と「使われた」は別の事実）
        prompt_version: 呼び出し元が渡したプロンプト版（T-30）。未指定なら None
        attempts: CLI を呼んだ回数（スキーマ不一致のリトライを含む）
        duration_ms: 成功した試行の所要時間（封筒 `duration_ms`）
        total_cost_usd: 成功した試行の費用（封筒 `total_cost_usd`）
        session_id: 成功した試行のセッションID（封筒 `session_id`。調査用）
    """

    requested_model: str
    models_used: tuple[str, ...] = ()
    prompt_version: str | None = None
    attempts: int = 1
    duration_ms: int | None = None
    total_cost_usd: float | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class AIResult[T]:
    """AI 呼び出しの結果。`value` は検証済みの出力スキーマのインスタンス。"""

    value: T
    meta: AICallMeta


type OutputSchema[T] = type[T] | TypeAdapter[T]
"""出力スキーマの渡し方。

Pydantic モデルそのもの（`RawArticle`）でも、`TypeAdapter` でもよい。
トップレベルが配列のスキーマ（`raw_articles.json` は array。設計書 §2.3）は
モデル型では表せないので、既にある `RAW_ARTICLES_ADAPTER` のような
`TypeAdapter` をそのまま渡せる形にしてある。
"""


def resolve_output_adapter[T](output_schema: OutputSchema[T]) -> TypeAdapter[T]:
    """出力スキーマを `TypeAdapter` へ正規化する。"""
    if isinstance(output_schema, TypeAdapter):
        return output_schema
    return TypeAdapter(output_schema)


@runtime_checkable
class AIClient(Protocol):
    """AI へ1回問い合わせて、検証済みの構造化出力を受け取る。

    ⚠️ **実装の詳細（CLI の引数・API のパラメータ）を漏らさないこと。** 上位が
    知ってよいのはプロンプト・出力スキーマ・タイムアウト・プロンプト版だけ。
    ここに CLI 固有の引数を足すと、本番の API 実装へ差し替えられなくなる。
    """

    async def complete[T](
        self,
        *,
        prompt: str,
        output_schema: OutputSchema[T],
        prompt_version: str | None = None,
        timeout: float | None = None,
    ) -> AIResult[T]:
        """プロンプトを投げ、出力スキーマで検証した結果とメタ情報を返す。

        Args:
            prompt: 送るプロンプト本体。**出力形式の指示は実装側が付ける**
                （CLI 実装は JSON Schema を添えて「JSON だけを出せ」と指示し、
                API 実装は structured outputs を使う想定）
            output_schema: 期待する出力の Pydantic スキーマ
            prompt_version: プロンプトの版（T-30）。メタへそのまま載る
            timeout: この呼び出しの制限時間（秒）。None なら実装の既定

        Returns:
            検証済みの値とメタ情報

        Raises:
            AIClientError: いずれの失敗も握り潰さず、原因ごとのサブクラスで返す
                （モジュール冒頭の表を参照）
        """
        ...


def describe_models(meta: AICallMeta) -> str:
    """監査ログ等へ1つの文字列として載せるときの表記。

    実際に使われたモデルが取れていればそれを（複数なら全部）、取れていなければ
    「指定した値しか分からない」ことが読める形にする。
    """
    if meta.models_used:
        return ",".join(meta.models_used)
    return f"{meta.requested_model}(未確認)"


def meta_to_audit_payload(meta: AICallMeta) -> dict[str, Any]:
    """監査ログ／validation メタへ載せる素の辞書（T-30 連携）。

    ⚠️ プロンプト本文・応答本文は入れない。記録したいのは「どのモデル・どの版で
    実行したか」＝再現性の手がかりで、本文は成果物ファイル側にある。
    """
    return {
        "model": describe_models(meta),
        "requested_model": meta.requested_model,
        "models_used": list(meta.models_used),
        "prompt_version": meta.prompt_version,
        "attempts": meta.attempts,
        "duration_ms": meta.duration_ms,
        "total_cost_usd": meta.total_cost_usd,
        "session_id": meta.session_id,
    }
