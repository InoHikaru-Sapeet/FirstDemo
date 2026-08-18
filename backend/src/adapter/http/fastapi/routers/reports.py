"""レポート一覧 API（T-27。設計書 §3.2・§3.3 ／ 仕様書 §6.2）。

- `GET /reports` → `{reports: [{period, type, industries}]}`（一覧。T-36）
- `GET /reports/{period}` → `{period, type, html_urls, xlsx_url, summary}`
- `GET /reports/{period}/articles` → 閲覧ページ用の記事データ（T-36）

**全ロール可**（§6.2）。実体の配信は `GET /files/{filename}`（`routers/files.py`）。

---

⚠️ **`GET /reports/{period}/articles` は「メール版の中身」を JSON で返す口**
（2026-08-17 の T-36）。

Web の閲覧ページは記事ごとのトグル開閉で要約・示唆を出す。メール版 HTML（配信物）
は §7.1 で JS が禁止なので、**同じ内容を構造化して返す口を別に置いた**。

⚠️ **返す項目は「メール版 HTML に出ているもの」に限る。** この口は全ロールが
叩けるのに対し、config は admin 以外に存在も中身も返さない（§6.1）。採否・上限・
業界の振り分けは config を見て決めるが（`select_articles()`）、**返すのは
カテゴリラベル・色・タイトル・要約・示唆・出典**——どれも配信済み HTML に載って
いるものだけ。⚠️ **合計スコアとしきい値は返さない**（HTML に出ていない＝
これを返すと config の値を推定できる新しい経路になる）。

⚠️ **示唆は全件返す**（メール版はセクション先頭1件だけ＝T-48 Step 1）。生成
テキストの内容そのものは配信物の本文なので、閲覧できる相手を HTML より狭める
理由が無い。⚠️ **`narrative_{period}.json` 自体は配信しない**（`is_servable()`
の許可リストに入れていない＝生の成果物はダウンロードさせない）。

⚠️ **図解（T-49）も同じ理由で返す。** 週刊は**メール版に図解を描かない**ので
（1通の縦を伸ばすと T-48 Step 1 の圧縮が無意味になる）、図解が読めるのは
この口を通した Web の閲覧ページだけ。**描画は決定的**——サーバーが返すのは
`Diagram`（3タイプ固定の構造化データ）で、HTML の断片ではない。

---

⚠️ **一覧（`GET /reports`）は「置いてある HTML」から作る。**

`ArtifactStore.rendered_periods()` が実ファイルを数える。config も中間xlsx の
シートも見ない（前者は §6.1、後者は「読めるレポートの一覧」という意味に絞る
ため）。件数サマリを一覧に載せないのは、period ごとに xlsx を開く必要があり
一覧の応答が期間数に比例して重くなるため（件数は詳細で返す）。

---

⚠️ **`html_url`（単数）→ `html_urls`（複数）に変えた。**

設計書 §3.3 は `"html_url": "/files/weekly_..._不動産_2026-W31.html"` と単数で
書いているが、**週刊は対象業界ごとに1通出る**（T-46 Step 4 で複数化済み）。
単数のままだと、業界が2つ以上あるときに「どれか1通」を返すことになり、
残りへ到達する手段が API から消える。→ **§3.3 の改訂が必要（T-38 に記録済み）**。

月刊は1通なので `html_urls` の要素は常に1件（`industry` は `null`）。
**形を2つに割らない**のは、フロントが種別で分岐せずに一覧を描けるようにするため。

---

⚠️ **一覧は config ではなく「置いてあるファイル」から作る。**

対象業界は `config.tunable_thresholds.target_industries` に書いてあるが、
このエンドポイントは**全ロールが叩ける**のに対し config は admin 以外に
**存在も中身も返さない**（仕様書 §2・§6.1）。config を読んで一覧を組み立てると、
設定値が非 admin へ漏れる経路になる。`ArtifactStore.weekly_html_paths()` が
実ファイルを数えるので、過去の period を引いたときに**その時点で出したもの**が
並ぶという利点もある。

⚠️ **`summary` は中間xlsx から数える**（`adopted` = その期間のシートの行数、
`excluded` = 除外ログのうちその期間の行数）。ジョブ記録からではないのは、
`GET /reports` が「今ある成果物」を答える口だから（実行の話は
`GET /run/{job_id}`）。
"""

