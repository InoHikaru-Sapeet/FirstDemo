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

ALLOWED_TOOLS_FLAG = "--allowedTools"
"""ツールの許可を渡す CLI のフラグ。**値は空白区切りで複数取る**（可変長引数）。"""

WEB_SEARCH_TOOLS = ("WebSearch", "WebFetch")
"""web 検索が要る呼び出し（crawl / PROMPT-1）へ許可するツール。**この2つだけ。**

⚠️ **`WebFetch` も要る（2026-08-16 実測）。** `WebSearch` だけを許可して
`make run-weekly` を通したところ、封筒の `permission_denials` に **`WebFetch` の拒否が
3件**入って `AIResponseError` で落ちた。PROMPT-1（仕様書 §13.2）は記事本文からの
「2〜4文の客観要約」を求めており、モデルは検索結果の**本文を読むため**に `WebFetch`
を使う。検索の実施だけを許可しても要約の材料が取れない。

⚠️ **増やさないこと。** ここに並べたツールがそのまま crawl の子プロセス（`claude`）へ
渡る。`Bash` やファイル書き込み系を足すと、収集の一往復に**実行系の経路が生える**。
収集に要るのは「検索する」「読む」の2つだけで、それ以外は許可しない
（`test_only_the_two_web_tools_are_allowed`）。
"""

WEB_SEARCH_CLI_ARGS = (ALLOWED_TOOLS_FLAG, *WEB_SEARCH_TOOLS)
"""web 検索を有効にする CLI 引数（2026-08-14 実測 ／ 2026-08-16 に `WebFetch` 追加）。

⚠️ **これは CLI 固有の書き方なので、この層より上へ出さない。** 上位（T-16 crawl）が
言えるのは `get_ai_client(web_search=True)` までで、`--allowedTools` を知らない。

⚠️ **許可を渡さないと「成功に見える失敗」になる。** 封筒は `is_error=false` /
`subtype="success"` のまま `permission_denials` に拒否記録が入る（`ClaudeCliClient`
冒頭の実測）。CLI 実装はそれを `AIResponseError` にし、crawl は別途
`modelUsage[].webSearchRequests` が 0 でないことも確かめる（二重の歯止め）。
**この2つの歯止めは `WebFetch` を足しても変えていない**（許可の付け忘れも、
許可はあるが検索しなかった場合も、これまでどおり落ちる）。
"""


def get_ai_client(*, web_search: bool = False) -> AIClient:
    """⚠️ **AI 呼び出し先の差し替え口はここ1箇所。**

    現在は Claude Code CLI（`claude -p`）実装ひとつだけ。**これは試作段階の手段**で、
    本番（AWS 展開・無人での定期実行）では Anthropic API 実装を追加し、この関数の
    戻り値だけを変える（TASKS.md §1.1「備考：AI呼び出し方式」）。

    ⚠️ **この差し替え口があること＝本番対応済み、ではない。** CLI 実装は
    「ログイン済みの PC が起動していること」を前提にしている。

    Args:
        web_search: web 検索を使う呼び出しか（crawl / PROMPT-1 は前提にしている）。
            **「何ができる必要があるか」だけを言う引数**で、実現方法（CLI の
            `--allowedTools "WebSearch" "WebFetch"` / API のサーバーツール定義）は
            この層が持つ。**検索して本文を読むところまでが「web 検索を使う」**
            （`WEB_SEARCH_TOOLS` の ⚠️ を参照）。
            API 実装へ差し替えるときは、ここで web 検索ツールを有効にし、
            `AICallMeta.web_search_requests` を埋めること
    """
    return ClaudeCliClient.from_settings(
        get_settings(), extra_args=WEB_SEARCH_CLI_ARGS if web_search else ()
    )


__all__ = [
    "ALLOWED_TOOLS_FLAG",
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
    "WEB_SEARCH_CLI_ARGS",
    "WEB_SEARCH_TOOLS",
    "ClaudeCliClient",
    "OutputSchema",
    "describe_models",
    "get_ai_client",
    "meta_to_audit_payload",
]
