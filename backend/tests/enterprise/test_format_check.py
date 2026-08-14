"""スコアリング根拠フォーマットチェック（設計書 §6.5 ／ 仕様書 §12 ／ T-20）。

**§12.1 の必須項目を1つずつ壊して、error になることを確かめる**。基準は
`data/config_initial.json`（§5.2 の確定値）で、期待値もそこから引く
（軸点の上限・enum の実値をテスト側にベタ書きしない）。

`error` と `warning` の区分（§12.2）は `ok` の意味と直結している:
error があると `ok=false` かつ**その記事は本編から外れる**、warning は残る。
"""

from typing import Any

import pytest

from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.report_columns import (
    EXCLUSION_LOG_COLUMNS,
    WEEKLY_ARTICLE_COLUMNS,
    WEEKLY_ARTICLE_SHEET,
    WEEKLY_AXIS_SCORE_COLUMNS,
    WEEKLY_TAG_COLUMNS,
    axis_score_bounds,
    header_row,
)
from enterprise.entities.validation_report import (
    dump_validation_report,
    parse_validation_report,
)
from enterprise.services.format_check import (
    CATEGORY_FORMAT_ERROR,
    MIN_SUMMARY_SENTENCES,
    REASON_VALIDATION_ERROR,
    allowed_values,
    check_article,
    check_articles,
    count_sentences,
    format_error_log_entry,
    format_error_log_row,
)

FIRST_DATA_ROW = WEEKLY_ARTICLE_SHEET.first_data_row


def _valid_record() -> dict[str, Any]:
    """§12.1 をすべて満たす行（週次22列）。

    6軸の和 10+8+12+16+15+20 = 81 を `合計スコア` に置く。
    """
    return {
        "収集日": "2026-07-27",
        "情報カテゴリ": "ai_agent_automation",
        "タイトル": "OpenAI が企業向けエージェント基盤を発表",
        "一言要約": (
            "OpenAI が企業向けのエージェント基盤を発表した。"
            "既存の業務システムと連携できる点が新しい。"
        ),
        "合計スコア": 81,
        "緊急性鮮度_点": 10,
        "信頼性_点": 8,
        "アドバイザリー活用度_点": 12,
        "AI業界市場インパクト_点": 16,
        "実務活用可能性_点": 15,
        "顧客関連度_点": 20,
        "レポート採用区分": "参考情報",
        "実務活用可能性": "具体例参考",
        "顧客関連度": "近く応用可能",
        "信頼性": "高",
        "地域": ["海外"],
        "情報種別": "主要メディア報道",
        "業務領域": ["営業"],
        "業界": ["不動産"],
        "AIテーマ": ["AIエージェント"],
        "ソース": "TechCrunch",
        "URL": "https://example.com/news/agent-platform",
    }


def _check(record: dict[str, Any], config: IntelligenceConfig) -> Any:
    return check_article(record, config, row=FIRST_DATA_ROW)


def _error_fields(record: dict[str, Any], config: IntelligenceConfig) -> list[str]:
    return [issue.field for issue in _check(record, config).errors]


# --- 正常系 -----------------------------------------------------------------


def test_a_complete_record_passes(config: IntelligenceConfig) -> None:
    issues = _check(_valid_record(), config)

    assert issues.errors == []
    assert issues.warnings == []
    assert not issues.has_error
    assert issues.row == FIRST_DATA_ROW


def test_the_fixture_covers_every_column(config: IntelligenceConfig) -> None:
    """行が22列そろっていること（列定義との取り違えを防ぐ）。"""
    assert set(_valid_record()) == {column.name for column in WEEKLY_ARTICLE_COLUMNS}
    assert len(WEEKLY_ARTICLE_COLUMNS) == 22


def test_the_fixture_total_is_the_axis_sum() -> None:
    record = _valid_record()
    assert record["合計スコア"] == sum(
        record[column.name] for column in WEEKLY_AXIS_SCORE_COLUMNS
    )


# --- 非空（§12.1：10必須タグ／一言要約・URL・ソース・収集日）----------------


@pytest.mark.parametrize(
    "column", [column for column in WEEKLY_ARTICLE_COLUMNS if column.required_non_empty]
)
@pytest.mark.parametrize("empty_value", [None, "", "   ", []])
def test_every_required_field_must_be_non_empty(
    config: IntelligenceConfig, column: Any, empty_value: Any
) -> None:
    """非空必須の列は、空・空白だけ・空の列のいずれでも error（§12.1）。"""
    record = _valid_record()
    record[column.name] = empty_value

    assert column.name in _error_fields(record, config)


