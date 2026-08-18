"""分類・10タグ付与・6軸採点（T-19 ／ 設計書 §6.1-3/4・§6.4 ／ 仕様書 §13.3-3/4）。

⚠️ **実際の `claude` は起動しない。** `AIClient`（T-15 のプロトコル）を
`FakeAIClient` へ差し替える。CI に CLI とログインを要求しない。

重点:

- **出力スキーマの enum は config から動的に生成**され、config 外の値は
  **構造的に出せない**
- **6軸の範囲は実行時の `weight`**（§5.2 では 25/20/20/15/10/10）で型に焼かれる
- **合計スコアは6軸の和をアプリ側が計算**し、LLM の申告値は受け取る口すら無い
- **`adoption_class` は `adoption_class_score_map` から決定的に決まり**、
  `low_priority` の記事は**採点→区分決定→降格**の順（§6.1）で1段下がる
- 除外判定・重複判定・フォーマットチェックの判断をこの層に持ち込まない
- **除外判定に要る事実（当たったルール番号・鮮度）は申告させるが、その先
  （除外か低優先か採用か）を返す口は作らない**（2026-08-16 の決定1）
"""

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from adapter.llm.ai_client import (
    AICallMeta,
    AIOutputParseError,
    AIProcessError,
    AIResult,
    OutputSchema,
    resolve_output_adapter,
)
from application.usecases.classify_and_score import (
    CONFIG_AUTHORITY_INSTRUCTION,
    FACTS_FIELD,
    IS_STALE_FIELD,
    MATCHED_RULES_FIELD,
    PROMPT_VERSION,
    SCORES_FIELD,
    SUMMARY_FIELD,
    SUMMARY_MAX_SENTENCES,
    SUMMARY_MIN_SENTENCES,
    TAGS_FIELD,
    AdoptionDecision,
    AnalyzedArticle,
    ArticleClassifier,
    ArticleFacts,
    ClassificationError,
    ClassifiedArticle,
    build_classification_prompt,
    build_classification_schema,
    decide_adoption,
    decide_adoption_class,
    tag_candidates,
    total_score,
)
from enterprise.entities.config import (
    REQUIRED_TAG_IDS,
    AdoptionClass,
    IntelligenceConfig,
    Severity,
)
from enterprise.entities.json_document import (
    DocumentParseError,
    parse_json_document,
)
from enterprise.entities.raw_article import RawArticle
from enterprise.entities.report_columns import (
    WEEKLY_ARTICLE_COLUMNS,
    axis_score_bounds,
)
from enterprise.services.exclusion import (
    ExclusionAction,
    ExclusionVerdict,
    ScreenedArticle,
    downgrade_adoption_class,
    evaluate_exclusions,
)
from enterprise.services.format_check import (
    MIN_SUMMARY_SENTENCES,
    allowed_values,
    check_article,
    count_sentences,
)

INITIAL_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)


