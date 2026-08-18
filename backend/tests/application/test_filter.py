"""filter オーケストレーション（T-21 ／ 設計書 §6.1 ／ 仕様書 §13.3）。

⚠️ **実際の `claude` は起動しない。** `AIClient`（T-15 のプロトコル）を
`ScriptedAIClient` へ差し替える。CI に CLI とログインを要求しない。

重点（T-21 完了条件の各経路を通す）:

- **除外**（`full_exclude` / `default_exclude` の例外採用 / `low_priority` の降格）
- **重複統合**（同じ実行内・過去週の両方）と除外ログ
- **採否**（低スコア・信頼性不足）とその理由
- **フォーマット不備**（§12 の error は本編から外して除外ログへ）
- **合計スコア降順**の整列と `validation_{period}.json` の書き出し
- **config@revision の固定参照**（実行中に書き換えられても揺れない）
- **月次**の事例昇格・章束ね・解説3段落
"""

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from adapter.html.weekly_renderer import render_weekly_html
from adapter.llm.ai_client import (
    AICallMeta,
    AIResult,
    OutputSchema,
    resolve_output_adapter,
)
from adapter.storage.artifact_store import ArtifactStore
from adapter.xlsx.report_writer import ReportStore
from application.usecases import filter as filter_module
from application.usecases.classify_and_score import (
    FACTS_FIELD,
    IS_STALE_FIELD,
    MATCHED_RULES_FIELD,
    SCORES_FIELD,
    SUMMARY_FIELD,
    TAGS_FIELD,
)
from application.usecases.filter import (
    CATEGORY_LOW_SCORE,
    FilterError,
    FilterWorker,
    RawArticlesNotFoundError,
    category_distribution,
    format_category_distribution,
    format_score_distribution,
)
from application.usecases.monthly_cases import CHAPTER_LABEL_FORMAT
from application.usecases.narrative import (
    WEEKLY_NARRATIVE_PROMPT_VERSION,
    to_weekly_narrative,
)
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.json_document import parse_json_document
from enterprise.entities.narrative import (
    MonthlyNarrativeDocument,
    WeeklyNarrativeDocument,
    parse_narrative,
)
from enterprise.entities.raw_article import RawArticle, dump_raw_articles
from enterprise.entities.report_columns import (
    EXCLUSION_LOG_COLUMNS,
    MONTHLY_CASE_COLUMNS,
    PARAGRAPH_SEPARATOR,
    WEEKLY_ARTICLE_COLUMNS,
    format_cell,
)
from enterprise.entities.validation_report import (
    ValidationIssue,
    ValidationReport,
    parse_validation_report,
)
from enterprise.services.dedup import (
    CATEGORY_MERGED,
    REASON_DUPLICATE,
    DedupHistory,
    KnownArticle,
    KnownOrigin,
)
from enterprise.services.format_check import (
    CATEGORY_FORMAT_ERROR,
    ArticleFormatIssues,
    FormatCheckResult,
    RejectedArticle,
)

INITIAL_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)

WEEKLY_PERIOD = "2026-W31"
MONTHLY_PERIOD = "2026-07"

# §5.2 の初期 config の対象業界（1件）。生成テキストは業界ごと（T-46 Step 4）。
TARGET_INDUSTRY = "不動産"


@pytest.fixture(scope="session")
def initial_raw() -> dict[str, Any]:
    return json.loads(INITIAL_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def config(initial_raw: dict[str, Any]) -> IntelligenceConfig:
    return IntelligenceConfig.model_validate(copy.deepcopy(initial_raw))


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path)


# --- 入力（crawl の出力）-----------------------------------------------------


def article(
    *,
    title: str = "大手不動産がAIエージェントで契約業務を自動化",
    url: str = "https://example.com/news/1",
    source: str = "ITmedia",
    collected_at: str = "2026-07-28",
    published_at: str | None = "2026-07-27",
) -> RawArticle:
    return RawArticle(
        collected_at=collected_at,
        published_at=published_at,
        title=title,
        url=url,
        source=source,
        raw_summary="国内大手がAIエージェントを導入した。契約業務が自動化された。",
        region_hint="日本",
        primary_or_secondary="報道",
    )


def write_articles(
    store: ArtifactStore, articles: list[RawArticle], period: str
) -> None:
    store.write_text(store.raw_articles_path(period), dump_raw_articles(articles))


# --- AI 出力（分類・採点＋事実申告 / 月次の事例 / 章の束ね）-----------------

TWO_SENTENCE_SUMMARY = (
    "大手不動産がAIエージェントを契約業務へ導入した。定型作業の一部が自動化された。"
)

VALID_TAGS: dict[str, Any] = {
    "information_category": "enterprise_ai_case",
    "ai_theme": ["AIエージェント"],
    "industry": ["不動産"],
    "business_area": ["業務プロセス改革"],
    "info_type": "専門メディア報道",
    "region": ["日本"],
    "reliability": "高",
    "customer_relevance": "直接関係",
    "practical_usability": "すぐ活用",
}

# 合計 22+17+15+12+9+8 = 83（§5.2 のしきい値では採用・「参考情報」）。
VALID_SCORES: dict[str, int] = {
    "customer_relevance": 22,
    "practical_usability": 17,
    "market_impact": 15,
    "advisory_usability": 12,
    "reliability": 9,
    "urgency_freshness": 8,
}


def classification(
    *,
    tags: dict[str, Any] | None = None,
    scores: dict[str, Any] | None = None,
    summary: str = TWO_SENTENCE_SUMMARY,
    rules: list[int] | None = None,
    is_stale: bool = False,
) -> dict[str, Any]:
    return {
        TAGS_FIELD: {**VALID_TAGS, **(tags or {})},
        SCORES_FIELD: {**VALID_SCORES, **(scores or {})},
        SUMMARY_FIELD: summary,
        FACTS_FIELD: {
            MATCHED_RULES_FIELD: list(rules or []),
            IS_STALE_FIELD: is_stale,
        },
    }


