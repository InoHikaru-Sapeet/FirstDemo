"""除外ルール判定エンジン（設計書 §6.2 ／ 仕様書 §5.4 ／ T-17）。

**13ルール × 5 severity の 65 通り**を総当たりで固定する
（`test_every_rule_and_severity_combination`）。ルール本文は自然文で当たり判定は
上流に委ねるが、**当たったあとに何が起きるかは config だけで決まる**——これが
このモジュールの存在理由なので、そこを網羅で押さえる。

判定に使う config は仕様書 §5.2 の確定値（`data/config_initial.json`）。しきい値は
`config.tunable_thresholds` から引き、テスト側にも数値をベタ書きしない。
"""

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from enterprise.entities.config import (
    EXCLUSION_RULE_COUNT,
    AdoptionClass,
    CustomerRelevance,
    IntelligenceConfig,
    Severity,
)
from enterprise.entities.raw_article import PrimaryOrSecondary, RawArticle, RegionHint
from enterprise.entities.report_columns import EXCLUSION_LOG_COLUMNS, header_row
from enterprise.services.exclusion import (
    ADOPTION_CLASS_DESCENDING,
    CATEGORY_DEFAULT_EXCLUDE,
    CATEGORY_FULL_EXCLUDE,
    CATEGORY_LOW_PRIORITY_OR_EXCLUDE,
    DEFAULT_EXCLUDE_EXCEPTION_NOTE,
    DIRECT_CUSTOMER_RELEVANCE,
    ExclusionAction,
    ExclusionError,
    ScreenedArticle,
    downgrade_adoption_class,
    enabled_rules_in_order,
    evaluate_exclusions,
    exclusion_log_entry,
    exclusion_log_row,
    matched_rule_names,
)

ALL_RULE_NOS = tuple(range(1, EXCLUSION_RULE_COUNT + 1))
ALL_SEVERITIES = tuple(Severity)


@pytest.fixture
def article() -> RawArticle:
    """判定対象の記事1件。除外ログ6列の値の出どころでもある。"""
    return RawArticle(
        collected_at="2026-07-27",
        published_at="2026-07-26",
        title="OpenAI が企業向けエージェント基盤を発表",
        url="https://example.com/news/agent-platform?utm_source=x",
        source="TechCrunch",
        raw_summary="OpenAI が企業向けのエージェント基盤を発表した。",
        region_hint=RegionHint.OVERSEAS,
        primary_or_secondary=PrimaryOrSecondary.REPORTED,
    )


def _config_with_severity(
    raw: dict[str, Any], rule_no: int, severity: Severity
) -> IntelligenceConfig:
    """指定ルールの `severity` だけを差し替えた config。

    admin は管理画面から `severity` を変えられる（仕様書 §7.2）。同じ記事・同じ
    当たり方でも結果が変わることを、この差し替えで確かめる。
    """
    for rule in raw["exclusion_rules"]:
        if rule["no"] == rule_no:
            rule["severity"] = severity.value
    return IntelligenceConfig.model_validate(raw)


def _rule(config: IntelligenceConfig, rule_no: int) -> Any:
    return next(rule for rule in config.exclusion_rules if rule.no == rule_no)


# --- 13ルール × 5 severity の総当たり ---------------------------------------


