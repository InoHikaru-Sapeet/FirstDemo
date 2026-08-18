"""config.json のクロスフィールドバリデータ（設計書 §2.1.1 ／ 仕様書 §7.4）。

§2.1.1 の6項目を1項目ずつ固定する。基準は仕様書 §5.2 の確定 config
（`conftest.py` の `config` フィクスチャ）で、そこから1箇所だけ崩して
「どのパスがなぜダメか」が返ることを確かめる形にしている。

**設計判断A（保存拒否・自動正規化しない）** は
`test_weight_sum_violation_never_normalizes_the_input` で明示的に固定している。
"""

from typing import Any

import pytest

from enterprise.entities.config import (
    INFORMATION_CATEGORY_IDS,
    REQUIRED_TAG_IDS,
    SCORING_AXIS_IDS,
    ConfigEnums,
    IntelligenceConfig,
    Priority,
    Severity,
)
from enterprise.entities.config_validation import (
    ENUM_SOURCE_PREFIX,
    INITIAL_CATEGORY_PRIORITIES,
    INITIAL_EXCLUSION_SEVERITIES,
    INITIAL_SCORING_WEIGHTS,
    INITIAL_TUNABLE_THRESHOLDS,
    ConfigIssueCode,
    ConfigValidationError,
    check_adoption_threshold_order,
    check_fixed_identities,
    check_target_industries_reference,
    check_value_source_references,
    check_weight_sum,
    ensure_valid_config,
    raise_for_issues,
    validate_config,
    validate_initial_config,
)


def _codes(issues: list[Any]) -> list[ConfigIssueCode]:
    return [issue.code for issue in issues]


def _paths(issues: list[Any]) -> list[str]:
    return [issue.path for issue in issues]


# --- 確定 config は全項目を通る -----------------------------------------------


def test_confirmed_config_has_no_issues(config: IntelligenceConfig) -> None:
    """§5.2 の確定 config が §2.1.1 の 1〜5 をすべて満たすこと。"""
    assert validate_config(config) == []


def test_confirmed_config_matches_the_documented_initial_values(
    config: IntelligenceConfig,
) -> None:
    """§2.1.1-6。移行 CLI（T-14 手順5）の受け入れ判定がこの状態で通ること。"""
    assert validate_initial_config(config) == []


def test_ensure_valid_config_returns_the_same_object(
    config: IntelligenceConfig,
) -> None:
    """補正した別物ではなく受け取った config を返す（設計判断A）。"""
    assert ensure_valid_config(config) is config


# --- §2.1.1-1: Σ weight == 100（設計判断A: 保存拒否）------------------------


@pytest.mark.parametrize("weight", [24, 26, 0, 100])
def test_weight_sum_other_than_the_total_is_rejected(
    config: IntelligenceConfig, weight: int
) -> None:
    """合計が `scoring_total` と一致しなければ保存拒否（設計判断A）。"""
    config.scoring_axes[0].weight = weight

    issues = check_weight_sum(config)

    assert _paths(issues) == ["scoring_axes"]
    assert _codes(issues) == [ConfigIssueCode.WEIGHT_SUM_MISMATCH]


def test_weight_sum_issue_reports_the_actual_total_and_breakdown(
    config: IntelligenceConfig,
) -> None:
    """admin が何点ズレているかを見て直せること。"""
    config.scoring_axes[0].weight = 30  # 25 → 30 なので合計 105

    (issue,) = check_weight_sum(config)

    assert issue.path == "scoring_axes"
    assert "100" in issue.reason
    assert "105" in issue.reason
    assert "customer_relevance=30" in issue.reason


def test_weight_sum_violation_never_normalizes_the_input(
    config: IntelligenceConfig,
) -> None:
    """**設計判断A**: サーバは按分で100へ寄せない。入力値をそのまま保つ。

    自動正規化は非整数 weight を生み、整数レンジの band（§5.2）と §12 の
    フォーマットチェック・採点の再現性を壊す。監査 diff（§4.4）も「入力＝保存」
    でないと説明できない。補正は UI 補助（T-33）に閉じる。
    """
    config.scoring_axes[0].weight = 40
    before = [axis.weight for axis in config.scoring_axes]

    issues = validate_config(config)

    assert issues != []
    assert [axis.weight for axis in config.scoring_axes] == before
    assert sum(axis.weight for axis in config.scoring_axes) == 115