POINT_OF_WEEK_SENTENCES = [
    "今週はAIエージェントの実務投入が相次いだ。",
    "契約や審査など定型度の高い業務から広がっている。",
    "不動産では現場の運用設計が論点になる。",
]


def weekly_narrative_draft(urls: list[str]) -> dict[str, Any]:
    """今週のポイント＋記事ごとの示唆（T-44。**宛先はプロンプトの URL**）。

    ⚠️ 今週のポイントは**見出し＋詳細**の組（T-52 Step 1）。
    """
    return {
        "point_of_week": [
            {"heading": heading, "detail": f"{heading}の詳細。"}
            for heading in POINT_OF_WEEK_SENTENCES
        ],
        "insights": [
            {"url": url, "insight": f"{url} を自社ではこう捉える。"} for url in urls
        ],
    }


def monthly_narrative_draft(
    chapters: list[str], case_numbers: list[int]
) -> dict[str, Any]:
    """巻頭言・章導入文・むすび・図解（T-44 ／ T-49）。

    ⚠️ 図解は**全事例で `null`**（「該当するタイプが無ければ作らない」＝正常経路）。
    """
    return {
        "editorial_subtitle": "『導入したか』ではなく『作り直したか』が問われた月",
        "editorial_overview": "俯瞰の段落。",
        "editorial_analysis": "共通する変化の段落。",
        "editorial_takeaway": "持ち帰る視点の段落。",
        "chapter_intros": [
            {"chapter": chapter, "intro": f"{chapter} には…を集めた。"}
            for chapter in chapters
        ],
        "closing_summary": "今月の総括。",
        "closing_outlook": "来月への視点。",
        "case_diagrams": [{"no": no, "diagram": None} for no in case_numbers],
    }


def case_draft(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "organizations": ["大手不動産"],
        "case_title": "契約業務のAIエージェント化",
        "chapter_theme": "業務自動化",
        "commentary_fact": "大手不動産がAIエージェントを導入した。",
        "commentary_detail": "契約書のドラフト作成を自動化し、工数を半減させた。",
        "commentary_implication": "定型度の高い業務から着手するのが現実的である。",
    }
    payload.update(overrides)
    return payload


@dataclass(frozen=True, slots=True)
class Call:
    prompt: str
    output_schema: Any
    prompt_version: str | None
    timeout: float | None


class ScriptedAIClient:
    """`AIClient` のテストダブル。**出力スキーマの形で用途を見分ける。**

    filter は1回の実行で4種類の呼び出しをする（分類・採点／月次の事例本文／章の
    束ね直し／生成テキスト＝T-44）ので、順番に pop する形だと読みづらい。ここでは
    「どのスキーマで聞かれたか」で応答を選び、記事ごとの応答は**プロンプトに
    載っている記事タイトル**で引く（本物と同じく、渡された出力スキーマで検証する）。

    生成テキストの宛先（記事URL・章ラベル）は `Literal` で閉じられているので、
    **プロンプトに載っている値をそのまま拾って**応答を組み立てる（宛先を取り違えた
    応答はスキーマで落ちる＝本物と同じ挙動になる）。
    """

    def __init__(
        self,
        *,
        classifications: dict[str, dict[str, Any]] | None = None,
        default_classification: dict[str, Any] | None = None,
        cases: dict[str, dict[str, Any]] | None = None,
        default_case: dict[str, Any] | None = None,
        chapters: dict[str, Any] | None = None,
    ) -> None:
        self.classifications = classifications or {}
        self.default_classification = default_classification or classification()
        self.cases = cases or {}
        self.default_case = default_case or case_draft()
        self.chapters = chapters
        self.calls: list[Call] = []

    async def complete[T](
        self,
        *,
        prompt: str,
        output_schema: OutputSchema[T],
        prompt_version: str | None = None,
        timeout: float | None = None,
    ) -> AIResult[T]:
        self.calls.append(
            Call(
                prompt=prompt,
                output_schema=output_schema,
                prompt_version=prompt_version,
                timeout=timeout,
            )
        )
        adapter = resolve_output_adapter(output_schema)
        payload = self._payload_for(prompt, output_schema)
        value = parse_json_document(
            adapter, json.dumps(payload, ensure_ascii=False), label="AI 出力"
        )
        return AIResult(
            value=value,
            meta=AICallMeta(
                requested_model="claude-opus-5",
                models_used=("claude-opus-5",),
                prompt_version=prompt_version,
            ),
        )

    def _payload_for(self, prompt: str, schema: Any) -> dict[str, Any]:
        fields = set(getattr(schema, "model_fields", {}))
        if TAGS_FIELD in fields:
            return self._by_title(prompt, self.classifications) or (
                self.default_classification
            )
        if "chapters" in fields:
            if self.chapters is None:
                raise AssertionError("章の束ね直しは呼ばれない想定です")
            return self.chapters
        if "point_of_week" in fields:
            return weekly_narrative_draft(_urls_in(prompt))
        if "editorial_subtitle" in fields:
            return monthly_narrative_draft(
                _chapters_in(prompt), _case_numbers_in(prompt)
            )
        return self._by_title(prompt, self.cases) or self.default_case

    @staticmethod
    def _by_title(
        prompt: str, table: dict[str, dict[str, Any]]
    ) -> dict[str, Any] | None:
        for title, payload in table.items():
            if title in prompt:
                return payload
        return None

    @property
    def classification_calls(self) -> int:
        return self._calls_with(TAGS_FIELD)

    @property
    def narrative_calls(self) -> int:
        """生成テキストの往復（T-44。**period ごとに1回**であること）。"""
        return self._calls_with("point_of_week") + self._calls_with(
            "editorial_subtitle"
        )

    def _calls_with(self, field_name: str) -> int:
        return sum(
            1
            for call in self.calls
            if field_name in set(call.output_schema.model_fields)
        )


def _urls_in(prompt: str) -> list[str]:
    """プロンプトに載っている記事URL（示唆の宛先）。"""
    return re.findall(r"URL: (\S+)", prompt)