@pytest.mark.parametrize("rule_no", ALL_RULE_NOS)
@pytest.mark.parametrize("severity", ALL_SEVERITIES)
def test_every_rule_and_severity_combination(
    raw: dict[str, Any], article: RawArticle, rule_no: int, severity: Severity
) -> None:
    """13ルールのどれが当たっても、分岐は §5.4 の severity どおりになる。

    条件つきの2つ（`default_exclude` の例外採用・`low_priority_or_exclude` の
    鮮度）は、このテスト内で**両方の枝**を確かめる。
    """
    config = _config_with_severity(raw, rule_no, severity)
    rule = _rule(config, rule_no)
    threshold = config.tunable_thresholds.min_total_score_to_publish
    screened = ScreenedArticle(article=article, matched_rule_nos=frozenset({rule_no}))

    verdict = evaluate_exclusions(screened, config)

    assert verdict.rule_no == rule_no
    assert verdict.rule_name == rule.name

    match severity:
        case Severity.FULL_EXCLUDE:
            assert verdict.action is ExclusionAction.EXCLUDE
            assert verdict.category == CATEGORY_FULL_EXCLUDE
            assert verdict.reason == rule.name

        case Severity.DEFAULT_EXCLUDE:
            # 原則は除外。
            assert verdict.action is ExclusionAction.EXCLUDE
            assert verdict.category == CATEGORY_DEFAULT_EXCLUDE
            assert verdict.reason == rule.name
            # 「直接関係」かつ 合計見込み ≥ しきい値 なら例外採用（理由つき）。
            adopted = evaluate_exclusions(
                screened.model_copy(
                    update={
                        "customer_relevance": DIRECT_CUSTOMER_RELEVANCE,
                        "estimated_total_score": threshold,
                    }
                ),
                config,
            )
            assert adopted.action is ExclusionAction.KEEP
            assert adopted.note == DEFAULT_EXCLUDE_EXCEPTION_NOTE + rule.name
            assert adopted.category == ""

        case Severity.LOW_PRIORITY:
            assert verdict.action is ExclusionAction.LOW_PRIORITY
            assert verdict.is_low_priority
            assert verdict.category == ""

        case Severity.LOW_PRIORITY_OR_EXCLUDE:
            # 鮮度が低くなければ低優先。
            assert verdict.action is ExclusionAction.LOW_PRIORITY
            assert verdict.category == ""
            # 鮮度が低ければ除外。
            stale = evaluate_exclusions(
                screened.model_copy(update={"is_stale": True}), config
            )
            assert stale.action is ExclusionAction.EXCLUDE
            assert stale.category == CATEGORY_LOW_PRIORITY_OR_EXCLUDE
            assert stale.reason == rule.name

        case Severity.MERGE:
            # 除外ではない。`除外区分` は T-18 が `統合` を付ける。
            assert verdict.action is ExclusionAction.MERGE
            assert verdict.category == ""
            assert verdict.reason == rule.name
            assert not verdict.is_excluded


@pytest.mark.parametrize("rule_no", ALL_RULE_NOS)
def test_each_rule_follows_its_configured_severity_in_the_initial_config(
    config: IntelligenceConfig, article: RawArticle, rule_no: int
) -> None:
    """§5.2 の確定 config そのままで、13ルールが素の severity どおりに効く。"""
    expected_action = {
        Severity.FULL_EXCLUDE: ExclusionAction.EXCLUDE,
        Severity.DEFAULT_EXCLUDE: ExclusionAction.EXCLUDE,
        Severity.LOW_PRIORITY: ExclusionAction.LOW_PRIORITY,
        Severity.LOW_PRIORITY_OR_EXCLUDE: ExclusionAction.LOW_PRIORITY,
        Severity.MERGE: ExclusionAction.MERGE,
    }
    rule = _rule(config, rule_no)

    verdict = evaluate_exclusions(
        ScreenedArticle(article=article, matched_rule_nos=frozenset({rule_no})), config
    )

    assert verdict.action is expected_action[rule.severity]


def test_the_same_signals_follow_the_config_severity(
    raw: dict[str, Any], article: RawArticle
) -> None:
    """**同じ申告でも config を変えれば結果が変わる**（＝上流は分岐を決めない）。

    上流が「ルール11に当たる」と申告する事実は変えずに、admin が severity を
    `low_priority` → `full_exclude` に変えると、結果は採用から除外へ動く。
    逆に上流がどう申告しても severity を上書きする経路は無い。
    """
    screened = ScreenedArticle(article=article, matched_rule_nos=frozenset({11}))

    lenient = evaluate_exclusions(
        screened, _config_with_severity(raw, 11, Severity.LOW_PRIORITY)
    )
    strict = evaluate_exclusions(
        screened, _config_with_severity(raw, 11, Severity.FULL_EXCLUDE)
    )

    assert lenient.action is ExclusionAction.LOW_PRIORITY
    assert strict.action is ExclusionAction.EXCLUDE


# --- 評価順序と enabled ------------------------------------------------------


