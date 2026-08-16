"""`narrative_{period}.json` のスキーマ（2026-08-16 の決定3 ／ T-44）。

設計書 §7.3・§7.4 が「（生成テキスト）」としている項目——**今週のポイント**
（仕様書 §9.2-2）・**カード毎の示唆ボックス**（§9.2-4）・**巻頭言**（§10.2-2）・
**章導入文**（§10.2-4）・**むすび**（§10.2-5）——の置き場。

⚠️ **中間xlsx の列には入れない。** 週次22列 / 月次8列は §8.1・§8.2 の確定値で
増やせないので（決定3）、**生成テキストの正はこのファイル**。xlsx（確定値）と
narrative（生成テキスト）で正が分かれている、という関係をそのまま形にしてある。

⚠️ **これは filter ステップの出力**。パイプラインは crawl → filter → render の
3段構成のままで、narrative のための段は足していない（§3.1 の3プロンプトとの
1:1 対応・T-26 の状態機械 §8.4）。書くのは T-44（`application.usecases.narrative`）、
読むのは render 側。

---

**段落・文は「組み立てた文字列」ではなく要素の列で持つ**

T-24 / T-25 のレンダラが受け取るのは `\\n\\n` 区切りの文字列だが、このファイルは
**段落の列**（`editorial_paragraphs`）と**文の列**（`point_of_week_sentences`）で
持ち、連結は読み出し側（`to_weekly_narrative()` / `to_monthly_narrative()`）で行う。

- 生成時に段落数を構造で固定している（T-21 の解説3段落と同じ方式）ので、
  **ファイルへ落とした後もその構造が残る**（人が開いても段落の切れ目が読める）。
- 連結の区切りは T-07 の `PARAGRAPH_SEPARATOR` ひとつだけで、写しを持たない。

**件数の下限はこのスキーマでは課さない。** 段落数・文の数を固定するのは
**生成側の出力スキーマ**（`application.usecases.narrative`）の仕事で、ここで
下限を課すと「採用記事が0件だった実行」が空の narrative すら書けなくなる。
必須かどうかを決めるのは config（`point_of_week_required` /
`require_editorial_and_closing`）で、それを見るのはレンダラ（T-24 / T-25）。

---

**示唆の鍵は記事URL（正規化しない生の値）**

`insights` の鍵は中間xlsx 列22「URL」の値そのもの。T-24 の
`WeeklyNarrative.insight_for()` が当週シートの行から引くときの鍵がこれで、
**正規化した URL では引けない**（重複判定の正規化＝T-18 は「同じ記事か」を
判定するためのもので、行を指す鍵ではない）。
"""

from collections.abc import Iterable, Sequence
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
)

from enterprise.entities.json_document import (
    DocumentIssue,
    DocumentParseError,
    dump_json_document,
    parse_json_document,
)
from enterprise.entities.period import Period, PeriodError, parse_period
from enterprise.entities.report_columns import PARAGRAPH_SEPARATOR

NARRATIVE_LABEL = "narrative.json"

# 今週のポイントは「当週の総括3〜4文」（仕様書 §9.2-2）。**段落ではなく文**なので
# 連結に段落区切りは使わない（日本語の文は句点で終わるため区切り文字を挟まない）。
SENTENCE_JOINER = ""

_NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""空白だけの段落・文を通さない（描画すると空の `<p>` になる）。"""

_STRICT = ConfigDict(extra="forbid")


class _NarrativeDocument(BaseModel):
    """週次・月次に共通する骨格（対象期間の検証だけを持つ）。"""

    model_config = _STRICT

    period: str
    """対象期間（`2026-W31` / `2026-07`）。**どの実行の生成テキストか**を
    ファイルの中にも残す（読み出し側がファイル名との食い違いに気づけるように）。"""

    @field_validator("period")
    @classmethod
    def _period_must_be_parsable(cls, value: str) -> str:
        try:
            parse_period(value)
        except PeriodError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @property
    def parsed_period(self) -> Period:
        """実日付まで開いた対象期間。"""
        return parse_period(self.period)


class WeeklyNarrativeDocument(_NarrativeDocument):
    """週刊メルマガの生成テキスト（仕様書 §9.2-2・§9.2-4）。

    Attributes:
        period: 対象週（`2026-Www`）
        point_of_week_sentences: 今週のポイント（当週の総括3〜4文）。
            空＝生成していない（採用記事が0件だった実行）
        insights: 記事URL → 示唆ボックスの1段落。**鍵は当週シート列22 の値**
    """

    point_of_week_sentences: list[_NonEmptyText] = Field(default_factory=list)
    insights: dict[_NonEmptyText, _NonEmptyText] = Field(default_factory=dict)

    @property
    def point_of_week(self) -> str | None:
        """T-24 の `WeeklyNarrative.point_of_week` へそのまま渡せる形。"""
        joined = SENTENCE_JOINER.join(self.point_of_week_sentences).strip()
        return joined or None


class MonthlyNarrativeDocument(_NarrativeDocument):
    """月刊ビリーフの生成テキスト（仕様書 §10.2-2・§10.2-4・§10.2-5）。

    Attributes:
        period: 対象月（`YYYY-MM`）
        editorial_subtitle: 当月を一言で表すサブ見出し（§10.2-2）
        editorial_paragraphs: 巻頭言の総論（3段落）
        chapter_intros: 章ラベル（`第N章 …`＝月次8列の列2 の値）→ 章導入文
        closing_paragraphs: むすび（2段落：今月の総括 ＋ 来月の視点）
    """

    editorial_subtitle: str | None = None
    editorial_paragraphs: list[_NonEmptyText] = Field(default_factory=list)
    chapter_intros: dict[_NonEmptyText, _NonEmptyText] = Field(default_factory=dict)
    closing_paragraphs: list[_NonEmptyText] = Field(default_factory=list)

    @property
    def editorial(self) -> str | None:
        """T-25 の `MonthlyNarrative.editorial` へそのまま渡せる形。"""
        return _join_paragraphs(self.editorial_paragraphs)

    @property
    def closing(self) -> str | None:
        """T-25 の `MonthlyNarrative.closing` へそのまま渡せる形。"""
        return _join_paragraphs(self.closing_paragraphs)


def _join_paragraphs(paragraphs: Sequence[str]) -> str | None:
    """段落の列を `\\n\\n` 区切りへ（区切り文字は T-07 の定義だけを使う）。"""
    joined = PARAGRAPH_SEPARATOR.join(paragraphs).strip()
    return joined or None


WEEKLY_NARRATIVE_ADAPTER: TypeAdapter[WeeklyNarrativeDocument] = TypeAdapter(
    WeeklyNarrativeDocument
)
MONTHLY_NARRATIVE_ADAPTER: TypeAdapter[MonthlyNarrativeDocument] = TypeAdapter(
    MonthlyNarrativeDocument
)

type NarrativeDocument = WeeklyNarrativeDocument | MonthlyNarrativeDocument


def empty_narrative(period: Period) -> NarrativeDocument:
    """生成テキストが1つも無い narrative（採用記事・事例が0件だった実行）。

    ⚠️ **ファイル自体は書く。** 「無い」と「まだ作っていない」を read 側が
    区別できるようにするため（`point_of_week_required=true` の週刊は、空の
    narrative を渡された時点でレンダラが落ちる＝T-24 の意図した挙動）。
    """
    if period.is_weekly:
        return WeeklyNarrativeDocument(period=period.text)
    return MonthlyNarrativeDocument(period=period.text)


def parse_weekly_narrative(text: str) -> WeeklyNarrativeDocument:
    """週次の `narrative_{period}.json` を読み込む。

    Raises:
        DocumentParseError: JSON が壊れている／スキーマに合わない場合
    """
    return parse_json_document(WEEKLY_NARRATIVE_ADAPTER, text, label=NARRATIVE_LABEL)


def parse_monthly_narrative(text: str) -> MonthlyNarrativeDocument:
    """月次の `narrative_{period}.json` を読み込む。

    Raises:
        DocumentParseError: JSON が壊れている／スキーマに合わない場合
    """
    return parse_json_document(MONTHLY_NARRATIVE_ADAPTER, text, label=NARRATIVE_LABEL)


def parse_narrative(text: str, *, period: Period) -> NarrativeDocument:
    """対象期間の種別に合わせて読み込む（render 側の入口）。

    Args:
        text: ファイルの中身（`ArtifactStore` 経由で UTF-8 読み込み）
        period: どちらのスキーマで読むかを決める対象期間

    Returns:
        週次または月次の生成テキスト

    Raises:
        DocumentParseError: JSON が壊れている／スキーマに合わない場合。
            **`period` が食い違うファイルもここで落ちる**（週次のファイルを
            月次として読むと `extra="forbid"` に当たる）
    """
    document = (
        parse_weekly_narrative(text)
        if period.is_weekly
        else parse_monthly_narrative(text)
    )
    _ensure_same_period(document, period)
    return document


def _ensure_same_period(document: NarrativeDocument, period: Period) -> None:
    """ファイル名（引数の period）と中身の period の食い違いを落とす。

    別の期間の生成テキストを黙って使うと、**本文だけが先週のまま**の HTML が
    配信に回る（レンダラは xlsx と narrative を別々に受け取るので気づけない）。
    """
    if document.period != period.text:
        raise DocumentParseError(
            NARRATIVE_LABEL,
            [
                DocumentIssue(
                    path="period",
                    reason=(
                        f"対象期間が違います（読もうとした期間={period.text} / "
                        f"ファイルの中身={document.period}）"
                    ),
                )
            ],
        )


def dump_narrative(document: NarrativeDocument) -> str:
    """`narrative_{period}.json` として書き出す文字列。"""
    if isinstance(document, WeeklyNarrativeDocument):
        return dump_json_document(WEEKLY_NARRATIVE_ADAPTER, document)
    return dump_json_document(MONTHLY_NARRATIVE_ADAPTER, document)


def text_by_key(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """「宛先 → 文章」の対応表を整える（示唆・章導入文の共通処理）。

    前後の空白を落とし、空の宛先・空の文章は捨てる。**同じ宛先が2度現れたら
    先に来たほうが勝つ**（T-21 の章の割り当てと同じ向き。後勝ちにすると、
    出力の末尾に紛れた言い直しが本文を上書きする）。順序は渡された順のまま。
    """
    cleaned: dict[str, str] = {}
    for key, value in pairs:
        stripped_key, stripped_value = key.strip(), value.strip()
        if stripped_key and stripped_value:
            cleaned.setdefault(stripped_key, stripped_value)
    return cleaned


__all__ = [
    "MONTHLY_NARRATIVE_ADAPTER",
    "NARRATIVE_LABEL",
    "SENTENCE_JOINER",
    "WEEKLY_NARRATIVE_ADAPTER",
    "MonthlyNarrativeDocument",
    "NarrativeDocument",
    "WeeklyNarrativeDocument",
    "dump_narrative",
    "empty_narrative",
    "text_by_key",
    "parse_monthly_narrative",
    "parse_narrative",
    "parse_weekly_narrative",
]
