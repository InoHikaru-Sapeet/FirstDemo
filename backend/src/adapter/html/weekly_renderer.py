"""週刊メルマガ レンダラ（設計書 §7.3 ／ 仕様書 §9 ／ T-24）。

中間xlsx の当週シート（22列。T-22 の `ReportStore.read_weekly()`）と実行時 config
から、`weekly_ai_intelligence_newsletter_{industry}_{period}.html` を組み立てる。

**AI を呼ばない**（TASKS.md §1.1「render = 決定的 Python テンプレート」）。
同じシートと同じ config からは常に同じ HTML が出る（§14 再現性）。

---

**§7.3 のマッピング（この モジュールが実装する対応表）**

| HTML | 中間xlsx / config |
|---|---|
| ヘッダ「〈業界〉版」 | 描画する業界（`config.target_industries` のどれか） |
| ヘッダ「対象週」 | シート名（`YYYY-Www`） |
| 今週のポイント | （生成テキスト → `WeeklyNarrative`） |
| 業界関連トピック | 列19「業界」に**その業界**を含む記事 |
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

---

**⚠️ 見た目の圧縮（2026-08-17 の T-48 Step 1）**

1通に 20 件近いカードが並ぶと通読されない、という PM 要件で**カードを圧縮した**。
§9.2-4 に対する差分は3つで、いずれも**表示だけの変更**（採否・並び順・生成
テキストの中身は一切動かない）。→ **T-38 の改訂対象として記録済み**。

1. **カテゴリラベルを色つきバッジへ**（§9.2-4 は「カテゴリラベル（小・色分け）」）。
   色は §7.2 の確定マップそのままで、**文字色をその色にする代わりに背景へ回した**
2. **本文要約を全角 60 字で切る**（`one_line_summary()`。末尾 `…`）。§9.2-4 は
   「本文要約（`一言要約` を流用可）」で全文前提
3. **示唆ボックスは各セクションの先頭カード1件だけフル表示**し、残りは出さない。
   §9.2-4 は各カードの要素として挙げている

⚠️ **3 は `narrative` を絞らない。** 生成テキスト（`narrative_{period}.json`）は
filter が作ったまま全件持っており、**この層が描画時に間引いているだけ**（Web の
閲覧ページ＝T-36 は全件をトグルで開ける）。

---

**⚠️ 見出しとリンクの分離（2026-08-18 の T-50）**

圧縮したカードで見出しが埋もれる、という PM 要件で**見出しを大きくし、リンクを
出典行へ移した**。§9.2-4 に対する差分は2つで、いずれも**表示だけの変更**。
→ **T-38 の改訂対象**。

1. **見出しは `<a>` ではなくプレーンな段落**（`CARD_TITLE_FONT_SIZE`＝17px）。
   §9.2-4 は「タイトル（`<a>` リンク・黒文字）」
2. **記事へのリンクは出典行**（`出典：〈ソース〉（記事を読む）`）。§9.2-4 の
   出典行は「出典：媒体 ／ 記事を読む」

⚠️ **URL 列は今までどおり全カードで使う**（リンクの置き場が変わっただけ）。
リンクにできない URL では括弧ごと出さない（`_source_line()`）。

---

**⚠️ 週刊のメール版に図解は出さない**（2026-08-18 の T-49）

filter 段の AI は**週刊でも記事ごとに図解を申告する**（`WeeklyNarrative.
diagrams`）が、**この層は描かない**。1通あたりの縦を伸ばすと T-48 Step 1 の
圧縮（1行要約・示唆はセクション先頭1件）が無意味になるため。図解を読めるのは
**Web の閲覧ページ（T-36）のトグル展開内だけ**——示唆の間引きと同じ扱いで、
`narrative_{period}.json` には全件残る。
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
from enterprise.entities.diagram import Diagram
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
SOURCE_LINE_FORMAT = "出典：{source}"
READ_MORE_LABEL = "記事を読む"
SOURCE_LINK_WRAPPER = "（{link}）"
"""出典行に続く記事リンクの囲み（T-50）。**リンクにできない URL では出さない。**"""
DOCUMENT_TITLE_FORMAT = "{brand}｜{badge}（{period}）"

# --- 圧縮の確定値（T-48 Step 1）-----------------------------------------------

SUMMARY_MAX_FULLWIDTH_CHARS = 60
"""カードの1行要約の上限（**全角字**）。半角は0.5字ぶんとして数える。"""

ELLIPSIS = "…"
"""切り詰めた要約の末尾（1文字。三点リーダ）。"""

INSIGHTS_PER_SECTION = 1
"""示唆ボックスをフル表示するカード数（**各セクションの先頭から**）。"""

# --- 見出しの体裁（T-50）------------------------------------------------------

CARD_TITLE_FONT_SIZE = "17px"
"""カード見出しの字の大きさ（T-48 Step 1 の 15px から一回り大きく）。"""

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

# カテゴリ色バッジの文字色（背景に §7.2 の確定色を敷くので白抜き。T-48 Step 1）。
BADGE_TEXT = "#ffffff"


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
        diagrams: 記事URL → 図解（T-49）。**鍵は示唆と同じ**。
            ⚠️ **メール版は描かない**（下の注記）
    """

    point_of_week: str | None = None
    insights: Mapping[str, str] = field(default_factory=dict)
    diagrams: Mapping[str, Diagram] = field(default_factory=dict)

    def insight_for(self, url: object) -> str | None:
        """その記事の示唆。無ければ `None`（ボックスごと出さない）。"""
        text = self.insights.get(self._key(url))
        return text.strip() if text and text.strip() else None

    def diagram_for(self, url: object) -> Diagram | None:
        """その記事の図解（T-49）。無ければ `None`。

        ⚠️ **このモジュールは呼ばない。** 週刊のメール版に図解は出さない
        （1通あたりの縦を伸ばすと T-48 Step 1 の圧縮が無意味になる）。読むのは
        Web の閲覧ページ（T-36 の `GET /reports/{period}/articles`）。
        """
        return self.diagrams.get(self._key(url))

    @staticmethod
    def _key(url: object) -> str:
        return "" if url is None else str(url).strip()