def test_rules_are_evaluated_in_ascending_no_order(
    raw: dict[str, Any], article: RawArticle
) -> None:
    """**config の配列順ではなく `no` 昇順**で評価する（§13.3-1）。

    配列を逆順にしても結果が変わらないことで、順序の出どころが `no` だと分かる。
    ルール2（`full_exclude`）とルール11（`low_priority`）に同時に当たった記事は、
    番号の小さいルール2で打ち切られる。
    """
    raw["exclusion_rules"].reverse()
    config = IntelligenceConfig.model_validate(raw)
    screened = ScreenedArticle(article=article, matched_rule_nos=frozenset({11, 2}))

    verdict = evaluate_exclusions(screened, config)

    assert verdict.rule_no == 2
    assert verdict.action is ExclusionAction.EXCLUDE
    assert [rule.no for rule in enabled_rules_in_order(config)] == list(ALL_RULE_NOS)


def test_the_first_matching_rule_wins(raw: dict[str, Any], article: RawArticle) -> None:
    """当たったうち**最初の1件**で打ち切る。後ろに強いルールがあっても評価しない。"""
    config = _config_with_severity(raw, 3, Severity.LOW_PRIORITY)
    screened = ScreenedArticle(article=article, matched_rule_nos=frozenset({3, 2}))

    # ルール2（full_exclude）が先。
    assert evaluate_exclusions(screened, config).rule_no == 2

    # ルール2を無効にすると、次に当たるルール3の severity が効く。
    for rule in raw["exclusion_rules"]:
        if rule["no"] == 2:
            rule["enabled"] = False
    config_without_2 = _config_with_severity(raw, 3, Severity.LOW_PRIORITY)
    verdict = evaluate_exclusions(screened, config_without_2)
    assert verdict.rule_no == 3
    assert verdict.action is ExclusionAction.LOW_PRIORITY


@pytest.mark.parametrize("rule_no", ALL_RULE_NOS)
def test_disabled_rules_are_skipped(
    raw: dict[str, Any], article: RawArticle, rule_no: int
) -> None:
    """`enabled=false` のルールは当たっていても効かない（13ルールすべてで確認）。"""
    for rule in raw["exclusion_rules"]:
        if rule["no"] == rule_no:
            rule["enabled"] = False
    config = IntelligenceConfig.model_validate(raw)

    verdict = evaluate_exclusions(
        ScreenedArticle(article=article, matched_rule_nos=frozenset({rule_no})), config
    )

    assert verdict.action is ExclusionAction.KEEP
    assert verdict.rule_no is None
    assert rule_no not in {rule.no for rule in enabled_rules_in_order(config)}


