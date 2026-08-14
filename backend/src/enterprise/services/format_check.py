"""スコアリング根拠フォーマットチェック（設計書 §6.5 ／ 仕様書 §12 ／ T-20）。

フィルタ段の最後。1記事ぶんの行（週次22列）が §12.1 の必須項目をすべて満たして
いるかを検査し、`validation_{period}.json`（`{ok, errors[], warnings[]}`）の中身を
組み立てる。

---

**なぜ決定的 Python なのか**（TASKS.md §1.1）

分類・採点は LLM が行う（T-19）。**LLM は「合計スコアは78点です」と書きながら
6軸の和が別の値になる**ことがあり、enum に無い値や空のタグも出しうる。§12 は
その取りこぼしを機械的に潰すための工程なので、ここで再び LLM に「この行は妥当
ですか」と聞いたら意味が無い。**このモジュールが見るのは記事の行と config だけ。**

---

**`error` と `warning` の切り分け**（仕様書 §12.2 の確定事項）

- `error`: 合計スコア不一致・enum 外の値・必須タグ欠落。**該当記事は本編HTML生成の
  対象から外し、除外ログへ `除外区分=フォーマット不備` として記録する**
- `warning`: 要約が短すぎる等。記事は本編に残る

したがって **`ok` は「error が無いこと」**（`ValidationReport`・T-06）。

---

**検査の基準はすべて外から引く**

- 列・型・どの列が非空必須か → T-07 の `WEEKLY_ARTICLE_COLUMNS`
- 軸点の上限 → `axis_score_bounds(config)`（**静的な `value_range` ではなく
  実行時の `weight`**。admin が weight を変えたのに検査だけ旧値、を避ける）
- enum の実値 → `config.enums.*` / `config.information_categories[].id`

⚠️ **このモジュールに値の一覧を書かないこと。** 書いた時点で「config を変えたのに
検査が追随しない」壊れ方が生まれる。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.config_validation import ENUM_SOURCE_PREFIX
from enterprise.entities.report_columns import (
    EXCLUSION_LOG_COLUMNS,
    WEEKLY_ARTICLE_COLUMNS,
    WEEKLY_ARTICLE_SHEET,
    WEEKLY_AXIS_SCORE_COLUMNS,
    ReportColumn,
    axis_score_bounds,
    format_row,
)
from enterprise.entities.validation_report import ValidationIssue, ValidationReport

# 除外ログの `除外区分` / `除外理由`（仕様書 §12.2・設計書 §6.1 の擬似コード）。
# 何がダメだったかの明細は `validation_{period}.json` 側にある（除外ログは6列で、
# 理由欄に検証結果を詰め込む場所ではない）。
CATEGORY_FORMAT_ERROR = "フォーマット不備"
REASON_VALIDATION_ERROR = "§12検証エラー"

# 一言要約は「2〜3文」（仕様書 §8.1 / T-07 の列定義の注記）。これを下回るものを
# 「短すぎる」＝ `warning` とする（§12.2）。⚠️ 文字数のしきい値は仕様にも config
# にも無いので作らない。文の数だけを数える。
MIN_SUMMARY_SENTENCES = 2

# 文末とみなす記号（全角・半角）。
SENTENCE_TERMINATORS = "。．.！!？?"

# `情報カテゴリ` 列の `value_source`（T-07）。`enums.*` ではなくカテゴリID を指す。
CATEGORY_ID_SOURCE = "information_categories.id"

# 検査しない `value_source`（自由記述。設計書 §2.1）。
FREE_VALUE_SOURCE = "free_controlled"


@dataclass(frozen=True, slots=True)
class ArticleFormatIssues:
    """1記事ぶんの検査結果。"""

    row: int
    """xlsx の行番号（1-indexed）。"""

    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]

    @property
    def has_error(self) -> bool:
        """本編から外すべきか（§12.2）。"""
        return bool(self.errors)


@dataclass(frozen=True, slots=True)
class RejectedArticle:
    """error があって本編から外した記事1件。"""

    record: Mapping[str, Any]
    issues: ArticleFormatIssues


@dataclass(frozen=True, slots=True)
class FormatCheckResult:
    """`check_articles()` の結果。"""

    report: ValidationReport
    """`validation_{period}.json` の中身（書き出しは T-02 経由で T-21 が行う）。"""

    accepted: list[Mapping[str, Any]]
    """本編HTML生成へ進む記事（入力順）。warning だけの記事はここに残る。"""

    rejected: list[RejectedArticle]
    """error があって外した記事（入力順）。除外ログへ回す。"""


def check_article(
    record: Mapping[str, Any], config: IntelligenceConfig, *, row: int
) -> ArticleFormatIssues:
    """1記事ぶんの行を検査する（仕様書 §12.1 の必須項目すべて）。

    **見つかった違反をすべて返す**（最初の1件で打ち切らない）。§12.2 の検証
    レポートは修正のための一覧なので、1件直すたびに再実行させない。

    Args:
        record: 週次22列の行（列名 → 値）。列が欠けていても落ちない（欠落として報告）
        config: 実行開始時に固定参照している config（§6.3 の revision ピン留め済み）
        row: xlsx の行番号（1-indexed）。レポートの `row` にそのまま載る

    Returns:
        error と warning の一覧
    """
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    def error(field: str, reason: str) -> None:
        errors.append(ValidationIssue(row=row, field=field, reason=reason))

    def warning(field: str, reason: str) -> None:
        warnings.append(ValidationIssue(row=row, field=field, reason=reason))

    # --- 非空（§12.1：10必須タグ／一言要約・URL・ソース・収集日）-------------
    # どの列が非空必須かは T-07 の `required_non_empty` が持っている。
    for column in WEEKLY_ARTICLE_COLUMNS:
        if not column.required_non_empty:
            continue
        if _is_empty(record.get(column.name)):
            error(column.name, "欠落（§12.1 の必須項目）")

    # --- 6軸の点数が範囲内（§12.1）-----------------------------------------
    bounds = axis_score_bounds(config)
    for column in WEEKLY_AXIS_SCORE_COLUMNS:
        assert column.axis_id is not None  # WEEKLY_AXIS_SCORE_COLUMNS の定義より
        value = record.get(column.name)
        if _is_empty(value):
            continue  # 欠落は上で報告済み
        if not _is_integer(value):
            error(column.name, f"整数ではありません: {value!r}")
            continue
        low, high = bounds[column.axis_id]
        if not low <= int(value) <= high:
            error(column.name, f"範囲外（{low}〜{high}）: {value}")

    # --- 6軸の和 == 合計スコア（§12.1）-------------------------------------
    if mismatch := _total_score_mismatch(record):
        error("合計スコア", mismatch)

    # --- enum 系の値が config に存在（§12.1。未定義値はエラー）--------------
    for column in WEEKLY_ARTICLE_COLUMNS:
        allowed = allowed_values(column, config)
        if allowed is None:
            continue
        for value in _as_values(record.get(column.name)):
            if value not in allowed:
                error(
                    column.name,
                    f"config に無い値です: {value!r}（{column.value_source} から選ぶ）",
                )

    # --- warning（§12.2「要約が短すぎる等」）--------------------------------
    summary = record.get("一言要約")
    if isinstance(summary, str) and summary.strip():
        sentences = count_sentences(summary)
        if sentences < MIN_SUMMARY_SENTENCES:
            warning(
                "一言要約",
                f"文が {sentences} つしかありません（§8.1 は2〜3文）",
            )

    return ArticleFormatIssues(row=row, errors=errors, warnings=warnings)


def check_articles(
    records: Sequence[Mapping[str, Any]], config: IntelligenceConfig
) -> FormatCheckResult:
    """記事の一覧を検査し、検証レポートと採否を返す（仕様書 §12.2）。

    行番号は週次シートのレイアウト（T-07 の `WEEKLY_ARTICLE_SHEET`）から数える。
    週次の1件目は5行目（1行目タイトル / 2行目説明 / 3行目空行 / 4行目ヘッダ）。

    ⚠️ **`error` のある記事は `accepted` に入らない**（§12.2「エラーがある記事は
    本編HTML生成の対象から除外」）。`warning` だけの記事は残る。

    Args:
        records: 週次22列の行の一覧（合計スコア降順に整列する前でよい）
        config: 実行開始時に固定参照している config

    Returns:
        検証レポート・本編へ進む記事・外した記事
    """
    all_errors: list[ValidationIssue] = []
    all_warnings: list[ValidationIssue] = []
    accepted: list[Mapping[str, Any]] = []
    rejected: list[RejectedArticle] = []

    for index, record in enumerate(records):
        issues = check_article(
            record, config, row=WEEKLY_ARTICLE_SHEET.first_data_row + index
        )
        all_errors.extend(issues.errors)
        all_warnings.extend(issues.warnings)
        if issues.has_error:
            rejected.append(RejectedArticle(record=record, issues=issues))
        else:
            accepted.append(record)

    return FormatCheckResult(
        report=ValidationReport.from_issues(errors=all_errors, warnings=all_warnings),
        accepted=accepted,
        rejected=rejected,
    )


def allowed_values(
    column: ReportColumn, config: IntelligenceConfig
) -> frozenset[str] | None:
    """列の値が属すべき集合を config から引く（§12.1 の enum 検査）。

    `value_source` の記法は config の `required_tags[].value_source` と同じ（T-07）:

    - `enums.<key>` → `config.enums.<key>`
    - `information_categories.id` → 7カテゴリの ID
    - `free_controlled` / 指定なし → 検査しない（`None` を返す）

    Args:
        column: 週次22列のいずれか
        config: 実行時 config

    Returns:
        取り得る値の集合。検査対象でなければ `None`
    """
    source = column.value_source
    if source is None or source == FREE_VALUE_SOURCE:
        return None
    if source == CATEGORY_ID_SOURCE:
        return frozenset(category.id for category in config.information_categories)
    if source.startswith(ENUM_SOURCE_PREFIX):
        key = source.removeprefix(ENUM_SOURCE_PREFIX)
        values = getattr(config.enums, key, None)
        # 参照先の enum が実在するかは T-05（§2.1.1-4）の担当。ここに無いなら
        # config 自体が検証を通っていないので、記事の不備として扱わない。
        return None if values is None else frozenset(str(value) for value in values)
    return None


def count_sentences(text: str) -> int:
    """文の数を数える（`。` `.` `！` `？` 等で区切る）。

    区切り記号が1つも無い文字列は1文と数える（「短すぎる」の判定に使うだけで、
    これ自体が error になることはない）。
    """
    count = 0
    current = ""
    for char in text:
        if char in SENTENCE_TERMINATORS:
            if current.strip():
                count += 1
            current = ""
            continue
        current += char
    if current.strip():
        count += 1
    return count


def format_error_log_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    """フォーマット不備の除外ログ1行（仕様書 §12.2・設計書 §6.1）。

    `除外区分=フォーマット不備` / `除外理由=§12検証エラー`。**どの項目がなぜダメ
    だったかは `validation_{period}.json` を見る**（除外ログは6列しかない）。

    ⚠️ 除外ログ側の必須4項目（`収集日/タイトル/URL/ソース`）自体が空で外された
    記事もありうる。その場合は空セルのまま記録する（**記録を落とさない**方を採る。
    落とすと「本編にも除外ログにも無い記事」ができる）。
    """
    return {
        "収集日": record.get("収集日"),
        "タイトル": record.get("タイトル"),
        "URL": record.get("URL"),
        "ソース": record.get("ソース"),
        "除外区分": CATEGORY_FORMAT_ERROR,
        "除外理由": REASON_VALIDATION_ERROR,
    }


def format_error_log_row(record: Mapping[str, Any]) -> list[str | int | None]:
    """フォーマット不備の除外ログ1行を xlsx の列順（6列）で組み立てる。"""
    return format_row(EXCLUSION_LOG_COLUMNS, format_error_log_entry(record))


def _total_score_mismatch(record: Mapping[str, Any]) -> str | None:
    """6軸の和が `合計スコア` と一致しないなら、その理由を返す（§12.1）。

    どちらかが欠けている／整数でない場合は比較しない（その旨は別途 error 済みで、
    ここで重ねて報告しても直す先は同じ）。
    """
    total = record.get("合計スコア")
    if not _is_integer(total):
        return None

    axis_values = [record.get(column.name) for column in WEEKLY_AXIS_SCORE_COLUMNS]
    if not all(_is_integer(value) for value in axis_values):
        return None

    axis_sum = sum(int(value) for value in axis_values)
    if axis_sum == int(total):
        return None
    return f"6軸の和と不一致（6軸の和={axis_sum} / 合計スコア={int(total)}）"


def _is_empty(value: Any) -> bool:
    """§12.1 の「空でない」を判定する。

    `0` は空ではない（点数の 0 点は正当な値）。空白だけの文字列と空の列（multi 列で
    値が1つも無い）は空とみなす。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, frozenset)):
        return not value
    return False


def _is_integer(value: Any) -> bool:
    """整数として扱えるか。`bool` は除く（`True` を 1 点と読み替えない）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _as_values(value: Any) -> list[str]:
    """enum 検査の対象値を取り出す（multi 列は要素ごと、空は対象外）。"""
    if _is_empty(value):
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if not _is_empty(item)]
    return [str(value).strip()]
