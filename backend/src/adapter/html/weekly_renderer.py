"""週刊メルマガ レンダラ（設計書 §7.3 ／ 仕様書 §9 ／ T-24）。

中間xlsx の当週シート（22列。T-22 の `ReportStore.read_weekly()`）と実行時 config
から、`weekly_ai_intelligence_newsletter_{industry}_{period}.html` を組み立てる。

**AI を呼ばない**（TASKS.md §1.1「render = 決定的 Python テンプレート」）。
同じシートと同じ config からは常に同じ HTML が出る（§14 再現性）。

---

**§7.3 のマッピング（この モジュールが実装する対応表）**

| HTML | 中間xlsx / config |
|---|---|
| ヘッダ「〈業界〉版」 | `config.weekly.target_industry` |
| ヘッダ「対象週」 | シート名（`YYYY-Www`） |
| 今週のポイント | （生成テキスト → `WeeklyNarrative`） |
| 業界関連トピック | 列19「業界」に `target_industry` を含む記事 |
| 業界共通トピック | それ以外（「業界横断」等） |
| ├ カテゴリラベル | 列2「情報カテゴリ」→ §7.2 の色 ＋ config のラベル |
| ├ タイトル | 列3「タイトル」＋列22「URL」 |
| ├ 本文要約 | 列4「一言要約」 |
| ├ 示唆ボックス | （生成テキスト → `WeeklyNarrative.insights`） |
| └ 出典行 | 列21「ソース」＋列22「URL」 |
| 並び順 | 列5「合計スコア」降順 |
| 採用条件 | 列12 `≠ 不採用` かつ 列5 `≥ min_total_score_to_publish` |

---

**⚠️ 「生成テキスト」は渡してもらう**（2026-08-16 の決定3）

§7.3 の表で「（生成テキスト）」となっている2つ——**今週のポイント**と各カードの
**示唆ボックス**——は中間xlsx の22列に無い。列は §8.1 の確定値なので増やせない。
そこで **filter（T-21）が同じ実行の中で生成して `WeeklyNarrative` として渡す**形に
した（レンダラに AI を足すと §1.1 に反するため）。生成側の実装は別タスク。

`WeeklyNarrative()`（空）を渡せば生成テキスト無しでも描画できるが、
**`point_of_week_required=true` のときに今週のポイントが空なら落とす**
（§9.2-2 が「必須」と書いているものを黙って省くと、要件を満たさない HTML が
配信に回るため）。

---

**⚠️ タイトルが空の記事はカードにしない**（T-07 申し送り）

§12.1 の非空必須リストに「タイトル」が無いので、フォーマットチェック（T-20）は
タイトル欠落を落とさない。カードの見出しに使うのはこの層なので、ここでガードして
`logger.warning` に出す（見出しの無いカードを出す方が読み手に不親切）。
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adapter.html import mail_html as m
from adapter.html.category_colors import color_of
from adapter.storage.artifact_store import ArtifactStore
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.period import Period, PeriodError, parse_period
from enterprise.entities.report_columns import (
    MULTI_VALUE_SEPARATOR,
    WEEKLY_ARTICLE_COLUMNS_BY_NAME,
)
from enterprise.services.exclusion import ADOPTION_CLASS_DESCENDING

logger = logging.getLogger(__name__)

# --- 参照する列（T-07 の定義に実在することを import 時に確かめる）--------------

COLUMN_CATEGORY = "情報カテゴリ"
COLUMN_TITLE = "タイトル"
COLUMN_SUMMARY = "一言要約"
COLUMN_TOTAL_SCORE = "合計スコア"
COLUMN_ADOPTION_CLASS = "レポート採用区分"
COLUMN_INDUSTRY = "業界"
COLUMN_SOURCE = "ソース"
COLUMN_URL = "URL"

REFERENCED_COLUMNS: tuple[str, ...] = (
    COLUMN_CATEGORY,
    COLUMN_TITLE,
    COLUMN_SUMMARY,
    COLUMN_TOTAL_SCORE,
    COLUMN_ADOPTION_CLASS,
    COLUMN_INDUSTRY,
    COLUMN_SOURCE,
    COLUMN_URL,
)

# 採用条件の「不採用」（§9.3）。**文字列を書かず enum の末尾から引く**
# （`ADOPTION_CLASS_DESCENDING` は降順なので最後が最下位＝不採用。T-17）。
NOT_ADOPTED = ADOPTION_CLASS_DESCENDING[-1]

# --- 確定文言（仕様書 §9.2。逐語）--------------------------------------------

BRAND_TITLE = "Weekly AI Intelligence by Sapeet"
INDUSTRY_BADGE_FORMAT = "{industry} 版"
PERIOD_LABEL_FORMAT = "対象週：{period}"
POINT_OF_WEEK_HEADING = "今週のポイント"
INDUSTRY_SECTION_FORMAT = "{industry}関連トピック"
COMMON_SECTION_HEADING = "業界共通トピック"
SOURCE_LINE_FORMAT = "出典：{source} ／ "
READ_MORE_LABEL = "記事を読む"
DOCUMENT_TITLE_FORMAT = "{brand}｜{badge}（{period}）"

# §9.2-5 フッタ注記（「編集部整理であり投資・法務助言でない旨」）。
FOOTER_NOTE = (
    "本レポートは編集部が公開情報をもとに整理したものであり、"
    "投資・法務・税務に関する助言ではありません。"
    "掲載内容の正確性・完全性を保証するものではなく、"
    "実際のご判断は原典および専門家の見解をご確認ください。"
)

# --- 配色（設計書 §7.3「週刊 配色（確定）」・仕様書 §9.1）---------------------

PAGE_BACKGROUND = "#f3f4f6"
CONTENT_MAX_WIDTH = "680px"
HEADER_GRADIENT = "linear-gradient(135deg,#4f46e5,#7c3aed)"
ACCENT = "#4f46e5"
INSIGHT_BACKGROUND = "#eef2ff"
INSIGHT_BORDER = "#6366f1"

# グラデーションを解釈しないメールクライアント向けの単色フォールバック。
# **アクセント（グラデーション開始色）と同じ値**にしてある。
HEADER_FALLBACK = ACCENT

SURFACE = "#ffffff"
BORDER = "#e5e7eb"
TEXT = "#111827"
BODY_TEXT = "#374151"
MUTED_TEXT = "#6b7280"
INSIGHT_TEXT = "#3730a3"


class WeeklyRenderError(Exception):
    """週刊 HTML を組み立てられない入力。"""


def _check_referenced_columns() -> None:
    """参照している列名が T-07 の定義に実在するか（import 時に落とす）。

    列名を直接書いているのは中間xlsx の行が「列名 → 値」の dict だから
    （T-21 の `weekly_record()` と同じ形）。**列順は一切知らない**が、列名を
    変えられたときに黙って空欄のカードを出さないよう、ここで突き合わせる。
    """
    missing = [
        name
        for name in REFERENCED_COLUMNS
        if name not in WEEKLY_ARTICLE_COLUMNS_BY_NAME
    ]
    if missing:
        raise WeeklyRenderError(
            "週次22列の定義（T-07）に無い列を参照しています: " + "、".join(missing)
        )


_check_referenced_columns()


# --- 生成テキスト -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WeeklyNarrative:
    """render へ渡す「生成テキスト」（§7.3 の（生成テキスト）行）。

    ⚠️ **このモジュールは中身を作らない**（§1.1）。作るのは filter 側
    （2026-08-16 の決定3）。

    Attributes:
        point_of_week: 今週のポイント（当週の総括3〜4文。§9.2-2）。
            `\\n\\n` を含めば複数段落になる
        insights: 記事URL → 示唆ボックスの1段落（§9.2-4）。**鍵は URL**
            （§12.1 の非空必須項目で、記事を一意に指せる唯一の列）
    """

    point_of_week: str | None = None
    insights: Mapping[str, str] = field(default_factory=dict)

    def insight_for(self, url: object) -> str | None:
        """その記事の示唆。無ければ `None`（ボックスごと出さない）。"""
        key = "" if url is None else str(url).strip()
        if not key:
            return None
        text = self.insights.get(key)
        return text.strip() if text and text.strip() else None


# --- 採用条件と業界振り分け（§9.3）-------------------------------------------


@dataclass(frozen=True, slots=True)
class WeeklySelection:
    """当週シートから何をカードにするか決めた結果。

    Attributes:
        industry_topics: 業界関連トピック（上限適用後・合計スコア降順）
        common_topics: 業界共通トピック（同上）
        adopted: 採用条件を通った件数（**上限を適用する前**）
        untitled: タイトルが空で落とした件数（T-07 申し送りのガード）
    """

    industry_topics: tuple[Mapping[str, Any], ...]
    common_topics: tuple[Mapping[str, Any], ...]
    adopted: int
    untitled: int

    @property
    def rendered(self) -> int:
        """実際にカードにした件数。"""
        return len(self.industry_topics) + len(self.common_topics)


def is_adopted(record: Mapping[str, Any], config: IntelligenceConfig) -> bool:
    """§9.3 の採用条件：`採用区分 ≠ 不採用` **かつ** `合計スコア ≥ しきい値`。

    ⚠️ 合計スコアが空の行は**採用しない**（0点扱い）。§12.1 は合計スコアを
    非空必須にしているので、空なら検証を通っていない行が紛れている。
    """
    if record.get(COLUMN_ADOPTION_CLASS) == NOT_ADOPTED:
        return False
    return (
        total_score_of(record) >= config.tunable_thresholds.min_total_score_to_publish
    )


def total_score_of(record: Mapping[str, Any]) -> int:
    """列5「合計スコア」。読めない値は 0（採用条件で落ちる）。"""
    value = record.get(COLUMN_TOTAL_SCORE)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("合計スコアを読めませんでした: %r", value)
        return 0


def industries_of(record: Mapping[str, Any]) -> tuple[str, ...]:
    """列19「業界」（multi）。リーダは `list[str]` を返すが、文字列でも受ける。"""
    value = record.get(COLUMN_INDUSTRY)
    if value is None:
        return ()
    if isinstance(value, str):
        parts = value.split(MULTI_VALUE_SEPARATOR)
    elif isinstance(value, Sequence):
        parts = [str(part) for part in value]
    else:
        parts = [str(value)]
    return tuple(part.strip() for part in parts if part.strip())


def select_articles(
    articles: Sequence[Mapping[str, Any]], config: IntelligenceConfig
) -> WeeklySelection:
    """採用条件・業界振り分け・上限・並び順を決める（§9.3・§7.3）。

    Args:
        articles: 当週シートの行（列名 → 値）
        config: 実行時 config（固定参照済み）

    Returns:
        業界関連／業界共通に振り分けた結果
    """
    weekly = config.tunable_thresholds.weekly

    adopted = [record for record in articles if is_adopted(record, config)]
    untitled = 0

    # ⚠️ **並び順は合計スコア降順**（§9.3）。シートは既に降順だが、渡され方に
    # 依存させない。同点は渡された順のまま（安定ソート）。
    adopted.sort(key=total_score_of, reverse=True)

    industry_topics: list[Mapping[str, Any]] = []
    common_topics: list[Mapping[str, Any]] = []
    for record in adopted:
        title = str(record.get(COLUMN_TITLE) or "").strip()
        if not title:
            # T-20 はタイトル欠落を落とさない（§12.1 の非空必須に無い）。
            untitled += 1
            logger.warning(
                "タイトルが空の記事をカードにしませんでした: url=%r",
                record.get(COLUMN_URL),
            )
            continue
        if weekly.target_industry in industries_of(record):
            industry_topics.append(record)
        else:
            common_topics.append(record)

    return WeeklySelection(
        industry_topics=tuple(industry_topics[: weekly.max_industry_topics]),
        common_topics=tuple(common_topics[: weekly.max_common_topics]),
        adopted=len(adopted),
        untitled=untitled,
    )


# --- 組み立て -----------------------------------------------------------------


def category_labels(config: IntelligenceConfig) -> dict[str, str]:
    """カテゴリID → ラベル（config が正。色は §7.2 の ID 別マップ）。"""
    return {category.id: category.label for category in config.information_categories}


def _header(*, industry: str, period: str) -> str:
    """§9.2-1 ヘッダ（グラデ背景）。"""
    return m.row(
        [
            m.cell(
                "".join(
                    [
                        m.element(
                            "p",
                            m.escape(BRAND_TITLE),
                            style=m.styles(
                                "margin:0",
                                "font-size:19px",
                                "font-weight:bold",
                                "color:#ffffff",
                                "letter-spacing:0.02em",
                            ),
                        ),
                        m.element(
                            "p",
                            m.escape(INDUSTRY_BADGE_FORMAT.format(industry=industry)),
                            style=m.styles(
                                "margin:10px 0 0 0",
                                "font-size:14px",
                                "font-weight:bold",
                                "color:#e0e7ff",
                            ),
                        ),
                        m.element(
                            "p",
                            m.escape(PERIOD_LABEL_FORMAT.format(period=period)),
                            style=m.styles(
                                "margin:6px 0 0 0",
                                "font-size:12px",
                                "color:#c7d2fe",
                            ),
                        ),
                    ]
                ),
                style=m.styles(
                    f"background-color:{HEADER_FALLBACK}",
                    f"background-image:{HEADER_GRADIENT}",
                    "padding:30px 28px",
                ),
            )
        ]
    )


def _point_of_week(text: str) -> str:
    """§9.2-2 今週のポイント（白カード）。"""
    card = m.block(
        "".join(
            [
                m.element(
                    "p",
                    m.escape(POINT_OF_WEEK_HEADING),
                    style=m.styles(
                        "margin:0 0 10px 0",
                        "font-size:13px",
                        "font-weight:bold",
                        f"color:{ACCENT}",
                    ),
                ),
                m.paragraphs(
                    text,
                    style=m.styles(
                        "margin:10px 0 0 0",
                        "font-size:13px",
                        "line-height:1.9",
                        f"color:{BODY_TEXT}",
                    ),
                    first_style=m.styles(
                        "margin:0",
                        "font-size:13px",
                        "line-height:1.9",
                        f"color:{BODY_TEXT}",
                    ),
                ),
            ]
        ),
        style=m.styles(
            f"background-color:{SURFACE}",
            f"border:1px solid {BORDER}",
            "border-radius:6px",
        ),
        cell_style="padding:18px 20px",
    )
    return m.row([m.cell(card, style="padding:24px 28px 0 28px")])


def _section_heading(text: str) -> str:
    heading = m.element(
        "p",
        m.escape(text),
        style=m.styles(
            "margin:0",
            "font-size:15px",
            "font-weight:bold",
            f"color:{TEXT}",
            f"border-left:4px solid {ACCENT}",
            "padding-left:10px",
            "line-height:1.4",
        ),
    )
    return m.row([m.cell(heading, style="padding:28px 28px 0 28px")])


def _insight_box(text: str) -> str:
    """§9.2-4 示唆ボックス（背景 `#eef2ff`・左罫 `#6366f1`）。"""
    return m.block(
        m.element(
            "p",
            m.escape(text),
            style=m.styles(
                "margin:0",
                "font-size:12px",
                "line-height:1.9",
                f"color:{INSIGHT_TEXT}",
            ),
        ),
        style=m.styles(
            f"background-color:{INSIGHT_BACKGROUND}",
            f"border-left:3px solid {INSIGHT_BORDER}",
            "margin-top:14px",
        ),
        cell_style="padding:12px 14px",
    )


def _source_line(record: Mapping[str, Any]) -> str:
    """§9.2-4 出典行「出典：〈ソース〉 ／ 記事を読む」。"""
    source = str(record.get(COLUMN_SOURCE) or "").strip()
    return m.element(
        "p",
        m.escape(SOURCE_LINE_FORMAT.format(source=source))
        + m.link(
            READ_MORE_LABEL,
            record.get(COLUMN_URL),
            style=m.styles(f"color:{ACCENT}", "text-decoration:none"),
        ),
        style=m.styles("margin:14px 0 0 0", "font-size:11px", f"color:{MUTED_TEXT}"),
    )


def _card(
    record: Mapping[str, Any],
    *,
    labels: Mapping[str, str],
    narrative: WeeklyNarrative,
) -> str:
    """記事1件のカード（§9.2-4 の5要素）。"""
    category_id = record.get(COLUMN_CATEGORY)
    key = str(category_id) if category_id else ""
    parts = [
        m.element(
            "p",
            m.escape(labels.get(key, key)),
            style=m.styles(
                "margin:0",
                "font-size:11px",
                "font-weight:bold",
                f"color:{color_of(key)}",
                "letter-spacing:0.04em",
            ),
        ),
        m.element(
            "p",
            m.link(
                record.get(COLUMN_TITLE),
                record.get(COLUMN_URL),
                style=m.styles(f"color:{TEXT}", "text-decoration:none"),
            ),
            style=m.styles(
                "margin:8px 0 0 0",
                "font-size:16px",
                "font-weight:bold",
                "line-height:1.6",
                f"color:{TEXT}",
            ),
        ),
        m.element(
            "p",
            m.escape(record.get(COLUMN_SUMMARY)),
            style=m.styles(
                "margin:10px 0 0 0",
                "font-size:13px",
                "line-height:1.9",
                f"color:{BODY_TEXT}",
            ),
        ),
    ]
    if insight := narrative.insight_for(record.get(COLUMN_URL)):
        parts.append(_insight_box(insight))
    parts.append(_source_line(record))

    card = m.block(
        "".join(parts),
        style=m.styles(
            f"background-color:{SURFACE}",
            f"border:1px solid {BORDER}",
            "border-radius:6px",
        ),
        cell_style="padding:18px 20px",
    )
    return m.row([m.cell(card, style="padding:14px 28px 0 28px")])


def _footer() -> str:
    """§9.2-5 フッタ注記。"""
    note = m.element(
        "p",
        m.escape(FOOTER_NOTE),
        style=m.styles(
            "margin:0", "font-size:11px", "line-height:1.9", f"color:{MUTED_TEXT}"
        ),
    )
    return m.row(
        [
            m.cell(
                note,
                style=m.styles(
                    "padding:26px 28px 30px 28px", f"border-top:1px solid {BORDER}"
                ),
            )
        ]
    )


def render_weekly_html(
    *,
    period: str,
    articles: Sequence[Mapping[str, Any]],
    config: IntelligenceConfig,
    narrative: WeeklyNarrative | None = None,
) -> str:
    """当週シートから週刊メルマガ HTML を組み立てる（**AI を呼ばない**）。

    Args:
        period: `2026-W31`（シート名）
        articles: 当週シートの行（列名 → 値。T-22 の `read_weekly()`）
        config: 実行時 config（固定参照済み）
        narrative: 生成テキスト。`None` なら空（生成テキスト無しで描画）

    Returns:
        HTML 文字列（UTF-8 で書き出す前提）

    Raises:
        WeeklyRenderError: period が週次表記でない／`point_of_week_required=true`
            なのに今週のポイントが空／生成物が §7.1 の制約に反する
    """
    markup, _ = _render(
        period=period, articles=articles, config=config, narrative=narrative
    )
    return markup


def _render(
    *,
    period: str,
    articles: Sequence[Mapping[str, Any]],
    config: IntelligenceConfig,
    narrative: WeeklyNarrative | None,
) -> tuple[str, WeeklySelection]:
    """組み立て本体。**選別を2度走らせない**ため書き出し側もこれを使う。"""
    parsed = _parse_weekly(period)
    narrative = narrative or WeeklyNarrative()
    weekly = config.tunable_thresholds.weekly
    industry = weekly.target_industry

    point_of_week = (narrative.point_of_week or "").strip()
    if weekly.point_of_week_required and not point_of_week:
        raise WeeklyRenderError(
            "今週のポイントが空です（`point_of_week_required=true`・仕様書 §9.2-2）。"
            "生成テキストは filter 側が作って `WeeklyNarrative` で渡してください"
        )

    selection = select_articles(articles, config)
    if selection.rendered == 0:
        logger.warning(
            "カードになる記事がありません（period=%s・採用 %d 件）",
            parsed.text,
            selection.adopted,
        )

    labels = category_labels(config)
    rows: list[str] = [_header(industry=industry, period=parsed.text)]
    if point_of_week:
        rows.append(_point_of_week(point_of_week))

    for heading, records in (
        (INDUSTRY_SECTION_FORMAT.format(industry=industry), selection.industry_topics),
        (COMMON_SECTION_HEADING, selection.common_topics),
    ):
        if not records:
            continue
        rows.append(_section_heading(heading))
        rows.extend(
            _card(record, labels=labels, narrative=narrative) for record in records
        )

    rows.append(_footer())

    badge = INDUSTRY_BADGE_FORMAT.format(industry=industry)
    inner = m.table(
        rows,
        style=m.styles(
            f"max-width:{CONTENT_MAX_WIDTH}",
            "margin:0 auto",
            f"background-color:{SURFACE}",
        ),
    )
    outer = m.table(
        [
            m.row(
                [
                    m.cell(
                        inner,
                        style=m.styles("padding:24px 12px"),
                        attrs={"align": "center"},
                    )
                ]
            )
        ],
        style=f"background-color:{PAGE_BACKGROUND}",
    )
    markup = m.document(
        title=DOCUMENT_TITLE_FORMAT.format(
            brand=BRAND_TITLE, badge=badge, period=parsed.text
        ),
        body=outer,
        background=PAGE_BACKGROUND,
    )

    logger.info(
        "weekly html rendered (period=%s, industry=%s, industry_topics=%d,"
        " common_topics=%d, adopted=%d, untitled=%d)",
        parsed.text,
        industry,
        len(selection.industry_topics),
        len(selection.common_topics),
        selection.adopted,
        selection.untitled,
    )
    return _mail_safe(markup, period=parsed.text), selection


def _parse_weekly(period: str) -> Period:
    try:
        parsed = parse_period(period)
    except PeriodError as exc:
        raise WeeklyRenderError(str(exc)) from exc
    if not parsed.is_weekly:
        raise WeeklyRenderError(f"週次 period が必要です: {period!r}")
    return parsed


def _mail_safe(markup: str, *, period: str) -> str:
    """§7.1 の禁止構文が無いことを確かめる（書き出す前に落とす）。"""
    try:
        return m.assert_mail_safe(markup)
    except m.MailHtmlError as exc:
        raise WeeklyRenderError(f"{period} の週刊HTML: {exc}") from exc


# --- 書き出し（正規名は T-02 が解決・設計判断B）-------------------------------


@dataclass(frozen=True, slots=True)
class RenderedHtml:
    """書き出した結果（T-26 の監査ログ・`GET /reports` 用）。

    Attributes:
        path: 正規名のパス（上書き済み）
        archived: 退避先。初回実行なら `None`
        markup: 書き出した HTML
        cards: カードにした記事数
    """

    path: Path
    archived: Path | None
    markup: str
    cards: int


class WeeklyRenderer:
    """週刊 HTML の組み立てと書き出し（`ArtifactStore` 経由）。

    renderer = WeeklyRenderer(ArtifactStore.from_settings())
    result = renderer.render(
        period="2026-W31",
        articles=ReportStore(store).read_weekly("2026-W31"),
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
        articles: Sequence[Mapping[str, Any]],
        config: IntelligenceConfig,
        narrative: WeeklyNarrative | None = None,
        revision: int,
        run_id: str,
    ) -> RenderedHtml:
        """組み立てて `weekly_ai_intelligence_newsletter_{industry}_{period}.html` へ。

        ⚠️ **退避が先**（設計判断B。T-22 の `_save()` と同じ順序）。上書き後に
        退避すると、退避されるのは新しい内容になる。

        Raises:
            WeeklyRenderError: 組み立てられない入力
            ArtifactStoreError: industry / period をファイル名へ埋め込めない
        """
        markup, selection = _render(
            period=period, articles=articles, config=config, narrative=narrative
        )
        industry = config.tunable_thresholds.weekly.target_industry
        path = self._store.weekly_html_path(industry, period)
        archived = self._store.archive(
            path, period=period, revision=revision, run_id=run_id
        )
        self._store.write_text(path, markup)

        logger.info("weekly html written (path=%s, archived=%s)", path, archived)
        return RenderedHtml(
            path=path, archived=archived, markup=markup, cards=selection.rendered
        )


__all__ = [
    "BRAND_TITLE",
    "COMMON_SECTION_HEADING",
    "FOOTER_NOTE",
    "INDUSTRY_SECTION_FORMAT",
    "NOT_ADOPTED",
    "POINT_OF_WEEK_HEADING",
    "REFERENCED_COLUMNS",
    "RenderedHtml",
    "WeeklyNarrative",
    "WeeklyRenderError",
    "WeeklyRenderer",
    "WeeklySelection",
    "category_labels",
    "industries_of",
    "is_adopted",
    "render_weekly_html",
    "select_articles",
    "total_score_of",
]
