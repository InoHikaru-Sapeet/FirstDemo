"""`config.json` のモデルと生成 JSON Schema（設計書 §2.1 ／ 仕様書 §5.2）。

設計書末尾の指示どおり、**§2.1 の Schema をこのテストの基準**にする。
入力は `data/config_initial.json`（仕様書 §5.2 の確定 config を逐語でコピーした
もの）で、実データがそのまま通ることと、固定値を変えると落ちることを固定する。

クロスフィールド制約（Σweight==100 の強制・降順整合・参照整合）は T-05 の
担当なので、ここでは **この層では弾かない** ことも併せて固定する。

実データのフィクスチャ（`initial_raw` / `raw`）は `conftest.py` にある。
"""

import copy
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from adapter.cli.export_config_schema import DEFAULT_OUTPUT, is_up_to_date
from enterprise.entities.config import (
    EXCLUSION_RULE_COUNT,
    INFORMATION_CATEGORY_COUNT,
    INFORMATION_CATEGORY_IDS,
    JSON_SCHEMA_ID,
    JSON_SCHEMA_TITLE,
    REQUIRED_TAG_COUNT,
    REQUIRED_TAG_IDS,
    SCORING_AXIS_COUNT,
    SCORING_AXIS_IDS,
    IntelligenceConfig,
    Priority,
    Severity,
    config_json_schema,
    config_json_schema_text,
)

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return config_json_schema()


