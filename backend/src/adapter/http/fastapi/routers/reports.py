"""レポート一覧 API（T-27。設計書 §3.2・§3.3 ／ 仕様書 §6.2）。

`GET /reports/{period}` → `{period, type, html_urls, xlsx_url, summary}`。
**全ロール可**（§6.2）。実体の配信は `GET /files/{filename}`（`routers/files.py`）。

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

対象業界は `config.tunable_thresholds.weekly.target_industries` に書いてあるが、
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
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel

from adapter.http.fastapi.auth.dependencies import require_permission
from adapter.http.fastapi.routers.files import file_url
from adapter.storage.artifact_store import (
    WEEKLY_HTML_NAME,
    ArtifactStore,
    ArtifactStoreError,
)
from adapter.xlsx.report_writer import ReportStore
from config import Settings, get_settings
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


def _not_found(period: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error": "report_not_found",
            "message": f"{period} のレポートはまだありません。",
        },
    )


# --- エンドポイント -------------------------------------------------------------


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
    "ReportHtml",
    "ReportResponse",
    "ReportSummary",
    "SHEET_FRAGMENT",
    "router",
]
