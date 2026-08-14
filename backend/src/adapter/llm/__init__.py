"""AIクライアント層（設計書 §6・§9 ／ TASKS.md T-15）。

**AI 呼び出し先の差し替え口はこのファイルの `get_ai_client()` 1箇所。**
`AuthenticationBackend` の `get_authentication_backend()`（T-08）と同じ形。

上位（T-16 crawl / T-19 分類・採点）はここから `AIClient` を受け取り、
**プロンプトと出力スキーマだけ**を渡す:

    from adapter.llm import get_ai_client

    client = get_ai_client()
    result = await client.complete(
        prompt=prompt,                      # T-30 のテンプレートから組み立てたもの
        output_schema=RAW_ARTICLES_ADAPTER,  # 期待する出力（Pydantic）
        prompt_version=prompt_version,       # 監査／validation メタへ載る（T-30）
        timeout=settings.ai_crawl_timeout_seconds,  # crawl は長いので明示する
    )
    articles = result.value

⚠️ **タイムアウトは呼び出し側が用途で選ぶ。** 既定（`ai_timeout_seconds` = 10分）は
分類・採点系向けで、crawl は `ai_crawl_timeout_seconds`（30分）を渡すこと。
既定を短くしないこと（些細なプロンプトでも実測 約131秒。`claude_cli_client` 冒頭）。
"""

from adapter.llm.ai_client import (
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
    OutputSchema,
    describe_models,
    meta_to_audit_payload,
)
from adapter.llm.claude_cli_client import ClaudeCliClient
from config import get_settings


def get_ai_client() -> AIClient:
    """⚠️ **AI 呼び出し先の差し替え口はここ1箇所。**

    現在は Claude Code CLI（`claude -p`）実装ひとつだけ。**これは試作段階の手段**で、
    本番（AWS 展開・無人での定期実行）では Anthropic API 実装を追加し、この関数の
    戻り値だけを変える（TASKS.md §1.1「備考：AI呼び出し方式」）。

    ⚠️ **この差し替え口があること＝本番対応済み、ではない。** CLI 実装は
    「ログイン済みの PC が起動していること」を前提にしている。
    """
    return ClaudeCliClient.from_settings(get_settings())


__all__ = [
    "AICallMeta",
    "AIClient",
    "AIClientError",
    "AIOutputParseError",
    "AIProcessError",
    "AIProtocolError",
    "AIResponseError",
    "AIResult",
    "AITimeoutError",
    "AIUnavailableError",
    "ClaudeCliClient",
    "OutputSchema",
    "describe_models",
    "get_ai_client",
    "meta_to_audit_payload",
]