def _set(raw: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """`tunable_thresholds.weekly.max_common_topics` や `scoring_axes.0.weight`
    形式のパスへ値を書き込む。"""
    keys = path.split(".")
    node: Any = raw
    for key in keys[:-1]:
        node = node[int(key)] if isinstance(node, list) else node[key]
    last = keys[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value
    return raw


def _error_paths(exc_info: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    """どのパスが弾かれたかを比較できる形にする。"""
    return {
        ".".join(str(part) for part in error["loc"])
        for error in exc_info.value.errors()
    }


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """`$ref` を1段たどる。§2.1 は定義をインライン展開しているため比較用。"""
    ref = node.get("$ref")
    if ref is None:
        return node
    return schema["$defs"][ref.removeprefix("#/$defs/")]


def _prop(schema: dict[str, Any], path: str) -> dict[str, Any]:
    """`tunable_thresholds.weekly.max_common_topics` のようなドット区切りで
    プロパティ定義を引く（`$ref` は自動でたどる）。"""
    node = schema
    for key in path.split("."):
        node = _resolve(schema, node["properties"][key])
    return node


def _objects(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """スキーマ中のオブジェクト定義（`properties` を持つノード）を全部集める。"""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "properties" in node:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found


# --- 仕様書 §5.2 の実データ -------------------------------------------------


def test_confirmed_config_is_accepted(raw: dict[str, Any]) -> None:
    """§5.2 の確定 config がそのまま通ること（移行 CLI の投入対象・T-14）。"""
    config = IntelligenceConfig.model_validate(raw)

    assert config.schema_version == "1.0"
    assert config.scoring_total == 100
    assert len(config.information_categories) == INFORMATION_CATEGORY_COUNT == 7
    assert len(config.required_tags) == REQUIRED_TAG_COUNT == 10
    assert len(config.scoring_axes) == SCORING_AXIS_COUNT == 6
    assert len(config.exclusion_rules) == EXCLUSION_RULE_COUNT == 13


def test_confirmed_ids_are_present_in_order(raw: dict[str, Any]) -> None:
    """ID とその順序は中間xlsx 互換の前提（仕様書 §5.1）。"""
    config = IntelligenceConfig.model_validate(raw)

    categories = tuple(c.id for c in config.information_categories)
    assert categories == INFORMATION_CATEGORY_IDS
    assert tuple(t.id for t in config.required_tags) == REQUIRED_TAG_IDS
    assert tuple(a.id for a in config.scoring_axes) == SCORING_AXIS_IDS
    assert [r.no for r in config.exclusion_rules] == list(range(1, 14))


def test_confirmed_weights_sum_to_the_scoring_total(raw: dict[str, Any]) -> None:
    """6軸の配点が 25+20+20+15+10+10=100（仕様書 §5.2）。

    合計100 の**強制**は T-05（設計判断A: 保存拒否）。ここは実データの確認。
    """
    config = IntelligenceConfig.model_validate(raw)

    assert [a.weight for a in config.scoring_axes] == [25, 20, 20, 15, 10, 10]
    assert sum(a.weight for a in config.scoring_axes) == config.scoring_total


def test_confirmed_initial_thresholds(raw: dict[str, Any]) -> None:
    """初期しきい値。移行時の一致チェック（設計書 §2.1.1-6）の基準になる値。"""
    tunable = IntelligenceConfig.model_validate(raw).tunable_thresholds

    assert tunable.min_total_score_to_publish == 60
    assert tunable.adoption_class_score_map.propose_next_meeting == 85
    assert tunable.adoption_class_score_map.reference_info == 70
    assert tunable.adoption_class_score_map.share_only == 60
    assert tunable.min_reliability_score_to_publish == 5
    assert tunable.weekly.target_industry == "不動産"
    assert tunable.weekly.max_industry_topics == 5
    assert tunable.weekly.max_common_topics == 6
    assert tunable.weekly.point_of_week_required is True
    assert tunable.monthly.target_case_count == 15
    assert tunable.monthly.chapter_count_hint == 5
    assert tunable.monthly.min_score_for_case == 80
    assert tunable.monthly.require_editorial_and_closing is True
    assert tunable.dedup.lookback_weeks == 8
    assert tunable.dedup.title_similarity_threshold == 0.85
    assert tunable.dedup.treat_same_url_as_duplicate is True


def test_round_trip_reproduces_the_source_json(initial_raw: dict[str, Any]) -> None:
    """読んで書き戻しても内容とキー順が変わらないこと。

    `config.json` はファイルが正（設計書 §8）で、revision 間の diff を監査ログに
    残す（§4.4）。モデルを通すだけでキー順や表現が動くと diff がノイズだらけに
    なるので、ラウンドトリップの同一性を固定する。
    """
    config = IntelligenceConfig.model_validate(initial_raw)

    dumped = config.model_dump(mode="json")

    assert dumped == initial_raw
    assert list(dumped) == list(initial_raw)


def test_updated_at_keeps_the_jst_offset(raw: dict[str, Any]) -> None:
    """日時は Asia/Tokyo 基準で扱う（設計書 §0・§14）。"""
    config = IntelligenceConfig.model_validate(raw)

    assert config.meta.updated_at is not None
    assert config.meta.updated_at == datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
    assert config.model_dump(mode="json")["meta"]["updated_at"] == (
        "2026-08-12T00:00:00+09:00"
    )


# --- 未知キーの拒否（§2.1 additionalProperties: false）----------------------


@pytest.mark.parametrize(
    ("path", "expected_loc"),
    [
        ("", "typo"),
        ("meta", "meta.typo"),
        ("information_categories.0", "information_categories.0.typo"),
        ("required_tags.0", "required_tags.0.typo"),
        ("scoring_axes.0", "scoring_axes.0.typo"),
        ("exclusion_rules.0", "exclusion_rules.0.typo"),
        ("enums", "enums.typo"),
        ("tunable_thresholds", "tunable_thresholds.typo"),
        (
            "tunable_thresholds.adoption_class_score_map",
            "tunable_thresholds.adoption_class_score_map.typo",
        ),
        ("tunable_thresholds.weekly", "tunable_thresholds.weekly.typo"),
        ("tunable_thresholds.monthly", "tunable_thresholds.monthly.typo"),
        ("tunable_thresholds.dedup", "tunable_thresholds.dedup.typo"),
    ],
)
def test_unknown_keys_are_rejected_everywhere(
    raw: dict[str, Any], path: str, expected_loc: str
) -> None:
    """タイポしたキーが黙って無視されると「設定したつもり」の事故になる。"""
    _set(raw, f"{path}.typo" if path else "typo", "うっかり")

    with pytest.raises(ValidationError) as exc_info:
        IntelligenceConfig.model_validate(raw)

    assert expected_loc in _error_paths(exc_info)


# --- 固定値（ID・const・件数）------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "information_categories.0.id",
        "required_tags.0.id",
        "scoring_axes.0.id",
    ],
)
def test_fixed_ids_cannot_be_renamed(raw: dict[str, Any], path: str) -> None:
    """IDを変えると中間xlsx の互換が壊れる（仕様書 §5.1）。Literal で型ごと拒否。"""
    _set(raw, path, "renamed_id")

    with pytest.raises(ValidationError) as exc_info:
        IntelligenceConfig.model_validate(raw)

    assert path in _error_paths(exc_info)


@pytest.mark.parametrize(
    ("section", "count"),
    [
        ("information_categories", INFORMATION_CATEGORY_COUNT),
        ("required_tags", REQUIRED_TAG_COUNT),
        ("scoring_axes", SCORING_AXIS_COUNT),
        ("exclusion_rules", EXCLUSION_RULE_COUNT),
    ],
)
def test_section_counts_are_fixed(
    raw: dict[str, Any], section: str, count: int
) -> None:
    """7カテゴリ / 10タグ / 6軸 / 13除外ルールを漏れなく保持する（仕様書 §5.1）。"""
    assert len(raw[section]) == count

    too_few = copy.deepcopy(raw)
    too_few[section] = too_few[section][:-1]
    with pytest.raises(ValidationError):
        IntelligenceConfig.model_validate(too_few)

    too_many = copy.deepcopy(raw)
    too_many[section] = [*too_many[section], copy.deepcopy(too_many[section][0])]
    with pytest.raises(ValidationError):
        IntelligenceConfig.model_validate(too_many)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("schema_version", "2.0"),
        ("scoring_total", 90),
        ("meta.config_name", "something_else"),
        ("meta.source_of_truth_xlsx", "other.xlsx"),
        ("meta.editable_by", ["editor"]),
        ("meta.visible_to", ["viewer"]),
        ("meta.editable_by", []),
        ("meta.visible_to", []),
        ("required_tags.0.required", False),
        ("required_tags.0.type", "single_or_multi"),
        ("scoring_axes.0.bands", []),
    ],
)
def test_fixed_values_are_rejected_when_changed(
    raw: dict[str, Any], path: str, value: Any
) -> None:
    """config は admin 以外に露出しない前提（§6.1）で、config を見せない代わりに
    構造の固定はサーバ側で強制する。"""
    _set(raw, path, value)

    with pytest.raises(ValidationError) as exc_info:
        IntelligenceConfig.model_validate(raw)

    assert any(error.startswith(path) for error in _error_paths(exc_info))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("enums.priority", ["low", "mid", "middle_high", "high"]),
        ("enums.severity", ["full_exclude", "soft_exclude"]),
        ("enums.reliability", ["高", "中程度", "要確認", "低"]),
        ("enums.customer_relevance", ["直接関係あり"]),
        ("enums.practical_usability", ["即活用"]),
        ("enums.adoption_class", ["次回定例で提案する"]),
        ("enums.region", ["国内"]),
        ("enums.info_type", ["一次情報（公式発表）"]),
        ("information_categories.0.priority", "middle"),
        ("exclusion_rules.0.severity", "always_exclude"),
    ],
)
def test_enum_values_are_the_confirmed_japanese_values(
    raw: dict[str, Any], path: str, value: Any
) -> None:
    """enum の日本語値は確定値。推測で変えると採点・分類が config 外の値を
    取りうる（T-19 が出力スキーマの enum をここから生成する）。"""
    _set(raw, path, value)

    with pytest.raises(ValidationError):
        IntelligenceConfig.model_validate(raw)