def test_an_article_matching_nothing_is_kept(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """どのルールにも当たらなければ採用（理由も除外区分も付かない）。"""
    verdict = evaluate_exclusions(ScreenedArticle(article=article), config)

    assert verdict.action is ExclusionAction.KEEP
    assert verdict.rule_no is None
    assert verdict.category == ""
    assert verdict.reason == ""
    assert verdict.note == ""
    assert not verdict.is_excluded
    assert not verdict.is_low_priority


# --- `default_exclude` の例外採用（§5.4）------------------------------------


def test_the_exception_needs_the_score_to_reach_the_threshold(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """例外採用は **合計見込み ≥ しきい値**。1点下は原則どおり除外。"""
    threshold = config.tunable_thresholds.min_total_score_to_publish

    def verdict_for(score: int) -> ExclusionAction:
        return evaluate_exclusions(
            ScreenedArticle(
                article=article,
                matched_rule_nos=frozenset({3}),  # default_exclude
                customer_relevance=DIRECT_CUSTOMER_RELEVANCE,
                estimated_total_score=score,
            ),
            config,
        ).action

    assert verdict_for(threshold - 1) is ExclusionAction.EXCLUDE
    assert verdict_for(threshold) is ExclusionAction.KEEP
    assert verdict_for(threshold + 1) is ExclusionAction.KEEP


@pytest.mark.parametrize(
    "customer_relevance",
    [value for value in get_args(CustomerRelevance) if value != "直接関係"] + [None],
)
def test_the_exception_needs_direct_customer_relevance(
    config: IntelligenceConfig, article: RawArticle, customer_relevance: str | None
) -> None:
    """顧客関連度が「直接関係」以外（未判定を含む）なら例外採用しない。"""
    verdict = evaluate_exclusions(
        ScreenedArticle(
            article=article,
            matched_rule_nos=frozenset({3}),
            customer_relevance=customer_relevance,
            estimated_total_score=config.tunable_thresholds.min_total_score_to_publish,
        ),
        config,
    )

    assert verdict.action is ExclusionAction.EXCLUDE
    assert verdict.category == CATEGORY_DEFAULT_EXCLUDE


def test_an_unscored_article_is_not_adopted_by_exception(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """合計見込みが未判定なら例外採用しない（安全側＝原則どおり除外）。"""
    verdict = evaluate_exclusions(
        ScreenedArticle(
            article=article,
            matched_rule_nos=frozenset({3}),
            customer_relevance=DIRECT_CUSTOMER_RELEVANCE,
            estimated_total_score=None,
        ),
        config,
    )

    assert verdict.action is ExclusionAction.EXCLUDE


def test_the_exception_only_applies_to_default_exclude(
    raw: dict[str, Any], article: RawArticle
) -> None:
    """例外採用は `default_exclude` 限定。`full_exclude` は無条件で除外（§5.4）。"""
    config = _config_with_severity(raw, 3, Severity.FULL_EXCLUDE)

    verdict = evaluate_exclusions(
        ScreenedArticle(
            article=article,
            matched_rule_nos=frozenset({3}),
            customer_relevance=DIRECT_CUSTOMER_RELEVANCE,
            estimated_total_score=config.tunable_thresholds.min_total_score_to_publish,
        ),
        config,
    )

    assert verdict.action is ExclusionAction.EXCLUDE
    assert verdict.category == CATEGORY_FULL_EXCLUDE


def test_direct_relevance_is_a_config_enum_value(config: IntelligenceConfig) -> None:
    """例外採用条件の「直接関係」が config の enum に実在する値であること。"""
    assert DIRECT_CUSTOMER_RELEVANCE in get_args(CustomerRelevance)
    assert DIRECT_CUSTOMER_RELEVANCE in config.enums.customer_relevance


# --- 入力の作り（LLM に分岐を渡さない構造）----------------------------------


def test_screened_article_carries_facts_only() -> None:
    """`ScreenedArticle` が持ってよいのは**事実**だけ。

    ⚠️ このテストが落ちたときは、フィールドを1つ足したということ。それが
    「除外する / 採用する / 採用区分を上げる」に相当する値なら、**上流（LLM）が
    severity 分岐を上書きできるようになったということ**なので足してはいけない
    （モジュール docstring）。事実（観測）を足したのなら期待値を更新してよい。
    """
    assert set(ScreenedArticle.model_fields) == {
        "article",
        "matched_rule_nos",
        "customer_relevance",
        "estimated_total_score",
        "is_stale",
    }


def test_unknown_fields_are_rejected(article: RawArticle) -> None:
    """知らないキーは受け取らない（`action` 等を紛れ込ませる経路を塞ぐ）。"""
    with pytest.raises(ValidationError):
        ScreenedArticle(article=article, action="keep")  # type: ignore[call-arg]


@pytest.mark.parametrize("rule_no", [0, -1, EXCLUSION_RULE_COUNT + 1, 99])
def test_rule_numbers_outside_the_config_range_are_rejected(
    article: RawArticle, rule_no: int
) -> None:
    """config の `no` の値域（1〜13）外は入力の時点で弾く。"""
    with pytest.raises(ValidationError, match="範囲外"):
        ScreenedArticle(article=article, matched_rule_nos=frozenset({rule_no}))


def test_a_rule_no_missing_from_the_config_is_an_error(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """値域内でも config に無い番号なら落とす（黙って無視しない）。

    config の `exclusion_rules` が13件そろっていることは T-05 が保証するが、
    保証の外側から渡された config で「当たったはずのルールが消える」のは
    再現性の事故なので、握り潰さず例外にする。
    """
    config = config.model_copy(
        update={"exclusion_rules": [r for r in config.exclusion_rules if r.no != 7]}
    )

    with pytest.raises(ExclusionError, match="7"):
        evaluate_exclusions(
            ScreenedArticle(article=article, matched_rule_nos=frozenset({7})), config
        )


# --- 採用区分の降格（§5.4「共有のみ寄り」）----------------------------------


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("次回定例で提案", "参考情報"),
        ("参考情報", "共有のみ"),
        ("共有のみ", "共有のみ"),
        ("不採用", "不採用"),
    ],
)
def test_downgrade_moves_one_step_toward_share_only(
    current: AdoptionClass, expected: AdoptionClass
) -> None:
    """1段だけ下げ、`共有のみ` で止まる。

    §5.4 は「**採用はするが** `adoption_class` を下げる」なので、降格で
    `不採用` にはしない。既に `不採用` の記事は採否判定（§13.3-5）で落ちる。
    """
    assert downgrade_adoption_class(current) == expected


def test_downgrade_is_idempotent_at_the_floor() -> None:
    """何度下げても `共有のみ` より下へは行かない。"""
    value: AdoptionClass = "次回定例で提案"
    for _ in range(5):
        value = downgrade_adoption_class(value)
    assert value == "共有のみ"


def test_adoption_class_order_matches_the_config(config: IntelligenceConfig) -> None:
    """降格に使う並びが config の `enums.adoption_class` と同じであること。"""
    assert list(ADOPTION_CLASS_DESCENDING) == list(config.enums.adoption_class)


def test_adoption_class_order_matches_the_score_map(
    config: IntelligenceConfig,
) -> None:
    """並びが強い順であることを `adoption_class_score_map` の大小で裏取りする。

    「1段下げる」が意味を持つのは、この並びがスコアの高い順と一致するときだけ
    （§6.4 のしきい値は propose ≥ reference ≥ share の順）。
    """
    score_map = config.tunable_thresholds.adoption_class_score_map
    assert score_map.propose_next_meeting >= score_map.reference_info
    assert score_map.reference_info >= score_map.share_only
    assert ADOPTION_CLASS_DESCENDING == (
        "次回定例で提案",
        "参考情報",
        "共有のみ",
        "不採用",
    )


# --- 除外ログ（6列）---------------------------------------------------------


def test_the_exclusion_log_row_follows_the_column_definition(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """除外ログ行は T-07 の6列・その順序どおり（設計書 §2.2.2）。"""
    screened = ScreenedArticle(article=article, matched_rule_nos=frozenset({1}))
    verdict = evaluate_exclusions(screened, config)
    rule = _rule(config, 1)

    entry = exclusion_log_entry(screened, verdict)
    row = exclusion_log_row(screened, verdict)

    assert list(entry) == header_row(EXCLUSION_LOG_COLUMNS)
    assert len(row) == len(EXCLUSION_LOG_COLUMNS) == 6
    assert row == [
        article.collected_at,
        article.title,
        article.url,
        article.source,
        CATEGORY_FULL_EXCLUDE,
        rule.name,
    ]


def test_the_exclusion_log_keeps_the_url_unnormalized(
    config: IntelligenceConfig, article: RawArticle
) -> None:
    """除外ログには収集したままの URL を書く。

    正規化は重複判定（T-18）の内部処理で、ログは「何を見て落としたか」の記録。
    """
    screened = ScreenedArticle(article=article, matched_rule_nos=frozenset({1}))
    entry = exclusion_log_entry(screened, evaluate_exclusions(screened, config))

    assert entry["URL"] == article.url
    assert "utm_source" in entry["URL"]


@pytest.mark.parametrize(
    "matched",
    [frozenset(), frozenset({11}), frozenset({12})],  # keep / low_priority / merge
)
def test_only_excluded_articles_get_a_log_row(
    config: IntelligenceConfig, article: RawArticle, matched: frozenset[int]
) -> None:
    """除外以外の判定で除外ログ行を作ろうとしたら落とす。

    低優先・統合・採用はいずれも本編側に残る（統合は T-18 が代表へまとめてから
    ログを書く）。ここで黙って行を作ると本編と除外ログの両方に載る。
    """
    screened = ScreenedArticle(article=article, matched_rule_nos=matched)
    verdict = evaluate_exclusions(screened, config)

    assert not verdict.is_excluded
    with pytest.raises(ExclusionError, match="exclude"):
        exclusion_log_entry(screened, verdict)


# --- 補助 --------------------------------------------------------------------


def test_matched_rule_names_are_returned_in_no_order(
    config: IntelligenceConfig,
) -> None:
    """ルール名の引き当ては `no` 昇順（監査・デバッグ表示用）。"""
    assert matched_rule_names(config, frozenset({11, 2})) == [
        _rule(config, 2).name,
        _rule(config, 11).name,
    ]
    assert matched_rule_names(config, frozenset()) == []
