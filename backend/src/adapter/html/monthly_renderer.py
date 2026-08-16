"""月刊ビリーフ レンダラ（設計書 §7.4 ／ 仕様書 §10 ／ T-25）。

中間xlsx の当月シート（8列。T-22 の `ReportStore.read_monthly()`）と実行時 config
から、`monthly_belief_{YYYY-MM}.html` を組み立てる。

**AI を呼ばない**（TASKS.md §1.1「render = 決定的 Python テンプレート」）。

---

**§7.4 のマッピング（この モジュールが実装する対応表）**

| HTML | 中間xlsx（月次） |
|---|---|
| ヘッダ号バッジ／対象期間 | 列7「掲載月」（＝シート名） |
| 巻頭言 EDITORIAL | （生成テキスト → `MonthlyNarrative`） |
| 目次 CONTENTS | 列2「トピック(章)」を集約（章一覧＋件数＋`全N事例・M章`） |
| 章ヘッダ | 列2「トピック(章)」（下端 `2px solid #4FA8DB`） |
| 事例カード見出し | 列3「企業・組織」＋列4「タイトル」＋列5「URL」 |
| 事例本文 | 列8「解説」（`\\n\\n` を `<p>` 分割・最終段は示唆トーン） |
| 出典行 | 列6「出典」（上罫＋グレー小） |
| むすび CLOSING | （生成テキスト → `MonthlyNarrative`） |
| フッタ件数バッジ | 列1「No」の件数・章数 |
| 並び順 | 列1「No」昇順＝章グルーピング順 |

---

**⚠️ 「生成テキスト」は渡してもらう**（2026-08-16 の決定3。T-24 と同じ）

巻頭言（3段落）・章導入文・むすび（2段落）は月次8列に無い。列は §8.2 の確定値
なので増やせない。**filter（T-21）が生成して `MonthlyNarrative` として渡す**。
`require_editorial_and_closing=true` のときに巻頭言・むすびが空なら落とす
（§10.2-2・§10.2-5 が要求しているものを黙って省かないため）。

---

**⚠️ `No` が昇順でなければ組み立てない**

`No` の昇順は章のグルーピング順そのもの（§8.2・§10.3）で、T-22 のライタも
「並べ替えて救わない」方針を採っている。レンダラが黙って並べ直すと、章の束ね方
（T-21 が決めたもの）と違う構成の HTML が出る。

**章は「最初に現れた順」でまとめる。** 同じ章ラベルが離れた位置に現れても1章として
扱い、章ヘッダを2度出さない（目次の件数と本編の見出し数が食い違わないため）。
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from adapter.html import mail_html as m
from adapter.storage.artifact_store import ArtifactStore
from application.usecases.monthly_cases import CHAPTER_LABEL_FORMAT
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.period import Period, PeriodError, parse_period
from enterprise.entities.report_columns import (
    MONTHLY_CASE_COLUMNS_BY_NAME,
    ORGANIZATION_SEPARATOR,
)

logger = logging.getLogger(__name__)

# --- 参照する列（T-07 の定義に実在することを import 時に確かめる）--------------

COLUMN_NO = "No"
COLUMN_CHAPTER = "トピック(章)"
COLUMN_ORGANIZATIONS = "企業・組織"
COLUMN_TITLE = "タイトル"
COLUMN_URL = "URL"
COLUMN_SOURCE = "出典"
COLUMN_MONTH = "掲載月"
COLUMN_COMMENTARY = "解説"

REFERENCED_COLUMNS: tuple[str, ...] = (
    COLUMN_NO,
    COLUMN_CHAPTER,
    COLUMN_ORGANIZATIONS,
    COLUMN_TITLE,
    COLUMN_URL,
    COLUMN_SOURCE,
    COLUMN_MONTH,
    COLUMN_COMMENTARY,
)

# 章ラベル `第N章 <章タイトル>` を「バッジ」と「タイトル」へ割る。
# ⚠️ **体裁の正は T-21 の `CHAPTER_LABEL_FORMAT`**（書式から正規表現を導く）。
# 書式を変えたときにこちらだけ古いまま、という食い違いを起こさないため。
CHAPTER_LABEL_RE = re.compile(
    re.escape(CHAPTER_LABEL_FORMAT)
    .replace(r"\{number\}", r"(?P<number>\d+)")
    .replace(r"\{title\}", r"(?P<title>.*)")
    + r"$"
)
CHAPTER_BADGE_FORMAT = "第{number}章"

# --- 確定文言（仕様書 §10.2。逐語）------------------------------------------

HEADER_EYEBROW = "MONTHLY REPORT ON LEADING AI CASES"
BRAND_TITLE = "月刊ビリーフ by Sapeet"
ISSUE_BADGE_FORMAT = "{year}年{month}月号"
PERIOD_LABEL_FORMAT = "対象期間：{year}年{month}月1日 〜 {month}月{last_day}日"
HEADER_DESCRIPTION = (
    "今月公開された国内外の先進的なAI活用事例を、テーマごとの章に束ねて振り返ります。"
)

EDITORIAL_EYEBROW = "EDITORIAL"
EDITORIAL_HEADING = "巻頭言 ― 今月の総論"
CONTENTS_EYEBROW = "CONTENTS"
CONTENTS_HEADING = "目次"
CONTENTS_SUMMARY_FORMAT = "全{cases}事例・{chapters}章"
CHAPTER_COUNT_FORMAT = "{count}件"
CASE_LABEL_FORMAT = "CASE {no:02d} ／ {organizations}"
CLOSING_EYEBROW = "CLOSING"
CLOSING_HEADING = "むすび ― 来月への視点"
FOOTER_BRAND_FORMAT = "{brand} ／ {issue}"
FOOTER_CASE_BADGE_FORMAT = "収録事例 {count} 件"
FOOTER_CHAPTER_BADGE_FORMAT = "トピック {count} 章"
ISSUED_LABEL_FORMAT = "発行日：{year}年{month}月{day}日"
DOCUMENT_TITLE_FORMAT = "{brand}｜{issue}"

# --- 配色（設計書 §7.4「月刊 配色（確定）」・仕様書 §10.1）-------------------

PAGE_BACKGROUND = "#EEF2F6"
CONTENT_WIDTH = "680px"
CORNER_RADIUS = "8px"
NAVY = "#1F4E78"
ACCENT = "#4FA8DB"
ACCENT_LIGHT = "#9FD4F2"
BODY_TEXT = "#2C3E50"
PANEL_BACKGROUND = "#F7FAFC"
PANEL_BORDER = "#DCE7F0"
SURFACE = "#ffffff"
MUTED_TEXT = "#7B8A99"
INVERSE_TEXT = "#ffffff"


class MonthlyRenderError(Exception):
    """月刊 HTML を組み立てられない入力。"""


def _check_referenced_columns() -> None:
    """参照している列名が T-07 の定義に実在するか（import 時に落とす）。"""
    missing = [
        name for name in REFERENCED_COLUMNS if name not in MONTHLY_CASE_COLUMNS_BY_NAME
    ]
    if missing:
        raise MonthlyRenderError(
            "月次8列の定義（T-07）に無い列を参照しています: " + "、".join(missing)
        )


_check_referenced_columns()


# --- 生成テキスト -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonthlyNarrative:
    """render へ渡す「生成テキスト」（§7.4 の（生成N段落）行）。

    ⚠️ **このモジュールは中身を作らない**（§1.1）。作るのは filter 側
    （2026-08-16 の決定3）。

    Attributes:
        editorial_subtitle: 当月を一言で表すサブ見出し（§10.2-2）
        editorial: 巻頭言の総論（`\\n\\n` 区切りで3段落程度）
        chapter_intros: 章ラベル（`第N章 …`＝列2の値）→ 章導入文（§10.2-4）
        closing: むすび（`\\n\\n` 区切りで2段落。今月総括＋来月視点。§10.2-5）
    """

    editorial_subtitle: str | None = None
    editorial: str | None = None
    chapter_intros: Mapping[str, str] = field(default_factory=dict)
    closing: str | None = None

    def intro_for(self, chapter: str) -> str | None:
        """その章の導入文。無ければ `None`（導入文だけ出さない）。"""
        text = self.chapter_intros.get(chapter)
        return text.strip() if text and text.strip() else None


# --- 章のグルーピング（§10.3「`No` 昇順＝章グルーピング順」）-----------------


@dataclass(frozen=True, slots=True)
class Chapter:
    """本編の1章。

    Attributes:
        label: 列2 の値そのまま（`第1章 業務への組み込み`）
        number: `第N章` の N。書式に合わない場合は `None`
        title: 章タイトル。書式に合わない場合は `label` そのまま
        cases: その章の事例（`No` 昇順）
    """

    label: str
    number: int | None
    title: str
    cases: tuple[Mapping[str, Any], ...]

    @property
    def badge(self) -> str | None:
        """目次・章ヘッダのバッジ（`第N章`）。書式に合わなければ `None`。"""
        if self.number is None:
            return None
        return CHAPTER_BADGE_FORMAT.format(number=self.number)


def split_chapter_label(label: str) -> tuple[int | None, str]:
    """`第N章 <章タイトル>` を（N, タイトル）へ割る。

    書式に合わない値（手編集した xlsx 等）は **`(None, label)`** にして
    バッジ無しで描画する（章ごと落とすより読める形を優先）。
    """
    matched = CHAPTER_LABEL_RE.match(label.strip())
    if not matched:
        logger.warning("章ラベルの書式に合いません（バッジ無しで出します）: %r", label)
        return None, label.strip()
    return int(matched.group("number")), matched.group("title").strip()


def group_into_chapters(cases: Sequence[Mapping[str, Any]]) -> list[Chapter]:
    """事例を章へ束ねる（**最初に現れた順**。モジュール docstring）。

    Args:
        cases: 当月シートの行（`No` 昇順で渡すこと）

    Returns:
        章の一覧（各章の事例は渡された順のまま）
    """
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for case in cases:
        label = str(case.get(COLUMN_CHAPTER) or "").strip()
        buckets.setdefault(label, []).append(case)

    chapters: list[Chapter] = []
    for label, members in buckets.items():
        number, title = split_chapter_label(label) if label else (None, "")
        chapters.append(
            Chapter(label=label, number=number, title=title, cases=tuple(members))
        )
    return chapters


def ensure_ascending_numbers(cases: Sequence[Mapping[str, Any]]) -> None:
    """列1「No」が昇順であることを確かめる（§8.2・§10.3）。

    ⚠️ **ここで並べ替えない。** T-22 のライタと同じ理由——`No` の順序が章の
    束ね方そのものなので、レンダラが黙って直すと T-21 が決めた構成と違う HTML が
    出る（そして誰も気づかない）。

    Raises:
        MonthlyRenderError: `No` が無い／昇順でない場合
    """
    numbers: list[int] = []
    for case in cases:
        value = case.get(COLUMN_NO)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise MonthlyRenderError(f"月次の行に {COLUMN_NO} がありません")
        try:
            numbers.append(int(value))
        except (TypeError, ValueError) as exc:
            raise MonthlyRenderError(f"{COLUMN_NO} を読めません: {value!r}") from exc
    if numbers != sorted(numbers):
        raise MonthlyRenderError(f"月次の {COLUMN_NO} が昇順ではありません: {numbers}")


def organizations_of(case: Mapping[str, Any]) -> str:
    """列3「企業・組織」（multi）。複数主体は `・` で連ねる（§8.2）。"""
    value = case.get(COLUMN_ORGANIZATIONS)
    if value is None:
        return ""
    if isinstance(value, str):
        parts = value.split(ORGANIZATION_SEPARATOR)
    elif isinstance(value, Sequence):
        parts = [str(part) for part in value]
    else:
        parts = [str(value)]
    return ORGANIZATION_SEPARATOR.join(part.strip() for part in parts if part.strip())


# --- 組み立て -----------------------------------------------------------------


def _eyebrow(text: str, *, color: str) -> str:
    """セクション上の英字ラベル（EDITORIAL / CONTENTS / CLOSING）。"""
    return m.element(
        "p",
        m.escape(text),
        style=m.styles(
            "margin:0",
            "font-size:10px",
            "font-weight:bold",
            f"color:{color}",
            "letter-spacing:0.16em",
        ),
    )


def _heading(text: str, *, color: str = NAVY, top: str = "8px") -> str:
    return m.element(
        "p",
        m.escape(text),
        style=m.styles(
            f"margin:{top} 0 0 0",
            "font-size:17px",
            "font-weight:bold",
            f"color:{color}",
            "line-height:1.5",
        ),
    )


def _body_paragraphs(text: object, *, last_style: str | None = None) -> str:
    base = m.styles(
        "margin:12px 0 0 0", "font-size:13px", "line-height:1.95", f"color:{BODY_TEXT}"
    )
    first = m.styles(
        "margin:0", "font-size:13px", "line-height:1.95", f"color:{BODY_TEXT}"
    )
    return m.paragraphs(text, style=base, first_style=first, last_style=last_style)


def _header(*, period: Period) -> str:
    """§10.2-1 ヘッダ（ネイビー）。"""
    issue = ISSUE_BADGE_FORMAT.format(year=period.start.year, month=period.start.month)
    badge = m.block(
        m.element(
            "p",
            m.escape(issue),
            style=m.styles(
                "margin:0",
                "font-size:12px",
                "font-weight:bold",
                f"color:{NAVY}",
            ),
        ),
        style=m.styles(
            f"background-color:{ACCENT_LIGHT}",
            "border-radius:3px",
            "margin:14px 0 0 0",
        ),
        cell_style="padding:5px 12px",
        width=None,
    )
    return m.row(
        [
            m.cell(
                "".join(
                    [
                        _eyebrow(HEADER_EYEBROW, color=ACCENT_LIGHT),
                        m.element(
                            "p",
                            m.escape(BRAND_TITLE),
                            style=m.styles(
                                "margin:10px 0 0 0",
                                "font-size:22px",
                                "font-weight:bold",
                                f"color:{INVERSE_TEXT}",
                                "letter-spacing:0.04em",
                            ),
                        ),
                        badge,
                        m.element(
                            "p",
                            m.escape(
                                PERIOD_LABEL_FORMAT.format(
                                    year=period.start.year,
                                    month=period.start.month,
                                    last_day=period.end.day,
                                )
                            ),
                            style=m.styles(
                                "margin:16px 0 0 0",
                                "font-size:12px",
                                f"color:{ACCENT_LIGHT}",
                            ),
                        ),
                        m.element(
                            "p",
                            m.escape(HEADER_DESCRIPTION),
                            style=m.styles(
                                "margin:8px 0 0 0",
                                "font-size:12px",
                                "line-height:1.9",
                                "color:#D7E6F2",
                            ),
                        ),
                    ]
                ),
                style=m.styles(f"background-color:{NAVY}", "padding:32px 30px"),
            )
        ]
    )


def _panel(content: str) -> str:
    """`#F7FAFC` の囲み（巻頭言・むすび。§10.2-2・§10.2-5）。"""
    return m.block(
        content,
        style=m.styles(
            f"background-color:{PANEL_BACKGROUND}",
            f"border:1px solid {PANEL_BORDER}",
            "border-radius:6px",
            "margin:14px 0 0 0",
        ),
        cell_style="padding:18px 20px",
    )


def _editorial(narrative: MonthlyNarrative) -> str:
    """§10.2-2 巻頭言（見出し＋サブ見出し＋`#F7FAFC` カードに3段落）。"""
    parts = [_eyebrow(EDITORIAL_EYEBROW, color=ACCENT), _heading(EDITORIAL_HEADING)]
    if subtitle := (narrative.editorial_subtitle or "").strip():
        parts.append(
            m.element(
                "p",
                m.escape(subtitle),
                style=m.styles(
                    "margin:10px 0 0 0",
                    "font-size:13px",
                    "font-weight:bold",
                    f"color:{ACCENT}",
                    "line-height:1.7",
                ),
            )
        )
    parts.append(_panel(_body_paragraphs(narrative.editorial)))
    return m.row([m.cell("".join(parts), style="padding:30px 30px 0 30px")])


def _contents(chapters: Sequence[Chapter], *, case_count: int) -> str:
    """§10.2-3 目次（ネイビーカード・章一覧＋件数＋`全N事例・M章`）。"""
    entries: list[str] = []
    for chapter in chapters:
        badge = chapter.badge
        left = "".join(
            filter(
                None,
                [
                    m.element(
                        "span",
                        m.escape(badge),
                        style=m.styles(
                            f"color:{ACCENT_LIGHT}",
                            "font-weight:bold",
                            "font-size:11px",
                        ),
                    )
                    if badge
                    else None,
                    m.element(
                        "span",
                        m.escape(chapter.title),
                        style=m.styles(
                            f"color:{INVERSE_TEXT}",
                            "font-size:13px",
                            "padding-left:10px" if badge else None,
                        ),
                    ),
                ],
            )
        )
        entries.append(
            m.row(
                [
                    m.cell(left, style="padding:7px 0"),
                    m.cell(
                        m.escape(CHAPTER_COUNT_FORMAT.format(count=len(chapter.cases))),
                        style=m.styles(
                            "padding:7px 0", "font-size:11px", f"color:{ACCENT_LIGHT}"
                        ),
                        attrs={"align": "right"},
                    ),
                ]
            )
        )

    summary = m.element(
        "p",
        m.escape(
            CONTENTS_SUMMARY_FORMAT.format(cases=case_count, chapters=len(chapters))
        ),
        style=m.styles(
            "margin:14px 0 0 0",
            "font-size:11px",
            f"color:{ACCENT_LIGHT}",
            f"border-top:1px solid {ACCENT}",
            "padding-top:12px",
        ),
    )
    card = m.block(
        "".join(
            [
                _eyebrow(CONTENTS_EYEBROW, color=ACCENT_LIGHT),
                _heading(CONTENTS_HEADING, color=INVERSE_TEXT),
                m.table(entries, style="margin:12px 0 0 0") if entries else "",
                summary,
            ]
        ),
        style=m.styles(f"background-color:{NAVY}", "border-radius:6px"),
        cell_style="padding:20px 22px",
    )
    return m.row([m.cell(card, style="padding:26px 30px 0 30px")])


def _chapter_header(chapter: Chapter, intro: str | None) -> str:
    """§10.2-4 章ヘッダ（下端 `2px solid #4FA8DB`）。"""
    badge = chapter.badge
    parts = [
        m.element(
            "p",
            "".join(
                filter(
                    None,
                    [
                        m.element(
                            "span",
                            m.escape(badge),
                            style=m.styles(
                                f"color:{ACCENT}",
                                "font-size:11px",
                                "font-weight:bold",
                            ),
                        )
                        if badge
                        else None,
                        m.element(
                            "span",
                            m.escape(chapter.title),
                            style=m.styles(
                                f"color:{NAVY}",
                                "font-size:16px",
                                "font-weight:bold",
                                "padding-left:10px" if badge else None,
                            ),
                        ),
                    ],
                )
            ),
            style="margin:0;line-height:1.6",
        )
    ]
    if intro:
        parts.append(
            m.element(
                "p",
                m.escape(intro),
                style=m.styles(
                    "margin:10px 0 0 0",
                    "font-size:12px",
                    "line-height:1.9",
                    f"color:{BODY_TEXT}",
                ),
            )
        )
    return m.row(
        [
            m.cell(
                "".join(parts),
                style=m.styles(
                    "padding:30px 30px 12px 30px",
                    f"border-bottom:2px solid {ACCENT}",
                ),
            )
        ]
    )


def _case_card(case: Mapping[str, Any]) -> str:
    """§10.2-4 事例カード（`CASE NN ／ 〈企業〉`／タイトル／本文／出典行）。"""
    # ⚠️ `NN` は列1「No」そのもの（レンダラ側で数え直さない）。`No` が章の
    # グルーピング順を表す通し番号なので、表と HTML で番号が食い違わない。
    label = CASE_LABEL_FORMAT.format(
        no=int(case[COLUMN_NO]), organizations=organizations_of(case)
    )
    source = m.element(
        "p",
        m.escape(case.get(COLUMN_SOURCE)),
        style=m.styles(
            "margin:16px 0 0 0",
            f"border-top:1px solid {PANEL_BORDER}",
            "padding-top:10px",
            "font-size:11px",
            f"color:{MUTED_TEXT}",
        ),
    )
    body = "".join(
        [
            m.element(
                "p",
                m.escape(label),
                style=m.styles(
                    "margin:0",
                    "font-size:11px",
                    "font-weight:bold",
                    f"color:{ACCENT}",
                    "letter-spacing:0.04em",
                ),
            ),
            m.element(
                "p",
                m.link(
                    case.get(COLUMN_TITLE),
                    case.get(COLUMN_URL),
                    style=m.styles(f"color:{NAVY}", "text-decoration:none"),
                ),
                style=m.styles(
                    "margin:8px 0 14px 0",
                    "font-size:16px",
                    "font-weight:bold",
                    "line-height:1.6",
                    f"color:{NAVY}",
                ),
            ),
            _body_paragraphs(
                case.get(COLUMN_COMMENTARY),
                # §10.3「最終段落は示唆／持ち帰りトーン」。文面は AI 側（T-21）が
                # 書くので、この層は**見え方だけ**を分ける。
                last_style=m.styles(
                    "margin:12px 0 0 0",
                    "font-size:13px",
                    "line-height:1.95",
                    f"color:{NAVY}",
                    f"border-left:3px solid {ACCENT_LIGHT}",
                    "padding-left:12px",
                ),
            ),
            source,
        ]
    )
    return m.row([m.cell(body, style="padding:22px 30px 0 30px")])


def _closing(narrative: MonthlyNarrative) -> str:
    """§10.2-5 むすび（2段落）。"""
    body = "".join(
        [
            _eyebrow(CLOSING_EYEBROW, color=ACCENT),
            _heading(CLOSING_HEADING),
            _panel(_body_paragraphs(narrative.closing)),
        ]
    )
    return m.row([m.cell(body, style="padding:34px 30px 0 30px")])


def _footer(
    *, period: Period, case_count: int, chapter_count: int, issued_on: date
) -> str:
    """§10.2-6 フッタ（ネイビー・件数バッジ・対象期間・発行日）。"""
    issue = ISSUE_BADGE_FORMAT.format(year=period.start.year, month=period.start.month)
    badge_style = m.styles(
        f"background-color:{NAVY}",
        f"border:1px solid {ACCENT}",
        "border-radius:3px",
        "padding:5px 12px",
        "font-size:11px",
        f"color:{ACCENT_LIGHT}",
    )
    badge_cells: list[str] = []
    for text in (
        FOOTER_CASE_BADGE_FORMAT.format(count=case_count),
        FOOTER_CHAPTER_BADGE_FORMAT.format(count=chapter_count),
    ):
        if badge_cells:
            badge_cells.append(m.spacer_cell("8px"))
        badge_cells.append(m.cell(m.escape(text), style=badge_style))
    badges = m.table([m.row(badge_cells)], style="margin:12px 0 0 0", width=None)
    lines = m.element(
        "p",
        m.escape(
            PERIOD_LABEL_FORMAT.format(
                year=period.start.year,
                month=period.start.month,
                last_day=period.end.day,
            )
        )
        + "<br>"
        + m.escape(
            ISSUED_LABEL_FORMAT.format(
                year=issued_on.year, month=issued_on.month, day=issued_on.day
            )
        ),
        style=m.styles(
            "margin:14px 0 0 0", "font-size:11px", "line-height:1.9", "color:#B9CFE0"
        ),
    )
    return m.row(
        [
            m.cell(
                "".join(
                    [
                        m.element(
                            "p",
                            m.escape(
                                FOOTER_BRAND_FORMAT.format(
                                    brand=BRAND_TITLE, issue=issue
                                )
                            ),
                            style=m.styles(
                                "margin:0",
                                "font-size:13px",
                                "font-weight:bold",
                                f"color:{INVERSE_TEXT}",
                            ),
                        ),
                        badges,
                        lines,
                    ]
                ),
                style=m.styles(f"background-color:{NAVY}", "padding:28px 30px"),
            )
        ]
    )


def render_monthly_html(
    *,
    period: str,
    cases: Sequence[Mapping[str, Any]],
    config: IntelligenceConfig,
    narrative: MonthlyNarrative | None = None,
    issued_on: date | None = None,
) -> str:
    """当月シートから月刊ビリーフ HTML を組み立てる（**AI を呼ばない**）。

    Args:
        period: `2026-07`（シート名＝列7「掲載月」）
        cases: 当月シートの行（列名 → 値。**`No` 昇順**）
        config: 実行時 config（固定参照済み）
        narrative: 生成テキスト。`None` なら空
        issued_on: フッタの発行日。`None` なら**対象月の末日**。
            ⚠️ `date.today()` を既定にしないのは §14 の再現性のため
            （同じ入力から同じ HTML が出なくなる）

    Returns:
        HTML 文字列（UTF-8 で書き出す前提）

    Raises:
        MonthlyRenderError: period が月次表記でない／`No` が昇順でない／
            `require_editorial_and_closing=true` なのに巻頭言・むすびが空／
            生成物が §7.1 の制約に反する
    """
    markup, _ = _render(
        period=period,
        cases=cases,
        config=config,
        narrative=narrative,
        issued_on=issued_on,
    )
    return markup


def _render(
    *,
    period: str,
    cases: Sequence[Mapping[str, Any]],
    config: IntelligenceConfig,
    narrative: MonthlyNarrative | None,
    issued_on: date | None,
) -> tuple[str, list[Chapter]]:
    """組み立て本体。**章立てを2度作らない**ため書き出し側もこれを使う。"""
    parsed = _parse_monthly(period)
    narrative = narrative or MonthlyNarrative()
    monthly = config.tunable_thresholds.monthly

    if monthly.require_editorial_and_closing:
        missing = [
            name
            for name, text in (
                ("巻頭言", narrative.editorial),
                ("むすび", narrative.closing),
            )
            if not (text or "").strip()
        ]
        if missing:
            raise MonthlyRenderError(
                f"{'・'.join(missing)}が空です"
                "（`require_editorial_and_closing=true`・仕様書 §10.2-2／§10.2-5）。"
                "生成テキストは filter 側が作って `MonthlyNarrative` で渡してください"
            )

    ensure_ascending_numbers(cases)
    _warn_on_month_mismatch(cases, parsed)

    chapters = group_into_chapters(cases)
    _warn_on_composition(chapters, case_count=len(cases), config=config)

    rows: list[str] = [_header(period=parsed)]
    if (narrative.editorial or "").strip():
        rows.append(_editorial(narrative))
    rows.append(_contents(chapters, case_count=len(cases)))
    for chapter in chapters:
        rows.append(_chapter_header(chapter, narrative.intro_for(chapter.label)))
        rows.extend(_case_card(case) for case in chapter.cases)
    if (narrative.closing or "").strip():
        rows.append(_closing(narrative))
    rows.append(m.spacer_row("34px"))
    rows.append(
        _footer(
            period=parsed,
            case_count=len(cases),
            chapter_count=len(chapters),
            issued_on=issued_on or parsed.end,
        )
    )

    inner = m.table(
        rows,
        style=m.styles(
            f"width:{CONTENT_WIDTH}",
            "max-width:100%",
            "margin:0 auto",
            f"background-color:{SURFACE}",
            f"border-radius:{CORNER_RADIUS}",
            "overflow:hidden",
            "box-shadow:0 1px 3px rgba(31,78,120,0.12)",
        ),
        width=None,
    )
    outer = m.table(
        [
            m.row(
                [
                    m.cell(
                        inner,
                        style="padding:26px 12px",
                        attrs={"align": "center"},
                    )
                ]
            )
        ],
        style=f"background-color:{PAGE_BACKGROUND}",
    )
    markup = m.document(
        title=DOCUMENT_TITLE_FORMAT.format(
            brand=BRAND_TITLE,
            issue=ISSUE_BADGE_FORMAT.format(
                year=parsed.start.year, month=parsed.start.month
            ),
        ),
        body=outer,
        background=PAGE_BACKGROUND,
    )

    logger.info(
        "monthly html rendered (period=%s, cases=%d, chapters=%d)",
        parsed.text,
        len(cases),
        len(chapters),
    )
    return _mail_safe(markup, period=parsed.text), chapters


def _parse_monthly(period: str) -> Period:
    try:
        parsed = parse_period(period)
    except PeriodError as exc:
        raise MonthlyRenderError(str(exc)) from exc
    if not parsed.is_monthly:
        raise MonthlyRenderError(f"月次 period が必要です: {period!r}")
    return parsed


def _warn_on_month_mismatch(cases: Sequence[Mapping[str, Any]], period: Period) -> None:
    """列7「掲載月」が対象月と違う行を知らせる（落とさない）。

    §7.4 は号バッジ・対象期間を列7から引くとしているが、当月シートの列7 は
    period と同じ値になる（T-21 が `掲載月=period` で埋める）。ずれていたら
    **シートに別の月の行が混ざっている**ので、黙って号数を割らずに警告する。
    """
    mismatched = {
        str(case.get(COLUMN_MONTH))
        for case in cases
        if case.get(COLUMN_MONTH) and str(case.get(COLUMN_MONTH)) != period.text
    }
    if mismatched:
        logger.warning(
            "対象月と違う %s の行が混ざっています（period=%s）",
            "、".join(sorted(mismatched)),
            period.text,
        )


def _warn_on_composition(
    chapters: Sequence[Chapter], *, case_count: int, config: IntelligenceConfig
) -> None:
    """§10.3 の構成目安（`target_case_count` / `chapter_count_hint`）との差。

    ⚠️ **目安なので出力は変えない**（件数を切り詰めると T-21 が選んだ事例が
    黙って消える）。差が出ていることだけ残す。
    """
    monthly = config.tunable_thresholds.monthly
    if case_count > monthly.target_case_count:
        logger.warning(
            "事例が構成目安を超えています（%d 件 > target_case_count=%d）",
            case_count,
            monthly.target_case_count,
        )
    if monthly.chapter_count_hint and len(chapters) > monthly.chapter_count_hint:
        logger.warning(
            "章が構成目安を超えています（%d 章 > chapter_count_hint=%d）",
            len(chapters),
            monthly.chapter_count_hint,
        )


def _mail_safe(markup: str, *, period: str) -> str:
    try:
        return m.assert_mail_safe(markup)
    except m.MailHtmlError as exc:
        raise MonthlyRenderError(f"{period} の月刊HTML: {exc}") from exc


# --- 書き出し（正規名は T-02 が解決・設計判断B）-------------------------------


@dataclass(frozen=True, slots=True)
class RenderedHtml:
    """書き出した結果（T-26 の監査ログ・`GET /reports` 用）。

    Attributes:
        path: 正規名のパス（上書き済み）
        archived: 退避先。初回実行なら `None`
        markup: 書き出した HTML
        cases: 収録事例数
        chapters: 章数
    """

    path: Path
    archived: Path | None
    markup: str
    cases: int
    chapters: int


class MonthlyRenderer:
    """月刊 HTML の組み立てと書き出し（`ArtifactStore` 経由）。

    renderer = MonthlyRenderer(ArtifactStore.from_settings())
    result = renderer.render(
        period="2026-07",
        cases=ReportStore(store).read_monthly("2026-07"),
        config=pinned_config,
        narrative=narrative,
        revision=pinned_config.meta.revision,
        run_id=run_id,
    )
    """

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    @property
    def store(self) -> ArtifactStore:
        return self._store

    def render(
        self,
        *,
        period: str,
        cases: Sequence[Mapping[str, Any]],
        config: IntelligenceConfig,
        narrative: MonthlyNarrative | None = None,
        issued_on: date | None = None,
        revision: int,
        run_id: str,
    ) -> RenderedHtml:
        """組み立てて `monthly_belief_{YYYY-MM}.html` へ書き出す。

        ⚠️ **退避が先**（設計判断B。T-22 の `_save()` と同じ順序）。

        Raises:
            MonthlyRenderError: 組み立てられない入力
            ArtifactStoreError: period をファイル名へ埋め込めない
        """
        markup, chapters = _render(
            period=period,
            cases=cases,
            config=config,
            narrative=narrative,
            issued_on=issued_on,
        )
        path = self._store.monthly_html_path(period)
        archived = self._store.archive(
            path, period=period, revision=revision, run_id=run_id
        )
        self._store.write_text(path, markup)

        logger.info("monthly html written (path=%s, archived=%s)", path, archived)
        return RenderedHtml(
            path=path,
            archived=archived,
            markup=markup,
            cases=len(cases),
            chapters=len(chapters),
        )


__all__ = [
    "BRAND_TITLE",
    "CASE_LABEL_FORMAT",
    "CLOSING_HEADING",
    "CONTENTS_SUMMARY_FORMAT",
    "EDITORIAL_HEADING",
    "FOOTER_CASE_BADGE_FORMAT",
    "FOOTER_CHAPTER_BADGE_FORMAT",
    "HEADER_EYEBROW",
    "ISSUE_BADGE_FORMAT",
    "REFERENCED_COLUMNS",
    "Chapter",
    "MonthlyNarrative",
    "MonthlyRenderError",
    "MonthlyRenderer",
    "RenderedHtml",
    "ensure_ascending_numbers",
    "group_into_chapters",
    "organizations_of",
    "render_monthly_html",
    "split_chapter_label",
]