def test_industry_and_business_area_accept_new_values(raw: dict[str, Any]) -> None:
    """業界・業務領域だけは運用で増減しうるので自由文字列（§2.1）。"""
    raw["enums"]["industry"] = [*raw["enums"]["industry"], "建設"]
    raw["enums"]["business_area"] = [*raw["enums"]["business_area"], "内部監査"]

    config = IntelligenceConfig.model_validate(raw)

    assert "建設" in config.enums.industry
    assert "内部監査" in config.enums.business_area


# --- 値域 --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("meta.revision", 0),
        ("scoring_axes.0.weight", -1),
        ("scoring_axes.0.weight", 101),
        ("exclusion_rules.0.no", 0),
        ("exclusion_rules.0.no", 14),
        ("tunable_thresholds.min_total_score_to_publish", -1),
        ("tunable_thresholds.min_total_score_to_publish", 101),
        ("tunable_thresholds.adoption_class_score_map.propose_next_meeting", 101),
        ("tunable_thresholds.adoption_class_score_map.reference_info", -1),
        ("tunable_thresholds.adoption_class_score_map.share_only", 101),
        # 信頼性は6軸中 0-10 点なので上限 10（仕様書 §5.2 scoring_axes.reliability）
        ("tunable_thresholds.min_reliability_score_to_publish", 11),
        ("tunable_thresholds.min_reliability_score_to_publish", -1),
        ("tunable_thresholds.weekly.max_industry_topics", -1),
        ("tunable_thresholds.weekly.max_common_topics", -1),
        ("tunable_thresholds.monthly.target_case_count", -1),
        ("tunable_thresholds.monthly.chapter_count_hint", -1),
        ("tunable_thresholds.monthly.min_score_for_case", 101),
        ("tunable_thresholds.dedup.lookback_weeks", -1),
        ("tunable_thresholds.dedup.title_similarity_threshold", 1.01),
        ("tunable_thresholds.dedup.title_similarity_threshold", -0.01),
    ],
)
def test_out_of_range_values_are_rejected(
    raw: dict[str, Any], path: str, value: Any
) -> None:
    _set(raw, path, value)

    with pytest.raises(ValidationError) as exc_info:
        IntelligenceConfig.model_validate(raw)

    assert path in _error_paths(exc_info)