def test_weight_can_be_redistributed_as_long_as_the_total_holds(
    config: IntelligenceConfig,
) -> None:
    """weight は可変（仕様書 §7.2）。合計100 なら別の配分でも通る。"""
    for axis, weight in zip(config.scoring_axes, [30, 20, 15, 15, 10, 10], strict=True):
        axis.weight = weight

    assert check_weight_sum(config) == []


# --- §2.1.1-2: 採用区分しきい値の降順整合 -----------------------------------


@pytest.mark.parametrize(
    ("attribute", "value", "expected_path"),
    [
        # propose(85) ≥ reference(70) を崩す
        (
            "reference_info",
            90,
            "tunable_thresholds.adoption_class_score_map.reference_info",
        ),
        # reference(70) ≥ share(60) を崩す
        (
            "share_only",
            75,
            "tunable_thresholds.adoption_class_score_map.share_only",
        ),
    ],
)
def test_broken_adoption_threshold_links_are_reported(
    config: IntelligenceConfig, attribute: str, value: int, expected_path: str
) -> None:
    setattr(config.tunable_thresholds.adoption_class_score_map, attribute, value)

    issues = check_adoption_threshold_order(config)

    assert _paths(issues) == [expected_path]
    assert _codes(issues) == [ConfigIssueCode.ADOPTION_THRESHOLD_ORDER]


def test_min_total_score_above_share_only_is_reported(
    config: IntelligenceConfig,
) -> None:
    """チェーンの末尾 `share_only ≥ min_total_score_to_publish` も見ること。"""
    config.tunable_thresholds.min_total_score_to_publish = 65  # share_only は 60

    issues = check_adoption_threshold_order(config)

    assert _paths(issues) == ["tunable_thresholds.min_total_score_to_publish"]


def test_adoption_threshold_issue_names_both_sides(config: IntelligenceConfig) -> None:
    """どの2つがどう逆転しているかが読めること。"""
    config.tunable_thresholds.adoption_class_score_map.reference_info = 90

    (issue,) = check_adoption_threshold_order(config)

    assert "参考情報(90)" in issue.reason
    assert "次回定例で提案(85)" in issue.reason
    assert "次回定例で提案 ≥ 参考情報 ≥ 共有のみ ≥ 掲載最低スコア" in issue.reason


def test_every_broken_link_is_reported_not_just_the_first(
    config: IntelligenceConfig,
) -> None:
    """3リンクすべて逆転させたら3件返る（早期 return しない）。"""
    score_map = config.tunable_thresholds.adoption_class_score_map
    score_map.propose_next_meeting = 10
    score_map.reference_info = 20
    score_map.share_only = 30
    config.tunable_thresholds.min_total_score_to_publish = 40

    issues = check_adoption_threshold_order(config)

    assert _paths(issues) == [
        "tunable_thresholds.adoption_class_score_map.reference_info",
        "tunable_thresholds.adoption_class_score_map.share_only",
        "tunable_thresholds.min_total_score_to_publish",
    ]


def test_equal_thresholds_satisfy_the_descending_order(
    config: IntelligenceConfig,
) -> None:
    """要件は `≥` なので同値は許す（境界値）。"""
    score_map = config.tunable_thresholds.adoption_class_score_map
    score_map.propose_next_meeting = 60
    score_map.reference_info = 60
    score_map.share_only = 60
    config.tunable_thresholds.min_total_score_to_publish = 60

    assert check_adoption_threshold_order(config) == []


# --- §2.1.1-3: target_industries[*] ∈ enums.industry ------------------------


def test_target_industry_outside_the_enum_is_rejected(
    config: IntelligenceConfig,
) -> None:
    config.tunable_thresholds.target_industries = ["存在しない業界"]

    issues = check_target_industries_reference(config)

    assert _paths(issues) == ["tunable_thresholds.target_industries.0"]
    assert _codes(issues) == [ConfigIssueCode.UNKNOWN_INDUSTRY_REFERENCE]
    assert "存在しない業界" in issues[0].reason
    # 選べる値を示して直せるようにする
    assert "不動産" in issues[0].reason


def test_each_unknown_industry_is_reported_with_its_index(
    config: IntelligenceConfig,
) -> None:
    """T-46 Step 3：違反した要素だけをフォームの欄へ対応づけられること。"""
    config.tunable_thresholds.target_industries = [
        "不動産",
        "存在しない業界",
        "宇宙開発",
    ]

    issues = check_target_industries_reference(config)

    assert _paths(issues) == [
        "tunable_thresholds.target_industries.1",
        "tunable_thresholds.target_industries.2",
    ]


