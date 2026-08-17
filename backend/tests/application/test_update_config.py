"""patch の許可リスト（T-13。仕様書 §7.2 ／ 設計書 §3.3）。

HTTP を通さずに `apply_patch` そのものを固定する。重点は:

- **許可リストが仕様書 §7.2 と一致する**（勝手に広がっていない）
- **1件でも違反があれば何も適用しない**（部分適用しない）
- **ID系は変更経路そのものが無い**（`id` / `no` はセレクタ扱い）
- クロスフィールド検証（T-05）は**この層では行わない**（保存直前の1箇所に集約）
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from application.usecases.update_config import (
    EDITABLE_PATHS,
    LIST_SELECTORS,
    ConfigPatchError,
    apply_patch,
    apply_patch_with_paths,
)
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.config_validation import ConfigIssueCode, validate_config

INITIAL_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)


@pytest.fixture(scope="session")
def initial_raw() -> dict[str, Any]:
    return json.loads(INITIAL_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def config(initial_raw: dict[str, Any]) -> IntelligenceConfig:
    return IntelligenceConfig.model_validate(copy.deepcopy(initial_raw))


def codes(exc: ConfigPatchError) -> list[str]:
    return [issue.code.value for issue in exc.issues]


# --- 許可リストが §7.2 と一致する -------------------------------------------


def test_the_allow_list_matches_spec_7_2() -> None:
    """⚠️ ここが緩むと「固定項目は編集できない」（§7.4）が崩れる。

    §7.2 の表の10行に対応するパスと、表に行が無い4項目
    （`tunable_thresholds` 配下。モジュールの説明を参照）で構成される。
    """
    assert EDITABLE_PATHS == frozenset(
        {
            "scoring_axes.*.weight",
            "tunable_thresholds.min_total_score_to_publish",
            "tunable_thresholds.adoption_class_score_map.propose_next_meeting",
            "tunable_thresholds.adoption_class_score_map.reference_info",
            "tunable_thresholds.adoption_class_score_map.share_only",
            "exclusion_rules.*.enabled",
            "exclusion_rules.*.severity",
            "information_categories.*.priority",
            "tunable_thresholds.weekly.target_industries",
            "tunable_thresholds.weekly.max_industry_topics",
            "tunable_thresholds.weekly.max_common_topics",
            "tunable_thresholds.monthly.target_case_count",
            "tunable_thresholds.monthly.chapter_count_hint",
            "tunable_thresholds.dedup.lookback_weeks",
            "tunable_thresholds.dedup.title_similarity_threshold",
            "tunable_thresholds.dedup.treat_same_url_as_duplicate",
            # §5.2 に無い鍵（2026-08-16 の決定2。§11.1 の月次遡り月数）
            "tunable_thresholds.dedup.monthly_lookback_months",
            # §7.2 の表に行が無いが可変（→ T-38 で表へ追記）
            "tunable_thresholds.min_reliability_score_to_publish",
            "tunable_thresholds.weekly.point_of_week_required",
            "tunable_thresholds.monthly.min_score_for_case",
            "tunable_thresholds.monthly.require_editorial_and_closing",
        }
    )


def test_the_allow_list_covers_every_tunable_threshold(
    config: IntelligenceConfig,
) -> None:
    """`tunable_thresholds` に編集できない項目が残っていないこと。

    残ると、その値は `config.json` の手編集でしか変えられなくなる。
    """
    tunables = {
        path for path in EDITABLE_PATHS if path.startswith("tunable_thresholds.")
    }

    def leaves(value: Any, prefix: str) -> set[str]:
        if isinstance(value, dict):
            return {
                leaf
                for key, item in value.items()
                for leaf in leaves(item, f"{prefix}.{key}")
            }
        return {prefix}

    actual = leaves(
        config.tunable_thresholds.model_dump(mode="json"), "tunable_thresholds"
    )
    assert tunables == actual


def test_no_fixed_section_is_editable() -> None:
    """ID系・`scoring_total`・`schema_version`・`meta`・`enums` は許可リスト外。"""
    forbidden_prefixes = (
        "schema_version",
        "meta",
        "scoring_total",
        "enums",
        "required_tags",
        "source_whitelist_hint",
    )
    assert not [path for path in EDITABLE_PATHS if path.startswith(forbidden_prefixes)]
    # 配列要素の `id` / `no` は「どれか」を指すセレクタで、編集対象ではない。
    for section, selector in LIST_SELECTORS.items():
        assert f"{section}.*.{selector}" not in EDITABLE_PATHS


# --- 適用 --------------------------------------------------------------------


def test_an_empty_patch_changes_nothing(config: IntelligenceConfig) -> None:
    assert apply_patch(config, {}) == config


def test_a_list_entry_is_matched_by_its_identifier(config: IntelligenceConfig) -> None:
    """⚠️ 添字ではなく `id` / `no` で対応づける（並び替えられた一覧でもずれない）。"""
    patched = apply_patch(config, {"exclusion_rules": [{"no": 13, "enabled": False}]})

    by_no = {rule.no: rule for rule in patched.exclusion_rules}
    assert by_no[13].enabled is False
    assert by_no[1].enabled is True
    assert [rule.no for rule in patched.exclusion_rules] == list(range(1, 14))


def test_the_input_config_is_not_mutated(config: IntelligenceConfig) -> None:
    """呼び出し元が持つ現行 config を書き換えない（diff の before が壊れる）。"""
    before = config.model_dump(mode="json")

    apply_patch(config, {"scoring_axes": [{"id": "reliability", "weight": 11}]})

    assert config.model_dump(mode="json") == before


def test_meta_is_left_untouched(config: IntelligenceConfig) -> None:
    """`meta` は保存時にサーバが打ち直す（T-11）。この層では触らない。"""
    patched = apply_patch(
        config, {"tunable_thresholds": {"min_total_score_to_publish": 55}}
    )

    assert patched.meta == config.meta


# --- 拒否 --------------------------------------------------------------------


def test_nothing_is_applied_when_any_path_is_rejected(
    config: IntelligenceConfig,
) -> None:
    """⚠️ 部分適用しない。「一部だけ通った」に admin が気づけないため。"""
    with pytest.raises(ConfigPatchError):
        apply_patch(
            config,
            {
                "tunable_thresholds": {"min_total_score_to_publish": 55},
                "scoring_total": 90,
            },
        )

    assert config.tunable_thresholds.min_total_score_to_publish == 60


def test_every_violation_is_reported_at_once(config: IntelligenceConfig) -> None:
    with pytest.raises(ConfigPatchError) as exc_info:
        apply_patch(
            config,
            {
                "scoring_total": 90,
                "schema_version": "2.0",
                "scoring_axes": [{"id": "reliability", "label": "信頼度"}],
            },
        )

    assert codes(exc_info.value) == [ConfigIssueCode.FIELD_NOT_EDITABLE.value] * 3


def test_changing_an_identifier_is_impossible(config: IntelligenceConfig) -> None:
    """⚠️ ID を変えるには「存在しない ID を指す」しかなく、それは 422 になる。"""
    with pytest.raises(ConfigPatchError) as exc_info:
        apply_patch(config, {"scoring_axes": [{"id": "creativity", "weight": 10}]})

    assert codes(exc_info.value) == [ConfigIssueCode.UNKNOWN_TARGET.value]


def test_a_list_entry_without_a_selector_is_rejected(
    config: IntelligenceConfig,
) -> None:
    with pytest.raises(ConfigPatchError) as exc_info:
        apply_patch(config, {"scoring_axes": [{"weight": 10}]})

    assert codes(exc_info.value) == [ConfigIssueCode.UNKNOWN_TARGET.value]
    assert exc_info.value.issues[0].path == "scoring_axes.0"


def test_an_unknown_key_is_distinguished_from_a_fixed_one(
    config: IntelligenceConfig,
) -> None:
    """タイポ（`unknown_field`）と固定項目（`field_not_editable`）を区別する。"""
    with pytest.raises(ConfigPatchError) as exc_info:
        apply_patch(config, {"tunable_thresholdz": {}, "scoring_total": 90})

    assert codes(exc_info.value) == [
        ConfigIssueCode.UNKNOWN_FIELD.value,
        ConfigIssueCode.FIELD_NOT_EDITABLE.value,
    ]


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param({"scoring_axes": {"reliability": 10}}, id="list_as_object"),
        pytest.param({"scoring_axes": ["reliability"]}, id="entry_as_scalar"),
        pytest.param({"tunable_thresholds": 5}, id="object_as_scalar"),
    ],
)
def test_a_malformed_patch_shape_is_rejected(
    config: IntelligenceConfig, patch: dict[str, Any]
) -> None:
    with pytest.raises(ConfigPatchError) as exc_info:
        apply_patch(config, patch)

    assert codes(exc_info.value) == [ConfigIssueCode.INVALID_VALUE.value]


def test_a_value_outside_the_model_range_is_rejected(
    config: IntelligenceConfig,
) -> None:
    """モデル（T-04）の値域も `ConfigIssue` の形で返す（フロントの扱いを揃える）。"""
    with pytest.raises(ConfigPatchError) as exc_info:
        apply_patch(config, {"scoring_axes": [{"id": "reliability", "weight": 500}]})

    issue = exc_info.value.issues[0]
    assert issue.code is ConfigIssueCode.INVALID_VALUE
    assert issue.path == "scoring_axes.4.weight"


def test_an_unknown_enum_value_is_rejected(config: IntelligenceConfig) -> None:
    with pytest.raises(ConfigPatchError) as exc_info:
        apply_patch(config, {"exclusion_rules": [{"no": 1, "severity": "maybe"}]})

    assert codes(exc_info.value) == [ConfigIssueCode.INVALID_VALUE.value]


# --- 境界：クロスフィールド検証はこの層で行わない ----------------------------


def test_cross_field_rules_are_not_enforced_here(config: IntelligenceConfig) -> None:
    """⚠️ Σweight≠100 の候補も**ここでは作れる**。拒否するのは保存直前（T-05）。

    責務を1箇所に集約するための境界。ここでも弾くと、保存経路とドライラン
    （T-29）で判定が二重になり、片方だけ直す事故が起きる。
    """
    candidate = apply_patch(
        config, {"scoring_axes": [{"id": "customer_relevance", "weight": 30}]}
    )

    assert sum(axis.weight for axis in candidate.scoring_axes) == 105
    issues = validate_config(candidate)
    assert [issue.code for issue in issues] == [ConfigIssueCode.WEIGHT_SUM_MISMATCH]


def test_the_rejected_input_is_never_normalized(config: IntelligenceConfig) -> None:
    """⚠️ 設計判断A：合計を100へ按分する処理をこの層に足さないこと。"""
    candidate = apply_patch(
        config, {"scoring_axes": [{"id": "customer_relevance", "weight": 30}]}
    )

    weights = {axis.id: axis.weight for axis in candidate.scoring_axes}
    assert weights["customer_relevance"] == 30
    assert weights["practical_usability"] == 20  # 按分されていない


# --- 触れたパスの列挙（T-29 のドライランが仕分けに使う）----------------------


def test_the_touched_paths_use_the_allow_list_notation(
    config: IntelligenceConfig,
) -> None:
    """⚠️ **適用と列挙が同じ1本の走査であること**（表記がずれない）。

    配列要素は `*` に畳み、セレクタ（`id` / `no`）は「どの要素か」の指定なので
    含めない。ドライラン（T-29）はこの表記で `DETERMINISTIC_PATHS` と突き合わせる。
    """
    _, touched = apply_patch_with_paths(
        config,
        {
            "tunable_thresholds": {
                "min_total_score_to_publish": 62,
                "adoption_class_score_map": {"reference_info": 72},
            },
            "exclusion_rules": [
                {"no": 11, "enabled": False},
                {"no": 12, "enabled": False},
            ],
        },
    )

    assert set(touched) <= EDITABLE_PATHS
    assert set(touched) == {
        "tunable_thresholds.min_total_score_to_publish",
        "tunable_thresholds.adoption_class_score_map.reference_info",
        "exclusion_rules.*.enabled",
    }


def test_an_empty_patch_touches_nothing(config: IntelligenceConfig) -> None:
    assert apply_patch_with_paths(config, {})[1] == ()


def test_a_selector_alone_is_not_a_change(config: IntelligenceConfig) -> None:
    """`{"no": 11}` だけの要素は「どれか」の指定であって変更ではない。"""
    assert apply_patch_with_paths(config, {"exclusion_rules": [{"no": 11}]})[1] == ()


def test_a_rejected_patch_reports_no_paths(config: IntelligenceConfig) -> None:
    """1件でも違反があれば何も適用しない＝列挙も返らない（部分適用しない）。"""
    with pytest.raises(ConfigPatchError):
        apply_patch_with_paths(
            config,
            {
                "tunable_thresholds": {"min_total_score_to_publish": 62},
                "scoring_total": 90,
            },
        )
