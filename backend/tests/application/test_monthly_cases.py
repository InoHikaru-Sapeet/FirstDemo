"""月次の事例昇格・章束ね（T-21 ／ 仕様書 §8.2・§13.3 出力1）。

重点:

- **昇格の判定は決定的**（カテゴリ `enterprise_ai_case` ＋ config のしきい値・件数）
- 章は `chapter_count_hint` を超えたときだけ束ね直す（AI の往復を増やさない）
- **束ね直しの出力が欠けても事例を落とさない**（漏れたテーマは自分自身が章になる）
- `第N章` / `No` / `出典` / `掲載月` / 段落の連結は**アプリが決める**
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adapter.llm.ai_client import (
    AICallMeta,
    AIResult,
    OutputSchema,
    resolve_output_adapter,
)
from application.usecases.monthly_cases import (
    CASE_CATEGORY_ID,
    CaseCandidate,
    MonthlyCaseBuilder,
    MonthlyCaseError,
    build_case_prompt,
    build_chapter_prompt,
    build_chapter_schema,
    case_row,
    select_case_candidates,
    source_text_of,
)
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.json_document import parse_json_document
from enterprise.entities.raw_article import RawArticle
from enterprise.entities.report_columns import MONTHLY_CASE_COLUMNS

INITIAL_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)

PERIOD = "2026-07"


@pytest.fixture(scope="session")
def initial_raw() -> dict[str, Any]:
    return json.loads(INITIAL_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def config(initial_raw: dict[str, Any]) -> IntelligenceConfig:
    return IntelligenceConfig.model_validate(copy.deepcopy(initial_raw))


def article(title: str = "記事", published_at: str | None = "2026-07-27") -> RawArticle:
    return RawArticle(
        collected_at="2026-07-28",
        published_at=published_at,
        title=title,
        url=f"https://example.com/{title}",
        source="ITmedia",
        raw_summary="国内大手がAIを導入した。工数が半減した。",
        region_hint="日本",
        primary_or_secondary="報道",
    )


def candidate(title: str, *, total: int = 90) -> CaseCandidate:
    return CaseCandidate(
        article=article(title), total_score=total, summary="要約。もう1文。"
    )


def draft(theme: str, *, title: str | None = None) -> dict[str, Any]:
    return {
        "organizations": ["大手不動産"],
        "case_title": title or f"{theme}の事例",
        "chapter_theme": theme,
        "commentary_fact": "事実。",
        "commentary_detail": "詳細。",
        "commentary_implication": "示唆。",
    }


class ScriptedAIClient:
    """出力スキーマの形で用途を見分けるテストダブル（サブプロセスを起動しない）。"""

    def __init__(
        self,
        *,
        drafts: dict[str, dict[str, Any]] | None = None,
        chapters: dict[str, Any] | None = None,
    ) -> None:
        self.drafts = drafts or {}
        self.chapters = chapters
        self.prompts: list[str] = []
        self.chapter_calls = 0

    async def complete[T](
        self,
        *,
        prompt: str,
        output_schema: OutputSchema[T],
        prompt_version: str | None = None,
        timeout: float | None = None,
    ) -> AIResult[T]:
        self.prompts.append(prompt)
        fields = set(getattr(output_schema, "model_fields", {}))
        if "chapters" in fields:
            self.chapter_calls += 1
            if self.chapters is None:
                raise AssertionError("章の束ね直しは呼ばれない想定です")
            payload: dict[str, Any] = self.chapters
        else:
            payload = next(
                (value for title, value in self.drafts.items() if title in prompt),
                draft("既定テーマ"),
            )

        value = parse_json_document(
            resolve_output_adapter(output_schema),
            json.dumps(payload, ensure_ascii=False),
            label="AI 出力",
        )
        return AIResult(
            value=value,
            meta=AICallMeta(
                requested_model="claude-opus-5", prompt_version=prompt_version
            ),
        )


def builder(config: IntelligenceConfig, client: ScriptedAIClient) -> MonthlyCaseBuilder:
    return MonthlyCaseBuilder(client=client, config=config)


# --- 昇格の判定（決定的）-----------------------------------------------------


def record(category: str = CASE_CATEGORY_ID, total: int = 90) -> dict[str, Any]:
    return {"情報カテゴリ": category, "合計スコア": total}


def test_only_the_case_category_is_selected(config: IntelligenceConfig) -> None:
    records = [record(), record("ai_governance_risk"), record()]

    assert select_case_candidates(records, [article()] * 3, config) == [0, 2]


def test_the_score_threshold_comes_from_the_config(config: IntelligenceConfig) -> None:
    """`monthly.min_score_for_case`（既定80）。境界ちょうどは昇格する。"""
    records = [record(total=80), record(total=79)]

    assert select_case_candidates(records, [article()] * 2, config) == [0]


def test_the_count_is_capped_by_the_config(config: IntelligenceConfig) -> None:
    config.tunable_thresholds.monthly.target_case_count = 2
    records = [record(total=95), record(total=90), record(total=85)]

    assert select_case_candidates(records, [article()] * 3, config) == [0, 1]


def test_the_selection_is_ordered_by_total_score(config: IntelligenceConfig) -> None:
    records = [record(total=85), record(total=95), record(total=90)]

    assert select_case_candidates(records, [article()] * 3, config) == [1, 2, 0]


def test_mismatched_inputs_are_rejected(config: IntelligenceConfig) -> None:
    with pytest.raises(MonthlyCaseError):
        select_case_candidates([record()], [], config)


# --- 章の束ね直し ------------------------------------------------------------


async def test_the_chapters_are_not_regrouped_when_they_already_fit(
    config: IntelligenceConfig,
) -> None:
    """テーマ数が `chapter_count_hint` 以下なら **AI を呼ばない**（1回数分）。"""
    client = ScriptedAIClient(
        drafts={"記事1": draft("テーマ1"), "記事2": draft("テーマ2")},
    )

    cases = await builder(config, client).build(
        [candidate("記事1"), candidate("記事2")], period=PERIOD
    )

    assert client.chapter_calls == 0
    assert [case.chapter for case in cases] == ["第1章 テーマ1", "第2章 テーマ2"]


async def test_too_many_themes_are_regrouped(config: IntelligenceConfig) -> None:
    """テーマが `chapter_count_hint` より多いときだけ束ね直す。"""
    config.tunable_thresholds.monthly.chapter_count_hint = 2
    client = ScriptedAIClient(
        drafts={f"記事{n}": draft(f"テーマ{n}") for n in range(1, 4)},
        chapters={
            "chapters": [
                {"title": "自動化", "themes": ["テーマ1", "テーマ3"]},
                {"title": "ガバナンス", "themes": ["テーマ2"]},
            ]
        },
    )

    cases = await builder(config, client).build(
        [candidate(f"記事{n}") for n in range(1, 4)], period=PERIOD
    )

    assert client.chapter_calls == 1
    # 章順は「最初にその章が現れた事例の位置」。同じ章の事例は連続配置（§8.2）。
    assert [(case.no, case.chapter) for case in cases] == [
        (1, "第1章 自動化"),
        (2, "第1章 自動化"),
        (3, "第2章 ガバナンス"),
    ]


async def test_a_theme_left_out_of_the_grouping_keeps_itself(
    config: IntelligenceConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """⚠️ 割り当てから漏れたテーマがあっても**事例を落とさない**。

    章立ての網羅性を AI の出力に依存させると、事例そのものが消える形の失敗に
    なる。漏れたことはログに出す（静かに劣化させない）。
    """
    config.tunable_thresholds.monthly.chapter_count_hint = 1
    client = ScriptedAIClient(
        drafts={"記事1": draft("テーマ1"), "記事2": draft("テーマ2")},
        chapters={"chapters": [{"title": "自動化", "themes": ["テーマ1"]}]},
    )

    with caplog.at_level("WARNING"):
        cases = await builder(config, client).build(
            [candidate("記事1"), candidate("記事2")], period=PERIOD
        )

    assert [case.chapter for case in cases] == ["第1章 自動化", "第2章 テーマ2"]
    assert any("漏れた" in record.message for record in caplog.records)


async def test_a_theme_assigned_twice_keeps_the_first_chapter(
    config: IntelligenceConfig,
) -> None:
    """同じテーマが2つの章に現れても、事例が2つの章へ分かれない。"""
    config.tunable_thresholds.monthly.chapter_count_hint = 1
    client = ScriptedAIClient(
        drafts={"記事1": draft("テーマ1"), "記事2": draft("テーマ2")},
        chapters={
            "chapters": [
                {"title": "先の章", "themes": ["テーマ1", "テーマ2"]},
                {"title": "後の章", "themes": ["テーマ1"]},
            ]
        },
    )

    cases = await builder(config, client).build(
        [candidate("記事1"), candidate("記事2")], period=PERIOD
    )

    assert {case.chapter for case in cases} == {"第1章 先の章"}


def test_the_grouping_schema_only_allows_the_given_themes() -> None:
    """⚠️ テーマ名は `Literal`。言い換えた名前＝どの事例にも対応しない章を防ぐ。"""
    schema = build_chapter_schema(["テーマ1", "テーマ2"])

    schema.model_validate({"chapters": [{"title": "章", "themes": ["テーマ1"]}]})

    with pytest.raises(ValidationError):
        schema.model_validate({"chapters": [{"title": "章", "themes": ["テーマ3"]}]})


def test_the_grouping_schema_needs_themes() -> None:
    with pytest.raises(MonthlyCaseError):
        build_chapter_schema([])


# --- 行の組み立て -------------------------------------------------------------


async def test_the_row_uses_the_eight_columns(config: IntelligenceConfig) -> None:
    client = ScriptedAIClient(drafts={"記事": draft("テーマ", title="事例の見出し")})

    cases = await builder(config, client).build([candidate("記事")], period=PERIOD)
    row = cases[0].to_row()

    assert list(row) == [column.name for column in MONTHLY_CASE_COLUMNS]
    assert row["タイトル"] == "事例の見出し"
    assert row["掲載月"] == PERIOD
    assert row["解説"] == ["事実。", "詳細。", "示唆。"]
    assert len(case_row(cases[0])) == len(MONTHLY_CASE_COLUMNS)


async def test_duplicate_organizations_are_folded(config: IntelligenceConfig) -> None:
    """`企業・組織` 欄が `A・A` にならないこと（週次の multi タグと同じ扱い）。"""
    client = ScriptedAIClient(
        drafts={"記事": {**draft("テーマ"), "organizations": ["A社", "A社", "B社"]}}
    )

    cases = await builder(config, client).build([candidate("記事")], period=PERIOD)

    assert cases[0].organizations == ("A社", "B社")


def test_the_source_falls_back_to_the_media_name() -> None:
    """公開日が分からない記事は媒体名だけ（**収集日で代用しない**）。"""
    assert source_text_of(article(published_at="2026-07-27")) == "ITmedia（2026-07-27）"
    assert source_text_of(article(published_at=None)) == "ITmedia"


async def test_no_candidates_means_no_ai_call(config: IntelligenceConfig) -> None:
    client = ScriptedAIClient()

    assert await builder(config, client).build([], period=PERIOD) == []
    assert client.prompts == []


# --- プロンプト ---------------------------------------------------------------


def test_the_case_prompt_keeps_the_generated_fields_out(
    config: IntelligenceConfig,
) -> None:
    """`出典` / `掲載月` / `URL` / `No` はアプリが埋める（AI に書かせない）。"""
    prompt = build_case_prompt(candidate("記事"), config)

    assert "URL・出典・掲載月・通し番号は**書かない**" in prompt
    assert "記事に書かれている事実だけを使う" in prompt


def test_the_case_prompt_carries_the_chapter_hint(config: IntelligenceConfig) -> None:
    prompt = build_case_prompt(candidate("記事"), config)

    hint = config.tunable_thresholds.monthly.chapter_count_hint
    assert f"月全体で{hint}前後の章に束ねる" in prompt


def test_the_chapter_prompt_lists_the_themes() -> None:
    prompt = build_chapter_prompt(["テーマ1", "テーマ2"], 5)

    assert "5前後の章" in prompt
    assert "- テーマ1" in prompt
    assert "そのままの文字列" in prompt