def test_a_duplicated_target_industry_is_rejected(
    config: IntelligenceConfig,
) -> None:
    """⚠️ 業界の数がそのまま生成物の数（同じ HTML を2回書かせない）。"""
    config.tunable_thresholds.target_industries = ["不動産", "金融", "不動産"]

    issues = check_target_industries_reference(config)

    assert _paths(issues) == ["tunable_thresholds.target_industries"]
    assert _codes(issues) == [ConfigIssueCode.DUPLICATE_INDUSTRY_REFERENCE]
    assert "不動産" in issues[0].reason


@pytest.mark.parametrize("industry", ["業界横断", "金融", "航空宇宙"])
def test_any_industry_in_the_enum_is_accepted(
    config: IntelligenceConfig, industry: str
) -> None:
    """対象業界は可変（仕様書 §7.2「週刊：対象業界」）。"""
    config.tunable_thresholds.target_industries = [industry]

    assert check_target_industries_reference(config) == []


def test_several_industries_are_accepted(config: IntelligenceConfig) -> None:
    """複数業界（2026-08-17 の PM 要件。T-46 Step 3）。"""
    config.tunable_thresholds.target_industries = ["不動産", "金融", "製造"]

    assert check_target_industries_reference(config) == []


def test_reference_integrity_follows_the_enum_not_a_hardcoded_list(
    config: IntelligenceConfig,
) -> None:
    """enums.industry に足せばその業界も選べる（参照整合であること）。"""
    config.enums.industry = [*config.enums.industry, "建設"]
    config.tunable_thresholds.target_industries = ["建設"]

    assert check_target_industries_reference(config) == []


# --- §2.1.1-4: value_source の参照先 enum が実在 -----------------------------


def test_value_source_pointing_at_a_missing_enum_is_rejected(
    config: IntelligenceConfig,
) -> None:
    config.required_tags[2].value_source = "enums.sector"

    issues = check_value_source_references(config)

    assert _paths(issues) == ["required_tags.2.value_source"]
    assert _codes(issues) == [ConfigIssueCode.UNKNOWN_ENUM_REFERENCE]
    assert "sector" in issues[0].reason
    assert "industry" in issues[0].reason


def test_every_broken_value_source_is_reported(config: IntelligenceConfig) -> None:
    config.required_tags[2].value_source = "enums.sector"
    config.required_tags[5].value_source = "enums.area"

    issues = check_value_source_references(config)

    assert _paths(issues) == [
        "required_tags.2.value_source",
        "required_tags.5.value_source",
    ]


@pytest.mark.parametrize(
    "value_source", ["free_controlled", "information_categories.id"]
)
def test_non_enum_value_sources_are_left_alone(
    config: IntelligenceConfig, value_source: str
) -> None:
    """`enums.` 以外は設計書 §2.1 で自由文字列。ここで判定しない。"""
    config.required_tags[1].value_source = value_source

    assert check_value_source_references(config) == []


def test_all_ten_enum_keys_are_referenceable(config: IntelligenceConfig) -> None:
    """`enums.*` の全キーが参照先として通ること。"""
    for key in ConfigEnums.model_fields:
        config.required_tags[1].value_source = f"{ENUM_SOURCE_PREFIX}{key}"
        assert check_value_source_references(config) == [], key


# --- §2.1.1-5: ID系が現行値と一致 -------------------------------------------


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("information_categories", "id"),
        ("required_tags", "id"),
        ("scoring_axes", "id"),
        ("exclusion_rules", "no"),
    ],
)
def test_duplicated_identity_is_rejected(
    config: IntelligenceConfig, section: str, field: str
) -> None:
    """件数は合っていても同じIDが2つあれば弾く（モデルでは通ってしまう）。"""
    items = getattr(config, section)
    items[1] = items[0].model_copy(deep=True)

    issues = check_fixed_identities(config)

    assert _paths(issues) == [section]
    assert _codes(issues) == [ConfigIssueCode.FIXED_ID_CHANGED]
    assert "重複" in issues[0].reason
    assert "不足" in issues[0].reason
    assert field in issues[0].reason


