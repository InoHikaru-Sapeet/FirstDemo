"""中間xlsx の列スキーマ（設計書 §2.2 ／ 仕様書 §8）。

列名と順序は顧客指定の既存ファイルに合わせた確定値なので、**この一覧をベタ書きで
固定する**。設計書末尾の指示どおり、ここと config の Schema（T-04/T-05）が
単体テストの基準になる。

配点整合（§2.2.1 の確認事項「軸点の上限＝10+10+15+20+20+25＝100」）は
`test_axis_score_upper_bounds_sum_to_the_scoring_total` で固定している。
"""

from datetime import date, datetime
from typing import Any

import pytest

from enterprise.entities.config import (
    REQUIRED_TAG_IDS,
    SCORING_AXIS_IDS,
    SCORING_TOTAL,
    ConfigEnums,
    IntelligenceConfig,
)
from enterprise.entities.report_columns import (
    EXCLUSION_LOG_COLUMNS,
    EXCLUSION_LOG_SHEET,
    EXCLUSION_LOG_SHEET_NAME,
    MONTHLY_CASE_COLUMNS,
    MONTHLY_CASE_SHEET,
    MULTI_VALUE_SEPARATOR,
    ORGANIZATION_SEPARATOR,
    PARAGRAPH_SEPARATOR,
    SOURCE_MERGE_SEPARATOR,
    WEEKLY_ARTICLE_COLUMNS,
    WEEKLY_ARTICLE_COLUMNS_BY_NAME,
    WEEKLY_ARTICLE_SHEET,
    WEEKLY_AXIS_SCORE_COLUMNS,
    WEEKLY_TAG_COLUMNS,
    ColumnKind,
    ReportColumn,
    ReportColumnError,
    axis_score_bounds,
    columns_by_name,
    format_cell,
    format_row,
    header_row,
    parse_cell,
    parse_row,
)

# 仕様書 §8.1 の22列（順序厳守）
WEEKLY_COLUMN_NAMES = [
    "収集日",
    "情報カテゴリ",
    "タイトル",
    "一言要約",
    "合計スコア",
    "緊急性鮮度_点",
    "信頼性_点",
    "アドバイザリー活用度_点",
    "AI業界市場インパクト_点",
    "実務活用可能性_点",
    "顧客関連度_点",
    "レポート採用区分",
    "実務活用可能性",
    "顧客関連度",
    "信頼性",
    "地域",
    "情報種別",
    "業務領域",
    "業界",
    "AIテーマ",
    "ソース",
    "URL",
]

# 仕様書 §8.1「除外ログ」の6列
EXCLUSION_LOG_COLUMN_NAMES = [
    "収集日",
    "タイトル",
    "URL",
    "ソース",
    "除外区分",
    "除外理由",
]

# 仕様書 §8.2 の8列（順序厳守）
MONTHLY_COLUMN_NAMES = [
    "No",
    "トピック(章)",
    "企業・組織",
    "タイトル",
    "URL",
    "出典",
    "掲載月",
    "解説",
]

ALL_TABLES = [
    (WEEKLY_ARTICLE_COLUMNS, WEEKLY_COLUMN_NAMES),
    (EXCLUSION_LOG_COLUMNS, EXCLUSION_LOG_COLUMN_NAMES),
    (MONTHLY_CASE_COLUMNS, MONTHLY_COLUMN_NAMES),
]


# --- 列名と順序 --------------------------------------------------------------


def test_weekly_sheet_has_the_twenty_two_columns_in_order() -> None:
    """22列・順序厳守（仕様書 §8.1）。HTML生成が固定名で読むため崩せない。"""
    assert [column.name for column in WEEKLY_ARTICLE_COLUMNS] == WEEKLY_COLUMN_NAMES
    assert len(WEEKLY_ARTICLE_COLUMNS) == 22


def test_exclusion_log_has_the_six_columns_in_order() -> None:
    assert [column.name for column in EXCLUSION_LOG_COLUMNS] == (
        EXCLUSION_LOG_COLUMN_NAMES
    )
    assert len(EXCLUSION_LOG_COLUMNS) == 6
    assert EXCLUSION_LOG_SHEET_NAME == "除外ログ"


def test_monthly_sheet_has_the_eight_columns_in_order() -> None:
    assert [column.name for column in MONTHLY_CASE_COLUMNS] == MONTHLY_COLUMN_NAMES
    assert len(MONTHLY_CASE_COLUMNS) == 8