import logging
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.config_repository import ConfigNotFoundError, ConfigRepository
from adapter.html.category_colors import color_of
from adapter.html.mail_html import safe_url
from adapter.html.weekly_renderer import (
    COLUMN_CATEGORY,
    COLUMN_SOURCE,
    COLUMN_SUMMARY,
    COLUMN_TITLE,
    COLUMN_URL,
    COMMON_SECTION_HEADING,
    INDUSTRY_SECTION_FORMAT,
    WeeklyNarrative,
    category_labels,
    select_articles,
)
from adapter.http.fastapi.auth.dependencies import get_db_session, require_permission
from adapter.http.fastapi.routers.files import file_url
from adapter.storage.artifact_store import (
    WEEKLY_HTML_NAME,
    ArtifactStore,
    ArtifactStoreError,
)
from adapter.xlsx.report_writer import ReportStore
from application.usecases.narrative import to_weekly_narrative
from config import Settings, get_settings
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.diagram import Diagram
from enterprise.entities.narrative import WeeklyNarrativeDocument, parse_narrative
from enterprise.entities.period import Period, PeriodError, parse_period
from enterprise.entities.principal import Principal
from enterprise.entities.run_job import RunType, run_type_of

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])

# `#sheet=` は中間xlsx の**どのシートか**を指す目印（設計書 §3.3 の例と同じ）。
# フラグメントなのでサーバへは送られない（人が見て分かるための表記）。
SHEET_FRAGMENT = "#sheet="


# --- I/O（設計書 §3.3）--------------------------------------------------------


class ReportHtml(BaseModel):
    """生成 HTML 1通。

    Attributes:
        industry: 週刊の対象業界。**月刊は `None`**（業界別ではない）
        url: `GET /files/{filename}` の URL
    """

    industry: str | None
    url: str


class ReportSummary(BaseModel):
    """件数サマリ（設計書 §3.3 の `summary`）。"""

    adopted: int
    excluded: int


class ReportResponse(BaseModel):
    """`GET /reports/{period}` → 200。"""

    period: str
    type: RunType
    html_urls: list[ReportHtml]
    xlsx_url: str
    summary: ReportSummary


class ReportListEntry(BaseModel):
    """一覧の1行（T-36）。

    Attributes:
        period: `2026-W31` / `2026-07`
        type: period の表記から決まる種別
        industries: 週刊の業界（出ている HTML のぶんだけ）。**月刊は空**
    """

    period: str
    type: RunType
    industries: list[str]


class ReportListResponse(BaseModel):
    """`GET /reports` → 200（**新しい号が先**）。"""

    reports: list[ReportListEntry]


class ArticleCard(BaseModel):
    """閲覧ページの記事1件（T-36）。

    ⚠️ **メール版 HTML に出ている項目だけ**（モジュール docstring）。合計スコアと
    しきい値は入れない。

    Attributes:
        category_id: `information_categories[].id`（色を引く鍵）
        category_label: config のラベル
        category_color: §7.2 の確定色（`#rrggbb`）
        title: 列3「タイトル」
        url: 列22「URL」。`http`/`https` でなければ `None`（リンクにしない）
        summary: 列4「一言要約」。⚠️ **切り詰めない**（メール版は全角60字で
            切るが、Web はトグルで開くので全文を出せる）
        insight: 示唆ボックスの1段落。**メール版が出していない分も返す**
        diagram: 図解（T-49）。⚠️ **週刊のメール版は図解を描かない**ので、
            これは「Web だけに出る」項目。無い記事は `None`（それが正常）
        source: 列21「ソース」
    """

    category_id: str
    category_label: str
    category_color: str
    title: str
    url: str | None
    summary: str
    insight: str | None
    diagram: Diagram | None
    source: str


class ArticleSection(BaseModel):
    """閲覧ページのセクション（業界関連／業界共通）。

    Attributes:
        heading: §9.2-3・§9.2-4 の見出し（`〈業界〉関連トピック` ほか）
        articles: そのセクションのカード（合計スコア降順・上限適用後）
    """

    heading: str
    articles: list[ArticleCard]


class ArticlesResponse(BaseModel):
    """`GET /reports/{period}/articles` → 200（**週刊のみ**）。

    Attributes:
        period: 対象週
        industry: どの業界版か
        industries: その週に HTML が出ている業界（閲覧ページの切り替え用）
        point_of_week: 今週のポイント（§9.2-2）。無ければ `None`
        sections: 業界関連トピック → 業界共通トピックの順
    """

    period: str
    industry: str
    industries: list[str]
    point_of_week: str | None
    sections: list[ArticleSection]


def _not_found(period: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error": "report_not_found",
            "message": f"{period} のレポートはまだありません。",
        },
    )


# --- エンドポイント -------------------------------------------------------------


