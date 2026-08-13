"""中間xlsx の列スキーマ（設計書 §2.2 ／ 仕様書 §8）。

中間xlsx は「ファイルが正」方針の受け渡し単位で、HTML生成（PROMPT-3 / T-24・T-25）が
入力として読む。**列名と順序は顧客指定の既存ファイルに合わせた確定値**なので、
ここを唯一の定義とし、**ライタ（T-22）とリーダ（T-24・T-25・T-18）はこの定義だけを
参照する**。どちらか片方が列順をハードコードすると、片方を直したときにもう片方が
黙って壊れる。

各列は「型」「値域」「multi の区切り」「対応する config キー」を持つ:

- `kind` / `value_range`: セル値の型と範囲（検査そのものは T-20 のフォーマットチェック）
- `separator`: multi 値の区切り。週次の4列は `;`、月次の企業・組織は `・`、
  解説は `\\n\\n`
- `tag_id` / `axis_id` / `value_source`: config のどのタグ・どの軸に対応し、
  妥当な値がどこから来るか（`enums.*` を指す列は T-20 が config の実値と突き合わせる）

**配点整合（設計書 §2.2.1 の確認事項）**: 軸点列の上限は 10+10+15+20+20+25＝100 で
`scoring_total` と一致する。軸点の上限は各軸の `weight` そのものなので、weight を
admin が変えた場合の実行時上限は `axis_score_bounds()` を使う（→ 同関数の docstring）。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from enterprise.entities.config import (
    SCORING_TOTAL,
    IntelligenceConfig,
    RequiredTagId,
    ScoringAxisId,
)

# multi 値の区切り。週次の4列は `;`（仕様書 §8.1）。
MULTI_VALUE_SEPARATOR = ";"
# 月次「企業・組織」の複数主体は `・`（仕様書 §8.2 `A・B`）。
ORGANIZATION_SEPARATOR = "・"
# 月次「解説」の段落区切り（仕様書 §8.2。①事実 ②詳細 ③示唆 の3段落）。
PARAGRAPH_SEPARATOR = "\n\n"
# 統合時の代表記事の「ソース」欄（仕様書 §11.3 `A / B(統合)`）。組み立ては T-18。
SOURCE_MERGE_SEPARATOR = " / "

EXCLUSION_LOG_SHEET_NAME = "除外ログ"


class ReportColumnError(Exception):
    """列定義と噛み合わない行データ。"""


class ColumnKind(StrEnum):
    """セル値の型。ライタの書き出し方とリーダの読み戻し方を決める。"""

    DATE = "date"
    """`YYYY-MM-DD`。"""

    MONTH = "month"
    """`YYYY-MM`。"""

    TEXT = "text"
    URL = "url"

    INTEGER = "integer"
    """整数。`value_range` を必ず持つ。"""

    ENUM = "enum"
    """単一値。`value_source` が示す集合に属する。"""

    MULTI = "multi"
    """`separator` 区切りの複数値。"""

    PARAGRAPHS = "paragraphs"
    """`separator` 区切りの段落列。"""


@dataclass(frozen=True, slots=True)
class ReportColumn:
    """中間xlsx の1列。

    Attributes:
        name: xlsx のヘッダ文字列。**この文字列と並び順が顧客指定の確定値**
        kind: セル値の型
        separator: MULTI / PARAGRAPHS の区切り文字
        value_range: INTEGER の下限・上限（両端を含む）
        value_source: 妥当な値の出どころ。`enums.*` / `free_controlled` /
            `information_categories.id`。config の `required_tags[].value_source`
            と同じ記法なので、T-20 は config から実値を引ける
        axis_id: 6軸のどれかの点数列である場合その軸ID
        tag_id: 10必須タグのどれかを載せる列である場合そのタグID
        required_non_empty: §12 のフォーマットチェックで非空を要求する列か
        note: 由来・注意点
    """

    name: str
    kind: ColumnKind
    separator: str | None = None
    value_range: tuple[int, int] | None = None
    value_source: str | None = None
    axis_id: ScoringAxisId | None = None
    tag_id: RequiredTagId | None = None
    required_non_empty: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        """定義そのものの取り違えを import 時に落とす。

        区切りの無い multi 列や値域の無い数値列は、ライタ・リーダが実行時まで
        気づけない壊れ方をする。
        """
        needs_separator = self.kind in (ColumnKind.MULTI, ColumnKind.PARAGRAPHS)
        if needs_separator and not self.separator:
            raise ReportColumnError(f"{self.name}: {self.kind} には separator が必要")
        if not needs_separator and self.separator:
            raise ReportColumnError(f"{self.name}: {self.kind} に separator は不要")
        if (self.kind is ColumnKind.INTEGER) != (self.value_range is not None):
            raise ReportColumnError(f"{self.name}: integer 列には value_range が必要")
        if self.value_range and self.value_range[0] > self.value_range[1]:
            raise ReportColumnError(f"{self.name}: value_range の下限が上限を超える")


# --- 週次 `weekly_ai_intelligence_report.xlsx`（各週シート・22列）------------
# 設計書 §2.2.1 ／ 仕様書 §8.1。**順序厳守**。

WEEKLY_ARTICLE_COLUMNS: tuple[ReportColumn, ...] = (
    ReportColumn(
        name="収集日",
        kind=ColumnKind.DATE,
        note="非空（§12.1）",
    ),
    ReportColumn(
        name="情報カテゴリ",
        kind=ColumnKind.ENUM,
        value_source="information_categories.id",
        tag_id="information_category",
        note="カテゴリID(英)。§5.2 の7ID",
    ),
    ReportColumn(
        name="タイトル",
        kind=ColumnKind.TEXT,
        required_non_empty=False,
        note=(
            "⚠️ §12.1 の非空必須リストにタイトルは挙がっていないため False。"
            "カード見出しに使う T-24 側でガードする"
        ),
    ),
    ReportColumn(
        name="一言要約",
        kind=ColumnKind.TEXT,
        note="2〜3文。非空（§12.1）。短すぎるものは warning（§12.2）",
    ),
    ReportColumn(
        name="合計スコア",
        kind=ColumnKind.INTEGER,
        value_range=(0, SCORING_TOTAL),
        note="config 対応は scoring_total。6軸点の和と一致すること（§12.1）",
    ),
    # --- 6軸の点数（上限＝各軸の weight。合計 100）---
    ReportColumn(
        name="緊急性鮮度_点",
        kind=ColumnKind.INTEGER,
        value_range=(0, 10),
        axis_id="urgency_freshness",
    ),
    ReportColumn(
        name="信頼性_点",
        kind=ColumnKind.INTEGER,
        value_range=(0, 10),
        axis_id="reliability",
        note="min_reliability_score_to_publish 以上であること（§13.3・T-21）",
    ),
    ReportColumn(
        name="アドバイザリー活用度_点",
        kind=ColumnKind.INTEGER,
        value_range=(0, 15),
        axis_id="advisory_usability",
    ),
    ReportColumn(
        name="AI業界市場インパクト_点",
        kind=ColumnKind.INTEGER,
        value_range=(0, 20),
        axis_id="market_impact",
    ),
    ReportColumn(
        name="実務活用可能性_点",
        kind=ColumnKind.INTEGER,
        value_range=(0, 20),
        axis_id="practical_usability",
    ),
    ReportColumn(
        name="顧客関連度_点",
        kind=ColumnKind.INTEGER,
        value_range=(0, 25),
        axis_id="customer_relevance",
    ),
    # --- 10必須タグ（情報カテゴリは上の2列目）---
    ReportColumn(
        name="レポート採用区分",
        kind=ColumnKind.ENUM,
        value_source="enums.adoption_class",
        tag_id="adoption_class",
        note="スコアから決定的に決める（§6.4・T-19）",
    ),
    ReportColumn(
        name="実務活用可能性",
        kind=ColumnKind.ENUM,
        value_source="enums.practical_usability",
        tag_id="practical_usability",
    ),
    ReportColumn(
        name="顧客関連度",
        kind=ColumnKind.ENUM,
        value_source="enums.customer_relevance",
        tag_id="customer_relevance",
    ),
    ReportColumn(
        name="信頼性",
        kind=ColumnKind.ENUM,
        value_source="enums.reliability",
        tag_id="reliability",
    ),
    ReportColumn(
        name="地域",
        kind=ColumnKind.MULTI,
        separator=MULTI_VALUE_SEPARATOR,
        value_source="enums.region",
        tag_id="region",
    ),
    ReportColumn(
        name="情報種別",
        kind=ColumnKind.ENUM,
        value_source="enums.info_type",
        tag_id="info_type",
    ),
    ReportColumn(
        name="業務領域",
        kind=ColumnKind.MULTI,
        separator=MULTI_VALUE_SEPARATOR,
        value_source="enums.business_area",
        tag_id="business_area",
    ),
    ReportColumn(
        name="業界",
        kind=ColumnKind.MULTI,
        separator=MULTI_VALUE_SEPARATOR,
        value_source="enums.industry",
        tag_id="industry",
        note="週刊の業界別トピック抽出はこの列を見る（§7.3・T-24）",
    ),
    ReportColumn(
        name="AIテーマ",
        kind=ColumnKind.MULTI,
        separator=MULTI_VALUE_SEPARATOR,
        value_source="free_controlled",
        tag_id="ai_theme",
    ),
    ReportColumn(
        name="ソース",
        kind=ColumnKind.TEXT,
        note="媒体名。非空（§12.1）。統合時は `A / B(統合)`（§11.3・T-18）",
    ),
    ReportColumn(
        name="URL",
        kind=ColumnKind.URL,
        note="非空（§12.1）",
    ),
)

# --- `除外ログ` シート（週次側・6列）----------------------------------------
# 設計書 §2.2.2 ／ 仕様書 §8.1・§11.3。重複チェック（T-18）の参照元でもある。

EXCLUSION_LOG_COLUMNS: tuple[ReportColumn, ...] = (
    ReportColumn(name="収集日", kind=ColumnKind.DATE),
    ReportColumn(name="タイトル", kind=ColumnKind.TEXT),
    ReportColumn(name="URL", kind=ColumnKind.URL),
    ReportColumn(name="ソース", kind=ColumnKind.TEXT),
    ReportColumn(
        name="除外区分",
        kind=ColumnKind.TEXT,
        note=(
            "severity の日本語。完全除外／原則除外／低優先／統合／"
            "フォーマット不備／低スコア 等（§2.2.2。閉じた集合ではない）"
        ),
    ),
    ReportColumn(
        name="除外理由",
        kind=ColumnKind.TEXT,
        note="ルール名または検証理由（§2.2.2）",
    ),
)

# --- 月次 `monthly_ai_leading_cases.xlsx`（各月シート・8列）-----------------
# 設計書 §2.2.3 ／ 仕様書 §8.2。**順序厳守**。`No` 昇順＝章グルーピング順。

MONTHLY_CASE_COLUMNS: tuple[ReportColumn, ...] = (
    ReportColumn(
        name="No",
        kind=ColumnKind.INTEGER,
        value_range=(1, 9999),
        note="通し番号（1〜）。昇順＝章グルーピング順（§10.3）",
    ),
    ReportColumn(
        name="トピック(章)",
        kind=ColumnKind.TEXT,
        note="`第N章 <章タイトル>`。同一章の事例は連続配置（§8.2）",
    ),
    ReportColumn(
        name="企業・組織",
        kind=ColumnKind.MULTI,
        separator=ORGANIZATION_SEPARATOR,
        note="主体。複数可（`A・B`。§8.2）",
    ),
    ReportColumn(name="タイトル", kind=ColumnKind.TEXT, note="事例見出し"),
    ReportColumn(name="URL", kind=ColumnKind.URL, note="一次/報道URL"),
    ReportColumn(
        name="出典",
        kind=ColumnKind.TEXT,
        note="`媒体（日付）／ プレスリリース` 形式（§8.2）",
    ),
    ReportColumn(name="掲載月", kind=ColumnKind.MONTH),
    ReportColumn(
        name="解説",
        kind=ColumnKind.PARAGRAPHS,
        separator=PARAGRAPH_SEPARATOR,
        note="3段落（①事実 ②詳細 ③示唆）。T-25 が段落を `<p>` へ分割する",
    ),
)


# --- シート内の行レイアウト -------------------------------------------------


@dataclass(frozen=True, slots=True)
class SheetLayout:
    """ヘッダ行とデータ開始行（1-indexed。openpyxl と同じ数え方）。

    リーダがヘッダを探すために必要なので、列定義と同じ場所に置く。
    """

    columns: tuple[ReportColumn, ...]
    header_row: int
    first_data_row: int


# 週次の各週シートは 1行目タイトル / 2行目説明 / 3行目空行 / 4行目ヘッダ /
# 5行目以降データ（仕様書 §8.1・設計書 §2.2.1 に明記）。
WEEKLY_ARTICLE_SHEET = SheetLayout(
    columns=WEEKLY_ARTICLE_COLUMNS, header_row=4, first_data_row=5
)

# ⚠️ 除外ログと月次シートの前置き行は仕様書・設計書に規定がない。週次の各週シート
# だけがタイトル・説明行を持つと明記されているため、この2つは1行目ヘッダとする。
EXCLUSION_LOG_SHEET = SheetLayout(
    columns=EXCLUSION_LOG_COLUMNS, header_row=1, first_data_row=2
)
MONTHLY_CASE_SHEET = SheetLayout(
    columns=MONTHLY_CASE_COLUMNS, header_row=1, first_data_row=2
)


# --- 派生ビュー（用途別の引き当て）-----------------------------------------

WEEKLY_AXIS_SCORE_COLUMNS: tuple[ReportColumn, ...] = tuple(
    column for column in WEEKLY_ARTICLE_COLUMNS if column.axis_id is not None
)
"""6軸の点数列。§2.2.1 の並び（緊急性鮮度 → … → 顧客関連度）。"""

WEEKLY_TAG_COLUMNS: tuple[ReportColumn, ...] = tuple(
    column for column in WEEKLY_ARTICLE_COLUMNS if column.tag_id is not None
)
"""10必須タグを載せる列（§12.1 のタグ欠落チェック対象）。"""


def columns_by_name(columns: Sequence[ReportColumn]) -> dict[str, ReportColumn]:
    """ヘッダ文字列から列定義を引く索引。

    `axis_id` と `tag_id` は重複しうる（`reliability` は軸IDでもタグIDでもある）が、
    ヘッダ文字列は表ごとに一意なのでこちらを鍵にする。
    """
    return {column.name: column for column in columns}


WEEKLY_ARTICLE_COLUMNS_BY_NAME = columns_by_name(WEEKLY_ARTICLE_COLUMNS)
EXCLUSION_LOG_COLUMNS_BY_NAME = columns_by_name(EXCLUSION_LOG_COLUMNS)
MONTHLY_CASE_COLUMNS_BY_NAME = columns_by_name(MONTHLY_CASE_COLUMNS)


def header_row(columns: Sequence[ReportColumn]) -> list[str]:
    """ヘッダ行に書く文字列。ライタはこれをそのまま書く。"""
    return [column.name for column in columns]


def axis_score_bounds(config: IntelligenceConfig) -> dict[str, tuple[int, int]]:
    """実行時の軸点上限を config から引く。

    **軸点の上限＝その軸の `weight`**。`value_range` に持たせている 0-25 等は
    §5.2 の初期 weight と同じ値だが、weight は admin が変更できる（仕様書 §7.2）。
    採点範囲の検査（T-20）は静的な `value_range` ではなくこちらを見ることで、
    config を変えたのに検査だけ旧値のまま、という食い違いを避ける。

    Args:
        config: 実行時に固定参照している config（§6.3 の revision ピン留め済み）

    Returns:
        軸ID → (下限, 上限)
    """
    return {axis.id: (0, axis.weight) for axis in config.scoring_axes}


# --- セル値の書き出し / 読み戻し ---------------------------------------------


def format_cell(column: ReportColumn, value: Any) -> str | int | None:
    """Python 値を xlsx セルへ書ける形にする。

    Args:
        column: 対象列
        value: 書き出す値。`None` と空列は空セル（`None`）になる

    Returns:
        セルへ書く値

    Raises:
        ReportColumnError: multi 列に文字列を1つ渡した等、型が噛み合わない場合
    """
    if value is None:
        return None

    if column.kind in (ColumnKind.MULTI, ColumnKind.PARAGRAPHS):
        if isinstance(value, str):
            raise ReportColumnError(
                f"{column.name}: {column.kind} 列には列（list/tuple）を渡してください"
            )
        parts = [str(part).strip() for part in value if str(part).strip()]
        assert column.separator is not None  # __post_init__ が保証
        return column.separator.join(parts) or None

    if column.kind is ColumnKind.INTEGER:
        return int(value)

    if column.kind is ColumnKind.DATE:
        return _as_date_text(value)

    if column.kind is ColumnKind.MONTH:
        return _as_month_text(value)

    return str(value)


def parse_cell(column: ReportColumn, cell: Any) -> Any:
    """xlsx セルを Python 値へ読み戻す。

    値の妥当性（値域・enum 所属・非空）は検査しない。§12 のフォーマットチェック
    （T-20）が config と突き合わせて判定するため、ここは型の復元だけに徹する。

    Args:
        column: 対象列
        cell: openpyxl が返したセル値

    Returns:
        multi / paragraphs は `list[str]`、integer は `int | None`、他は `str | None`
    """
    is_blank = cell is None or (isinstance(cell, str) and not cell.strip())

    if column.kind in (ColumnKind.MULTI, ColumnKind.PARAGRAPHS):
        if is_blank:
            return []
        assert column.separator is not None  # __post_init__ が保証
        return [
            part.strip() for part in str(cell).split(column.separator) if part.strip()
        ]

    if is_blank:
        return None

    if column.kind is ColumnKind.INTEGER:
        return int(cell)

    if column.kind is ColumnKind.DATE:
        return _as_date_text(cell)

    if column.kind is ColumnKind.MONTH:
        return _as_month_text(cell)

    return str(cell).strip()


def format_row(
    columns: Sequence[ReportColumn], values: Mapping[str, Any]
) -> list[str | int | None]:
    """列定義の順序どおりに1行を組み立てる（ライタ用）。

    Args:
        columns: 対象の列定義
        values: 列名 → 値。**全列そろっている必要がある**

    Returns:
        セル値のリスト（列定義と同じ順序）

    Raises:
        ReportColumnError: 未知の列名が含まれる、または欠けている列がある場合
    """
    expected = {column.name for column in columns}
    if unknown := sorted(set(values) - expected):
        raise ReportColumnError(f"定義に無い列: {', '.join(unknown)}")
    if missing := [column.name for column in columns if column.name not in values]:
        raise ReportColumnError(f"値が渡されていない列: {', '.join(missing)}")

    return [format_cell(column, values[column.name]) for column in columns]


def parse_row(columns: Sequence[ReportColumn], cells: Sequence[Any]) -> dict[str, Any]:
    """1行を列名つきの dict へ読み戻す（リーダ用）。

    Args:
        columns: 対象の列定義
        cells: セル値（列定義と同じ順序・同じ個数）

    Returns:
        列名 → 値

    Raises:
        ReportColumnError: 列数が定義と合わない場合
    """
    if len(cells) != len(columns):
        raise ReportColumnError(
            f"列数が定義と合わない（定義 {len(columns)} 列 / 実際 {len(cells)} 列）"
        )
    return {
        column.name: parse_cell(column, cell)
        for column, cell in zip(columns, cells, strict=True)
    }


def _as_date_text(value: Any) -> str:
    """`YYYY-MM-DD` へ。openpyxl が日付書式のセルを datetime で返す場合に備える。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _as_month_text(value: Any) -> str:
    """`YYYY-MM` へ。"""
    if isinstance(value, date):  # datetime は date のサブクラス
        return f"{value.year:04d}-{value.month:02d}"
    return str(value).strip()
