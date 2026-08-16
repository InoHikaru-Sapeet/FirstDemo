"""クローリング収集（設計書 §8.2 ／ 仕様書 §13.2（PROMPT-1）／ T-16）。

パイプラインの最初の段。対象期間の AI 関連ニュースを **web 検索で広く集めて**
`raw_articles_{period}.json`（T-06 のスキーマ）へ書き出すところまでを担う。

---

**この層の責務（これ以外を持たせないこと）**

| やること | やらないこと（担当） |
|---|---|
| PROMPT-1 の組み立て（期間・ソース・カテゴリ・業界） | 除外ルール判定（T-17） |
| web 検索で収集させ、T-06 のスキーマで検証する | 重複・統合判定（T-18） |
| `raw_articles_{period}.json` の書き出し（T-02 経由） | 分類・タグ・採点（T-19） |
| 「検索が実施されたか」の確認 | 採否・しきい値（T-21） |

⚠️ **右列をこの段でやらせないこと**は §13.2 が明記している（「スコアリング・除外
判定・タグ確定はしない」「重複しうる記事もこの段階では落とさず全て残す」）。
crawl が間引くと、同じ発表をどの媒体が報じたかが失われ、代表記事の `ソース` 欄
（`A / B(統合)`・§11.3）を組み立てられなくなる（`raw_article` モジュール冒頭）。

---

⚠️ **web 検索が実施されていない収集結果は受け取らない。**

PROMPT-1 は web 検索を前提にしている。検索なしで返ってきた記事一覧は
**モデルの記憶からの推測**（実在しない URL・古い日付・作り話の可能性）で、
形の上では T-06 のスキーマを通ってしまう。そこで呼び出し後に
`AICallMeta.web_search_requests` を確認し、**0 のときも `None`（実装が報告して
いない）のときも失敗**にする（`SearchNotPerformedError`）。

`None` も失敗にするのは、「報告フィールドの名前が変わった／API 実装で埋め忘れた」
ときにこの歯止めが**黙って無効化される**のを防ぐため。安全側は「止まる」方。

歯止めは二重になっている:

1. CLI へツールの許可を渡し忘れると、封筒は成功のまま `permission_denials` に
   拒否記録が入る → `AIResponseError`（T-15 `claude_cli_client`）
2. 許可はあるが実際には検索しなかった → ここの `SearchNotPerformedError`

---

**AI 呼び出しは `AIClient`（T-15）経由の1本だけ。** 渡すのはプロンプトと出力
スキーマだけで、呼び出し先が Claude Code CLI か Anthropic API かをこの層は知らない。
**web 検索の有効化方法（CLI の `--allowedTools`）もこの層は知らない**：
`adapter.llm.get_ai_client(web_search=True)` が用途だけを受け取って解決する。

⚠️ **出力形式（「JSON だけを出せ」＋ JSON Schema）の指示は `AIClient` の実装側が
付ける。** このモジュールのプロンプトに書き足さないこと（二重指示になり、実装を
API へ差し替えたときに片方だけ残る）。

⚠️ **タイムアウトは crawl 用の 30分（`Settings.ai_crawl_timeout_seconds`）。**
分類・採点系の既定（10分）ではない。7カテゴリぶんの web 検索を伴うので、
1回の呼び出しが長い（些細なプロンプトでも起動に約131秒。T-15 備考）。

組み立て方（T-24 のジョブ定義から使う想定）:

    worker = CrawlWorker(
        client=get_ai_client(web_search=True),   # ← 許可はここで付く
        store=ArtifactStore.from_settings(),
        config=config,                            # 実行開始時に固定した revision
    )
    result = await worker.crawl("2026-W31")
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from adapter.llm import AIClient
from adapter.llm.ai_client import AICallMeta
from adapter.storage.artifact_store import ArtifactStore
from config import Settings, get_settings
from enterprise.entities.config import IntelligenceConfig

# period の表記検証と実日付への展開は `enterprise.entities.period` が唯一の定義
# （T-21 で共通の値オブジェクトへ寄せた。T-16 / T-18 備考の申し送り）。
from enterprise.entities.period import Period, PeriodError, parse_period
from enterprise.entities.raw_article import (
    RAW_ARTICLES_ADAPTER,
    PrimaryOrSecondary,
    RawArticle,
    RegionHint,
    dump_raw_articles,
)

logger = logging.getLogger(__name__)

# プロンプトの版（T-30 で `prompts/PROMPT-1.md` へ切り出し、ローダが読む予定）。
# それまではこのモジュールが唯一の置き場。⚠️ **本文を変えたら版も上げること**
# （実行時の版は `AICallMeta.prompt_version` として監査／validation メタに載る。
# 設計書 §9.2 の再現性要件）。
PROMPT_NAME = "PROMPT-1/crawl"
# 0.2.0: 対象業界の重点と月次の「導入企業側の事例」を足した（T-46 Step 1）。
PROMPT_VERSION = "0.2.0"

# 仕様書 §13.2 の「収集対象の優先ソース」（**逐語**）。
# ⚠️ **config には無い確定値**（§5.2 の可変項目に「優先ソース」は無い）ので、
# 仕様書の文面をここに置く。config に持つべきだと決まったら移すこと。
PRIORITY_SOURCES = (
    "TechCrunch / VentureBeat / Ledge.ai / ITmedia / 各社公式プレスリリース / "
    "政府・公的機関発表。"
)
OTHER_SOURCES = "その他でも信頼できる主要・専門メディアは可。"
EXCLUDED_SOURCES = "個人ブログ・SNS単独・まとめアフィリエイトは収集しない。"

# 週次・月次で重心が違う（§13.2）。⚠️ **どちらか一方しか出さない**
# （両方出すと「新規性も事例も」になって重み付けの指示が消える）。
WEEKLY_EMPHASIS = "「今週」の新規性を重視する（対象期間に新しく出た動きを優先する）。"
# ⚠️ **月次は「導入企業側」を明示する**（T-46 Step 1。初運用 2026-07 の実測で、
# 週次採用15件に `enterprise_ai_case` が0件＝ベンダーの製品・モデル発表ばかりが
# 集まった）。§13.2 の文言（「先進企業の具体的活用事例」）はそのまま残し、
# **何を事例と数えないか**を足している。
MONTHLY_EMPHASIS = (
    "「先進企業の具体的活用事例」を重視する"
    "——**導入した企業（ユーザー企業）側の事例**、つまり"
    "どの企業が何をどう使い、業務・体制・成果がどう変わったかが分かる記事を優先する。"
    "**ベンダーの製品・モデル・機能の発表そのものは活用事例として数えない**"
    "（発表は他のカテゴリの記事として収集してよい）。"
)

# 対象業界の重点（T-46 Step 1）。⚠️ **網羅指示に「足す」ものであって、
# 置き換えではない**（初運用 2026-W33 の実測で、収集の母集団に対象業界の記事が
# 1件も入らず §9.2-3 の業界関連トピックが構造的に空になった）。絞り込みへ
# 転じさせないため、次の3行はいつも一緒に出す。
INDUSTRY_FOCUS_HEADING = "■ 対象業界（必ず含める。絞り込みではない）"
INDUSTRY_FOCUS_FORMAT = "- 対象業界: {industries}"
INDUSTRY_FOCUS_INSTRUCTION = (
    "- 上の{count}カテゴリの網羅は維持したうえで、"
    "**対象業界に直接関わる記事を必ず収集対象に含める**"
    "（その業界の企業によるAI活用、その業界向けのAIサービス・製品、"
    "業界固有の規制・市場動向）。"
)
INDUSTRY_NOT_A_FILTER_NOTICE = (
    "- ⚠️ 対象業界で**絞り込まない**。対象業界の記事が見つからないときに"
    "他のカテゴリの記事を減らして帳尻を合わせない"
    "（業界タグの確定・採否の判断は次段の責務）。"
)
INDUSTRY_SEPARATOR = " / "

# §13.2 の「この段階でやらないこと」（**逐語**）。
NO_JUDGEMENT_NOTICE = (
    "スコアリング・除外判定・タグ確定はしない（次段の責務）。"
    "ここは網羅的に集めることに徹する。"
)
NO_DEDUP_NOTICE = "重複しうる記事もこの段階では落とさず全て残す（次段で統合判定する）。"

# period の値オブジェクトは `enterprise.entities.period.Period`（T-21 で共通化）。
# 以前ここにあった `PeriodSpan` はその別名で、既存の呼び出し（`span.kind` /
# `span.start` / `span.end` / `span.is_weekly`）はそのまま動く。
PeriodSpan = Period


class CrawlError(Exception):
    """crawl に使えない入力（period 表記の誤りなど）。

    ⚠️ **AI 呼び出しの失敗はこれに包まない。** `AIClientError` とその
    サブクラス（T-15）が原因ごとに分かれており、ジョブの再実行判断に使うため
    そのまま呼び出し元へ通す。
    """


class SearchNotPerformedError(CrawlError):
    """web 検索が実施されていない（または実施を確認できない）。

    ⚠️ **成果物を書く前に落とす。** 検索なしの収集結果＝モデルの記憶からの推測が
    `raw_articles_{period}.json` として残ると、後段（分類・採点・レポート）は
    それを実在の記事として扱う。

    Attributes:
        requested: 報告された検索回数。`None` は「報告が無い＝分からない」
    """

    def __init__(self, message: str, *, requested: int | None) -> None:
        self.requested = requested
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CrawlResult:
    """crawl の結果（T-24 のジョブが監査ログへ載せる素）。

    Attributes:
        period: 対象期間
        path: 書き出した `raw_articles_{period}.json` のパス
        articles: 収集した記事（**収集順のまま・重複も残したまま**）
        meta: AI 呼び出しの出自（使用モデル・`prompt_version`・検索回数。T-30）
    """

    period: str
    path: Path
    articles: tuple[RawArticle, ...]
    meta: AICallMeta

    @property
    def article_count(self) -> int:
        return len(self.articles)


class CrawlWorker:
    """対象期間の記事を収集して `raw_articles_{period}.json` を書く。

    `config` は実行開始時に固定参照しているもの（§6.3 の revision ピン留め済み）を
    渡すこと。crawl が config から取るのは**情報カテゴリの一覧と対象業界だけ**で、
    しきい値・配点・除外ルールは見ない（この段で判断しないため）。⚠️ 対象業界は
    **収集の重点**として渡すのであって、絞り込みの条件ではない（T-46 Step 1。
    `build_crawl_prompt` の ⚠️）。
    """

    def __init__(
        self,
        *,
        client: AIClient,
        store: ArtifactStore,
        config: IntelligenceConfig,
        timeout: float | None = None,
        today: date | None = None,
        settings: Settings | None = None,
    ) -> None:
        """
        Args:
            client: AI クライアント。⚠️ **web 検索が使えるもの**を渡すこと
                （`adapter.llm.get_ai_client(web_search=True)`）
            store: 成果物の置き場（T-02）
            config: 実行開始時に固定参照している config
            timeout: 制限時間（秒）。`None` なら **crawl 用の
                `Settings.ai_crawl_timeout_seconds`（30分）**。⚠️ 分類・採点系の
                既定（10分）へ落とさないこと
            today: 収集日として扱う日付（既定は設定タイムゾーンの今日）
            settings: 実行設定（既定は `get_settings()`）
        """
        settings = settings or get_settings()
        self._client = client
        self._store = store
        self._config = config
        self._timeout = (
            float(settings.ai_crawl_timeout_seconds)
            if timeout is None
            else float(timeout)
        )
        self._tz = settings.tzinfo
        self._today = today

    @property
    def timeout_seconds(self) -> float:
        """1回の収集に許す時間（既定は crawl 用の30分）。"""
        return self._timeout

    def build_prompt(self, period: str) -> str:
        """送るプロンプト（`AIClient` が出力形式の指示を後ろへ足す）。"""
        return build_crawl_prompt(
            period, self._config, collected_at=self.collected_on()
        )

    def collected_on(self) -> date:
        """収集日（各記事の `collected_at` に入れさせる日付）。"""
        return self._today or datetime.now(tz=self._tz).date()

    async def crawl(self, period: str) -> CrawlResult:
        """対象期間の記事を収集し、`raw_articles_{period}.json` を書き出す。

        Args:
            period: `2026-W31`（週次）または `2026-07`（月次）

        Returns:
            収集した記事・書き出し先・AI 呼び出しのメタ

        Raises:
            CrawlError: period の表記が不正・実在しない期間の場合
            SearchNotPerformedError: web 検索が実施されていない／確認できない場合
                （**この場合ファイルは書かない**）
            AIClientError: AI 呼び出しの失敗（原因ごとのサブクラス。握り潰さない）
            DocumentParseError: 出力が T-06 のスキーマに合わない場合
                （`AIClient` がリトライ上限まで試した後 `AIOutputParseError`）
        """
        span = period_span(period)
        logger.info(
            "crawl started (period=%s, kind=%s, %s..%s, timeout=%.0fs)",
            period,
            span.kind,
            span.start,
            span.end,
            self._timeout,
        )

        result = await self._client.complete(
            prompt=self.build_prompt(period),
            output_schema=RAW_ARTICLES_ADAPTER,
            prompt_version=PROMPT_VERSION,
            timeout=self._timeout,
        )
        # ⚠️ 書き出しより**前**に確かめる（検索なしの結果をファイルに残さない）。
        ensure_search_was_performed(result.meta, period=period)

        articles = tuple(result.value)
        if not articles:
            # 落とさない（§13.2 は件数を約束していない）が、静かに0件を通すと
            # 「収集できていない」ことが後段の空レポートまで気づかれない。
            logger.warning("crawl returned no articles (period=%s)", period)

        path = self._store.raw_articles_path(period)
        self._store.write_text(path, dump_raw_articles(articles))
        logger.info(
            "crawl finished (period=%s, articles=%d, web_search_requests=%s, path=%s)",
            period,
            len(articles),
            result.meta.web_search_requests,
            path,
        )
        return CrawlResult(
            period=period, path=path, articles=articles, meta=result.meta
        )


def ensure_search_was_performed(meta: AICallMeta, *, period: str) -> None:
    """web 検索が実施されたことを確かめる（モジュール冒頭の ⚠️ を参照）。

    Raises:
        SearchNotPerformedError: 回数が 0、または報告が無い（`None`）場合
    """
    requested = meta.web_search_requests
    if requested is None:
        raise SearchNotPerformedError(
            f"web 検索が実施されたかを確認できません（period={period}）。"
            "AI クライアントが検索回数を報告していません"
            "（CLI の封筒なら modelUsage[].webSearchRequests、"
            "API 実装なら server_tool_use.web_search_requests）。"
            "確認できない収集結果は、記憶からの推測と区別が付かないので受け取りません",
            requested=None,
        )
    if requested <= 0:
        raise SearchNotPerformedError(
            f"web 検索が1回も実施されていません（period={period}）。"
            "収集結果がモデルの記憶からの推測になりうるため受け取りません。"
            "AI クライアントに web 検索が許可されているかを確認してください"
            "（get_ai_client(web_search=True)）",
            requested=requested,
        )


def period_span(period: str) -> Period:
    """`2026-W31` / `2026-07` を実際の日付範囲に開く。

    ⚠️ **実在しない期間はここで落とす**（`2026-13` / 53週を持たない年の `-W53`）。
    表記が合っているだけの period をそのままプロンプトへ載せると、モデルが適当な
    期間を補って収集してしまう。

    判定そのものは `enterprise.entities.period.parse_period()`（唯一の定義）で、
    ここは失敗を **crawl の例外へ包み直す**だけ（呼び出し元が工程で判別できる形を
    保つ）。

    Raises:
        CrawlError: 週次・月次のどちらの表記でもない、または実在しない期間
    """
    try:
        return parse_period(period)
    except PeriodError as exc:
        raise CrawlError(str(exc)) from exc


def build_crawl_prompt(
    period: str, config: IntelligenceConfig, *, collected_at: date
) -> str:
    """PROMPT-1 を組み立てる（仕様書 §13.2）。

    §13.2 の本文をテンプレート化したもの。**足したのは4つだけ**で、いずれも
    「モデルが推測で補う余地」または「収集の母集団の偏り」を消すためのもの:

    1. **期間の実日付**（`2026-W31` の解釈を実行ごとに揺らさない）
    2. **収集日**（`collected_at` はモデルが今日を知らないと埋められない）
    3. **web 検索を必ず使う指示**（検索なしの収集は受け取らない。冒頭 ⚠️）
    4. **対象業界の重点**（T-46 Step 1。下の ⚠️）

    ⚠️ **7カテゴリは config から取る**（§13.2 自身が「config.json の7カテゴリ」と
    書いている）。ここに名前を写すと、admin がカテゴリを変えても crawl の網羅指示が
    追随しない。**対象業界も同じく config から取る**。

    ⚠️ **対象業界は「必ず含める」であって「これだけ集める」ではない。**
    初運用（2026-W33）の実測では、対象業界を渡さない収集の結果、採用15件の `業界`
    タグがすべて「業界横断」「IT」系になり、§9.2-3 の業界関連トピックが構造的に
    空になった。**収集の母集団に無いものは後段では作れない**ので、この段で
    「網羅は維持したまま対象業界の記事も必ず入れる」ことを求める。
    ⚠️ **絞り込みに転じさせないこと**（`INDUSTRY_NOT_A_FILTER_NOTICE` を必ず一緒に
    出す）。ここで絞ると、業界タグの確定（T-19）と除外（T-17）が判断する材料自体が
    消える——これが渡さない設計にしていた元の理由で、その懸念は残っている。

    ⚠️ **出力形式（JSON だけを出せ・JSON Schema）の指示は含めない。**
    `AIClient` の実装が付ける（`claude_cli_client.OUTPUT_INSTRUCTIONS`）。

    Args:
        period: `2026-W31`（週次）または `2026-07`（月次）
        config: 実行開始時に固定参照している config（情報カテゴリの正）
        collected_at: 収集日（各記事の `collected_at` に入れさせる日付）

    Returns:
        プロンプト本文

    Raises:
        CrawlError: period が不正な場合
    """
    span = period_span(period)
    categories = config.information_categories
    period_label = "ISO週・月曜始まり" if span.is_weekly else "暦月"

    sections = [
        "あなたはAI動向のリサーチャーです。"
        f"対象期間 {period}（{span.start}〜{span.end}。{period_label}）に"
        "公開されたAI関連ニュースを収集してください。",
        "",
        "■ 収集のしかた（厳守）",
        "- **必ず web 検索を使って実在の記事を集めること。** "
        "記憶や推測で記事・URL・公開日を書かない。",
        f"- 対象期間（{span.start}〜{span.end}）に**公開された**記事を集める。",
        f"- 収集日は {collected_at}（本日）。",
        "",
        "■ 収集対象の優先ソース",
        PRIORITY_SOURCES,
        f"{OTHER_SOURCES}{EXCLUDED_SOURCES}",
        "",
        f"■ 収集範囲の観点（次の{len(categories)}カテゴリを網羅するよう広く）",
        *(
            f"- {category.label}（{category.id}） — {category.description}"
            for category in categories
        ),
        f"- {WEEKLY_EMPHASIS if span.is_weekly else MONTHLY_EMPHASIS}",
        "",
        INDUSTRY_FOCUS_HEADING,
        INDUSTRY_FOCUS_FORMAT.format(
            industries=INDUSTRY_SEPARATOR.join(
                config.tunable_thresholds.weekly.industries
            )
        ),
        INDUSTRY_FOCUS_INSTRUCTION.format(count=len(categories)),
        INDUSTRY_NOT_A_FILTER_NOTICE,
        "",
        "■ この段階でやらないこと",
        f"- {NO_JUDGEMENT_NOTICE}",
        f"- {NO_DEDUP_NOTICE}",
        "",
        "■ 各記事に入れる内容",
        f"- collected_at: 収集日（本日 = {collected_at}）",
        "- published_at: 公開日（わかる範囲。分からなければ null）",
        "- title: 記事タイトル",
        "- url: 記事の URL（正規化前でよい）",
        "- source: 媒体名",
        "- raw_summary: 本文から2〜4文の客観要約（意見を混ぜない）",
        f"- region_hint: {_choices(RegionHint)}",
        f"- primary_or_secondary: {_choices(PrimaryOrSecondary)}",
    ]
    return "\n".join(sections)


def _choices(
    values: Sequence[str] | type[RegionHint] | type[PrimaryOrSecondary],
) -> str:
    """enum の候補を提示する形（候補は T-06 のスキーマが正）。"""
    return " / ".join(str(value) for value in values)


__all__ = [
    "EXCLUDED_SOURCES",
    "INDUSTRY_FOCUS_FORMAT",
    "INDUSTRY_FOCUS_HEADING",
    "INDUSTRY_FOCUS_INSTRUCTION",
    "INDUSTRY_NOT_A_FILTER_NOTICE",
    "INDUSTRY_SEPARATOR",
    "MONTHLY_EMPHASIS",
    "NO_DEDUP_NOTICE",
    "NO_JUDGEMENT_NOTICE",
    "PRIORITY_SOURCES",
    "PROMPT_NAME",
    "PROMPT_VERSION",
    "WEEKLY_EMPHASIS",
    "CrawlError",
    "CrawlResult",
    "CrawlWorker",
    "PeriodSpan",
    "SearchNotPerformedError",
    "build_crawl_prompt",
    "ensure_search_was_performed",
    "period_span",
]