@router.get("")
async def list_reports(
    _caller: Annotated[Principal, Depends(require_permission)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReportListResponse:
    """読めるレポートの一覧を返す（**全ロール可**。T-36）。

    - **200** `{reports: [{period, type, industries}]}`（**新しい号が先**）

    ⚠️ **件数サマリは入れない**（period ごとに中間xlsx を開くことになる）。
    件数が要るときは `GET /reports/{period}` を引く。
    """
    store = ArtifactStore.from_settings(settings)
    entries = [
        ReportListEntry(
            period=period,
            type=run_type_of(period),
            industries=_industries_in(store, period),
        )
        for period in store.rendered_periods()
    ]
    logger.info("reports listed (count=%d)", len(entries))
    return ReportListResponse(reports=entries)


@router.get("/{period}/articles")
async def get_report_articles(
    period: Annotated[str, Path(description="2026-W31（週次のみ）")],
    _caller: Annotated[Principal, Depends(require_permission)],
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    industry: Annotated[
        str | None, Query(description="どの業界版か（既定はその週の先頭）")
    ] = None,
) -> ArticlesResponse:
    """閲覧ページ用に、その号の記事を構造化して返す（**全ロール可**。T-36）。

    ⚠️ **週刊だけ**（月刊は章立ての読み物なので HTML をそのまま見せる）。

    - **200** `{period, industry, industries, point_of_week, sections}`
    - **404** その週の HTML がまだ無い／指定した業界版が出ていない
    - **422** period が週次表記でない・実在しない週

    ⚠️ **採否・並び順・上限はメール版と同じ判定を通す**（`select_articles()`）。
    Web 用に別の選び方をすると「メールに載っていない記事が Web にある」状態に
    なり、どちらが号の内容なのか決まらなくなる。
    """
    parsed = _parse(period)
    if not parsed.is_weekly:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "articles_not_available",
                "message": (
                    "記事ごとの表示は週刊だけです。"
                    "月刊は生成 HTML をそのままご覧ください。"
                ),
            },
        )

    store = ArtifactStore.from_settings(settings)
    industries = _industries_in(store, parsed.text)
    if not industries:
        raise _not_found(parsed.text)

    target = industry if industry is not None else industries[0]
    if target not in industries:
        # ⚠️ 出していない業界版は 404（存在しない号を作って見せない）。
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "industry_not_published",
                "message": f"{parsed.text} に {target} 版はありません。",
            },
        )

    # ⚠️ **config を読むのは選別のため**だけ（返す項目には出さない。
    # モジュール docstring）。
    config = _load_config_for_selection(db, settings, parsed)
    narrative = _weekly_narrative(store, parsed, target)
    selection = select_articles(
        ReportStore(store).read_weekly(parsed.text), config, industry=target
    )
    labels = category_labels(config)

    sections = [
        ArticleSection(
            heading=heading,
            articles=[
                _card(record, labels=labels, narrative=narrative) for record in records
            ],
        )
        for heading, records in (
            (
                INDUSTRY_SECTION_FORMAT.format(industry=target),
                selection.industry_topics,
            ),
            (COMMON_SECTION_HEADING, selection.common_topics),
        )
        if records
    ]

    logger.info(
        "report articles served (period=%s, industry=%s, sections=%d, articles=%d)",
        parsed.text,
        target,
        len(sections),
        sum(len(section.articles) for section in sections),
    )
    return ArticlesResponse(
        period=parsed.text,
        industry=target,
        industries=industries,
        point_of_week=narrative.point_of_week,
        sections=sections,
    )


def _load_config_for_selection(
    db: AsyncSession, settings: Settings, period: Period
) -> IntelligenceConfig:
    """選別（`select_articles()`）に使う config を読む。

    ⚠️ **`config.json` が無いことを呼び手へ伝えない。** この口は全ロールが叩ける
    ので、`config_not_found` を返すと**非 admin が config の存在を推定できる**
    （仕様書 §6.1「admin 以外には存在も中身も返さない」）。config が無ければ
    記事の採否も決められないので、「そのレポートは読めない」＝レポートの 404 と
    同じ本文へ畳む。

    Raises:
        HTTPException: 404（config が無い）
    """
    try:
        return ConfigRepository.from_settings(db, settings).load()
    except ConfigNotFoundError as exc:
        logger.warning("config.json が無いので記事一覧を返せません: %s", exc)
        raise _not_found(period.text) from exc


def _industries_in(store: ArtifactStore, period: str) -> list[str]:
    """その period に HTML が出ている業界（月次は空）。"""
    if not run_type_of(period) == RunType.WEEKLY:
        return []
    return [
        industry
        for path in store.weekly_html_paths(period)
        if (industry := _industry_of(path.name)) is not None
    ]


def _weekly_narrative(
    store: ArtifactStore, period: Period, industry: str
) -> WeeklyNarrative:
    """`narrative_{period}.json` のその業界ぶん。**無ければ空**。

    ⚠️ **落とさない**（render は無いと落とすが、こちらは閲覧）。生成テキストが
    無い号でも記事の一覧は読めるほうがよく、示唆の有無はカードごとに分かる。
    """
    path = store.narrative_path(period.text)
    if not store.exists(path):
        logger.warning("生成テキストがありません（示唆なしで返します）: %s", path)
        return WeeklyNarrative()
    document = parse_narrative(store.read_text(path), period=period)
    if not isinstance(document, WeeklyNarrativeDocument):  # pragma: no cover
        return WeeklyNarrative()
    if industry not in document.industries:
        logger.warning(
            "生成テキストに %s 版がありません（示唆なしで返します）", industry
        )
        return WeeklyNarrative()
    return to_weekly_narrative(document, industry)