# --- 可変項目 ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("scoring_axes.0.weight", 30),
        ("exclusion_rules.0.severity", Severity.LOW_PRIORITY.value),
        ("exclusion_rules.0.enabled", False),
        ("information_categories.0.priority", Priority.LOW.value),
        ("tunable_thresholds.min_total_score_to_publish", 65),
        ("tunable_thresholds.adoption_class_score_map.propose_next_meeting", 90),
        ("tunable_thresholds.min_reliability_score_to_publish", 7),
        ("tunable_thresholds.weekly.target_industry", "金融"),
        ("tunable_thresholds.weekly.max_industry_topics", 8),
        ("tunable_thresholds.weekly.point_of_week_required", False),
        ("tunable_thresholds.monthly.target_case_count", 12),
        ("tunable_thresholds.monthly.require_editorial_and_closing", False),
        ("tunable_thresholds.dedup.title_similarity_threshold", 0.9),
        ("tunable_thresholds.dedup.treat_same_url_as_duplicate", False),
    ],
)
def test_editable_parameters_stay_editable(
    raw: dict[str, Any], path: str, value: Any
) -> None:
    """仕様書 §7.2 の編集可能パラメータを固定してしまっていないこと。"""
    _set(raw, path, value)

    IntelligenceConfig.model_validate(raw)


def test_cross_field_rules_are_not_enforced_here(raw: dict[str, Any]) -> None:
    """クロスフィールド制約はこの層では弾かない（設計書 §2.1.1 → T-05）。

    JSON Schema 単体で表現できない制約をここへ混ぜると、生成スキーマと
    モデルの守備範囲がずれる。合計100 の保存拒否（設計判断A）・降順整合・
    参照整合は T-05 の責務。
    """
    _set(raw, "scoring_axes.0.weight", 99)  # 合計 174
    _set(raw, "tunable_thresholds.adoption_class_score_map.share_only", 95)  # 降順崩れ
    _set(raw, "tunable_thresholds.weekly.target_industry", "存在しない業界")

    config = IntelligenceConfig.model_validate(raw)

    assert sum(a.weight for a in config.scoring_axes) != config.scoring_total


# --- 任意項目 ----------------------------------------------------------------


def test_optional_meta_fields_may_be_omitted(raw: dict[str, Any]) -> None:
    """初期投入時は updated_by が null（設計書 §10.3 手順6）。"""
    del raw["meta"]["updated_at"]
    del raw["meta"]["updated_by"]

    config = IntelligenceConfig.model_validate(raw)

    assert config.meta.updated_at is None
    assert config.meta.updated_by is None


def test_source_whitelist_hint_defaults_to_empty(raw: dict[str, Any]) -> None:
    """§2.1 の top-level required に含まれない任意項目。"""
    del raw["source_whitelist_hint"]

    assert IntelligenceConfig.model_validate(raw).source_whitelist_hint == []


@pytest.mark.parametrize(
    "key",
    [
        "schema_version",
        "meta",
        "information_categories",
        "required_tags",
        "scoring_axes",
        "scoring_total",
        "exclusion_rules",
        "enums",
        "tunable_thresholds",
    ],
)
def test_required_sections_cannot_be_omitted(raw: dict[str, Any], key: str) -> None:
    del raw[key]

    with pytest.raises(ValidationError) as exc_info:
        IntelligenceConfig.model_validate(raw)

    assert key in _error_paths(exc_info)