@pytest.mark.parametrize(("columns", "names"), ALL_TABLES)
def test_header_row_is_exactly_the_column_names(
    columns: tuple[ReportColumn, ...], names: list[str]
) -> None:
    """ライタはこの戻り値をそのままヘッダ行に書く。"""
    assert header_row(columns) == names


@pytest.mark.parametrize(("columns", "names"), ALL_TABLES)
def test_column_names_are_unique_within_a_table(
    columns: tuple[ReportColumn, ...], names: list[str]
) -> None:
    """ヘッダ文字列が索引の鍵なので表内で一意であること。"""
    assert len(columns_by_name(columns)) == len(columns)


# --- 配点整合（設計書 §2.2.1 の確認事項）------------------------------------


def test_axis_score_upper_bounds_sum_to_the_scoring_total() -> None:
    """**軸点の上限合計が 10+10+15+20+20+25＝100**（設計書 §2.2.1）。

    ここがズレると `合計スコア` が理論上 100 を超え、§12 のスコア整合チェックと
    採点の再現性が崩れる。
    """
    upper_bounds = [column.value_range[1] for column in WEEKLY_AXIS_SCORE_COLUMNS]

    assert upper_bounds == [10, 10, 15, 20, 20, 25]
    assert sum(upper_bounds) == 100
    assert sum(upper_bounds) == SCORING_TOTAL


def test_axis_score_columns_cover_all_six_axes() -> None:
    """6軸すべてに点数列があること（§12.1 は6軸すべての点数を要求する）。"""
    assert len(WEEKLY_AXIS_SCORE_COLUMNS) == 6
    assert {column.axis_id for column in WEEKLY_AXIS_SCORE_COLUMNS} == set(
        SCORING_AXIS_IDS
    )


def test_axis_score_columns_are_in_the_order_the_sheet_uses() -> None:
    """§2.2.1 の列6〜11 の並び（config の軸宣言順とは違う）。"""
    assert [column.axis_id for column in WEEKLY_AXIS_SCORE_COLUMNS] == [
        "urgency_freshness",
        "reliability",
        "advisory_usability",
        "market_impact",
        "practical_usability",
        "customer_relevance",
    ]


def test_axis_score_lower_bounds_are_zero() -> None:
    for column in WEEKLY_AXIS_SCORE_COLUMNS:
        assert column.value_range[0] == 0, column.name


def test_total_score_column_spans_the_full_scoring_range() -> None:
    total = WEEKLY_ARTICLE_COLUMNS_BY_NAME["合計スコア"]

    assert total.kind is ColumnKind.INTEGER
    assert total.value_range == (0, SCORING_TOTAL)


# --- config との対応 ---------------------------------------------------------


def test_axis_upper_bounds_match_the_confirmed_weights(
    config: IntelligenceConfig,
) -> None:
    """軸点の上限＝その軸の `weight`。§5.2 の初期値と一致していること。"""
    weights = {axis.id: axis.weight for axis in config.scoring_axes}

    for column in WEEKLY_AXIS_SCORE_COLUMNS:
        assert column.value_range == (0, weights[column.axis_id]), column.name


def test_axis_score_bounds_follow_the_config_not_the_static_range(
    config: IntelligenceConfig,
) -> None:
    """weight は可変（§7.2）。実行時の上限は config を見る。"""
    assert axis_score_bounds(config) == {
        column.axis_id: column.value_range for column in WEEKLY_AXIS_SCORE_COLUMNS
    }

    config.scoring_axes[0].weight = 30  # customer_relevance を 25 → 30

    assert axis_score_bounds(config)["customer_relevance"] == (0, 30)


def test_tag_columns_map_one_to_one_onto_the_ten_required_tags() -> None:
    """10必須タグにそれぞれ列が1つ（§12.1 のタグ欠落チェックの対象）。"""
    tag_ids = [column.tag_id for column in WEEKLY_TAG_COLUMNS]

    assert len(tag_ids) == 10
    assert sorted(tag_ids) == sorted(REQUIRED_TAG_IDS)


def test_tag_columns_agree_with_the_config_value_sources(
    config: IntelligenceConfig,
) -> None:
    """列の `value_source` が config の `required_tags[].value_source` と一致。

    T-20 はこの文字列を辿って config の実値と突き合わせるので、ここがズレると
    enum 外の値を検出できなくなる。
    """
    by_tag = {tag.id: tag.value_source for tag in config.required_tags}

    for column in WEEKLY_TAG_COLUMNS:
        assert column.value_source == by_tag[column.tag_id], column.name