@pytest.fixture(scope="session")
def initial_raw() -> dict[str, Any]:
    """仕様書 §5.2 の確定 config（xlsx 実データより生成された初期値）。"""
    return json.loads(INITIAL_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def raw(initial_raw: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(initial_raw)


@pytest.fixture
def config(raw: dict[str, Any]) -> IntelligenceConfig:
    return IntelligenceConfig.model_validate(raw)


@pytest.fixture
def article() -> RawArticle:
    return RawArticle(
        collected_at="2026-08-14",
        published_at="2026-08-12",
        title="大手不動産がAIエージェントで契約業務を自動化",
        url="https://example.com/news/1?utm_source=x",
        source="ITmedia",
        raw_summary="国内大手がAIエージェントを導入した。契約業務の一部を自動化した。",
        region_hint="日本",
        primary_or_secondary="報道",
    )


# --- AIClient のテストダブル -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Call:
    """`AIClient.complete()` に渡された引数（上位が何を渡したかの記録）。"""

    prompt: str
    output_schema: Any
    prompt_version: str | None
    timeout: float | None


class FakeAIClient:
    """`AIClient` プロトコルのテストダブル。

    ⚠️ **サブプロセスを起動しない。** 出力の検証は本物（`ClaudeCliClient`）と
    同じ経路（渡された出力スキーマで `parse_json_document`）を通し、スキーマ不一致は
    本物と同じ `AIOutputParseError` にする。
    """

    def __init__(
        self,
        payloads: list[dict[str, Any]] | None = None,
        *,
        meta: AICallMeta | None = None,
        error: Exception | None = None,
    ) -> None:
        self.payloads = list(payloads or [])
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
        payload = self.payloads.pop(0)
        try:
            value = parse_json_document(
                adapter, json.dumps(payload, ensure_ascii=False), label="AI 出力"
            )
        except DocumentParseError as exc:
            raise AIOutputParseError(
                f"AI 出力がスキーマに一致しません — {exc}",
                attempts=1,
                issues=exc.issues,
                payload=json.dumps(payload, ensure_ascii=False),
            ) from exc

        return AIResult(
            value=value,
            meta=self.meta
            or AICallMeta(
                requested_model="claude-opus-5",
                models_used=("claude-opus-5",),
                prompt_version=prompt_version,
            ),
        )


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

# 合計 22+17+15+12+9+8 = 83 → §5.2 のしきい値では「参考情報」（70〜84）。
VALID_SCORES: dict[str, int] = {
    "customer_relevance": 22,
    "practical_usability": 17,
    "market_impact": 15,
    "advisory_usability": 12,
    "reliability": 9,
    "urgency_freshness": 8,
}
VALID_TOTAL = 83


# 既定の事実申告＝「どの除外ルールにも当たらない・鮮度は低くない」（決定1）。
VALID_FACTS: dict[str, Any] = {
    MATCHED_RULES_FIELD: [],
    IS_STALE_FIELD: False,
}


def payload(
    *,
    tags: dict[str, Any] | None = None,
    scores: dict[str, Any] | None = None,
    summary: str = TWO_SENTENCE_SUMMARY,
    facts: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """LLM が返す想定の出力（既定は §5.2 の config に沿った妥当な1件）。"""
    return {
        TAGS_FIELD: {**VALID_TAGS, **(tags or {})},
        SCORES_FIELD: {**VALID_SCORES, **(scores or {})},
        SUMMARY_FIELD: summary,
        FACTS_FIELD: {**VALID_FACTS, **(facts or {})},
        **extra,
    }


def properties_of(schema: type[BaseModel], field: str) -> dict[str, Any]:
    """ネストしたモデル（`tags` / `scores`）の JSON Schema properties を引く。"""
    document = schema.model_json_schema()
    ref = document["properties"][field]["$ref"].removeprefix("#/$defs/")
    return document["$defs"][ref]["properties"]


def classifier(
    config: IntelligenceConfig,
    payloads: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> tuple[ArticleClassifier, FakeAIClient]:
    client = FakeAIClient(payloads if payloads is not None else [payload()])
    return ArticleClassifier(client=client, config=config, **kwargs), client


# --- 出力スキーマ：enum は config から動的に生成 -----------------------------


@pytest.mark.parametrize(
    "tag_id, enum_key",
    [
        ("industry", "industry"),
        ("business_area", "business_area"),
        ("info_type", "info_type"),
        ("region", "region"),
        ("reliability", "reliability"),
        ("customer_relevance", "customer_relevance"),
        ("practical_usability", "practical_usability"),
    ],
)
def test_the_enum_candidates_come_from_the_config_enums(
    config: IntelligenceConfig, tag_id: str, enum_key: str
) -> None:
    """候補は `config.enums.*` の**実行時の値**（順序も config のまま）。"""
    schema = build_classification_schema(config)

    field = properties_of(schema, TAGS_FIELD)[tag_id]
    is_array = field.get("type") == "array"
    enum_values = field["items"]["enum"] if is_array else field["enum"]

    assert enum_values == [str(value) for value in getattr(config.enums, enum_key)]


def test_the_information_category_candidates_are_the_seven_category_ids(
    config: IntelligenceConfig,
) -> None:
    """`information_category` の出どころは `enums` ではなく7カテゴリの ID（T-07）。"""
    schema = build_classification_schema(config)

    assert properties_of(schema, TAGS_FIELD)["information_category"]["enum"] == [
        category.id for category in config.information_categories
    ]


def test_adding_a_config_enum_value_changes_the_schema(raw: dict[str, Any]) -> None:
    """スキーマは config から**動的に**作られる（値を写し持っていない）。

    運用で増減するのは `enums.industry` / `enums.business_area`（自由文字列。T-04）。
    """
    raw["enums"]["industry"].append("宇宙開発")
    config = IntelligenceConfig.model_validate(raw)

    schema = build_classification_schema(config)

    assert (
        properties_of(schema, TAGS_FIELD)["industry"]["items"]["enum"][-1] == "宇宙開発"
    )
    schema.model_validate(payload(tags={"industry": ["宇宙開発"]}))


def test_a_value_outside_the_config_enums_cannot_be_produced(
    config: IntelligenceConfig,
) -> None:
    """⚠️ ここが緩むと config を唯一の基準とする §5.1 が崩れる。"""
    schema = build_classification_schema(config)

    with pytest.raises(ValidationError) as exc_info:
        schema.model_validate(payload(tags={"region": ["アジア"]}))

    assert "region" in str(exc_info.value)


def test_an_unknown_information_category_cannot_be_produced(
    config: IntelligenceConfig,
) -> None:
    schema = build_classification_schema(config)

    with pytest.raises(ValidationError):
        schema.model_validate(payload(tags={"information_category": "ai_other"}))


def test_the_free_text_tag_accepts_any_value(config: IntelligenceConfig) -> None:
    """`ai_theme` は `free_controlled`（T-07）なので候補で縛らない。"""
    schema = build_classification_schema(config)

    value = schema.model_validate(payload(tags={"ai_theme": ["RAG", "推論モデル"]}))

    assert getattr(value, TAGS_FIELD).ai_theme == ["RAG", "推論モデル"]
    assert "enum" not in properties_of(schema, TAGS_FIELD)["ai_theme"]["items"]


def test_the_free_text_tag_still_rejects_blank_values(
    config: IntelligenceConfig,
) -> None:
    """自由記述でも空白だけは通さない（§12.1「必須タグは非空」）。"""
    schema = build_classification_schema(config)

    with pytest.raises(ValidationError):
        schema.model_validate(payload(tags={"ai_theme": ["  "]}))


def test_multi_tags_require_at_least_one_value(config: IntelligenceConfig) -> None:
    """空配列は「欠落」（T-20 の `_is_empty` と同じ扱い）。構造で弾く。"""
    schema = build_classification_schema(config)

    for tag_id in ("ai_theme", "industry", "business_area", "region"):
        with pytest.raises(ValidationError):
            schema.model_validate(payload(tags={tag_id: []}))


def test_single_tags_do_not_accept_a_list(config: IntelligenceConfig) -> None:
    """`type=single` / `enum` のタグ（T-07）に複数値を入れさせない。"""
    schema = build_classification_schema(config)

    with pytest.raises(ValidationError):
        schema.model_validate(payload(tags={"info_type": ["主要メディア報道"]}))


def test_every_tag_is_required(config: IntelligenceConfig) -> None:
    """10必須タグのうち9つ（`adoption_class` を除く）が必須（§12.1）。"""
    schema = build_classification_schema(config)
    tags = dict(VALID_TAGS)
    tags.pop("industry")

    with pytest.raises(ValidationError):
        schema.model_validate(
            {TAGS_FIELD: tags, SCORES_FIELD: VALID_SCORES, SUMMARY_FIELD: "a。b。"}
        )


def test_the_candidates_match_what_the_format_check_allows(
    config: IntelligenceConfig,
) -> None:
    """⚠️ T-19 の候補と T-20 の enum 検査（§12.1）が食い違わないこと。

    片方だけ広い／狭いと、「生成した値が検証で落ちる」または「検証を通る
    config 外の値が作れる」ようになる。
    """
    by_tag = {tag.id: tag for tag in config.required_tags}

    for column in WEEKLY_ARTICLE_COLUMNS:
        if column.tag_id is None or column.tag_id == "adoption_class":
            continue
        candidates = tag_candidates(by_tag[column.tag_id], config)
        allowed = allowed_values(column, config)
        if allowed is None:
            assert candidates is None, column.tag_id
        else:
            assert candidates is not None and set(candidates) == allowed, column.tag_id


def test_tag_candidates_refuses_an_unreadable_value_source(
    config: IntelligenceConfig,
) -> None:
    """未知の `value_source` を黙って自由記述へ落とさない（config 外の値の穴）。"""
    tag = config.required_tags[0].model_copy(update={"value_source": "somewhere.else"})

    with pytest.raises(ClassificationError):
        tag_candidates(tag, config)


def test_an_empty_enum_cannot_become_a_schema(raw: dict[str, Any]) -> None:
    """候補が空の enum は `Literal` にできない＝config 外の値を防げないので落とす。"""
    raw["enums"]["industry"] = []
    config = IntelligenceConfig.model_validate(raw)

    with pytest.raises(ClassificationError):
        build_classification_schema(config)


# --- 出力スキーマ：LLM に決めさせないもの -----------------------------------


def test_the_adoption_class_has_no_field_in_the_schema(
    config: IntelligenceConfig,
) -> None:
    """§6.4 の決定はアプリ側。LLM は申告する口を持たない。"""
    schema = build_classification_schema(config)

    assert "adoption_class" not in properties_of(schema, TAGS_FIELD)

    with pytest.raises(ValidationError):
        schema.model_validate(payload(tags={"adoption_class": "次回定例で提案"}))


def test_a_claimed_total_score_is_rejected(config: IntelligenceConfig) -> None:
    """「合計スコアは78点です」を受け取る口を作らない（`extra="forbid"`）。"""
    schema = build_classification_schema(config)

    with pytest.raises(ValidationError):
        schema.model_validate(payload(total_score=78))
    with pytest.raises(ValidationError):
        schema.model_validate(payload(scores={"total": 78}))


def test_the_summary_is_required_and_not_blank(config: IntelligenceConfig) -> None:
    schema = build_classification_schema(config)

    with pytest.raises(ValidationError):
        schema.model_validate(payload(summary="   "))
    with pytest.raises(ValidationError):
        schema.model_validate({TAGS_FIELD: VALID_TAGS, SCORES_FIELD: VALID_SCORES})


# --- 事実の申告（2026-08-16 の決定1＝要確認事項 #10）------------------------


def test_the_facts_carry_no_verdict(config: IntelligenceConfig) -> None:
    """申告できるのは**ルール番号と鮮度だけ**。判断そのものは返させない。

    ⚠️ このテストが落ちたときは「口を増やしてよいか」を先に考えること。
    severity・除外可否・採否を返せるようにした時点で、T-17 の決定的な分岐は
    LLM に上書きされる（`exclusion.ScreenedArticle` の docstring と同じ境界）。
    """
    schema = build_classification_schema(config)

    assert set(properties_of(schema, FACTS_FIELD)) == {
        MATCHED_RULES_FIELD,
        IS_STALE_FIELD,
    }

    for claimed in (
        {"severity": "full_exclude"},
        {"should_exclude": True},
        {"exclusion_category": "完全除外"},
        {"adoption_class": "不採用"},
    ):
        with pytest.raises(ValidationError):
            schema.model_validate(payload(facts=claimed))


def test_only_rule_numbers_that_exist_in_the_config_can_be_declared(
    config: IntelligenceConfig,
) -> None:
    """候補は `exclusion_rules[].no` の実値（`Literal`）。範囲外は構造的に出せない。"""
    schema = build_classification_schema(config)

    value = schema.model_validate(payload(facts={MATCHED_RULES_FIELD: [1, 13]}))
    assert getattr(value, FACTS_FIELD).matched_exclusion_rule_nos == [1, 13]

    for out_of_range in ([0], [14], ["1"]):
        with pytest.raises(ValidationError):
            schema.model_validate(payload(facts={MATCHED_RULES_FIELD: out_of_range}))


def test_declaring_no_rule_is_valid(config: IntelligenceConfig) -> None:
    """「どれにも当たらない」は空配列（§6.2 の `action=keep`）。"""
    schema = build_classification_schema(config)

    value = schema.model_validate(payload(facts={MATCHED_RULES_FIELD: []}))

    assert getattr(value, FACTS_FIELD).matched_exclusion_rule_nos == []


def test_the_stale_flag_does_not_accept_a_string(config: IntelligenceConfig) -> None:
    """`strict=True`：`"true"` / 1 を真と読み替えない（軸点と同じ扱い）。"""
    schema = build_classification_schema(config)

    for claimed in ("true", 1, "はい"):
        with pytest.raises(ValidationError):
            schema.model_validate(payload(facts={IS_STALE_FIELD: claimed}))


async def test_the_declared_facts_reach_the_result(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """申告は `ArticleFacts` として返り、そのまま T-17 の入力になる。"""
    subject, _ = classifier(
        config,
        [
            payload(
                facts={MATCHED_RULES_FIELD: [3, 1, 3], IS_STALE_FIELD: True},
            )
        ],
    )

    analyzed = await subject.analyze(article)

    # 重複した番号は畳む（`frozenset`）。順序は判定に影響しない（T-17 は no 昇順）。
    assert analyzed.facts == ArticleFacts(
        matched_rule_nos=frozenset({1, 3}), is_stale=True
    )


async def test_the_facts_survive_into_the_classified_article(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """監査で「なぜそのルールに当たったか」を追えるよう、結果にも残す。"""
    subject, _ = classifier(config, [payload(facts={MATCHED_RULES_FIELD: [11]})])

    classified = await subject.classify(article)

    assert classified.facts.matched_rule_nos == frozenset({11})


# --- 出力スキーマ：6軸の範囲を型で拘束 --------------------------------------


def test_the_axis_bounds_are_the_spec_weights(config: IntelligenceConfig) -> None:
    """§5.2 / T-19 完了条件の 25/20/20/15/10/10（＝各軸の `weight`）。"""
    schema = build_classification_schema(config)
    scores = properties_of(schema, SCORES_FIELD)

    bounds = {
        axis_id: (scores[axis_id]["minimum"], scores[axis_id]["maximum"])
        for axis_id in scores
    }

    assert bounds == axis_score_bounds(config)
    assert bounds == {
        "customer_relevance": (0, 25),
        "practical_usability": (0, 20),
        "market_impact": (0, 20),
        "advisory_usability": (0, 15),
        "reliability": (0, 10),
        "urgency_freshness": (0, 10),
    }


def test_the_axis_bound_follows_the_config_weight(raw: dict[str, Any]) -> None:
    """⚠️ 上限は**実行時の `weight`**（T-20 と同じ `axis_score_bounds`）。

    静的な `value_range`（T-07）を見ていたら、weight を変えたのに上限が旧値のまま
    になる。
    """
    for axis in raw["scoring_axes"]:
        if axis["id"] == "customer_relevance":
            axis["weight"] = 30
        elif axis["id"] == "urgency_freshness":
            axis["weight"] = 5
    config = IntelligenceConfig.model_validate(raw)

    schema = build_classification_schema(config)

    schema.model_validate(
        payload(scores={"customer_relevance": 30, "urgency_freshness": 5})
    )
    with pytest.raises(ValidationError):
        schema.model_validate(payload(scores={"customer_relevance": 31}))
    with pytest.raises(ValidationError):
        schema.model_validate(payload(scores={"urgency_freshness": 6}))


def test_a_score_outside_the_range_is_rejected(config: IntelligenceConfig) -> None:
    schema = build_classification_schema(config)

    with pytest.raises(ValidationError):
        schema.model_validate(payload(scores={"customer_relevance": 26}))
    with pytest.raises(ValidationError):
        schema.model_validate(payload(scores={"customer_relevance": -1}))


def test_a_boolean_is_not_read_as_a_point(config: IntelligenceConfig) -> None:
    """`true` を 1 点と読み替えない（T-20 の `_is_integer` と同じ立場）。"""
    schema = build_classification_schema(config)

    with pytest.raises(ValidationError):
        schema.model_validate(payload(scores={"reliability": True}))


def test_a_non_integer_score_is_rejected(config: IntelligenceConfig) -> None:
    """`9.5` / `"9"` を通すと「整数ではない点数」が xlsx まで流れる。"""
    schema = build_classification_schema(config)

    for value in (9.5, "9"):
        with pytest.raises(ValidationError):
            schema.model_validate(payload(scores={"reliability": value}))


def test_all_six_axes_are_required(config: IntelligenceConfig) -> None:
    schema = build_classification_schema(config)
    scores = dict(VALID_SCORES)
    scores.pop("market_impact")

    with pytest.raises(ValidationError):
        schema.model_validate(
            {
                TAGS_FIELD: VALID_TAGS,
                SCORES_FIELD: scores,
                SUMMARY_FIELD: TWO_SENTENCE_SUMMARY,
            }
        )


def test_an_unknown_axis_is_rejected(config: IntelligenceConfig) -> None:
    schema = build_classification_schema(config)

    with pytest.raises(ValidationError):
        schema.model_validate(payload(scores={"brand_fit": 5}))


# --- 合計スコアはアプリ側が6軸を合算する ------------------------------------


def test_the_total_is_the_sum_of_the_six_axes(config: IntelligenceConfig) -> None:
    assert total_score(VALID_SCORES, config) == VALID_TOTAL
    assert VALID_TOTAL == sum(VALID_SCORES.values())


def test_the_total_uses_every_axis(config: IntelligenceConfig) -> None:
    """1軸でも落とすと合計が変わる＝全軸が式に入っていること。"""
    for axis_id in VALID_SCORES:
        bumped = {**VALID_SCORES, axis_id: VALID_SCORES[axis_id] + 1}
        assert total_score(bumped, config) == VALID_TOTAL + 1


def test_the_total_refuses_a_missing_axis(config: IntelligenceConfig) -> None:
    """黙って和を取ると「5軸の合計」が合計スコアとして通る。"""
    scores = {key: value for key, value in VALID_SCORES.items() if key != "reliability"}

    with pytest.raises(ClassificationError):
        total_score(scores, config)


def test_the_total_refuses_an_unknown_axis(config: IntelligenceConfig) -> None:
    with pytest.raises(ClassificationError):
        total_score({**VALID_SCORES, "brand_fit": 3}, config)


async def test_a_claimed_total_never_reaches_the_application(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """LLM が合計を申告した出力は**パースに失敗する**（本物も同じ経路）。"""
    subject, _ = classifier(config, [payload(total_score=100)])

    with pytest.raises(AIOutputParseError):
        await subject.classify(article)


# --- adoption_class は config のしきい値から決定的に決める（§6.4）-----------


def test_the_adoption_class_boundaries_follow_the_config_map(
    config: IntelligenceConfig,
) -> None:
    """境界はすべて `≥`（しきい値ちょうどは上の区分）。値は config から引く。"""
    score_map = config.tunable_thresholds.adoption_class_score_map

    cases: list[tuple[int, AdoptionClass]] = [
        (100, "次回定例で提案"),
        (score_map.propose_next_meeting, "次回定例で提案"),
        (score_map.propose_next_meeting - 1, "参考情報"),
        (score_map.reference_info, "参考情報"),
        (score_map.reference_info - 1, "共有のみ"),
        (score_map.share_only, "共有のみ"),
        (score_map.share_only - 1, "不採用"),
        (0, "不採用"),
    ]

    for total, expected in cases:
        assert decide_adoption_class(total, config) == expected, total


def test_the_spec_thresholds_are_the_ones_in_use(config: IntelligenceConfig) -> None:
    """§5.2 の確定値（85 / 70 / 60）での実際の区分。"""
    assert decide_adoption_class(85, config) == "次回定例で提案"
    assert decide_adoption_class(84, config) == "参考情報"
    assert decide_adoption_class(70, config) == "参考情報"
    assert decide_adoption_class(69, config) == "共有のみ"
    assert decide_adoption_class(60, config) == "共有のみ"
    assert decide_adoption_class(59, config) == "不採用"


def test_changing_the_config_map_changes_the_class(raw: dict[str, Any]) -> None:
    """しきい値は admin が編集できる（§7.2）。ハードコードしていないこと。"""
    raw["tunable_thresholds"]["adoption_class_score_map"] = {
        "propose_next_meeting": 70,
        "reference_info": 50,
        "share_only": 30,
    }
    config = IntelligenceConfig.model_validate(raw)

    assert decide_adoption_class(70, config) == "次回定例で提案"
    assert decide_adoption_class(59, config) == "参考情報"
    assert decide_adoption_class(30, config) == "共有のみ"
    assert decide_adoption_class(29, config) == "不採用"


def test_the_decision_carries_the_total_and_the_class(
    config: IntelligenceConfig,
) -> None:
    decision = decide_adoption(VALID_SCORES, config)

    assert decision == AdoptionDecision(
        total_score=VALID_TOTAL,
        scored_class="参考情報",
        adoption_class="参考情報",
    )
    assert not decision.is_downgraded


# --- ★ T-17 の降格関数との連携（§6.1 の順序）-------------------------------


def low_priority_verdict() -> ExclusionVerdict:
    return ExclusionVerdict(
        action=ExclusionAction.LOW_PRIORITY, rule_no=8, rule_name="一般論のみの記事"
    )


def test_a_low_priority_verdict_downgrades_one_step(
    config: IntelligenceConfig,
) -> None:
    """§5.4「採用はするが `adoption_class` を下げる」。降格幅は T-17 が持つ。"""
    decision = decide_adoption(VALID_SCORES, config, verdict=low_priority_verdict())

    assert decision.scored_class == "参考情報"
    assert decision.adoption_class == "共有のみ"
    assert decision.is_downgraded


def test_the_order_matches_the_pseudocode(config: IntelligenceConfig) -> None:
    """⚠️ **採点 → 合算 → 区分決定 → 降格** の順（設計書 §6.1 の 4）。

    90点の `low_priority` 記事は「次回定例で提案」を決めてから1段下げた
    **参考情報**になる。順序を組み替えた実装（降格してから区分を決め直す・
    しきい値をずらして代用する）ではこの値にならない。
    """
    scores = {**VALID_SCORES, "customer_relevance": 25, "practical_usability": 20}
    total = sum(scores.values())
    score_map = config.tunable_thresholds.adoption_class_score_map
    assert total >= score_map.propose_next_meeting

    decision = decide_adoption(scores, config, verdict=low_priority_verdict())

    assert decision.total_score == total
    assert decision.scored_class == "次回定例で提案"
    assert decision.adoption_class == "参考情報"


def test_the_downgrade_is_the_t17_function(config: IntelligenceConfig) -> None:
    """降格の実装を写し持たない（T-17 の `downgrade_adoption_class` を呼ぶ）。"""
    for customer_relevance in range(0, 26):
        scores = {**VALID_SCORES, "customer_relevance": customer_relevance}
        decision = decide_adoption(scores, config, verdict=low_priority_verdict())

        assert decision.adoption_class == downgrade_adoption_class(
            decide_adoption_class(decision.total_score, config)
        )


def test_the_downgrade_never_reaches_not_adopted(config: IntelligenceConfig) -> None:
    """下限は `共有のみ`（降格で `不採用` にはしない。§5.4「採用はする」）。"""
    share_only = config.tunable_thresholds.adoption_class_score_map.share_only
    scores = {**VALID_SCORES, "customer_relevance": 0}
    while sum(scores.values()) > share_only:
        scores = {**scores, "practical_usability": scores["practical_usability"] - 1}

    decision = decide_adoption(scores, config, verdict=low_priority_verdict())

    assert decision.scored_class == "共有のみ"
    assert decision.adoption_class == "共有のみ"
    assert not decision.is_downgraded


def test_a_score_below_the_map_stays_not_adopted(config: IntelligenceConfig) -> None:
    scores = dict.fromkeys(VALID_SCORES, 0)

    decision = decide_adoption(scores, config, verdict=low_priority_verdict())

    assert decision.adoption_class == "不採用"


@pytest.mark.parametrize("action", [ExclusionAction.KEEP, ExclusionAction.MERGE])
def test_other_verdicts_do_not_downgrade(
    config: IntelligenceConfig, action: ExclusionAction
) -> None:
    """降格するのは `low_priority` だけ（`merge` は統合の話・T-18）。"""
    decision = decide_adoption(
        VALID_SCORES, config, verdict=ExclusionVerdict(action=action)
    )

    assert decision.adoption_class == "参考情報"
    assert not decision.is_downgraded


def test_no_verdict_does_not_downgrade(config: IntelligenceConfig) -> None:
    assert decide_adoption(VALID_SCORES, config, verdict=None).adoption_class == (
        "参考情報"
    )


def test_an_excluded_verdict_is_refused(config: IntelligenceConfig) -> None:
    """除外された記事は §6.1 で `continue` される＝ここへ来ない。"""
    verdict = ExclusionVerdict(
        action=ExclusionAction.EXCLUDE,
        rule_no=1,
        rule_name="真偽不明の噂",
        category="完全除外",
        reason="真偽不明の噂",
    )

    with pytest.raises(ClassificationError):
        decide_adoption(VALID_SCORES, config, verdict=verdict)


async def test_an_excluded_article_is_not_sent_to_the_ai(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """AI を呼ぶ前に落とす（1回あたり数分かかるので無駄打ちしない）。"""
    subject, client = classifier(config)
    verdict = ExclusionVerdict(
        action=ExclusionAction.EXCLUDE, category="完全除外", reason="真偽不明の噂"
    )

    with pytest.raises(ClassificationError):
        await subject.classify(article, verdict=verdict)

    assert client.calls == []


# --- analyze() / finalize()：決定1 の順序（AI → 除外判定 → 区分決定）--------


async def test_analyze_does_not_decide_the_adoption_class(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """AI 呼び出しの時点では判定がまだ無いので、区分も降格も当てない。

    区分を先に決めてから降格を当て直すと、§5.4 の「採用はするが下げる」から
    外れる（`decide_adoption()` が順序を1本で持つ理由）。
    """
    subject, _ = classifier(config)

    analyzed = await subject.analyze(article)

    assert "adoption_class" not in analyzed.tags
    assert not hasattr(analyzed, "adoption")


async def test_finalize_applies_the_downgrade_after_the_verdict(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """事実の申告 → 除外判定 → 区分決定＋降格、が1周つながること（決定1の順序）。

    ルール11（`low_priority`）に当たったと申告された記事は、区分を決めてから
    1段下がる（§6.1 の 4）。
    """
    subject, client = classifier(config, [payload(facts={MATCHED_RULES_FIELD: [11]})])

    analyzed = await subject.analyze(article)
    screened = ScreenedArticle(
        article=article, matched_rule_nos=analyzed.facts.matched_rule_nos
    )
    verdict = evaluate_exclusions(screened, config)
    classified = subject.finalize(analyzed, verdict=verdict)

    assert verdict.action is ExclusionAction.LOW_PRIORITY
    assert classified.adoption.scored_class == "参考情報"
    assert classified.adoption_class == "共有のみ"
    # ⚠️ 降格のために AI をもう一度呼ばない（申告は最初の1往復に含まれている）。
    assert len(client.calls) == 1


async def test_finalize_does_not_call_the_ai(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """`finalize()` は決定的な計算だけ（合算・区分決定・降格）。"""
    subject, client = classifier(config)

    analyzed = await subject.analyze(article)
    subject.finalize(analyzed)
    subject.finalize(analyzed)

    assert len(client.calls) == 1


def test_finalize_refuses_an_excluded_verdict(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """除外された記事に採用区分を付けない（本編と除外ログの両方に載る事故）。"""
    subject, _ = classifier(config)
    analyzed = AnalyzedArticle(
        article=article,
        tags={},
        scores=dict(VALID_SCORES),
        summary=TWO_SENTENCE_SUMMARY,
        facts=ArticleFacts(),
        meta=AICallMeta(requested_model="claude-opus-5"),
    )

    with pytest.raises(ClassificationError):
        subject.finalize(
            analyzed,
            verdict=ExclusionVerdict(
                action=ExclusionAction.EXCLUDE,
                category="完全除外",
                reason="真偽不明の噂",
            ),
        )


# --- classify()：AIClient 経由の1本 -----------------------------------------


async def test_the_client_receives_the_prompt_schema_and_version(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """上位が渡すのはプロンプトと出力スキーマだけ（T-15 のプロトコル）。"""
    subject, client = classifier(config)

    await subject.classify(article)

    call = client.calls[0]
    assert call.prompt == subject.build_prompt(article)
    assert call.output_schema is subject.output_schema
    assert call.prompt_version == PROMPT_VERSION


async def test_the_timeout_defaults_to_the_client_default(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """⚠️ 分類・採点は既定（`ai_timeout_seconds`=10分）。crawl の30分は渡さない。"""
    subject, client = classifier(config)

    await subject.classify(article)

    assert client.calls[0].timeout is None


async def test_an_explicit_timeout_is_passed_through(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    subject, client = classifier(config, timeout=123.0)

    await subject.classify(article)

    assert client.calls[0].timeout == pytest.approx(123.0)


async def test_the_schema_is_built_once_per_classifier(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """記事ごとに作り直さない（週あたり数十件を回すので無駄にしない）。"""
    subject, client = classifier(config, [payload(), payload()])

    await subject.classify(article)
    await subject.classify(article)

    assert client.calls[0].output_schema is client.calls[1].output_schema


async def test_all_ten_required_tags_come_back_filled(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """LLM の9つ ＋ アプリ側が決めた `adoption_class` で10タグ（§12.1）。"""
    subject, _ = classifier(config)

    classified = await subject.classify(article)

    assert set(classified.tags) == set(REQUIRED_TAG_IDS)
    assert all(classified.tags[tag_id] for tag_id in REQUIRED_TAG_IDS)
    assert classified.tags["adoption_class"] == "参考情報"


async def test_the_result_carries_the_computed_total_and_class(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    subject, _ = classifier(config)

    classified = await subject.classify(article)

    assert classified.scores == VALID_SCORES
    assert classified.total_score == VALID_TOTAL
    assert classified.adoption_class == "参考情報"
    assert classified.article is article


async def test_the_low_priority_downgrade_applies_through_classify(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    subject, _ = classifier(config)

    classified = await subject.classify(article, verdict=low_priority_verdict())

    assert classified.adoption.scored_class == "参考情報"
    assert classified.adoption_class == "共有のみ"
    assert classified.tags["adoption_class"] == "共有のみ"


async def test_multi_tags_become_tuples_without_duplicates(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """`業界` 欄が `不動産;不動産` にならないように畳む（順序は出力順のまま）。"""
    subject, _ = classifier(
        config, [payload(tags={"industry": ["不動産", "業界横断", "不動産"]})]
    )

    classified = await subject.classify(article)

    assert classified.tags["industry"] == ("不動産", "業界横断")
    assert classified.tags["reliability"] == "高"


async def test_the_summary_is_trimmed(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    subject, _ = classifier(config, [payload(summary=f"  {TWO_SENTENCE_SUMMARY} ")])

    classified = await subject.classify(article)

    assert classified.summary == TWO_SENTENCE_SUMMARY


async def test_the_call_meta_is_passed_through(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """使用モデル・`prompt_version` は監査／validation メタへ載る（T-30）。"""
    client = FakeAIClient(
        [payload()],
        meta=AICallMeta(
            requested_model="claude-opus-5",
            models_used=("claude-opus-5",),
            prompt_version=PROMPT_VERSION,
            attempts=2,
            duration_ms=131497,
        ),
    )
    subject = ArticleClassifier(client=client, config=config)

    classified = await subject.classify(article)

    assert classified.meta.models_used == ("claude-opus-5",)
    assert classified.meta.prompt_version == PROMPT_VERSION
    assert classified.meta.attempts == 2


async def test_ai_failures_are_not_swallowed(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """原因ごとの例外（T-15）をそのまま通す＝ジョブの再実行判断を上位に残す。"""
    client = FakeAIClient(
        error=AIProcessError("未ログインの疑い", exit_code=1, stderr="not logged in")
    )
    subject = ArticleClassifier(client=client, config=config)

    with pytest.raises(AIProcessError):
        await subject.classify(article)


async def test_a_schema_violation_surfaces_as_an_output_parse_error(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    subject, _ = classifier(config, [payload(tags={"region": ["火星"]})])

    with pytest.raises(AIOutputParseError):
        await subject.classify(article)


# --- プロンプト（§13.3）-----------------------------------------------------


def test_the_prompt_forbids_overriding_the_config_values(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """T-19 完了条件：§13.3 の一文を明記する。"""
    prompt = build_classification_prompt(article, config)

    assert CONFIG_AUTHORITY_INSTRUCTION in prompt
    assert "実行時点の値をそのまま使用" in prompt


def test_the_prompt_shows_the_runtime_weights(raw: dict[str, Any]) -> None:
    """⚠️ 配点をプロンプトに書き写していないこと（config を変えたら追随する）。"""
    for axis in raw["scoring_axes"]:
        if axis["id"] == "customer_relevance":
            axis["weight"] = 30
        elif axis["id"] == "market_impact":
            axis["weight"] = 15
    config = IntelligenceConfig.model_validate(raw)
    article = RawArticle(
        collected_at="2026-08-14",
        title="t",
        url="https://example.com/1",
        source="ITmedia",
        raw_summary="a。b。",
        region_hint="日本",
        primary_or_secondary="報道",
    )

    prompt = build_classification_prompt(article, config)

    assert "顧客関連度（0〜30点）" in prompt
    assert "AI業界・市場インパクト（0〜15点）" in prompt
    assert "0〜25点" not in prompt


def test_the_prompt_shows_the_config_candidates(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    prompt = build_classification_prompt(article, config)

    for value in config.enums.region:
        assert value in prompt
    for value in config.enums.practical_usability:
        assert value in prompt
    for category in config.information_categories:
        assert category.id in prompt
        assert category.priority.value in prompt


def test_a_new_config_candidate_shows_up_in_the_prompt(raw: dict[str, Any]) -> None:
    raw["enums"]["industry"].append("宇宙開発")
    config = IntelligenceConfig.model_validate(raw)
    article = RawArticle(
        collected_at="2026-08-14",
        title="t",
        url="https://example.com/1",
        source="ITmedia",
        raw_summary="a。b。",
        region_hint="日本",
        primary_or_secondary="報道",
    )

    assert "宇宙開発" in build_classification_prompt(article, config)


def test_the_prompt_shows_the_bands_and_criteria(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """得点帯（§5.2 `scoring_axes[].bands`）に従って付けさせる（§13.3-4）。"""
    prompt = build_classification_prompt(article, config)

    for axis in config.scoring_axes:
        assert axis.criterion in prompt
        for band in axis.bands:
            assert band in prompt


def test_the_prompt_shows_the_target_industry(raw: dict[str, Any]) -> None:
    """§13.3 が config パラメータに挙げている「対象業界」（顧客関連度の基準）。"""
    raw["tunable_thresholds"]["target_industries"] = ["金融"]
    config = IntelligenceConfig.model_validate(raw)
    article = RawArticle(
        collected_at="2026-08-14",
        title="t",
        url="https://example.com/1",
        source="ITmedia",
        raw_summary="a。b。",
        region_hint="日本",
        primary_or_secondary="報道",
    )

    prompt = build_classification_prompt(article, config)

    assert "■ 対象業界（顧客関連度の判断基準" in prompt
    assert "金融" in prompt


def test_the_prompt_shows_every_target_industry(raw: dict[str, Any]) -> None:
    """対象業界は複数ありうる（T-46 Step 3）。顧客関連度は「いずれか」で見る。"""
    raw["tunable_thresholds"]["target_industries"] = ["不動産", "金融"]
    config = IntelligenceConfig.model_validate(raw)
    article = RawArticle(
        collected_at="2026-08-14",
        title="t",
        url="https://example.com/1",
        source="ITmedia",
        raw_summary="a。b。",
        region_hint="日本",
        primary_or_secondary="報道",
    )

    prompt = build_classification_prompt(article, config)

    assert "不動産 / 金融" in prompt
    assert "いずれかに関係すれば" in prompt


def test_the_prompt_tells_the_model_not_to_decide_the_total_or_the_class(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    prompt = build_classification_prompt(article, config)

    assert "合計スコア（6軸の点数をアプリ側で合算する）" in prompt
    assert "レポート採用区分（合計スコアと adoption_class_score_map から決まる）" in (
        prompt
    )
    assert "adoption_class: レポート採用区分 — **出力しない**" in prompt


def test_the_prompt_declares_the_deterministic_steps_out_of_scope(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """除外・重複・§12 検証・採否は Python 側（T-17 / T-18 / T-20 / T-21）。

    ⚠️ 対象外なのは**決定**であって、事実の申告（当たったルール番号・鮮度）は
    対象内（決定1）。この2つの線引きが本文でも読めることを固定する。
    """
    prompt = build_classification_prompt(article, config)

    assert "除外するかどうかの決定" in prompt
    assert "重複や統合の判定" in prompt
    assert "§12 のフォーマット検証" in prompt
    assert "掲載可否（しきい値の適用）" in prompt


def test_the_prompt_lists_every_exclusion_rule(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """13ルールの `no` / `name` / `examples` を提示する（当たり判定の材料）。"""
    prompt = build_classification_prompt(article, config)

    for rule in config.exclusion_rules:
        assert f"- {rule.no}: {rule.name}" in prompt
        assert rule.examples in prompt


def test_the_prompt_hides_the_severity_and_the_enabled_flag(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """⚠️ 分岐そのものを AI に渡さない（決定1）。

    severity を見せると「これは full_exclude だから当たったことにしない」という
    逆算の余地が生まれ、事実の申告ではなくなる。有効/無効の適用は T-17。
    """
    prompt = build_classification_prompt(article, config)

    for severity in Severity:
        assert severity.value not in prompt


def test_a_disabled_rule_is_still_offered_for_declaration(
    raw: dict[str, Any], article: RawArticle
) -> None:
    """`enabled=false` のルールも候補に残す（有効/無効の適用は T-17 の責務）。

    候補から外すと、AI の申告が config のスイッチに依存し始める＝「当たったか」
    という事実の申告でなくなる。
    """
    raw["exclusion_rules"][1]["enabled"] = False
    config = IntelligenceConfig.model_validate(raw)
    disabled = config.exclusion_rules[1]

    prompt = build_classification_prompt(article, config)
    schema = build_classification_schema(config)

    assert f"- {disabled.no}: {disabled.name}" in prompt
    value = schema.model_validate(payload(facts={MATCHED_RULES_FIELD: [disabled.no]}))
    assert getattr(value, FACTS_FIELD).matched_exclusion_rule_nos == [disabled.no]


def test_the_prompt_asks_for_two_to_three_sentences(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    prompt = build_classification_prompt(article, config)

    assert f"{SUMMARY_MIN_SENTENCES}〜{SUMMARY_MAX_SENTENCES}文の日本語で書く" in prompt


def test_the_prompt_carries_the_article_facts(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    prompt = build_classification_prompt(article, config)

    for value in (
        article.title,
        article.url,
        article.source,
        article.raw_summary,
        article.collected_at,
        article.region_hint.value,
        article.primary_or_secondary.value,
    ):
        assert value in prompt


def test_the_prompt_leaves_the_output_format_to_the_client(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """出力形式（JSON だけ・JSON Schema）の指示は `AIClient` 実装が付ける（T-15）。

    ここに書くと二重指示になり、実装を API へ差し替えたとき片方だけが残る。
    """
    prompt = build_classification_prompt(article, config)

    assert "JSON Schema" not in prompt
    assert "```" not in prompt


# --- T-20（§12 フォーマットチェック）との整合 ------------------------------


def weekly_row(classified: ClassifiedArticle) -> dict[str, Any]:
    """T-19 の結果を週次22列へ写す（実際の組み立ては T-21 の担当）。

    列名をここに書かず、T-07 の列定義（`axis_id` / `tag_id`）から機械的に引く。
    """
    article = classified.article
    fixed = {
        "収集日": article.collected_at,
        "タイトル": article.title,
        "一言要約": classified.summary,
        "合計スコア": classified.total_score,
        "ソース": article.source,
        "URL": article.url,
    }
    row: dict[str, Any] = {}
    for column in WEEKLY_ARTICLE_COLUMNS:
        if column.axis_id is not None:
            row[column.name] = classified.scores[column.axis_id]
        elif column.tag_id is not None:
            value = classified.tags[column.tag_id]
            row[column.name] = list(value) if isinstance(value, tuple) else value
        else:
            row[column.name] = fixed[column.name]
    return row


async def test_the_output_passes_the_format_check(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """⚠️ この層の出力が §12 を通ること（通らなければ本編から外れる）。"""
    subject, _ = classifier(config)

    classified = await subject.classify(article)
    issues = check_article(weekly_row(classified), config, row=5)

    assert issues.errors == []
    assert issues.warnings == []


async def test_the_total_in_the_row_matches_the_six_axes(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """§12.1 の「6軸の和 == 合計スコア」は、合算をアプリ側で行う結果として成立する。"""
    subject, _ = classifier(config)

    classified = await subject.classify(article)
    row = weekly_row(classified)

    assert row["合計スコア"] == sum(
        row[column.name]
        for column in WEEKLY_ARTICLE_COLUMNS
        if column.axis_id is not None
    )


def test_the_summary_instruction_matches_the_t20_threshold() -> None:
    """⚠️ プロンプトの下限と T-20 の warning のしきい値は同じ定数から引く。

    ずらすと「指示どおり書いたのに warning」または「指示より短くても素通り」に
    なる（§12.2）。
    """
    assert SUMMARY_MIN_SENTENCES == MIN_SUMMARY_SENTENCES
    assert count_sentences(TWO_SENTENCE_SUMMARY) == SUMMARY_MIN_SENTENCES


async def test_a_single_sentence_summary_is_what_t20_warns_about(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """短すぎる要約は **T-20 が warning にする**（この層は検証を持たない）。"""
    subject, _ = classifier(config, [payload(summary="AIを導入した。")])

    classified = await subject.classify(article)
    issues = check_article(weekly_row(classified), config, row=5)

    assert issues.errors == []
    assert [issue.field for issue in issues.warnings] == ["一言要約"]


# --- 責務の境界（他の層のロジックを持たない）--------------------------------


async def test_a_low_score_article_is_still_classified(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """採否（§13.3-5）はこの層の責務ではない（T-21 が除外ログへ回す）。

    `min_total_score_to_publish` 未満でも、この層は分類・採点結果を返すだけ。
    """
    subject, _ = classifier(config, [payload(scores=dict.fromkeys(VALID_SCORES, 0))])

    classified = await subject.classify(article)

    assert classified.total_score == 0
    assert classified.total_score < config.tunable_thresholds.min_total_score_to_publish
    assert classified.adoption_class == "不採用"