@pytest.mark.parametrize("column", WEEKLY_TAG_COLUMNS)
def test_a_missing_required_tag_is_an_error(
    config: IntelligenceConfig, column: Any
) -> None:
    """10必須タグの欠落は error（§12.2）。列そのものが無い場合も同じ。"""
    record = _valid_record()
    del record[column.name]

    issues = _check(record, config)

    assert column.name in [issue.field for issue in issues.errors]
    assert issues.has_error


def test_there_are_ten_required_tags() -> None:
    """§12.1 の10タグ。列定義側の確定値と突き合わせる。"""
    assert len(WEEKLY_TAG_COLUMNS) == 10


def test_a_title_may_be_empty(config: IntelligenceConfig) -> None:
    """⚠️ タイトルは §12.1 の非空必須リストに無い（T-07 の注記どおり）。"""
    record = _valid_record()
    record["タイトル"] = ""

    assert _check(record, config).errors == []


def test_a_zero_score_is_not_empty(config: IntelligenceConfig) -> None:
    """0点は「空」ではない（合計も合わせれば正当な行）。"""
    record = _valid_record()
    record["緊急性鮮度_点"] = 0
    record["合計スコア"] = 71

    assert _check(record, config).errors == []


# --- 6軸の範囲（§12.1）------------------------------------------------------


@pytest.mark.parametrize("column", WEEKLY_AXIS_SCORE_COLUMNS)
def test_an_axis_score_above_its_weight_is_an_error(
    config: IntelligenceConfig, column: Any
) -> None:
    """上限は config の `weight`（静的な value_range ではない）。"""
    _, high = axis_score_bounds(config)[column.axis_id]
    record = _valid_record()
    record[column.name] = high + 1
    record["合計スコア"] = sum(record[axis.name] for axis in WEEKLY_AXIS_SCORE_COLUMNS)

    reasons = [
        issue.reason
        for issue in _check(record, config).errors
        if issue.field == column.name
    ]

    assert reasons and "範囲外" in reasons[0]


@pytest.mark.parametrize("column", WEEKLY_AXIS_SCORE_COLUMNS)
def test_a_negative_axis_score_is_an_error(
    config: IntelligenceConfig, column: Any
) -> None:
    record = _valid_record()
    record[column.name] = -1
    record["合計スコア"] = sum(record[axis.name] for axis in WEEKLY_AXIS_SCORE_COLUMNS)

    assert column.name in _error_fields(record, config)


def test_the_range_follows_the_config_weight(raw: dict[str, Any]) -> None:
    """admin が weight を下げたら、その軸の上限も下がる（検査だけ旧値にならない）。

    顧客関連度 25 → 20 / 実務活用可能性 20 → 25 に振り替える（合計100は維持）。
    """
    for axis in raw["scoring_axes"]:
        if axis["id"] == "customer_relevance":
            axis["weight"] = 20
        if axis["id"] == "practical_usability":
            axis["weight"] = 25
    config = IntelligenceConfig.model_validate(raw)

    record = _valid_record()
    record["顧客関連度_点"] = 25  # 旧上限。新しい weight では範囲外
    record["合計スコア"] = sum(record[axis.name] for axis in WEEKLY_AXIS_SCORE_COLUMNS)

    assert "顧客関連度_点" in _error_fields(record, config)


@pytest.mark.parametrize("value", ["10", 10.5, True])
def test_a_non_integer_axis_score_is_an_error(
    config: IntelligenceConfig, value: Any
) -> None:
    """点数は整数（§13.3-4）。`True` を 1 点と読み替えない。"""
    record = _valid_record()
    record["緊急性鮮度_点"] = value

    reasons = [
        issue.reason
        for issue in _check(record, config).errors
        if issue.field == "緊急性鮮度_点"
    ]

    assert reasons and "整数" in reasons[0]


# --- 合計スコア（§12.1）-----------------------------------------------------


def test_a_total_that_differs_from_the_axis_sum_is_an_error(
    config: IntelligenceConfig,
) -> None:
    """LLM の申告した合計が6軸の和と食い違ったら error（§12.2）。"""
    record = _valid_record()
    record["合計スコア"] = 82  # 6軸の和は 81

    errors = [issue for issue in _check(record, config).errors]

    assert [issue.field for issue in errors] == ["合計スコア"]
    assert "6軸の和と不一致" in errors[0].reason
    assert "81" in errors[0].reason


