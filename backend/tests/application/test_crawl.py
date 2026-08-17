"""クローリング収集（T-16 ／ 設計書 §8.2 ／ 仕様書 §13.2（PROMPT-1））。

⚠️ **実際の `claude` は起動しない。** `AIClient`（T-15 のプロトコル）を
`FakeAIClient` へ差し替える。CI に CLI とログインを要求しない。

重点:

- PROMPT-1 が §13.2 の内容（優先ソース・7カテゴリ・週次/月次の重心・
  「この段階でやらない」）を持つこと。**7カテゴリは config から来る**
- **web 検索が実施されていない収集結果を受け取らない**（0 も「報告なし」も失敗。
  かつ**その場合ファイルを書かない**）
- 出力は T-06 のスキーマで検証され、`raw_articles_{period}.json` へ **T-02 経由**で
  書かれること。**重複も収集順もそのまま**
- タイムアウトは **crawl 用の30分**（分類・採点系の10分ではない）
"""

import copy
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from adapter.llm.ai_client import (
    AICallMeta,
    AIOutputParseError,
    AIProcessError,
    AIResult,
    OutputSchema,
    meta_to_audit_payload,
    resolve_output_adapter,
)
from adapter.storage.artifact_store import ArtifactStore
from application.usecases.crawl import (
    EXCLUDED_SOURCES,
    INDUSTRY_NOT_A_FILTER_NOTICE,
    MONTHLY_EMPHASIS,
    NO_DEDUP_NOTICE,
    NO_JUDGEMENT_NOTICE,
    PRIORITY_SOURCES,
    PROMPT_VERSION,
    WEEKLY_EMPHASIS,
    CrawlError,
    CrawlWorker,
    SearchNotPerformedError,
    build_crawl_prompt,
    ensure_search_was_performed,
    period_span,
)
from config import Settings
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.json_document import (
    DocumentParseError,
    parse_json_document,
)
from enterprise.entities.raw_article import (
    RAW_ARTICLES_ADAPTER,
    parse_raw_articles,
)

INITIAL_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)

WEEKLY_PERIOD = "2026-W31"
MONTHLY_PERIOD = "2026-07"
TODAY = date(2026, 8, 14)


@pytest.fixture(scope="session")
def initial_raw() -> dict[str, Any]:
    """仕様書 §5.2 の確定 config（xlsx 実データより生成された初期値）。"""
    return json.loads(INITIAL_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def config(initial_raw: dict[str, Any]) -> IntelligenceConfig:
    return IntelligenceConfig.model_validate(copy.deepcopy(initial_raw))


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path)


def article_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "collected_at": "2026-08-14",
        "published_at": "2026-07-28",
        "title": "大手不動産がAIエージェントで契約業務を自動化",
        "url": "https://example.com/news/1",
        "source": "ITmedia",
        "raw_summary": (
            "国内大手がAIエージェントを導入した。契約業務の一部を自動化した。"
        ),
        "region_hint": "日本",
        "primary_or_secondary": "報道",
    }
    payload.update(overrides)
    return payload


# --- AIClient のテストダブル -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Call:
    """`AIClient.complete()` に渡された引数（上位が何を渡したかの記録）。"""

    prompt: str
    output_schema: Any
    prompt_version: str | None
    timeout: float | None