def _card(
    record: Mapping[str, Any],
    *,
    labels: Mapping[str, str],
    narrative: WeeklyNarrative,
) -> ArticleCard:
    """当週シートの1行を閲覧ページのカードへ。"""
    category_id = str(record.get(COLUMN_CATEGORY) or "")
    url = record.get(COLUMN_URL)
    return ArticleCard(
        category_id=category_id,
        category_label=labels.get(category_id, category_id),
        category_color=color_of(category_id),
        title=str(record.get(COLUMN_TITLE) or "").strip(),
        # ⚠️ **`http`/`https` 以外はリンクにしない**（メール版 `link()` と同じ
        # 判定。閲覧ページでも `javascript:` を `href` へ置く経路を作らない）。
        url=safe_url(url),
        summary=str(record.get(COLUMN_SUMMARY) or "").strip(),
        insight=narrative.insight_for(url),
        # ⚠️ **図解はメール版に出ていない**（週刊のメール版は描かない＝T-49）。
        # 示唆の間引き（T-48 Step 1）と同じ扱いで、生成テキストの中身そのものは
        # 配信物の本文なので Web で読める相手を HTML より狭める理由が無い。
        diagram=narrative.diagram_for(url),
        source=str(record.get(COLUMN_SOURCE) or "").strip(),
    )


@router.get("/{period}")
async def get_report(
    period: Annotated[str, Path(description="2026-W31（週次）/ 2026-07（月次）")],
    _caller: Annotated[Principal, Depends(require_permission)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReportResponse:
    """その期間の生成物と件数を返す（**全ロール可**）。

    - **200** `{period, type, html_urls, xlsx_url, summary}`
    - **404** その期間の成果物がまだ無い
    - **422** period の表記が不正（`YYYY-Www` / `YYYY-MM` でない・実在しない期間）

    ⚠️ **種別は period の表記から決まる**（`2026-W31` → weekly）。
    クエリで指定させないのは、取り違えた組み合わせを受け付けないため。
    """
    parsed = _parse(period)
    store = ArtifactStore.from_settings(settings)
    reports = ReportStore(store)

    if parsed.is_weekly:
        rows = reports.read_weekly(parsed.text)
        htmls = [
            ReportHtml(industry=_industry_of(path.name), url=file_url(path.name))
            for path in store.weekly_html_paths(parsed.text)
        ]
        xlsx_name = store.weekly_report_path().name
    else:
        rows = reports.read_monthly(parsed.text)
        monthly_html = store.monthly_html_path(parsed.text)
        htmls = (
            [ReportHtml(industry=None, url=file_url(monthly_html.name))]
            if monthly_html.is_file()
            else []
        )
        xlsx_name = store.monthly_cases_path().name

    if not rows and not htmls:
        raise _not_found(parsed.text)

    return ReportResponse(
        period=parsed.text,
        type=run_type_of(parsed.text),
        html_urls=htmls,
        xlsx_url=f"{file_url(xlsx_name)}{SHEET_FRAGMENT}{parsed.text}",
        summary=ReportSummary(
            adopted=len(rows),
            excluded=len(reports.read_exclusions(parsed.text)),
        ),
    )


def _parse(period: str) -> Period:
    """`{period}` の形式検証（T-27 の完了条件）。

    ⚠️ **表記だけでなく実在も見る**（`2026-W53` は表記としては通るが、53週を
    持たない年がある）。不正な period をそのまま `ArtifactStore` へ渡すと
    `artifact_root` の外を指しうるので、ここで落とすのが関門になる。
    """
    try:
        return parse_period(period)
    except PeriodError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_period", "message": str(exc)},
        ) from exc
    except ArtifactStoreError as exc:  # pragma: no cover - parse_period が先に落ちる
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_period", "message": str(exc)},
        ) from exc


def _industry_of(filename: str) -> str | None:
    """週刊 HTML の正規名から業界名を取り出す（一覧の表示用）。

    ⚠️ 解析も生成も `WEEKLY_HTML_NAME`（T-02）の1つの書式から導かれる。
    ここで正規表現を書き直さないこと。
    """
    fields = WEEKLY_HTML_NAME.parse(filename)
    return fields["industry"] if fields else None


__all__ = [
    "ArticleCard",
    "ArticleSection",
    "ArticlesResponse",
    "ReportHtml",
    "ReportListEntry",
    "ReportListResponse",
    "ReportResponse",
    "ReportSummary",
    "SHEET_FRAGMENT",
    "router",
]