# --- 描画する業界（§9.2-1・§9.2-3）-------------------------------------------


def resolve_industry(config: IntelligenceConfig, industry: str | None = None) -> str:
    """この HTML が「どの業界版」かを1つ決める（T-46 Step 3）。

    週刊は**業界ごとに1通**（`weekly_..._{industry}_{period}.html`）なので、
    1回の描画で扱う業界は必ず1つ。config が複数業界を持つときに**どれを描くか
    決めるのは呼び出し側**（業界数ぶん回すのは run_pipeline / T-26 の仕事）。

    Args:
        config: 実行時 config（固定参照済み）
        industry: 描画する業界。`None` なら config の**先頭**

    Returns:
        描画する業界名

    Raises:
        WeeklyRenderError: config の対象業界に無い業界を指定された場合
            （誰も選んでいない業界版を出さない）
    """
    industries = config.tunable_thresholds.industries
    if industry is None:
        if len(industries) > 1:
            # ⚠️ 黙って先頭だけを描かない（残りの業界版が出ていないことに
            # 気づけるように）。業界数ぶん回すのは呼び出し側の責務。
            logger.warning(
                "対象業界が %d 件ありますが、業界の指定が無いので先頭（%s）だけを"
                "描画します: %s",
                len(industries),
                industries[0],
                " / ".join(industries),
            )
        return industries[0]

    if industry not in industries:
        raise WeeklyRenderError(
            f"config の対象業界に無い業界です: {industry!r}"
            f"（対象業界: {' / '.join(industries)}）"
        )
    return industry


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
    articles: Sequence[Mapping[str, Any]],
    config: IntelligenceConfig,
    *,
    industry: str | None = None,
) -> WeeklySelection:
    """採用条件・業界振り分け・上限・並び順を決める（§9.3・§7.3）。

    Args:
        articles: 当週シートの行（列名 → 値）
        config: 実行時 config（固定参照済み）
        industry: どの業界版として振り分けるか。`None` なら `resolve_industry()`
            が config から決める

    Returns:
        業界関連／業界共通に振り分けた結果
    """
    weekly = config.tunable_thresholds.weekly
    target = resolve_industry(config, industry)

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
        if target in industries_of(record):
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