def _chapters_in(prompt: str) -> list[str]:
    """プロンプトに載っている章ラベル（章導入文の宛先）。"""
    return list(dict.fromkeys(re.findall(r"^\[(.+?)\] CASE ", prompt, re.MULTILINE)))


def _case_numbers_in(prompt: str) -> list[int]:
    """プロンプトに載っている事例の `No`（図解の宛先。T-49）。"""
    return [int(no) for no in re.findall(r"\] CASE (\d+) ／", prompt)]


class RecordingHistoryReader:
    """`HistoryReader` のテストダブル（どの period を聞かれたかを残す）。"""

    def __init__(self, entries: list[KnownArticle] | None = None) -> None:
        self.entries = entries or []
        self.asked: list[str] = []

    def read_history(self, periods: list[str]) -> DedupHistory:
        self.asked = list(periods)
        allowed = set(periods)
        return DedupHistory(entry for entry in self.entries if entry.period in allowed)


def worker(
    config: IntelligenceConfig,
    store: ArtifactStore,
    client: ScriptedAIClient,
    **kwargs: Any,
) -> FilterWorker:
    return FilterWorker(client=client, store=store, config=config, **kwargs)


def cell(records: list[dict[str, Any]], column: str) -> list[Any]:
    return [record[column] for record in records]


# --- 入力の取り扱い -----------------------------------------------------------