@pytest.mark.parametrize(
    "section",
    ["information_categories", "required_tags", "scoring_axes", "exclusion_rules"],
)
def test_reordered_identities_are_rejected(
    config: IntelligenceConfig, section: str
) -> None:
    """並び順の変更も「現行値と不一致」（設計書 §2.1.1-5）。"""
    items = getattr(config, section)
    items[0], items[1] = items[1], items[0]

    issues = check_fixed_identities(config)

    assert _paths(issues) == [section]
    assert "順序が異なる" in issues[0].reason


def test_identity_issue_explains_why_ids_cannot_change(
    config: IntelligenceConfig,
) -> None:
    """理由（中間xlsx 互換）が読めること。UI では編集不可の欄なので API 越しの
    変更を弾くのがこのチェックの役目。"""
    config.scoring_axes[0], config.scoring_axes[1] = (
        config.scoring_axes[1],
        config.scoring_axes[0],
    )

    (issue,) = check_fixed_identities(config)

    assert "中間xlsx" in issue.reason


def test_all_sections_are_reported_when_all_are_broken(
    config: IntelligenceConfig,
) -> None:
    for section in (
        "information_categories",
        "required_tags",
        "scoring_axes",
        "exclusion_rules",
    ):
        items = getattr(config, section)
        items[0], items[1] = items[1], items[0]

    issues = check_fixed_identities(config)

    assert _paths(issues) == [
        "information_categories",
        "required_tags",
        "scoring_axes",
        "exclusion_rules",
    ]


def test_confirmed_identities_are_the_canonical_sequences(
    config: IntelligenceConfig,
) -> None:
    """「現行値」＝`config.py` が Literal で固定している正準列であること。"""
    assert tuple(c.id for c in config.information_categories) == (
        INFORMATION_CATEGORY_IDS
    )
    assert tuple(t.id for t in config.required_tags) == REQUIRED_TAG_IDS
    assert tuple(a.id for a in config.scoring_axes) == SCORING_AXIS_IDS
    assert tuple(r.no for r in config.exclusion_rules) == tuple(range(1, 14))
    assert check_fixed_identities(config) == []


# --- §2.1.1-6: 初期値が §5.2 実データと一致 ---------------------------------


def test_initial_constants_agree_with_the_confirmed_config(
    initial_raw: dict[str, Any],
) -> None:
    """期待値テーブルが §5.2 からズレていないこと。

    ここが §5.2 とズレると移行の受け入れ判定（T-14 手順5）が意味をなさない。
    """
    assert INITIAL_SCORING_WEIGHTS == {
        axis["id"]: axis["weight"] for axis in initial_raw["scoring_axes"]
    }
    assert INITIAL_CATEGORY_PRIORITIES == {
        category["id"]: Priority(category["priority"])
        for category in initial_raw["information_categories"]
    }
    assert INITIAL_EXCLUSION_SEVERITIES == {
        rule["no"]: Severity(rule["severity"])
        for rule in initial_raw["exclusion_rules"]
    }

    tunable = initial_raw["tunable_thresholds"]
    for path, expected in INITIAL_TUNABLE_THRESHOLDS.items():
        node: Any = tunable
        for key in path.removeprefix("tunable_thresholds.").split("."):
            node = node[key]
        assert node == expected, path


@pytest.mark.parametrize(
    ("path", "assign"),
    [
        ("scoring_axes.0.weight", lambda c: setattr(c.scoring_axes[0], "weight", 30)),
        (
            "information_categories.0.priority",
            lambda c: setattr(c.information_categories[0], "priority", Priority.LOW),
        ),
        (
            "exclusion_rules.0.severity",
            lambda c: setattr(c.exclusion_rules[0], "severity", Severity.MERGE),
        ),
        (
            "exclusion_rules.0.enabled",
            lambda c: setattr(c.exclusion_rules[0], "enabled", False),
        ),
        (
            "tunable_thresholds.min_total_score_to_publish",
            lambda c: setattr(c.tunable_thresholds, "min_total_score_to_publish", 62),
        ),
        (
            "tunable_thresholds.target_industries",
            lambda c: setattr(c.tunable_thresholds, "target_industries", ["金融"]),
        ),
        (
            "tunable_thresholds.dedup.title_similarity_threshold",
            lambda c: setattr(
                c.tunable_thresholds.dedup, "title_similarity_threshold", 0.9
            ),
        ),
    ],
)
def test_initial_value_drift_is_reported_with_its_path(
    config: IntelligenceConfig, path: str, assign: Any
) -> None:
    """移行結果が §5.2 とズレたらどのパスかが分かること。"""
    assign(config)

    issues = validate_initial_config(config)

    assert _paths(issues) == [path]
    assert _codes(issues) == [ConfigIssueCode.INITIAL_VALUE_MISMATCH]
    assert "§5.2" in issues[0].reason