class FakeAIClient:
    """`AIClient` プロトコルのテストダブル（サブプロセスを起動しない）。

    出力の検証は本物（`ClaudeCliClient`）と同じ経路（渡された出力スキーマで
    `parse_json_document`）を通し、スキーマ不一致は本物と同じ
    `AIOutputParseError` にする。
    """

    def __init__(
        self,
        payload: Any = None,
        *,
        web_search_requests: int | None = 3,
        meta: AICallMeta | None = None,
        error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.web_search_requests = web_search_requests
        self.meta = meta
        self.error = error
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
        if self.error is not None:
            raise self.error

        adapter = resolve_output_adapter(output_schema)
        text = json.dumps(self.payload, ensure_ascii=False)
        try:
            value = parse_json_document(adapter, text, label="AI 出力")
        except DocumentParseError as exc:
            raise AIOutputParseError(
                f"AI 出力がスキーマに一致しません — {exc}",
                attempts=1,
                issues=exc.issues,
                payload=text,
            ) from exc

        return AIResult(
            value=value,
            meta=self.meta
            or AICallMeta(
                requested_model="claude-opus-5",
                models_used=("claude-opus-5",),
                prompt_version=prompt_version,
                web_search_requests=self.web_search_requests,
            ),
        )


def build_worker(
    client: FakeAIClient,
    store: ArtifactStore,
    config: IntelligenceConfig,
    **kwargs: Any,
) -> CrawlWorker:
    return CrawlWorker(
        client=client,
        store=store,
        config=config,
        today=TODAY,
        settings=Settings(_env_file=None),
        **kwargs,
    )


# --- 収集と書き出し ---------------------------------------------------------


async def test_the_collected_articles_are_written_through_the_artifact_store(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    client = FakeAIClient([article_payload()])
    worker = build_worker(client, store, config)

    result = await worker.crawl(WEEKLY_PERIOD)

    assert result.path == store.raw_articles_path(WEEKLY_PERIOD)
    assert result.path.name == "raw_articles_2026-W31.json"
    written = parse_raw_articles(store.read_text(result.path))
    assert [a.title for a in written] == [a.title for a in result.articles]
    assert result.article_count == 1


async def test_the_output_is_validated_against_the_t06_schema(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """出力スキーマは `raw_articles.json`（T-06）そのもの。"""
    client = FakeAIClient([article_payload()])
    worker = build_worker(client, store, config)

    await worker.crawl(WEEKLY_PERIOD)

    assert client.calls[0].output_schema is RAW_ARTICLES_ADAPTER


async def test_output_that_breaks_the_schema_does_not_reach_the_artifact(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """スキーマ外の値（`region_hint` の enum 外）は書き出しまで届かない。"""
    client = FakeAIClient([article_payload(region_hint="関東")])
    worker = build_worker(client, store, config)

    with pytest.raises(AIOutputParseError):
        await worker.crawl(WEEKLY_PERIOD)

    assert not store.exists(store.raw_articles_path(WEEKLY_PERIOD))


async def test_duplicates_are_kept_because_merging_belongs_to_the_next_stage(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """⚠️ §13.2「重複しうる記事もこの段階では落とさず全て残す」。

    crawl が間引くと「同じ発表をどの媒体が報じたか」が失われ、代表記事の
    `ソース` 欄（`A / B(統合)`・§11.3）を組み立てられなくなる。
    """
    same_url = "https://example.com/news/1"
    client = FakeAIClient(
        [
            article_payload(url=same_url, source="ITmedia"),
            article_payload(url=same_url, source="Ledge.ai"),
            article_payload(url=same_url, source="ITmedia"),
        ]
    )
    worker = build_worker(client, store, config)

    result = await worker.crawl(WEEKLY_PERIOD)

    assert result.article_count == 3
    assert [a.source for a in parse_raw_articles(store.read_text(result.path))] == [
        "ITmedia",
        "Ledge.ai",
        "ITmedia",
    ]


async def test_the_collection_order_is_preserved(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    client = FakeAIClient([article_payload(title=f"記事{index}") for index in range(5)])
    worker = build_worker(client, store, config)

    result = await worker.crawl(WEEKLY_PERIOD)

    assert [a.title for a in result.articles] == [f"記事{index}" for index in range(5)]


async def test_an_empty_collection_is_still_written(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """0件は落とさない（§13.2 は件数を約束していない）。警告だけ出す。"""
    client = FakeAIClient([])
    worker = build_worker(client, store, config)

    result = await worker.crawl(WEEKLY_PERIOD)

    assert result.article_count == 0
    assert parse_raw_articles(store.read_text(result.path)) == []


async def test_the_call_metadata_is_carried_for_the_audit_log(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """使用モデル・`prompt_version`・検索回数は監査／validation メタへ載る（T-30）。"""
    client = FakeAIClient([article_payload()], web_search_requests=12)
    worker = build_worker(client, store, config)

    result = await worker.crawl(WEEKLY_PERIOD)

    payload = meta_to_audit_payload(result.meta)
    assert payload["prompt_version"] == PROMPT_VERSION
    assert payload["models_used"] == ["claude-opus-5"]
    assert payload["web_search_requests"] == 12


async def test_an_ai_failure_is_not_wrapped(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """⚠️ 原因ごとの例外（T-15）はそのまま通す。ジョブの再実行判断に使う。"""
    client = FakeAIClient(error=AIProcessError("boom", exit_code=1, stderr="/login"))
    worker = build_worker(client, store, config)

    with pytest.raises(AIProcessError):
        await worker.crawl(WEEKLY_PERIOD)


# --- web 検索が実施されたことの確認 ------------------------------------------


async def test_a_collection_without_any_web_search_is_rejected(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """⚠️ 検索なしの収集結果はモデルの記憶からの推測になりうる。受け取らない。"""
    client = FakeAIClient([article_payload()], web_search_requests=0)
    worker = build_worker(client, store, config)

    with pytest.raises(SearchNotPerformedError) as caught:
        await worker.crawl(WEEKLY_PERIOD)

    assert caught.value.requested == 0
    assert WEEKLY_PERIOD in str(caught.value)


async def test_the_artifact_is_not_written_when_the_search_did_not_happen(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """⚠️ 書き出しより**前**に確かめる（推測の結果をファイルに残さない）。"""
    client = FakeAIClient([article_payload()], web_search_requests=0)
    worker = build_worker(client, store, config)

    with pytest.raises(SearchNotPerformedError):
        await worker.crawl(WEEKLY_PERIOD)

    assert not store.exists(store.raw_articles_path(WEEKLY_PERIOD))


async def test_an_unreported_search_count_is_also_rejected(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """⚠️ 「報告が無い」を「検索した」に読み替えない（歯止めが黙って消える）。"""
    client = FakeAIClient([article_payload()], web_search_requests=None)
    worker = build_worker(client, store, config)

    with pytest.raises(SearchNotPerformedError) as caught:
        await worker.crawl(WEEKLY_PERIOD)

    assert caught.value.requested is None
    assert "確認できません" in str(caught.value)


async def test_a_collection_with_web_search_is_accepted(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    client = FakeAIClient([article_payload()], web_search_requests=1)
    worker = build_worker(client, store, config)

    result = await worker.crawl(WEEKLY_PERIOD)

    assert result.article_count == 1


def test_the_search_check_reads_the_provider_neutral_field() -> None:
    """API 実装へ差し替えるときも埋めるのは `AICallMeta.web_search_requests`。"""
    ensure_search_was_performed(
        AICallMeta(requested_model="any", web_search_requests=1), period=WEEKLY_PERIOD
    )

    with pytest.raises(SearchNotPerformedError):
        ensure_search_was_performed(
            AICallMeta(requested_model="any"), period=WEEKLY_PERIOD
        )


# --- タイムアウト -----------------------------------------------------------


async def test_crawl_uses_the_long_timeout_not_the_scoring_one(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """⚠️ crawl は30分（`AI_CRAWL_TIMEOUT_SECONDS`）。既定10分ではない。"""
    settings = Settings(_env_file=None)
    client = FakeAIClient([article_payload()])
    worker = build_worker(client, store, config)

    await worker.crawl(WEEKLY_PERIOD)

    assert client.calls[0].timeout == pytest.approx(settings.ai_crawl_timeout_seconds)
    assert client.calls[0].timeout != pytest.approx(settings.ai_timeout_seconds)
    assert worker.timeout_seconds >= 1800


async def test_the_caller_can_override_the_timeout(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    client = FakeAIClient([article_payload()])
    worker = build_worker(client, store, config, timeout=60)

    await worker.crawl(WEEKLY_PERIOD)

    assert client.calls[0].timeout == pytest.approx(60.0)


# --- period ------------------------------------------------------------------


def test_the_weekly_period_opens_into_an_iso_week() -> None:
    span = period_span("2026-W31")

    assert span.kind == "weekly"
    assert (span.start, span.end) == (date(2026, 7, 27), date(2026, 8, 2))
    assert span.start.weekday() == 0  # 月曜始まり（設計書 §0・§14）


def test_the_monthly_period_opens_into_a_calendar_month() -> None:
    span = period_span("2026-02")

    assert span.kind == "monthly"
    assert (span.start, span.end) == (date(2026, 2, 1), date(2026, 2, 28))


@pytest.mark.parametrize(
    "period",
    [
        "2026-13",  # 実在しない月
        "2026-00",
        "2025-W53",  # 2025 は53週を持たない（2026 は持つ）
        "2026W31",  # 表記違い
        "2026/07",
        "",
    ],
)
def test_an_impossible_period_is_rejected(period: str) -> None:
    """⚠️ 表記が合っているだけの period をプロンプトへ載せない（モデルが補う）。"""
    with pytest.raises(CrawlError):
        period_span(period)


async def test_a_bad_period_fails_before_the_ai_is_called(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    client = FakeAIClient([article_payload()])
    worker = build_worker(client, store, config)

    with pytest.raises(CrawlError):
        await worker.crawl("2026-13")

    assert client.calls == []


# --- PROMPT-1（仕様書 §13.2）-------------------------------------------------


def test_the_prompt_carries_the_priority_sources(config: IntelligenceConfig) -> None:
    prompt = build_crawl_prompt(WEEKLY_PERIOD, config, collected_at=TODAY)

    assert PRIORITY_SOURCES in prompt
    assert EXCLUDED_SOURCES in prompt
    for source in ("TechCrunch", "VentureBeat", "Ledge.ai", "ITmedia"):
        assert source in prompt


def test_the_prompt_covers_every_category_from_the_config(
    config: IntelligenceConfig,
) -> None:
    """⚠️ カテゴリは config が正（ここに名前を写すと admin の変更に追随しない）。"""
    prompt = build_crawl_prompt(WEEKLY_PERIOD, config, collected_at=TODAY)

    assert len(config.information_categories) == 7
    for category in config.information_categories:
        assert category.label in prompt
        assert category.id in prompt


def test_a_renamed_category_follows_into_the_prompt(
    initial_raw: dict[str, Any],
) -> None:
    raw = copy.deepcopy(initial_raw)
    raw["information_categories"][0]["label"] = "AI半導体動向"
    config = IntelligenceConfig.model_validate(raw)

    prompt = build_crawl_prompt(WEEKLY_PERIOD, config, collected_at=TODAY)

    assert "AI半導体動向" in prompt


def test_the_prompt_forbids_judging_at_this_stage(config: IntelligenceConfig) -> None:
    """§13.2「この段階でやらないこと」。採点・除外・タグ確定・重複除去はしない。"""
    prompt = build_crawl_prompt(WEEKLY_PERIOD, config, collected_at=TODAY)

    assert NO_JUDGEMENT_NOTICE in prompt
    assert NO_DEDUP_NOTICE in prompt


def test_the_weekly_prompt_asks_for_novelty(config: IntelligenceConfig) -> None:
    prompt = build_crawl_prompt(WEEKLY_PERIOD, config, collected_at=TODAY)

    assert WEEKLY_EMPHASIS in prompt
    assert MONTHLY_EMPHASIS not in prompt


def test_the_monthly_prompt_asks_for_concrete_cases(config: IntelligenceConfig) -> None:
    prompt = build_crawl_prompt(MONTHLY_PERIOD, config, collected_at=TODAY)

    assert MONTHLY_EMPHASIS in prompt
    assert WEEKLY_EMPHASIS not in prompt


def test_the_prompt_pins_the_period_dates_and_the_collection_date(
    config: IntelligenceConfig,
) -> None:
    """`2026-W31` の解釈と「今日」をモデルに補わせない。"""
    prompt = build_crawl_prompt(WEEKLY_PERIOD, config, collected_at=TODAY)

    assert WEEKLY_PERIOD in prompt
    assert "2026-07-27" in prompt
    assert "2026-08-02" in prompt
    assert str(TODAY) in prompt


def test_the_prompt_requires_the_use_of_web_search(config: IntelligenceConfig) -> None:
    """検索なしの収集は受け取らないので、プロンプトでも明示する。"""
    prompt = build_crawl_prompt(WEEKLY_PERIOD, config, collected_at=TODAY)

    assert "web 検索" in prompt
    assert "記憶や推測で記事・URL・公開日を書かない" in prompt


def test_the_prompt_lists_the_fields_of_the_t06_schema(
    config: IntelligenceConfig,
) -> None:
    prompt = build_crawl_prompt(WEEKLY_PERIOD, config, collected_at=TODAY)

    for field_name in (
        "collected_at",
        "published_at",
        "title",
        "url",
        "source",
        "raw_summary",
        "region_hint",
        "primary_or_secondary",
    ):
        assert field_name in prompt
    assert "2〜4文" in prompt  # §13.2 の客観要約
    assert "一次(公式)" in prompt  # 候補は T-06 の enum が正


def test_the_prompt_leaves_the_output_format_to_the_ai_client(
    config: IntelligenceConfig,
) -> None:
    """⚠️ 「JSON だけを出せ」＋ JSON Schema は `AIClient` の実装が付ける。

    ここに書くと二重指示になり、API 実装へ差し替えたときに片方だけ残る。
    """
    prompt = build_crawl_prompt(WEEKLY_PERIOD, config, collected_at=TODAY)

    assert "JSON Schema" not in prompt
    assert "JSON だけ" not in prompt


def test_the_prompt_asks_for_the_target_industry_without_narrowing(
    config: IntelligenceConfig,
) -> None:
    """T-46 Step 1：対象業界は「必ず含める」。**絞り込みの条件にはしない**。

    初運用（2026-W33）では対象業界を渡さなかった結果、収集の母集団に不動産の
    記事が1件も入らず、§9.2-3 の業界関連トピックが構造的に空になった。
    ⚠️ ここで絞ると、業界タグの確定（T-19）と除外（T-17）の材料が消える。
    """
    prompt = build_crawl_prompt(WEEKLY_PERIOD, config, collected_at=TODAY)

    for industry in config.tunable_thresholds.weekly.industries:
        assert industry in prompt
    assert "必ず収集対象に含める" in prompt
    assert INDUSTRY_NOT_A_FILTER_NOTICE in prompt
    # 網羅の指示は残っている（重点が置き換えになっていないこと）。
    for category in config.information_categories:
        assert category.label in prompt


def test_a_renamed_target_industry_follows_into_the_prompt(
    initial_raw: dict[str, Any],
) -> None:
    """⚠️ 業界名を写さず config から取る（admin の変更に追随する）。"""
    raw = copy.deepcopy(initial_raw)
    raw["tunable_thresholds"]["weekly"]["target_industries"] = ["医薬品"]
    config = IntelligenceConfig.model_validate(raw)

    prompt = build_crawl_prompt(WEEKLY_PERIOD, config, collected_at=TODAY)

    assert "医薬品" in prompt
    assert "不動産" not in prompt


def test_the_monthly_prompt_separates_user_cases_from_vendor_announcements(
    config: IntelligenceConfig,
) -> None:
    """T-46 Step 1：月次の「事例」は**導入企業側**（ベンダー発表ではない）。

    初運用（2026-07）では採用15件に `enterprise_ai_case` が0件で、集まったのは
    ベンダーの製品・モデル発表ばかりだった。
    """
    monthly = build_crawl_prompt(MONTHLY_PERIOD, config, collected_at=TODAY)
    weekly = build_crawl_prompt(WEEKLY_PERIOD, config, collected_at=TODAY)

    assert "導入した企業（ユーザー企業）側の事例" in monthly
    assert "活用事例として数えない" in monthly
    # ⚠️ 重心は週次・月次のどちらか一方だけ（両方出すと重み付けが消える）。
    assert "ユーザー企業" not in weekly


async def test_the_worker_sends_the_prompt_and_its_version(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    client = FakeAIClient([article_payload()])
    worker = build_worker(client, store, config)

    await worker.crawl(WEEKLY_PERIOD)

    assert client.calls[0].prompt == worker.build_prompt(WEEKLY_PERIOD)
    assert client.calls[0].prompt_version == PROMPT_VERSION