def test_enum_backed_columns_point_at_existing_enum_keys() -> None:
    """`enums.*` を指す列の参照先キーが実在すること。"""
    enum_keys = set(ConfigEnums.model_fields)

    referenced = [
        column.value_source.removeprefix("enums.")
        for column in WEEKLY_ARTICLE_COLUMNS
        if column.value_source and column.value_source.startswith("enums.")
    ]

    assert referenced
    assert set(referenced) <= enum_keys


def test_columns_without_a_config_mapping_carry_neither_id() -> None:
    """記事メタ（収集日・タイトル・要約・ソース・URL）と合計スコアは軸でもタグでもない。"""
    unmapped = [
        column.name
        for column in WEEKLY_ARTICLE_COLUMNS
        if column.axis_id is None and column.tag_id is None
    ]

    assert unmapped == ["収集日", "タイトル", "一言要約", "合計スコア", "ソース", "URL"]


# --- multi 区切り ------------------------------------------------------------


def test_weekly_multi_columns_use_the_semicolon() -> None:
    """週次の multi は `;` 区切り（仕様書 §8.1）。"""
    multi = [
        column for column in WEEKLY_ARTICLE_COLUMNS if column.kind is ColumnKind.MULTI
    ]

    assert [column.name for column in multi] == ["地域", "業務領域", "業界", "AIテーマ"]
    for column in multi:
        assert column.separator == MULTI_VALUE_SEPARATOR == ";"


def test_monthly_organization_column_uses_the_nakaguro() -> None:
    """月次「企業・組織」は `A・B`（仕様書 §8.2）。週次の `;` とは別。"""
    organization = columns_by_name(MONTHLY_CASE_COLUMNS)["企業・組織"]

    assert organization.kind is ColumnKind.MULTI
    assert organization.separator == ORGANIZATION_SEPARATOR == "・"


def test_monthly_commentary_column_splits_on_blank_lines() -> None:
    """月次「解説」は `\\n\\n` 区切りの3段落（仕様書 §8.2 / T-25 が `<p>` へ分割）。"""
    commentary = columns_by_name(MONTHLY_CASE_COLUMNS)["解説"]

    assert commentary.kind is ColumnKind.PARAGRAPHS
    assert commentary.separator == PARAGRAPH_SEPARATOR == "\n\n"


def test_merged_source_separator_is_defined_for_dedup() -> None:
    """統合時の `A / B(統合)`（仕様書 §11.3）。組み立ては T-18。"""
    assert SOURCE_MERGE_SEPARATOR == " / "


@pytest.mark.parametrize(("columns", "names"), ALL_TABLES)
def test_only_multi_and_paragraph_columns_have_separators(
    columns: tuple[ReportColumn, ...], names: list[str]
) -> None:
    for column in columns:
        needs = column.kind in (ColumnKind.MULTI, ColumnKind.PARAGRAPHS)
        assert bool(column.separator) is needs, column.name


@pytest.mark.parametrize(("columns", "names"), ALL_TABLES)
def test_only_integer_columns_have_a_value_range(
    columns: tuple[ReportColumn, ...], names: list[str]
) -> None:
    for column in columns:
        expected = column.kind is ColumnKind.INTEGER
        assert (column.value_range is not None) is expected, column.name


# --- 定義そのものの検査（import 時に落ちること）------------------------------


def test_multi_column_without_a_separator_is_a_definition_error() -> None:
    with pytest.raises(ReportColumnError, match="separator"):
        ReportColumn(name="dummy", kind=ColumnKind.MULTI)


def test_integer_column_without_a_range_is_a_definition_error() -> None:
    with pytest.raises(ReportColumnError, match="value_range"):
        ReportColumn(name="dummy", kind=ColumnKind.INTEGER)


def test_text_column_with_a_separator_is_a_definition_error() -> None:
    with pytest.raises(ReportColumnError, match="separator は不要"):
        ReportColumn(name="dummy", kind=ColumnKind.TEXT, separator=";")


def test_inverted_value_range_is_a_definition_error() -> None:
    with pytest.raises(ReportColumnError, match="下限が上限"):
        ReportColumn(name="dummy", kind=ColumnKind.INTEGER, value_range=(10, 0))


def test_columns_are_immutable() -> None:
    """列定義を実行時に書き換えられないこと。"""
    with pytest.raises(AttributeError):
        WEEKLY_ARTICLE_COLUMNS[0].name = "別の名前"


# --- 非空必須（§12.1）-------------------------------------------------------