def test_initial_check_is_separate_from_the_save_time_check(
    config: IntelligenceConfig,
) -> None:
    """admin が編集した config は §5.2 と一致しないが保存はできる。

    §2.1.1-6 は移行専用の関門で、通常の保存経路（`validate_config`）とは
    別の判定であることを固定する。
    """
    config.tunable_thresholds.min_total_score_to_publish = 55
    config.information_categories[0].priority = Priority.LOW

    assert validate_config(config) == []
    assert len(validate_initial_config(config)) == 2


def test_priority_normalisation_mistakes_are_caught(
    config: IntelligenceConfig,
) -> None:
    """`中〜高` → `mid_high`（仕様書 §5.3）を取り違えたら移行検証で落ちる。"""
    config.information_categories[0].priority = Priority.HIGH  # 正しくは mid_high

    (issue,) = validate_initial_config(config)

    assert issue.path == "information_categories.0.priority"
    assert "mid_high" in issue.reason


# --- 違反の返し方（HTTP 422 への載せ方）--------------------------------------


def test_all_violations_across_rules_are_returned_together(
    config: IntelligenceConfig,
) -> None:
    """1回の保存で複数の違反をまとめて直せること（T-34 の表示要件）。"""
    config.scoring_axes[0].weight = 30
    config.tunable_thresholds.adoption_class_score_map.reference_info = 90
    config.tunable_thresholds.target_industries = ["存在しない業界"]
    config.required_tags[2].value_source = "enums.sector"
    config.exclusion_rules[0], config.exclusion_rules[1] = (
        config.exclusion_rules[1],
        config.exclusion_rules[0],
    )

    issues = validate_config(config)

    assert _codes(issues) == [
        ConfigIssueCode.WEIGHT_SUM_MISMATCH,
        ConfigIssueCode.ADOPTION_THRESHOLD_ORDER,
        ConfigIssueCode.UNKNOWN_INDUSTRY_REFERENCE,
        ConfigIssueCode.UNKNOWN_ENUM_REFERENCE,
        ConfigIssueCode.FIXED_ID_CHANGED,
    ]


def test_issues_serialise_into_the_422_body_shape(
    config: IntelligenceConfig,
) -> None:
    """設計書 §3.3 の `issues:[{path, reason}]` にそのまま載る形であること。"""
    config.scoring_axes[0].weight = 30

    (issue,) = validate_config(config)
    body = issue.model_dump(mode="json")

    assert set(body) == {"path", "reason", "code"}
    assert body["path"] == "scoring_axes"
    assert body["reason"]
    # code は UI が補正ボタンを出し分けるための機械可読キー（T-33）
    assert body["code"] == "weight_sum_mismatch"


def test_issues_are_immutable(config: IntelligenceConfig) -> None:
    """検証結果を呼び出し側が書き換えられないこと。"""
    config.scoring_axes[0].weight = 30

    (issue,) = validate_config(config)

    with pytest.raises(ValueError):
        issue.path = "elsewhere"


def test_ensure_valid_config_raises_with_the_issues(
    config: IntelligenceConfig,
) -> None:
    """T-11 の検証済み書き込み・T-14 の「失敗時は書き込まず中断」用の入口。"""
    config.scoring_axes[0].weight = 30

    with pytest.raises(ConfigValidationError) as exc_info:
        ensure_valid_config(config)

    assert _codes(exc_info.value.issues) == [ConfigIssueCode.WEIGHT_SUM_MISMATCH]
    # 例外メッセージだけを見てもどこが悪いか分かること（ログ経路）
    assert "scoring_axes" in str(exc_info.value)


def test_raise_for_issues_is_a_no_op_when_valid() -> None:
    raise_for_issues([])


def test_migration_can_gate_on_both_checks(config: IntelligenceConfig) -> None:
    """T-14 手順4-5 は両方を通す。片方だけ落ちても中断できること。"""
    config.information_categories[0].priority = Priority.LOW

    with pytest.raises(ConfigValidationError) as exc_info:
        raise_for_issues([*validate_config(config), *validate_initial_config(config)])

    assert _paths(exc_info.value.issues) == ["information_categories.0.priority"]