async def test_the_crawl_output_is_required(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """crawl 未実行を「0件」として静かに通さない。"""
    client = ScriptedAIClient()

    with pytest.raises(RawArticlesNotFoundError):
        await worker(config, store, client).run(WEEKLY_PERIOD)

    assert client.calls == []


async def test_a_bad_period_fails_before_the_ai_is_called(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    client = ScriptedAIClient()

    with pytest.raises(FilterError):
        await worker(config, store, client).run("2026-13")

    assert client.calls == []


async def test_one_ai_round_trip_per_article(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """⚠️ 決定1 の要点：分類・採点と事実の申告を1往復にまとめてある。"""
    write_articles(
        store,
        [
            article(title="記事A", url="https://example.com/a"),
            article(title="記事B", url="https://example.com/b"),
        ],
        WEEKLY_PERIOD,
    )
    client = ScriptedAIClient()

    await worker(config, store, client).run(WEEKLY_PERIOD)

    assert client.classification_calls == 2


# --- 1〜2) 除外判定（T-17）---------------------------------------------------


async def test_a_full_exclude_article_goes_to_the_exclusion_log(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """ルール1（`full_exclude`）に当たったと申告された記事は無条件で外れる。"""
    write_articles(store, [article(title="真偽不明の噂")], WEEKLY_PERIOD)
    client = ScriptedAIClient(default_classification=classification(rules=[1]))

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert result.articles == []
    assert len(result.exclusion_log) == 1
    entry = result.exclusion_log[0]
    assert entry["除外区分"] == "完全除外"
    assert entry["除外理由"] == config.exclusion_rules[0].name
    assert entry["タイトル"] == "真偽不明の噂"


async def test_the_default_exclude_exception_uses_the_real_total(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """⚠️ 決定1：例外採用（§5.4）の判定は**確定した合計点**で行う。

    合計 83 ≥ `min_total_score_to_publish`（60）かつ顧客関連度が「直接関係」
    なので、`default_exclude` のルール3 に当たっても採用される。
    """
    write_articles(store, [article()], WEEKLY_PERIOD)
    client = ScriptedAIClient(default_classification=classification(rules=[3]))

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert result.exclusion_log == []
    assert len(result.articles) == 1


async def test_the_default_exclude_falls_back_to_exclusion(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """例外の条件を満たさなければ原則どおり除外（顧客関連度が「直接関係」でない）。"""
    write_articles(store, [article()], WEEKLY_PERIOD)
    client = ScriptedAIClient(
        default_classification=classification(
            rules=[3], tags={"customer_relevance": "一般参考"}
        )
    )

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert result.articles == []
    assert result.exclusion_log[0]["除外区分"] == "原則除外"


async def test_a_low_priority_article_is_downgraded(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """`low_priority`（ルール11）は**採用したうえで**採用区分を1段下げる（§5.4）。

    降格は T-19 の `decide_adoption()` 経由（この層で実装し直さない）。
    """
    write_articles(store, [article()], WEEKLY_PERIOD)
    client = ScriptedAIClient(default_classification=classification(rules=[11]))

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert len(result.articles) == 1
    # 83点は「参考情報」（70〜84）→ 1段下げて「共有のみ」。
    assert result.articles[0]["レポート採用区分"] == "共有のみ"
    assert result.classified[0].adoption.scored_class == "参考情報"


async def test_a_stale_article_follows_rule_13(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """`low_priority_or_exclude`（ルール13）は鮮度の申告で分岐する。"""
    write_articles(store, [article()], WEEKLY_PERIOD)

    stale = ScriptedAIClient(
        default_classification=classification(rules=[13], is_stale=True)
    )
    fresh = ScriptedAIClient(
        default_classification=classification(rules=[13], is_stale=False)
    )

    excluded = await worker(config, store, stale).run(WEEKLY_PERIOD)
    kept = await worker(config, store, fresh).run(WEEKLY_PERIOD)

    assert excluded.articles == []
    assert excluded.exclusion_log[0]["除外区分"] == "低優先/除外"
    assert len(kept.articles) == 1
    assert kept.articles[0]["レポート採用区分"] == "共有のみ"  # 低優先で1段下がる


# --- 3) 重複・統合（T-18）----------------------------------------------------


async def test_two_reports_of_the_same_announcement_are_merged(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """同一発表の別媒体は代表1件へ統合し、`ソース` を `A / B(統合)` にする（§11.3）。"""
    write_articles(
        store,
        [
            article(title="大手不動産がAIエージェントで契約業務を自動化"),
            article(
                title="大手不動産がAIエージェントで契約業務を自動化へ",
                url="https://other.example.com/news/9",
                source="日経",
            ),
        ],
        WEEKLY_PERIOD,
    )
    client = ScriptedAIClient()

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert len(result.articles) == 1
    assert result.articles[0]["ソース"] == "ITmedia / 日経(統合)"
    assert len(result.exclusion_log) == 1
    assert result.exclusion_log[0]["除外区分"] == CATEGORY_MERGED
    assert result.exclusion_log[0]["除外理由"] == REASON_DUPLICATE


async def test_an_article_seen_in_a_past_week_is_excluded(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """§11.1 の参照範囲（過去週のシート＋除外ログ）と突き合わせる。"""
    write_articles(store, [article()], WEEKLY_PERIOD)
    reader = RecordingHistoryReader(
        [
            KnownArticle(
                title="別のタイトル",
                url="https://example.com/news/1",
                source="ITmedia",
                period="2026-W30",
                origin=KnownOrigin.PUBLISHED,
            )
        ]
    )

    result = await worker(config, store, ScriptedAIClient(), history_reader=reader).run(
        WEEKLY_PERIOD
    )

    assert result.articles == []
    assert result.exclusion_log[0]["除外区分"] == CATEGORY_MERGED


async def test_the_weekly_history_scope_comes_from_the_config(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """遡る週数は `dedup.lookback_weeks`（既定8）。**対象週は含めない**（§14）。"""
    write_articles(store, [article()], WEEKLY_PERIOD)
    reader = RecordingHistoryReader()

    await worker(config, store, ScriptedAIClient(), history_reader=reader).run(
        WEEKLY_PERIOD
    )

    lookback = config.tunable_thresholds.dedup.lookback_weeks
    assert len(reader.asked) == lookback
    assert reader.asked[0] == "2026-W30"
    assert WEEKLY_PERIOD not in reader.asked


async def test_the_monthly_history_scope_drops_the_target_month(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """⚠️ §11.1 は当月を含むが、当月の cases は**この実行の出力**なので外す。

    含めると再実行（設計判断B）で全件が「既出」になる（§14 冪等性）。
    月数は決定2 で config に入れた `dedup.monthly_lookback_months`（既定3）。
    """
    write_articles(store, [article(collected_at="2026-07-28")], MONTHLY_PERIOD)
    reader = RecordingHistoryReader()

    await worker(config, store, ScriptedAIClient(), history_reader=reader).run(
        MONTHLY_PERIOD
    )

    assert reader.asked == ["2026-06", "2026-05", "2026-04"]
    assert MONTHLY_PERIOD not in reader.asked


async def test_a_missing_history_reader_is_reported(
    config: IntelligenceConfig,
    store: ArtifactStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """履歴なしで動けるが、**黙って**過去週との重複を見逃さない。"""
    write_articles(store, [article()], WEEKLY_PERIOD)

    with caplog.at_level("WARNING"):
        await worker(config, store, ScriptedAIClient()).run(WEEKLY_PERIOD)

    assert any("履歴リーダ" in record.message for record in caplog.records)


# --- 4) 採否（§13.3-5）-------------------------------------------------------


async def test_a_low_total_score_is_excluded_with_the_threshold_in_the_reason(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    write_articles(store, [article()], WEEKLY_PERIOD)
    client = ScriptedAIClient(
        default_classification=classification(
            scores={"customer_relevance": 0, "practical_usability": 0}
        )
    )

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert result.articles == []
    entry = result.exclusion_log[0]
    assert entry["除外区分"] == CATEGORY_LOW_SCORE
    assert "合計スコア 44 < 60" in entry["除外理由"]


async def test_a_low_reliability_score_is_excluded_even_with_a_high_total(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """§13.3-5 は合計と信頼性の**どちらか一方**が下回れば除外。"""
    write_articles(store, [article()], WEEKLY_PERIOD)
    client = ScriptedAIClient(
        default_classification=classification(
            scores={"reliability": 4, "customer_relevance": 25}
        )
    )

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert result.articles == []
    assert "信頼性点 4 < 5" in result.exclusion_log[0]["除外理由"]


async def test_the_threshold_boundary_is_inclusive(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """しきい値ちょうどは採用（`≥`）。T-17 / T-19 の境界と同じ向き。"""
    write_articles(store, [article()], WEEKLY_PERIOD)
    # 合計 60（= min_total_score_to_publish）／信頼性 5（= 下限）。
    client = ScriptedAIClient(
        default_classification=classification(
            scores={
                "customer_relevance": 20,
                "practical_usability": 15,
                "market_impact": 10,
                "advisory_usability": 5,
                "reliability": 5,
                "urgency_freshness": 5,
            }
        )
    )

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert result.exclusion_log == []
    assert result.articles[0]["合計スコア"] == 60


# --- 5) フォーマットチェック（T-20）------------------------------------------


async def test_the_classification_output_cannot_break_format_check(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """⚠️ この経路で §12 の error は**構造的に起きない**。

    （だから次のテストでは検査そのものを差し替えて経路を通す）

    合計はアプリが合算し、タグの候補は config の `Literal`、空文字はスキーマが
    弾く（T-19）。したがって error は「実装が壊れたとき」にしか出ない。
    """
    write_articles(store, [article()], WEEKLY_PERIOD)

    result = await worker(config, store, ScriptedAIClient()).run(WEEKLY_PERIOD)

    assert result.validation.ok
    assert result.validation.errors == []


async def test_a_format_error_drops_the_article_and_logs_it(
    config: IntelligenceConfig,
    store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§12.2：error のある記事は本編から外し、除外ログへ `フォーマット不備` で残す。

    実際の分類出力では error を作れない（前のテスト）ので、**検査そのものを
    差し替えて経路を通す**。ここで見たいのは「T-20 の `rejected` を除外ログへ
    回し、`accepted` だけを本編へ渡す」という配線。
    """
    write_articles(store, [article()], WEEKLY_PERIOD)

    def always_rejects(records: Any, _config: Any) -> FormatCheckResult:
        issues = ArticleFormatIssues(
            row=5,
            errors=[
                ValidationIssue(row=5, field="合計スコア", reason="6軸の和と不一致")
            ],
            warnings=[],
        )
        return FormatCheckResult(
            report=ValidationReport.from_issues(errors=issues.errors),
            accepted=[],
            rejected=[RejectedArticle(record=records[0], issues=issues)],
        )

    monkeypatch.setattr(filter_module, "check_articles", always_rejects)

    result = await worker(config, store, ScriptedAIClient()).run(WEEKLY_PERIOD)

    assert result.articles == []
    assert result.exclusion_log[0]["除外区分"] == CATEGORY_FORMAT_ERROR
    assert result.validation.ok is False
    # 明細は validation 側にある（除外ログ6列に詰め込まない）。
    assert result.validation.errors[0].field == "合計スコア"


# --- 6) 整列・出力 -----------------------------------------------------------


async def test_the_records_are_sorted_by_total_score_desc(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """§8.1 の「合計スコア降順」。同点は収集順のまま（安定ソート）。"""
    write_articles(
        store,
        [
            article(title="低い記事", url="https://example.com/low"),
            article(title="高い記事", url="https://example.com/high"),
            article(title="中くらいの記事", url="https://example.com/mid"),
        ],
        WEEKLY_PERIOD,
    )
    client = ScriptedAIClient(
        classifications={
            "低い記事": classification(scores={"customer_relevance": 10}),
            "高い記事": classification(scores={"customer_relevance": 25}),
            "中くらいの記事": classification(scores={"customer_relevance": 20}),
        }
    )

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert cell(result.articles, "タイトル") == [
        "高い記事",
        "中くらいの記事",
        "低い記事",
    ]
    assert cell(result.articles, "合計スコア") == [86, 81, 71]


async def test_the_rows_follow_the_column_definition(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """列名・列順は T-07 の定義だけを見る（ここに写しを持たない）。"""
    write_articles(store, [article()], WEEKLY_PERIOD)

    result = await worker(config, store, ScriptedAIClient()).run(WEEKLY_PERIOD)

    assert list(result.articles[0]) == [
        column.name for column in WEEKLY_ARTICLE_COLUMNS
    ]
    assert len(result.article_rows()[0]) == len(WEEKLY_ARTICLE_COLUMNS)


async def test_the_exclusion_log_rows_use_the_six_columns(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    write_articles(store, [article()], WEEKLY_PERIOD)
    client = ScriptedAIClient(default_classification=classification(rules=[1]))

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert list(result.exclusion_log[0]) == [
        column.name for column in EXCLUSION_LOG_COLUMNS
    ]
    assert len(result.exclusion_log_rows()[0]) == len(EXCLUSION_LOG_COLUMNS)


async def test_the_validation_report_is_written_through_the_store(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """T-20 申し送り③：`validation_{period}.json` を T-02 経由で書く。"""
    write_articles(store, [article()], WEEKLY_PERIOD)

    result = await worker(config, store, ScriptedAIClient()).run(WEEKLY_PERIOD)

    assert result.validation_path == store.validation_path(WEEKLY_PERIOD)
    written = parse_validation_report(store.read_text(result.validation_path))
    assert written == result.validation


# --- 生成テキスト（決定3 ／ T-44）--------------------------------------------


async def test_the_narrative_is_written_through_the_store(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """`narrative_{period}.json` を T-02 経由で書く（xlsx の列には入れない）。"""
    write_articles(store, [article()], WEEKLY_PERIOD)

    result = await worker(config, store, ScriptedAIClient()).run(WEEKLY_PERIOD)

    assert result.narrative_path == store.narrative_path(WEEKLY_PERIOD)
    written = parse_narrative(
        store.read_text(result.narrative_path), period=result.period
    )
    assert written == result.narrative


async def test_the_generated_text_is_not_in_the_xlsx_columns(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """⚠️ 週次22列は §8.1 の確定値。生成テキストの正は別JSON（決定3）。"""
    write_articles(store, [article()], WEEKLY_PERIOD)

    result = await worker(config, store, ScriptedAIClient()).run(WEEKLY_PERIOD)

    assert list(result.articles[0]) == [
        column.name for column in WEEKLY_ARTICLE_COLUMNS
    ]
    assert POINT_OF_WEEK_SENTENCES[0] not in json.dumps(
        result.articles, ensure_ascii=False
    )


async def test_the_weekly_narrative_covers_the_adopted_articles(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """⚠️ 生成は**採用が確定した後**（落ちた記事の示唆を書かせない）。"""
    write_articles(
        store,
        [
            article(title="採用される記事", url="https://example.com/kept"),
            article(title="落ちる記事", url="https://example.com/dropped"),
        ],
        WEEKLY_PERIOD,
    )
    client = ScriptedAIClient(
        classifications={
            "採用される記事": classification(),
            "落ちる記事": classification(rules=[1]),
        }
    )

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert isinstance(result.narrative, WeeklyNarrativeDocument)
    assert list(result.narrative.insights) == ["https://example.com/kept"]
    assert result.narrative.point_of_week is not None


async def test_the_narrative_costs_one_round_trip_per_run(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """⚠️ 記事ごとに往復しない（採用11件でも生成テキストの往復は1回）。"""
    write_articles(
        store,
        [
            article(title="記事A", url="https://example.com/a"),
            article(title="記事B", url="https://example.com/b"),
            article(title="記事C", url="https://example.com/c"),
        ],
        WEEKLY_PERIOD,
    )
    client = ScriptedAIClient()

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert client.classification_calls == 3
    assert client.narrative_calls == 1
    assert len(result.narrative.insights) == 3


async def test_the_weekly_narrative_does_not_multiply_with_the_industries(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """T-52 Step 1：業界版の廃止で、週次の生成テキストは1本・1往復に戻った。"""
    config.tunable_thresholds.target_industries = ["不動産", "金融"]
    write_articles(store, [article()], WEEKLY_PERIOD)
    client = ScriptedAIClient()

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert isinstance(result.narrative, WeeklyNarrativeDocument)
    assert client.narrative_calls == 1
    assert result.narrative.point_of_week is not None


async def test_the_monthly_narrative_carries_editorial_intros_and_closing(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """§10.2-2／§10.2-4／§10.2-5 の生成テキストを1往復で作る。"""
    write_articles(store, [article()], MONTHLY_PERIOD)

    result = await worker(config, store, ScriptedAIClient()).run(MONTHLY_PERIOD)

    narrative = result.narrative
    assert isinstance(narrative, MonthlyNarrativeDocument)
    assert len(narrative.editorial_paragraphs) == 3
    assert len(narrative.closing_paragraphs) == 2
    assert list(narrative.chapter_intros) == [result.cases[0]["トピック(章)"]]


async def test_a_weekly_run_makes_no_monthly_narrative(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """週次実行に巻頭言は要らない（どちらか一方だけを作る）。"""
    write_articles(store, [article()], WEEKLY_PERIOD)
    client = ScriptedAIClient()

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert isinstance(result.narrative, WeeklyNarrativeDocument)
    assert client.narrative_calls == 1


async def test_an_empty_run_still_writes_a_narrative(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """採用0件でもファイルは書く（「無い」と「まだ作っていない」を分ける）。

    ⚠️ 空の narrative を渡された週刊は、`point_of_week_required=true` の config で
    レンダラが落ちる（T-24 の意図した挙動）。ここで黙って埋めない。
    """
    write_articles(store, [article()], WEEKLY_PERIOD)
    client = ScriptedAIClient(default_classification=classification(rules=[1]))

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert result.articles == []
    assert client.narrative_calls == 0
    assert store.exists(result.narrative_path)
    assert result.narrative.point_of_week is None


async def test_the_previous_narrative_is_archived_before_the_overwrite(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """設計判断B：正規名は上書き、旧版は `_history/` へ退避（run_id が要る）。"""
    write_articles(store, [article()], WEEKLY_PERIOD)
    subject = worker(config, store, ScriptedAIClient())

    first = await subject.run(WEEKLY_PERIOD, run_id="run-0001")
    before = store.read_text(first.narrative_path)
    await subject.run(WEEKLY_PERIOD, run_id="run-0002")

    generation = (
        store.history_root / WEEKLY_PERIOD / f"{first.config_revision}_run-0002"
    )
    assert (generation / first.narrative_path.name).read_text(
        encoding="utf-8"
    ) == before


async def test_without_a_run_id_nothing_is_archived(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """手元の単発実行（退避先のディレクトリ名を作れない）。書き込みは行う。"""
    write_articles(store, [article()], WEEKLY_PERIOD)
    subject = worker(config, store, ScriptedAIClient())

    await subject.run(WEEKLY_PERIOD)
    result = await subject.run(WEEKLY_PERIOD)

    assert store.exists(result.narrative_path)
    assert not store.history_root.exists()


async def test_the_narrative_prompt_version_is_reported(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """§9.2：生成テキストの往復も監査へ載る（`prompt_version` を持つ）。"""
    write_articles(store, [article()], WEEKLY_PERIOD)

    result = await worker(config, store, ScriptedAIClient()).run(WEEKLY_PERIOD)

    assert WEEKLY_NARRATIVE_PROMPT_VERSION in [
        meta.prompt_version for meta in result.ai_calls
    ]


async def test_the_narrative_feeds_the_weekly_renderer(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """T-24 は無変更で繋がる（filter の出力2つをそのまま渡すだけ）。"""
    write_articles(store, [article()], WEEKLY_PERIOD)

    result = await worker(config, store, ScriptedAIClient()).run(WEEKLY_PERIOD)

    assert isinstance(result.narrative, WeeklyNarrativeDocument)
    markup = render_weekly_html(
        period=WEEKLY_PERIOD,
        articles=result.articles,
        config=config,
        narrative=to_weekly_narrative(result.narrative),
    )

    assert POINT_OF_WEEK_SENTENCES[0] in markup
    assert "自社ではこう捉える" in markup


async def test_the_intermediate_xlsx_is_not_written_here(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """xlsx を書くのは T-22（この層は「何を書くか」までを決める）。"""
    write_articles(store, [article()], WEEKLY_PERIOD)

    await worker(config, store, ScriptedAIClient()).run(WEEKLY_PERIOD)

    assert not store.exists(store.weekly_report_path())
    assert not store.exists(store.monthly_cases_path())


# --- config@revision の固定参照（§6.3・§14）---------------------------------


async def test_the_config_is_pinned_for_the_whole_run(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """⚠️ 実行中に config が書き換わっても判断基準が動かないこと。

    ここでは「呼び出し元が同じオブジェクトを持ち回して書き換えた」場合を再現する。
    しきい値を100へ上げても、すでに固定した60で採否が決まる。
    """
    write_articles(store, [article()], WEEKLY_PERIOD)
    subject = worker(config, store, ScriptedAIClient())

    config.tunable_thresholds.min_total_score_to_publish = 100
    config.meta.revision = 999

    result = await subject.run(WEEKLY_PERIOD)

    assert len(result.articles) == 1
    assert result.config_revision == 1
    assert subject.config.tunable_thresholds.min_total_score_to_publish == 60


async def test_the_revision_is_reported_for_the_audit_log(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """§9.2：`prompt_version`（AI メタ）と `config.revision` を記録できる形で返す。"""
    write_articles(store, [article()], WEEKLY_PERIOD)

    result = await worker(config, store, ScriptedAIClient()).run(WEEKLY_PERIOD)

    assert result.config_revision == config.meta.revision
    # 記事1件ぶんの分類（0.2.0）＋生成テキスト1回（T-44）。
    assert [meta.prompt_version for meta in result.ai_calls] == [
        "0.2.0",
        WEEKLY_NARRATIVE_PROMPT_VERSION,
    ]


# --- 月次（事例昇格・章束ね・解説3段落）--------------------------------------


async def test_a_monthly_run_promotes_cases_and_numbers_them(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """採用記事のうち `enterprise_ai_case` を事例へ昇格し、`No` 昇順に並べる。"""
    write_articles(
        store,
        [
            article(title="事例A", url="https://example.com/a"),
            article(title="事例B", url="https://example.com/b", source="日経"),
        ],
        MONTHLY_PERIOD,
    )
    client = ScriptedAIClient(
        classifications={
            "事例A": classification(scores={"customer_relevance": 25}),
            "事例B": classification(scores={"customer_relevance": 24}),
        },
        cases={
            "事例A": case_draft(case_title="Aの事例", chapter_theme="業務自動化"),
            "事例B": case_draft(case_title="Bの事例", chapter_theme="業務自動化"),
        },
    )

    result = await worker(config, store, client).run(MONTHLY_PERIOD)

    assert cell(result.cases, "No") == [1, 2]
    assert cell(result.cases, "タイトル") == ["Aの事例", "Bの事例"]
    assert (
        cell(result.cases, "トピック(章)")
        == [CHAPTER_LABEL_FORMAT.format(number=1, title="業務自動化")] * 2
    )
    assert cell(result.cases, "掲載月") == [MONTHLY_PERIOD, MONTHLY_PERIOD]
    assert list(result.cases[0]) == [column.name for column in MONTHLY_CASE_COLUMNS]


async def test_the_case_commentary_is_three_paragraphs(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """§8.2：解説は ①事実 ②詳細 ③示唆 の3段落を `\\n\\n` で区切る。

    段落数は出力スキーマ（3つの別フィールド）で固定し、連結は T-07 が行う。
    """
    write_articles(store, [article()], MONTHLY_PERIOD)

    result = await worker(config, store, ScriptedAIClient()).run(MONTHLY_PERIOD)

    paragraphs = result.cases[0]["解説"]
    assert len(paragraphs) == 3
    column = {column.name: column for column in MONTHLY_CASE_COLUMNS}["解説"]
    assert format_cell(column, paragraphs) == PARAGRAPH_SEPARATOR.join(paragraphs)


async def test_the_case_source_is_built_from_the_collected_facts(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """⚠️ `出典` は AI に書かせない（媒体名も公開日も収集済みの事実）。"""
    write_articles(
        store, [article(source="ITmedia", published_at="2026-07-27")], MONTHLY_PERIOD
    )

    result = await worker(config, store, ScriptedAIClient()).run(MONTHLY_PERIOD)

    assert result.cases[0]["出典"] == "ITmedia（2026-07-27）"


async def test_only_the_case_category_is_promoted(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """「企業・組織の具体的活用事例」は config のカテゴリ `enterprise_ai_case`。"""
    write_articles(store, [article(title="規制の記事")], MONTHLY_PERIOD)
    client = ScriptedAIClient(
        default_classification=classification(
            tags={"information_category": "ai_governance_risk"}
        )
    )

    result = await worker(config, store, client).run(MONTHLY_PERIOD)

    assert len(result.articles) == 1  # 本編には残る
    assert result.cases == []  # 事例へは昇格しない


async def test_a_case_needs_the_configured_score(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """`monthly.min_score_for_case`（既定80）未満は昇格しない。"""
    write_articles(store, [article()], MONTHLY_PERIOD)
    # 合計 79（80 未満だが採用の 60 以上）。
    client = ScriptedAIClient(
        default_classification=classification(scores={"customer_relevance": 18})
    )

    result = await worker(config, store, client).run(MONTHLY_PERIOD)

    assert result.articles[0]["合計スコア"] == 79
    assert result.cases == []


async def test_a_weekly_run_has_no_cases(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """月次の事例は週次実行では作らない（AI も呼ばない）。"""
    write_articles(store, [article()], WEEKLY_PERIOD)
    client = ScriptedAIClient()

    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    assert result.cases == []
    # 往復は「記事1件の分類」＋「生成テキスト1回」だけ＝事例の往復が無い。
    assert client.classification_calls + client.narrative_calls == len(client.calls)


# --- 診断ログ（T-46 Step 2）--------------------------------------------------
# ⚠️ 診断は**ログにだけ**出す（xlsx の列も validation のスキーマも増やさない）。


def test_the_category_distribution_keeps_the_empty_categories(
    config: IntelligenceConfig,
) -> None:
    """⚠️ 0件のカテゴリを落とさない。

    初運用で問題になったのは「`enterprise_ai_case` が **0件**」という事実そのもの
    で、出現したカテゴリだけを並べるとそれがログから読めない。
    """
    records = [
        {"情報カテゴリ": "ai_agent_automation"},
        {"情報カテゴリ": "ai_agent_automation"},
        {"情報カテゴリ": "ai_governance_risk"},
    ]

    counts = category_distribution(records, config)

    assert counts["ai_agent_automation"] == 2
    assert counts["ai_governance_risk"] == 1
    assert counts["enterprise_ai_case"] == 0
    # config の7カテゴリを起点に数える（出現したものだけではない）。
    assert set(counts) == {
        str(category.id) for category in config.information_categories
    }


def test_the_category_distribution_is_ordered_by_count(
    config: IntelligenceConfig,
) -> None:
    records = [
        {"情報カテゴリ": "ai_governance_risk"},
        {"情報カテゴリ": "ai_agent_automation"},
        {"情報カテゴリ": "ai_agent_automation"},
    ]

    line = format_category_distribution(records, config)

    assert line.startswith("ai_agent_automation=2 / ai_governance_risk=1 / ")
    assert "enterprise_ai_case=0" in line


def test_the_score_distribution_reports_max_median_min() -> None:
    """§5.2 のしきい値に届く記事が構造的に無いことを実行ログから読めるように。"""
    records = [{"合計スコア": score} for score in (73, 66, 60)]

    assert format_score_distribution(records) == "max=73 / median=66 / min=60"


def test_the_median_of_an_even_number_of_articles_keeps_the_half() -> None:
    records = [{"合計スコア": score} for score in (73, 67, 66, 60)]

    assert format_score_distribution(records) == "max=73 / median=66.5 / min=60"


def test_the_score_distribution_of_an_empty_run_is_readable() -> None:
    assert format_score_distribution([]) == "スコアなし（採用0件）"


async def test_the_finished_log_reports_both_distributions(
    config: IntelligenceConfig,
    store: ArtifactStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    write_articles(store, [article()], WEEKLY_PERIOD)

    with caplog.at_level("INFO"):
        await worker(config, store, ScriptedAIClient()).run(WEEKLY_PERIOD)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "filter category distribution" in message and "enterprise_ai_case=1" in message
        for message in messages
    )
    assert any(
        "filter score distribution" in message and "max=83" in message
        for message in messages
    )


async def test_the_monthly_case_selection_breakdown_is_logged(
    config: IntelligenceConfig,
    store: ArtifactStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """3条件（カテゴリ / `min_score_for_case` / `target_case_count`）の内訳。

    初運用（2026-07）で事例0件になったとき、どの条件で落ちたのかを後から
    診断できなかった（採用記事の一覧はどの成果物にも残らない）。
    """
    config.tunable_thresholds.monthly.target_case_count = 1
    write_articles(
        store,
        [
            article(
                url="https://example.com/news/1",
                title="大手不動産が契約業務をAIで自動化",
            ),
            article(
                url="https://example.com/news/2",
                title="地方銀行、審査の一次判定に生成AIを試験導入",
            ),
            article(
                url="https://example.com/news/3",
                title="政府がAI事業者向けの新指針を公表",
            ),
        ],
        MONTHLY_PERIOD,
    )
    client = ScriptedAIClient(
        classifications={
            # 83点・enterprise_ai_case → 昇格候補
            "大手不動産が契約業務をAIで自動化": classification(),
            # 79点（`min_score_for_case`=80 未満だが採用の 60 以上）
            "地方銀行、審査の一次判定に生成AIを試験導入": classification(
                scores={"customer_relevance": 18}
            ),
            # 83点だが別カテゴリ
            "政府がAI事業者向けの新指針を公表": classification(
                tags={"information_category": "ai_governance_risk"}
            ),
        }
    )

    with caplog.at_level("INFO"):
        result = await worker(config, store, client).run(MONTHLY_PERIOD)

    assert len(result.cases) == 1
    breakdown = next(
        message
        for message in (record.getMessage() for record in caplog.records)
        if "monthly case selection" in message
    )
    assert "enterprise_ai_case=2" in breakdown  # カテゴリ該当
    assert ">=min_score_for_case(80)=1" in breakdown  # うちしきい値以上
    assert "dropped_by_target_case_count(1)=0" in breakdown
    assert "promoted=1" in breakdown


async def test_the_zero_case_warning_survives_the_breakdown_log(
    config: IntelligenceConfig,
    store: ArtifactStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """内訳（INFO）を足しても、事例0件の WARNING は残す。"""
    write_articles(store, [article()], MONTHLY_PERIOD)
    client = ScriptedAIClient(
        default_classification=classification(
            tags={"information_category": "ai_governance_risk"}
        )
    )

    with caplog.at_level("INFO"):
        result = await worker(config, store, client).run(MONTHLY_PERIOD)

    assert result.cases == []
    assert any(
        record.levelname == "WARNING" and "事例へ昇格できる記事がありません" in message
        for record, message in (
            (record, record.getMessage()) for record in caplog.records
        )
    )
    assert any(
        "monthly case selection" in record.getMessage() and record.levelname == "INFO"
        for record in caplog.records
    )


# --- T-22（中間xlsx ライタ）との接続 -----------------------------------------


async def test_the_result_rows_can_be_written_and_read_back(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """T-21 の出力がそのまま中間xlsx になり、読み戻すと同じ行に戻ること。

    ⚠️ 行の形（列名・multi の扱い）がライタと食い違っていたらここで落ちる。
    """
    write_articles(
        store,
        [
            article(title="採用される記事", url="https://example.com/kept"),
            article(title="落ちる記事", url="https://example.com/dropped"),
        ],
        WEEKLY_PERIOD,
    )
    client = ScriptedAIClient(
        classifications={
            "採用される記事": classification(),
            "落ちる記事": classification(rules=[1]),
        }
    )
    result = await worker(config, store, client).run(WEEKLY_PERIOD)

    reports = ReportStore(store)
    reports.write_weekly(
        period=WEEKLY_PERIOD,
        articles=result.articles,
        exclusions=result.exclusion_log,
        revision=result.config_revision,
        run_id="run-0001",
    )

    assert reports.read_weekly(WEEKLY_PERIOD) == result.articles
    assert reports.read_exclusions() == result.exclusion_log


async def test_the_report_store_can_serve_as_the_history_reader(
    config: IntelligenceConfig, store: ArtifactStore
) -> None:
    """T-18 申し送り①：履歴は中間xlsx から読み戻して組み立てる。

    先週のシートに載っている記事と同じ URL の記事は、今週は統合として外れる。
    """
    reports = ReportStore(store)
    write_articles(store, [article()], WEEKLY_PERIOD)
    write_articles(store, [article()], "2026-W30")

    last_week = await worker(config, store, ScriptedAIClient()).run("2026-W30")
    reports.write_weekly(
        period="2026-W30",
        articles=last_week.articles,
        exclusions=last_week.exclusion_log,
        revision=last_week.config_revision,
        run_id="run-0001",
    )

    this_week = await worker(
        config, store, ScriptedAIClient(), history_reader=reports
    ).run(WEEKLY_PERIOD)

    assert len(last_week.articles) == 1
    assert this_week.articles == []
    assert this_week.exclusion_log[0]["除外区分"] == CATEGORY_MERGED