def one_line_summary(text: object, *, limit: int = SUMMARY_MAX_FULLWIDTH_CHARS) -> str:
    """一言要約を「1行」へ詰める（T-48 Step 1）。

    切り方（見た目の幅で数える・`…` ぶんは引かない）は T-23 の部品
    `mail_html.truncate_fullwidth()` が持つ。**月刊の引用ボックス（T-48 Step 2）
    と同じ実装を使う**ため、この層は既定の上限を与えるだけ。

    Args:
        text: 中間xlsx 列4「一言要約」の値
        limit: 上限（**全角字**。半角は0.5字ぶん）

    Returns:
        改行を空白へ潰し、上限を超えたら末尾を `…` にした1行。空なら空文字
    """
    return m.truncate_fullwidth(text, limit=limit, ellipsis=ELLIPSIS)


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
    """出典行「出典：〈ソース〉（記事を読む）」（T-50）。

    ⚠️ **記事へのリンクはこの行が持つ**（見出しはリンクにしない）。§9.2-4 の
    確定文言は「出典：媒体 ／ 記事を読む」だが、見出しから下線が消えた以上、
    リンクだと分かる形は括弧で括ったこの1箇所だけになる。→ **T-38 の改訂対象**。

    ⚠️ **リンクにできない URL では括弧ごと出さない。** `m.link()` は使えない URL を
    `<span>` にして返す（記事は落とさない）が、それを括弧に入れると「（記事を読む）」
    と書いてあるのに飛べない行になる。出典だけの行にする。
    """
    source = str(record.get(COLUMN_SOURCE) or "").strip()
    text = m.escape(SOURCE_LINE_FORMAT.format(source=source))
    if m.safe_url(record.get(COLUMN_URL)) is not None:
        text += SOURCE_LINK_WRAPPER.format(
            link=m.link(
                READ_MORE_LABEL,
                record.get(COLUMN_URL),
                style=m.styles(f"color:{ACCENT}", "text-decoration:none"),
            )
        )
    return m.element(
        "p",
        text,
        style=m.styles("margin:14px 0 0 0", "font-size:11px", f"color:{MUTED_TEXT}"),
    )


def _category_badge(label: str, *, color: str) -> str:
    """カテゴリ色バッジ（T-48 Step 1）。

    ⚠️ **色は §7.2 の確定マップそのまま**（`color_of()`）。文字色ではなく背景に
    敷いて白抜きにしただけで、色の値は動かしていない。

    バッジは `<div>` ではなく幅なしの1セル table（`block(width=None)`）で作る。
    inline 要素の `padding` はメールクライアントによって効かないため。
    """
    return m.block(
        m.element(
            "p",
            m.escape(label),
            style=m.styles(
                "margin:0",
                "font-size:10px",
                "font-weight:bold",
                f"color:{BADGE_TEXT}",
                "letter-spacing:0.04em",
                "line-height:1.4",
            ),
        ),
        style=m.styles(f"background-color:{color}", "border-radius:3px"),
        cell_style="padding:3px 8px",
        width=None,
    )


def _card(
    record: Mapping[str, Any],
    *,
    labels: Mapping[str, str],
    narrative: WeeklyNarrative,
    show_insight: bool,
) -> str:
    """記事1件のコンパクトカード（T-48 Step 1）。

    構成は **カテゴリ色バッジ → 見出し（プレーン）→ 1行要約 → ［示唆ボックス］→
    出典行（リンク）**。示唆ボックスは `show_insight=True` のカードだけに出す
    （各セクション先頭の `INSIGHTS_PER_SECTION` 件。モジュール docstring）。

    ⚠️ **見出しは `<a>` にしない**（T-50）。リンクは出典行だけが持つ。
    """
    category_id = record.get(COLUMN_CATEGORY)
    key = str(category_id) if category_id else ""
    parts = [
        _category_badge(labels.get(key, key), color=color_of(key)),
        m.element(
            "p",
            m.escape(str(record.get(COLUMN_TITLE) or "").strip()),
            style=m.styles(
                "margin:8px 0 0 0",
                f"font-size:{CARD_TITLE_FONT_SIZE}",
                "font-weight:bold",
                "line-height:1.5",
                f"color:{TEXT}",
            ),
        ),
        m.element(
            "p",
            m.escape(one_line_summary(record.get(COLUMN_SUMMARY))),
            style=m.styles(
                "margin:6px 0 0 0",
                "font-size:12px",
                "line-height:1.75",
                f"color:{BODY_TEXT}",
            ),
        ),
    ]
    if show_insight and (insight := narrative.insight_for(record.get(COLUMN_URL))):
        parts.append(_insight_box(insight))
    parts.append(_source_line(record))

    card = m.block(
        "".join(parts),
        style=m.styles(
            f"background-color:{SURFACE}",
            f"border:1px solid {BORDER}",
            "border-radius:6px",
        ),
        cell_style="padding:14px 16px",
    )
    return m.row([m.cell(card, style="padding:10px 28px 0 28px")])


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
    industry: str | None = None,
) -> str:
    """当週シートから週刊メルマガ HTML を組み立てる（**AI を呼ばない**）。

    Args:
        period: `2026-W31`（シート名）
        articles: 当週シートの行（列名 → 値。T-22 の `read_weekly()`）
        config: 実行時 config（固定参照済み）
        narrative: 生成テキスト。`None` なら空（生成テキスト無しで描画）
        industry: **どの業界版か**（`None` なら config の先頭。`resolve_industry()`）

    Returns:
        HTML 文字列（UTF-8 で書き出す前提）

    Raises:
        WeeklyRenderError: period が週次表記でない／`point_of_week_required=true`
            なのに今週のポイントが空／生成物が §7.1 の制約に反する
    """
    markup, _ = _render(
        period=period,
        articles=articles,
        config=config,
        narrative=narrative,
        industry=industry,
    )
    return markup