# --- 生成 JSON Schema が §2.1 と一致すること --------------------------------


def test_dialect_and_identity_match_the_design(schema: dict[str, Any]) -> None:
    assert schema["$schema"] == DRAFT_2020_12
    assert schema["$id"] == JSON_SCHEMA_ID
    assert schema["title"] == JSON_SCHEMA_TITLE
    assert schema["type"] == "object"
    # §2.1 と同じ並び（$schema → $id → title → 本体 → $defs）で読めること
    assert list(schema)[:3] == ["$schema", "$id", "title"]
    assert list(schema)[-1] == "$defs"


def test_top_level_required_matches_the_design(schema: dict[str, Any]) -> None:
    assert schema["required"] == [
        "schema_version",
        "meta",
        "information_categories",
        "required_tags",
        "scoring_axes",
        "scoring_total",
        "exclusion_rules",
        "enums",
        "tunable_thresholds",
    ]
    assert "source_whitelist_hint" in schema["properties"]


def test_every_object_forbids_additional_properties(schema: dict[str, Any]) -> None:
    """§2.1 は全オブジェクトが `additionalProperties: false`。"""
    objects = _objects(schema)

    assert len(objects) == 12  # root + 11 のネストモデル
    for node in objects:
        assert node["additionalProperties"] is False


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("meta.config_name", "ai_intelligence_requirements"),
        ("meta.source_of_truth_xlsx", "weekly_ai_intelligence_requirements.xlsx"),
        ("schema_version", "1.0"),
        ("scoring_total", 100),
    ],
)
def test_fixed_values_are_emitted_as_const(
    schema: dict[str, Any], path: str, expected: Any
) -> None:
    assert _prop(schema, path)["const"] == expected


def test_required_tag_required_flag_is_const_true(schema: dict[str, Any]) -> None:
    """10タグは全部必須なので `required` は `true` 固定（§2.1）。"""
    tag = _resolve(schema, _prop(schema, "required_tags")["items"])

    assert tag["properties"]["required"]["const"] is True
    assert tag["properties"]["type"]["enum"] == ["single", "multi", "enum"]


@pytest.mark.parametrize(
    ("section", "ids", "count"),
    [
        ("information_categories", INFORMATION_CATEGORY_IDS, 7),
        ("required_tags", REQUIRED_TAG_IDS, 10),
        ("scoring_axes", SCORING_AXIS_IDS, 6),
    ],
)
def test_fixed_ids_and_counts_are_in_the_schema(
    schema: dict[str, Any], section: str, ids: tuple[str, ...], count: int
) -> None:
    array = _prop(schema, section)

    assert array["minItems"] == array["maxItems"] == count
    item = _resolve(schema, array["items"])
    assert item["properties"]["id"]["enum"] == list(ids)


def test_exclusion_rules_are_thirteen_numbered_rules(schema: dict[str, Any]) -> None:
    array = _prop(schema, "exclusion_rules")

    assert array["minItems"] == array["maxItems"] == 13
    rule = _resolve(schema, array["items"])
    assert rule["required"] == ["no", "severity", "enabled", "name", "examples"]
    assert rule["properties"]["no"]["minimum"] == 1
    assert rule["properties"]["no"]["maximum"] == 13
    assert rule["properties"]["severity"]["$ref"] == "#/$defs/severity"


def test_shared_defs_are_referenced_by_the_names_the_design_uses(
    schema: dict[str, Any],
) -> None:
    """設計書 §2.1 が `#/$defs/priority` / `#/$defs/severity` を参照している。"""
    assert schema["$defs"]["priority"]["enum"] == ["low", "mid", "mid_high", "high"]
    assert schema["$defs"]["severity"]["enum"] == [
        "full_exclude",
        "default_exclude",
        "low_priority",
        "low_priority_or_exclude",
        "merge",
    ]
    category = _resolve(schema, _prop(schema, "information_categories")["items"])
    assert category["properties"]["priority"]["$ref"] == "#/$defs/priority"