def test_title_is_the_only_weekly_column_the_format_check_lets_through() -> None:
    """⚠️ §12.1 の非空必須リストは `一言要約 / URL / ソース / 収集日` ＋ 6軸点
    ＋10タグで、**タイトルが入っていない**。仕様どおりに False としているので、
    タイトル欠落は T-20 では落ちない（T-24 側でガードする）。
    """
    optional = [
        column.name
        for column in WEEKLY_ARTICLE_COLUMNS
        if not column.required_non_empty
    ]

    assert optional == ["タイトル"]


@pytest.mark.parametrize(
    "name", ["収集日", "一言要約", "ソース", "URL", "合計スコア", "情報カテゴリ"]
)
def test_columns_named_in_the_format_check_are_required(name: str) -> None:
    assert WEEKLY_ARTICLE_COLUMNS_BY_NAME[name].required_non_empty


def test_every_tag_and_axis_column_is_required() -> None:
    """6軸点と10タグは §12.1 が明示的に要求している。"""
    for column in (*WEEKLY_AXIS_SCORE_COLUMNS, *WEEKLY_TAG_COLUMNS):
        assert column.required_non_empty, column.name


# --- シートの行レイアウト ----------------------------------------------------


def test_weekly_sheet_header_is_on_the_fourth_row() -> None:
    """1行目タイトル / 2行目説明 / 3行目空行 / 4行目ヘッダ / 5行目以降（§8.1）。"""
    assert WEEKLY_ARTICLE_SHEET.header_row == 4
    assert WEEKLY_ARTICLE_SHEET.first_data_row == 5
    assert WEEKLY_ARTICLE_SHEET.columns is WEEKLY_ARTICLE_COLUMNS


@pytest.mark.parametrize("sheet", [EXCLUSION_LOG_SHEET, MONTHLY_CASE_SHEET])
def test_other_sheets_start_with_the_header_row(sheet: Any) -> None:
    """⚠️ 前置き行の規定が仕様書・設計書に無いため1行目ヘッダとしている。"""
    assert sheet.header_row == 1
    assert sheet.first_data_row == 2


# --- セル値の書き出し / 読み戻し ---------------------------------------------


@pytest.fixture
def weekly_row() -> dict[str, Any]:
    """22列そろった1行分の値。"""
    return {
        "収集日": "2026-07-27",
        "情報カテゴリ": "ai_agent_automation",
        "タイトル": "某社がAIエージェントを全社導入",
        "一言要約": "一文目。二文目。三文目。",
        "合計スコア": 87,
        "緊急性鮮度_点": 8,
        "信頼性_点": 9,
        "アドバイザリー活用度_点": 13,
        "AI業界市場インパクト_点": 17,
        "実務活用可能性_点": 18,
        "顧客関連度_点": 22,
        "レポート採用区分": "次回定例で提案",
        "実務活用可能性": "すぐ活用",
        "顧客関連度": "直接関係",
        "信頼性": "高",
        "地域": ["日本", "グローバル"],
        "情報種別": "一次情報(公式発表)",
        "業務領域": ["AI戦略", "業務プロセス改革"],
        "業界": ["不動産", "業界横断"],
        "AIテーマ": ["AIエージェント", "業務自動化"],
        "ソース": "ITmedia / TechCrunch(統合)",
        "URL": "https://example.com/article",
    }


def test_weekly_row_round_trips_through_the_definition(
    weekly_row: dict[str, Any],
) -> None:
    """ライタが書いた行をリーダが読み戻して元に戻ること。

    これが成り立つ限り、T-22 のライタと T-24 のリーダは列順を知らなくてよい。
    """
    cells = format_row(WEEKLY_ARTICLE_COLUMNS, weekly_row)

    assert len(cells) == 22
    assert parse_row(WEEKLY_ARTICLE_COLUMNS, cells) == weekly_row


def test_format_row_places_values_in_the_defined_order(
    weekly_row: dict[str, Any],
) -> None:
    cells = format_row(WEEKLY_ARTICLE_COLUMNS, weekly_row)

    assert cells[0] == "2026-07-27"
    assert cells[4] == 87
    assert cells[15] == "日本;グローバル"
    assert cells[21] == "https://example.com/article"


def test_monthly_row_round_trips(config: IntelligenceConfig) -> None:
    row: dict[str, Any] = {
        "No": 1,
        "トピック(章)": "第1章 エージェント導入の実像",
        "企業・組織": ["A社", "B社"],
        "タイトル": "全社導入の舞台裏",
        "URL": "https://example.com/case",
        "出典": "ITmedia（2026-07-10）／ プレスリリース",
        "掲載月": "2026-07",
        "解説": ["事実の段落。", "詳細の段落。", "示唆の段落。"],
    }

    cells = format_row(MONTHLY_CASE_COLUMNS, row)

    assert cells[2] == "A社・B社"
    assert cells[7] == "事実の段落。\n\n詳細の段落。\n\n示唆の段落。"
    assert parse_row(MONTHLY_CASE_COLUMNS, cells) == row