def _render(
    *,
    period: str,
    articles: Sequence[Mapping[str, Any]],
    config: IntelligenceConfig,
    narrative: WeeklyNarrative | None,
    industry: str | None,
) -> tuple[str, WeeklySelection]:
    """組み立て本体。**選別を2度走らせない**ため書き出し側もこれを使う。"""
    parsed = _parse_weekly(period)
    narrative = narrative or WeeklyNarrative()
    weekly = config.tunable_thresholds.weekly
    industry = resolve_industry(config, industry)

    point_of_week = (narrative.point_of_week or "").strip()
    if weekly.point_of_week_required and not point_of_week:
        raise WeeklyRenderError(
            "今週のポイントが空です（`point_of_week_required=true`・仕様書 §9.2-2）。"
            "生成テキストは filter 側が作って `WeeklyNarrative` で渡してください"
        )

    selection = select_articles(articles, config, industry=industry)
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

    shown_insights = 0
    held_insights = 0
    for heading, records in (
        (INDUSTRY_SECTION_FORMAT.format(industry=industry), selection.industry_topics),
        (COMMON_SECTION_HEADING, selection.common_topics),
    ):
        if not records:
            continue
        rows.append(_section_heading(heading))
        for index, record in enumerate(records):
            # 示唆ボックスは**セクション先頭の1件だけ**（T-48 Step 1）。
            show_insight = index < INSIGHTS_PER_SECTION
            if narrative.insight_for(record.get(COLUMN_URL)):
                if show_insight:
                    shown_insights += 1
                else:
                    held_insights += 1
            rows.append(
                _card(
                    record,
                    labels=labels,
                    narrative=narrative,
                    show_insight=show_insight,
                )
            )

    rows.append(m.spacer_row("26px"))
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
        " common_topics=%d, adopted=%d, untitled=%d, insights_shown=%d,"
        " insights_held=%d)",
        parsed.text,
        industry,
        len(selection.industry_topics),
        len(selection.common_topics),
        selection.adopted,
        selection.untitled,
        shown_insights,
        # ⚠️ **生成されているのに出していない示唆の件数**（T-48 Step 1）。
        # 黙って間引くと「示唆が生成されていない」と読まれるので残す。
        held_insights,
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
        industry="不動産",          # ← 対象業界が複数なら業界ごとに1回ずつ
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
        industry: str | None = None,
        revision: int,
        run_id: str,
    ) -> RenderedHtml:
        """組み立てて `weekly_ai_intelligence_newsletter_{industry}_{period}.html` へ。

        ⚠️ **1回の呼び出しで書くのは1業界ぶん**（正規名に業界が入る）。対象業界が
        複数ある config では**業界ごとに呼ぶ**（回すのは呼び出し側＝run_pipeline /
        T-26。`resolve_industry()`）。

        ⚠️ **退避が先**（設計判断B。T-22 の `_save()` と同じ順序）。上書き後に
        退避すると、退避されるのは新しい内容になる。

        Raises:
            WeeklyRenderError: 組み立てられない入力／config に無い業界の指定
            ArtifactStoreError: industry / period をファイル名へ埋め込めない
        """
        industry = resolve_industry(config, industry)
        markup, selection = _render(
            period=period,
            articles=articles,
            config=config,
            narrative=narrative,
            industry=industry,
        )
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
    "BADGE_TEXT",
    "BRAND_TITLE",
    "CARD_TITLE_FONT_SIZE",
    "COMMON_SECTION_HEADING",
    "ELLIPSIS",
    "FOOTER_NOTE",
    "INDUSTRY_SECTION_FORMAT",
    "INSIGHTS_PER_SECTION",
    "NOT_ADOPTED",
    "POINT_OF_WEEK_HEADING",
    "READ_MORE_LABEL",
    "REFERENCED_COLUMNS",
    "SOURCE_LINE_FORMAT",
    "SOURCE_LINK_WRAPPER",
    "SUMMARY_MAX_FULLWIDTH_CHARS",
    "RenderedHtml",
    "WeeklyNarrative",
    "WeeklyRenderError",
    "WeeklyRenderer",
    "WeeklySelection",
    "category_labels",
    "industries_of",
    "one_line_summary",
    "resolve_industry",
    "is_adopted",
    "render_weekly_html",
    "select_articles",
    "total_score_of",
]