def test_the_total_is_not_compared_when_an_axis_is_missing(
    config: IntelligenceConfig,
) -> None:
    """軸が欠けているときに「和が違う」と重ねて言わない（直す先は同じ）。"""
    record = _valid_record()
    del record["緊急性鮮度_点"]

    assert _error_fields(record, config) == ["緊急性鮮度_点"]


# --- enum（§12.1：未定義値はエラー）-----------------------------------------


@pytest.mark.parametrize(
    ("column_name", "bad_value"),
    [
        ("情報カテゴリ", "ai_unknown_category"),
        ("レポート採用区分", "とりあえず保留"),
        ("実務活用可能性", "たぶん使える"),
        ("顧客関連度", "たぶん関係ある"),
        ("信頼性", "ふつう"),
        ("情報種別", "社内メモ"),
    ],
)
def test_a_value_outside_the_config_enums_is_an_error(
    config: IntelligenceConfig, column_name: str, bad_value: str
) -> None:
    record = _valid_record()
    record[column_name] = bad_value

    reasons = [
        issue.reason
        for issue in _check(record, config).errors
        if issue.field == column_name
    ]

    assert reasons and "config に無い値" in reasons[0]


@pytest.mark.parametrize("column_name", ["地域", "業務領域", "業界"])
def test_multi_columns_are_checked_element_by_element(
    config: IntelligenceConfig, column_name: str
) -> None:
    """multi 列は要素ごとに enum を見る（1つでも外れたら error）。"""
    record = _valid_record()
    record[column_name] = [record[column_name][0], "存在しない値"]

    assert column_name in _error_fields(record, config)


def test_a_free_column_is_not_checked_against_enums(
    config: IntelligenceConfig,
) -> None:
    """`AIテーマ` は `free_controlled`（設計書 §2.1）なので値を縛らない。"""
    record = _valid_record()
    record["AIテーマ"] = ["まったく新しいテーマ", "もう一つ"]

    assert _check(record, config).errors == []


def test_enum_values_come_from_the_config(raw: dict[str, Any]) -> None:
    """config の `enums` を増やせば、その値が通るようになる。"""
    raw["enums"]["industry"].append("宇宙開発")
    config = IntelligenceConfig.model_validate(raw)

    record = _valid_record()
    record["業界"] = ["宇宙開発"]

    assert _check(record, config).errors == []


def test_allowed_values_resolves_each_value_source(
    config: IntelligenceConfig,
) -> None:
    """`value_source` の3種類（`enums.*` / カテゴリID / 自由）の解決。"""
    by_name = {column.name: column for column in WEEKLY_ARTICLE_COLUMNS}

    assert allowed_values(by_name["情報カテゴリ"], config) == frozenset(
        category.id for category in config.information_categories
    )
    assert allowed_values(by_name["信頼性"], config) == frozenset(
        config.enums.reliability
    )
    assert allowed_values(by_name["AIテーマ"], config) is None
    assert allowed_values(by_name["タイトル"], config) is None


# --- warning（§12.2：要約が短すぎる等）--------------------------------------


def test_a_one_sentence_summary_is_a_warning_not_an_error(
    config: IntelligenceConfig,
) -> None:
    """短い要約は warning。**記事は本編に残る**（§12.2）。"""
    record = _valid_record()
    record["一言要約"] = "OpenAI がエージェント基盤を発表した。"

    issues = _check(record, config)

    assert issues.errors == []
    assert [issue.field for issue in issues.warnings] == ["一言要約"]
    assert not issues.has_error


def test_a_two_sentence_summary_is_fine(config: IntelligenceConfig) -> None:
    """§8.1 の「2〜3文」を満たせば warning は出ない。"""
    record = _valid_record()
    record["一言要約"] = "OpenAI が基盤を発表した。既存システムと連携できる。"

    assert _check(record, config).warnings == []
    assert MIN_SUMMARY_SENTENCES == 2