def test_enums_section_matches_the_design(schema: dict[str, Any]) -> None:
    enums = _prop(schema, "enums")

    assert enums["required"] == [
        "priority",
        "severity",
        "reliability",
        "customer_relevance",
        "practical_usability",
        "adoption_class",
        "region",
        "info_type",
        "industry",
        "business_area",
    ]

    items = {key: value["items"] for key, value in enums["properties"].items()}
    assert items["priority"] == {"$ref": "#/$defs/priority"}
    assert items["severity"] == {"$ref": "#/$defs/severity"}
    assert items["reliability"]["enum"] == ["高", "中", "要確認", "低"]
    assert items["customer_relevance"]["enum"] == [
        "直接関係",
        "近く応用可能",
        "テーマ一部参考",
        "一般参考",
        "関連薄い",
    ]
    assert items["practical_usability"]["enum"] == [
        "すぐ活用",
        "具体例参考",
        "参考になる",
        "追加解釈が必要",
        "一般的",
        "見込み薄い",
    ]
    assert items["adoption_class"]["enum"] == [
        "次回定例で提案",
        "参考情報",
        "共有のみ",
        "不採用",
    ]
    assert items["region"]["enum"] == ["日本", "海外", "グローバル"]
    assert items["info_type"]["enum"] == [
        "一次情報(公式発表)",
        "主要メディア報道",
        "専門メディア報道",
        "ブログ・プレスリリース",
        "個人SNS・二次情報",
    ]
    # 運用で増減しうる2つだけ自由文字列
    assert items["industry"] == {"type": "string"}
    assert items["business_area"] == {"type": "string"}


def test_tunable_thresholds_ranges_match_the_design(schema: dict[str, Any]) -> None:
    tunable = _prop(schema, "tunable_thresholds")

    assert tunable["required"] == [
        "min_total_score_to_publish",
        "adoption_class_score_map",
        "min_reliability_score_to_publish",
        "weekly",
        "monthly",
        "dedup",
    ]

    minimum_total = tunable["properties"]["min_total_score_to_publish"]
    assert (minimum_total["minimum"], minimum_total["maximum"]) == (0, 100)

    reliability = tunable["properties"]["min_reliability_score_to_publish"]
    assert (reliability["minimum"], reliability["maximum"]) == (0, 10)

    threshold = _prop(schema, "tunable_thresholds.dedup.title_similarity_threshold")
    assert threshold["type"] == "number"
    assert (threshold["minimum"], threshold["maximum"]) == (0, 1)

    weight = _resolve(schema, _prop(schema, "scoring_axes")["items"])
    assert weight["properties"]["weight"]["type"] == "integer"
    assert (
        weight["properties"]["weight"]["minimum"],
        weight["properties"]["weight"]["maximum"],
    ) == (0, 100)


def test_meta_required_and_nullable_fields_match_the_design(
    schema: dict[str, Any],
) -> None:
    meta = _prop(schema, "meta")

    assert meta["required"] == [
        "config_name",
        "source_of_truth_xlsx",
        "editable_by",
        "visible_to",
        "revision",
    ]
    assert meta["properties"]["revision"]["minimum"] == 1
    assert meta["properties"]["editable_by"]["minItems"] == 1
    assert meta["properties"]["editable_by"]["items"]["const"] == "admin"
    assert meta["properties"]["visible_to"]["items"]["const"] == "admin"
    # `updated_at` は date-time もしくは null（§2.1 の `type: ["string","null"]`）
    assert meta["properties"]["updated_at"]["anyOf"] == [
        {"format": "date-time", "type": "string"},
        {"type": "null"},
    ]
    assert {"type": "null"} in meta["properties"]["updated_by"]["anyOf"]


# --- 生成物のドリフト検知 ----------------------------------------------------


def test_committed_schema_file_is_up_to_date() -> None:
    """`schemas/config.schema.json` はモデルからの生成物。

    モデルを変えたら `make config-schema` で生成し直してコミットする。
    """
    assert DEFAULT_OUTPUT.is_file(), f"{DEFAULT_OUTPUT} が無い（make config-schema）"
    assert is_up_to_date(DEFAULT_OUTPUT), (
        "schemas/config.schema.json が古い。`make config-schema` を実行してコミットする"
    )


def test_schema_file_is_valid_utf8_json_with_readable_japanese() -> None:
    """入出力はすべて UTF-8（設計書 §14）。日本語 enum をエスケープしない。"""
    text = DEFAULT_OUTPUT.read_text(encoding="utf-8")

    assert "要確認" in text
    assert text == config_json_schema_text()
    assert json.loads(text)["$schema"] == DRAFT_2020_12