def test_exclusion_log_row_round_trips() -> None:
    row: dict[str, Any] = {
        "収集日": "2026-07-27",
        "タイトル": "おすすめAIツール10選",
        "URL": "https://example.com/ad",
        "ソース": "まとめサイト",
        "除外区分": "原則除外",
        "除外理由": "アフィリエイト・広告色の強いツール紹介記事",
    }

    cells = format_row(EXCLUSION_LOG_COLUMNS, row)

    assert parse_row(EXCLUSION_LOG_COLUMNS, cells) == row


def test_multi_values_join_and_split_on_the_column_separator() -> None:
    column = WEEKLY_ARTICLE_COLUMNS_BY_NAME["業界"]

    assert format_cell(column, ["不動産", "金融"]) == "不動産;金融"
    assert parse_cell(column, "不動産;金融") == ["不動産", "金融"]
    # 前後の空白と空要素は落とす（手編集された xlsx を読むことがある）
    assert parse_cell(column, " 不動産 ; ; 金融 ") == ["不動産", "金融"]


def test_empty_multi_value_becomes_an_empty_cell_and_reads_back_as_empty_list() -> None:
    column = WEEKLY_ARTICLE_COLUMNS_BY_NAME["地域"]

    assert format_cell(column, []) is None
    assert parse_cell(column, None) == []
    assert parse_cell(column, "  ") == []


def test_passing_a_string_to_a_multi_column_is_rejected() -> None:
    """`"日本;海外"` を渡すと1要素として join されて壊れるので弾く。"""
    column = WEEKLY_ARTICLE_COLUMNS_BY_NAME["地域"]

    with pytest.raises(ReportColumnError, match="list/tuple"):
        format_cell(column, "日本;海外")


def test_dates_accept_both_text_and_date_objects() -> None:
    column = WEEKLY_ARTICLE_COLUMNS_BY_NAME["収集日"]

    assert format_cell(column, date(2026, 7, 27)) == "2026-07-27"
    assert format_cell(column, "2026-07-27") == "2026-07-27"
    # openpyxl は日付書式のセルを datetime で返すことがある
    assert parse_cell(column, datetime(2026, 7, 27, 9, 0)) == "2026-07-27"


def test_month_column_accepts_both_text_and_date_objects() -> None:
    column = columns_by_name(MONTHLY_CASE_COLUMNS)["掲載月"]

    assert format_cell(column, date(2026, 7, 1)) == "2026-07"
    assert format_cell(column, "2026-07") == "2026-07"
    assert parse_cell(column, datetime(2026, 7, 1)) == "2026-07"


def test_integer_cells_survive_being_read_back_as_text() -> None:
    """手編集で文字列になったセルも int へ戻す。"""
    column = WEEKLY_ARTICLE_COLUMNS_BY_NAME["合計スコア"]

    assert parse_cell(column, "87") == 87
    assert parse_cell(column, 87) == 87
    assert parse_cell(column, None) is None


def test_blank_scalar_cells_read_back_as_none() -> None:
    """空セルは None。非空検査は T-20 の担当なのでここでは落とさない。"""
    for name in ("一言要約", "URL", "情報種別"):
        assert parse_cell(WEEKLY_ARTICLE_COLUMNS_BY_NAME[name], "") is None


def test_parse_cell_does_not_enforce_the_value_range() -> None:
    """値域の検査は §12 のフォーマットチェック（T-20）。ここは型の復元だけ。"""
    column = WEEKLY_ARTICLE_COLUMNS_BY_NAME["顧客関連度_点"]

    assert parse_cell(column, 99) == 99


def test_missing_column_in_a_row_is_rejected(weekly_row: dict[str, Any]) -> None:
    del weekly_row["URL"]

    with pytest.raises(ReportColumnError, match="URL"):
        format_row(WEEKLY_ARTICLE_COLUMNS, weekly_row)


def test_unknown_column_in_a_row_is_rejected(weekly_row: dict[str, Any]) -> None:
    weekly_row["謎の列"] = "x"

    with pytest.raises(ReportColumnError, match="謎の列"):
        format_row(WEEKLY_ARTICLE_COLUMNS, weekly_row)


def test_wrong_cell_count_is_rejected() -> None:
    with pytest.raises(ReportColumnError, match="列数"):
        parse_row(WEEKLY_ARTICLE_COLUMNS, ["a", "b"])