def test_an_empty_summary_is_an_error_without_a_duplicate_warning(
    config: IntelligenceConfig,
) -> None:
    """空の要約は error（非空必須）。短さの warning は重ねない。"""
    record = _valid_record()
    record["一言要約"] = ""

    issues = _check(record, config)

    assert [issue.field for issue in issues.errors] == ["一言要約"]
    assert issues.warnings == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("一文だけ。", 1),
        ("一文目。二文目。", 2),
        ("句点が無い要約", 1),
        ("A. B! C?", 3),
        ("全角も。半角も.混ぜる！", 3),
        ("。。。", 0),
        ("", 0),
    ],
)
def test_count_sentences(text: str, expected: int) -> None:
    assert count_sentences(text) == expected


# --- レポートと採否（§12.2）-------------------------------------------------


def test_the_report_separates_accepted_and_rejected(
    config: IntelligenceConfig,
) -> None:
    """error のある記事だけが本編から外れる（warning だけの記事は残る）。"""
    valid = _valid_record()
    warned = _valid_record() | {"一言要約": "一文だけの要約。"}
    broken = _valid_record() | {"合計スコア": 99}

    result = check_articles([valid, warned, broken], config)

    assert result.accepted == [valid, warned]
    assert [rejected.record for rejected in result.rejected] == [broken]
    assert result.report.ok is False
    assert [issue.field for issue in result.report.errors] == ["合計スコア"]
    assert [issue.field for issue in result.report.warnings] == ["一言要約"]


def test_a_clean_run_reports_ok(config: IntelligenceConfig) -> None:
    result = check_articles([_valid_record(), _valid_record()], config)

    assert result.report.ok is True
    assert result.report.errors == []
    assert len(result.accepted) == 2
    assert result.rejected == []


def test_an_empty_run_reports_ok(config: IntelligenceConfig) -> None:
    result = check_articles([], config)

    assert result.report.ok is True
    assert result.accepted == []


def test_row_numbers_follow_the_sheet_layout(config: IntelligenceConfig) -> None:
    """行番号は週次シートのレイアウト（4行目ヘッダ / 5行目からデータ）に従う。"""
    broken = _valid_record() | {"合計スコア": 99}

    result = check_articles([_valid_record(), broken, broken], config)

    assert [issue.row for issue in result.report.errors] == [
        FIRST_DATA_ROW + 1,
        FIRST_DATA_ROW + 2,
    ]
    assert FIRST_DATA_ROW == 5


def test_the_report_round_trips_as_json(config: IntelligenceConfig) -> None:
    """`validation_{period}.json` として書き出して読み戻せる（設計書 §2.4）。"""
    result = check_articles([_valid_record() | {"合計スコア": 99}], config)

    restored = parse_validation_report(dump_validation_report(result.report))

    assert restored == result.report
    assert restored.ok is False
    assert restored.errors[0].row == FIRST_DATA_ROW


def test_all_issues_are_reported_at_once(config: IntelligenceConfig) -> None:
    """1件目で打ち切らない（修正のための一覧として使えること）。"""
    record = _valid_record()
    record["一言要約"] = ""
    record["信頼性"] = "ふつう"
    record["合計スコア"] = 99

    fields = _error_fields(record, config)

    assert set(fields) == {"一言要約", "信頼性", "合計スコア"}


# --- 除外ログ（6列）---------------------------------------------------------


def test_the_format_error_log_row_follows_the_column_definition(
    config: IntelligenceConfig,
) -> None:
    """`除外区分=フォーマット不備`（§12.2）。"""
    record = _valid_record() | {"合計スコア": 99}
    result = check_articles([record], config)

    entry = format_error_log_entry(result.rejected[0].record)
    row = format_error_log_row(result.rejected[0].record)

    assert list(entry) == header_row(EXCLUSION_LOG_COLUMNS)
    assert row == [
        record["収集日"],
        record["タイトル"],
        record["URL"],
        record["ソース"],
        CATEGORY_FORMAT_ERROR,
        REASON_VALIDATION_ERROR,
    ]
    assert len(row) == len(EXCLUSION_LOG_COLUMNS) == 6


def test_a_record_missing_its_log_fields_is_still_logged(
    config: IntelligenceConfig,
) -> None:
    """ログ用の4項目が空でも記録は落とさない。

    落とすと「本編にも除外ログにも無い記事」ができ、消えた理由を追えなくなる。
    """
    record = _valid_record()
    for field in ("収集日", "URL", "ソース"):
        record[field] = ""
    del record["タイトル"]

    row = format_error_log_row(record)

    # 無い列は空セル（None）、空文字はそのまま。行そのものは必ず出る。
    assert row == ["", None, "", "", CATEGORY_FORMAT_ERROR, REASON_VALIDATION_ERROR]
