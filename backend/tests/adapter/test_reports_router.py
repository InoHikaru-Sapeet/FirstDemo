"""レポート一覧と生成物配信（T-27。設計書 §3.2・§3.3 ／ 仕様書 §6.2）。

`GET /reports/{period}` と `GET /files/{filename}`。重点:

- §3.3 の形（`html_urls` / `xlsx_url` / `summary`）と、**週刊の複数業界対応**
  （T-46 で複数化済み。§3.3 の単数形との差分は T-38 に記録）
- **`config.json` と `scratch/` が配信経路から見えないこと**（許可リスト方式）
- **パストラバーサル**（`../` / 絶対パス / エンコード済みの区切り）

認可（どのロールが叩けるか）は `test_rbac.py` の網羅テストが担当する。
ここでは admin でログインして**中身**を見る。
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.database.base import Base
from adapter.database.models.user import User
from adapter.http.fastapi.auth.dependencies import get_db_session, get_session_factory
from adapter.http.fastapi.main import app
from adapter.storage.artifact_store import ArtifactStore
from adapter.xlsx.report_writer import ReportStore
from config import get_settings
from enterprise.entities.principal import Role
from enterprise.services.password import hash_password

PASSWORD = "correct horse battery staple"
WEEKLY_PERIOD = "2026-W31"
MONTHLY_PERIOD = "2026-07"
NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)

# `不動産` をパーセントエンコードしたもの（URL は一意に読める形で返す）。
INDUSTRY_ENCODED = "%E4%B8%8D%E5%8B%95%E7%94%A3"


@dataclass
class Harness:
    client: TestClient
    store: ArtifactStore
    reports: ReportStore


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    root = tmp_path / "artifacts"
    root.mkdir(parents=True)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("ARTIFACT_ROOT", str(root))
    get_settings.cache_clear()

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'reports.db'}", poolclass=NullPool
    )

    async def create_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_session_factory] = lambda: maker

    store = ArtifactStore(root=root)
    with TestClient(app) as client:
        client._maker = maker  # type: ignore[attr-defined]
        _login(client, maker)
        yield Harness(client=client, store=store, reports=ReportStore(store))

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _login(client: TestClient, maker: Any) -> None:
    async def insert() -> None:
        async with maker() as session:
            session.add(
                User(
                    user_id="usr_admin",
                    email="admin@sapeet.com",
                    display_name="テスト 花子",
                    password_hash=hash_password(PASSWORD),
                    role=Role.ADMIN,
                    is_active=True,
                    created_at=NOW,
                    updated_at=NOW,
                    password_updated_at=NOW,
                    failed_login_attempts=0,
                    locked_until=None,
                )
            )
            await session.commit()

    asyncio.run(insert())
    response = client.post(
        "/auth/login", json={"email": "admin@sapeet.com", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


# --- 成果物を置く補助 ---------------------------------------------------------


def weekly_article(index: int) -> dict[str, Any]:
    """週次22列の1行（列名は T-07 の定義。値そのものはこのテストの関心外）。"""
    return {
        "収集日": "2026-07-28",
        "情報カテゴリ": "enterprise_ai_case",
        "タイトル": f"記事{index}",
        "一言要約": "AIエージェントを導入した。契約業務が自動化された。",
        "合計スコア": 83,
        "緊急性鮮度_点": 8,
        "信頼性_点": 9,
        "アドバイザリー活用度_点": 12,
        "AI業界市場インパクト_点": 15,
        "実務活用可能性_点": 17,
        "顧客関連度_点": 22,
        "レポート採用区分": "参考情報",
        "実務活用可能性": "すぐ活用",
        "顧客関連度": "直接関係",
        "信頼性": "高",
        "地域": ["日本"],
        "情報種別": "専門メディア報道",
        "業務領域": ["業務プロセス改革"],
        "業界": ["不動産"],
        "AIテーマ": ["AIエージェント"],
        "ソース": "ITmedia",
        "URL": f"https://example.com/news/{index}",
    }


def write_weekly_sheet(harness: Harness, rows: int) -> None:
    """当週シートに `rows` 件の記事を置く（`summary.adopted` の素）。"""
    harness.reports.write_weekly(
        period=WEEKLY_PERIOD,
        articles=[weekly_article(i) for i in range(rows)],
        revision=1,
        run_id="job_test",
    )


def write_exclusions(harness: Harness, rows: int, *, collected: str) -> None:
    harness.reports.append_exclusions(
        period=WEEKLY_PERIOD,
        exclusions=[
            {
                "収集日": collected,
                "タイトル": f"除外{index}",
                "URL": f"https://example.com/dropped/{index}",
                "ソース": "個人ブログ",
                "除外区分": "完全除外",
                "除外理由": "真偽不明の噂・未確認情報",
            }
            for index in range(rows)
        ],
        revision=1,
        run_id="job_test",
    )


def write_weekly_html(harness: Harness, industry: str) -> Path:
    path = harness.store.weekly_html_path(industry, WEEKLY_PERIOD)
    harness.store.write_text(path, "<html>週刊</html>")
    return path


def write_monthly_html(harness: Harness) -> Path:
    path = harness.store.monthly_html_path(MONTHLY_PERIOD)
    harness.store.write_text(path, "<html>月刊</html>")
    return path


# =============================================================================
# GET /reports/{period}
# =============================================================================


def test_a_weekly_report_has_the_shape_of_design_3_3(harness: Harness) -> None:
    write_weekly_sheet(harness, rows=11)
    write_exclusions(harness, rows=3, collected="2026-07-28")
    write_weekly_html(harness, "不動産")

    response = harness.client.get(f"/reports/{WEEKLY_PERIOD}")

    assert response.status_code == 200
    assert response.json() == {
        "period": WEEKLY_PERIOD,
        "type": "weekly",
        "html_urls": [
            {
                "industry": "不動産",
                "url": (
                    f"/files/weekly_ai_intelligence_newsletter_"
                    f"{INDUSTRY_ENCODED}_{WEEKLY_PERIOD}.html"
                ),
            }
        ],
        "xlsx_url": (
            f"/files/weekly_ai_intelligence_report.xlsx#sheet={WEEKLY_PERIOD}"
        ),
        "summary": {"adopted": 11, "excluded": 3},
    }


def test_every_industry_html_is_listed(harness: Harness) -> None:
    """⚠️ **§3.3 の `html_url`（単数）との差分**（T-46 で週刊は業界ごとに1通）。

    単数のままだと「どれか1通」しか返せず、残りへ到達する手段が API から消える。
    → §3.3 の改訂は T-38。
    """
    write_weekly_sheet(harness, rows=5)
    write_weekly_html(harness, "不動産")
    write_weekly_html(harness, "金融")

    body = harness.client.get(f"/reports/{WEEKLY_PERIOD}").json()

    assert sorted(item["industry"] for item in body["html_urls"]) == ["不動産", "金融"]


def test_the_listing_comes_from_the_files_not_from_the_config(
    harness: Harness,
) -> None:
    """⚠️ config は admin 以外に**存在も中身も**返さない（§6.1）が、この口は全ロール可。

    config の `target_industries` を読んで一覧を作ると、設定値が非 admin へ
    漏れる経路になる。実ファイルから数えていれば、config に業界を足しただけで
    一覧が増えることはない。
    """
    write_weekly_sheet(harness, rows=1)
    write_weekly_html(harness, "不動産")
    # config 側にしか存在しない業界（HTML は出していない）。
    body = harness.client.get(f"/reports/{WEEKLY_PERIOD}").json()

    assert [item["industry"] for item in body["html_urls"]] == ["不動産"]


def test_another_period_html_is_not_listed(harness: Harness) -> None:
    """先週の HTML が今週の一覧に混ざらない。"""
    write_weekly_sheet(harness, rows=1)
    write_weekly_html(harness, "不動産")
    harness.store.write_text(
        harness.store.weekly_html_path("不動産", "2026-W30"), "<html>先週</html>"
    )

    body = harness.client.get(f"/reports/{WEEKLY_PERIOD}").json()

    assert len(body["html_urls"]) == 1
    assert WEEKLY_PERIOD in body["html_urls"][0]["url"]


def test_a_monthly_report_has_one_html_without_an_industry(harness: Harness) -> None:
    """月刊ビリーフは業界別ではない（1通）。形は週刊と同じ配列で返す。"""
    harness.reports.write_monthly(
        period=MONTHLY_PERIOD,
        cases=[
            {
                "No": 1,
                "トピック(章)": "第1章 業務自動化",
                "企業・組織": ["A社"],
                "タイトル": "事例1",
                "URL": "https://example.com/case/1",
                "出典": "ITmedia（2026-07-27）",
                "掲載月": MONTHLY_PERIOD,
                "解説": ["事実。", "詳細。", "示唆。"],
            }
        ],
        revision=1,
        run_id="job_test",
    )
    write_monthly_html(harness)

    body = harness.client.get(f"/reports/{MONTHLY_PERIOD}").json()

    assert body["type"] == "monthly"
    assert body["html_urls"] == [
        {"industry": None, "url": f"/files/monthly_belief_{MONTHLY_PERIOD}.html"}
    ]
    assert body["xlsx_url"] == (
        f"/files/monthly_ai_leading_cases.xlsx#sheet={MONTHLY_PERIOD}"
    )


def test_the_exclusions_are_counted_for_that_period_only(harness: Harness) -> None:
    """除外ログは週次ブックに全期間ぶん積まれる（§8.1）。期間で切り出す。"""
    write_weekly_sheet(harness, rows=2)
    write_exclusions(harness, rows=2, collected="2026-07-28")  # 2026-W31
    write_exclusions(harness, rows=5, collected="2026-07-21")  # 2026-W30

    body = harness.client.get(f"/reports/{WEEKLY_PERIOD}").json()

    assert body["summary"] == {"adopted": 2, "excluded": 2}


def test_a_report_that_does_not_exist_yet_is_404(harness: Harness) -> None:
    response = harness.client.get(f"/reports/{WEEKLY_PERIOD}")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "report_not_found"


def test_a_report_with_only_html_is_still_found(harness: Harness) -> None:
    """xlsx を消してしまっても、出した HTML へは到達できるようにする。"""
    write_weekly_html(harness, "不動産")

    assert harness.client.get(f"/reports/{WEEKLY_PERIOD}").status_code == 200


@pytest.mark.parametrize("period", ["2026-W99", "2026-13", "config.json", "2026-W1"])
def test_a_malformed_period_is_unprocessable(harness: Harness, period: str) -> None:
    """⚠️ 表記だけでなく**実在**も見る（53週を持たない年がある）。

    不正な period をそのまま `ArtifactStore` へ渡すと、`artifact_root` の外を
    指しうる。ここが関門。
    """
    response = harness.client.get(f"/reports/{period}")

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_period"


@pytest.mark.parametrize("period", ["2026/W31", "..", "../config.json", "%2e%2e"])
def test_a_period_cannot_walk_out_of_the_artifact_root(
    harness: Harness, period: str
) -> None:
    """区切りを含む period はルーティングにも当たらない（422 の手前で 404）。"""
    assert harness.client.get(f"/reports/{period}").status_code in (404, 422)


# =============================================================================
# GET /files/{filename}
# =============================================================================


def test_a_generated_html_is_served(harness: Harness) -> None:
    write_weekly_html(harness, "不動産")

    response = harness.client.get(
        f"/files/weekly_ai_intelligence_newsletter_{INDUSTRY_ENCODED}_"
        f"{WEEKLY_PERIOD}.html"
    )

    assert response.status_code == 200
    assert response.text == "<html>週刊</html>"
    assert response.headers["content-type"] == "text/html; charset=utf-8"


def test_the_intermediate_xlsx_is_served_with_its_own_content_type(
    harness: Harness,
) -> None:
    workbook = Workbook()
    workbook.save(harness.store.weekly_report_path())

    response = harness.client.get("/files/weekly_ai_intelligence_report.xlsx")

    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_a_url_from_the_reports_listing_can_be_fetched(harness: Harness) -> None:
    """一覧が返す URL がそのまま使えること（エンコードの取り違えを検出する）。"""
    write_weekly_sheet(harness, rows=1)
    write_weekly_html(harness, "不動産")
    url = harness.client.get(f"/reports/{WEEKLY_PERIOD}").json()["html_urls"][0]["url"]

    assert harness.client.get(url).status_code == 200


def test_a_missing_file_is_404(harness: Harness) -> None:
    response = harness.client.get(f"/files/monthly_belief_{MONTHLY_PERIOD}.html")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "file_not_found"


# --- ★ 配信経路から見えてはいけないもの ---------------------------------------


def test_the_config_is_never_served(harness: Harness) -> None:
    """⚠️ **最重要**（仕様書 §2 重要要件・§6.1）。

    `config.json` は `artifact_root` の直下に同居している。配信は全ロール可なので、
    ここが通ると「config は admin 以外に存在も中身も返さない」が崩れる。
    """
    harness.store.write_text(harness.store.config_path(), '{"meta": {"revision": 1}}')

    response = harness.client.get("/files/config.json")

    assert response.status_code == 404
    # 中身の断片も返っていないこと。
    assert "revision" not in response.text


def test_the_dry_run_scratch_is_never_served(harness: Harness) -> None:
    """⚠️ scratch は**未保存の config を適用した出力**（設計判断C）。

    admin だけが見てよいものが、配信経路から漏れないこと。
    """
    scratch = harness.store.dry_run_dir("dry_1")
    scratch.mkdir(parents=True)
    (scratch / "result.xlsx").write_bytes(b"x")

    for path in ("scratch", "scratch/dry-run/dry_1/result.xlsx", "result.xlsx"):
        assert harness.client.get(f"/files/{path}").status_code == 404


@pytest.mark.parametrize(
    "filename",
    [
        "raw_articles_2026-W31.json",
        "validation_2026-W31.json",
        "narrative_2026-W31.json",
    ],
)
def test_the_internal_handover_files_are_not_served(
    harness: Harness, filename: str
) -> None:
    """段の間の受け渡しファイル（§8.2）は成果物ではない。配信経路に載せない。"""
    (harness.store.root / filename).write_text("{}", encoding="utf-8")

    assert harness.client.get(f"/files/{filename}").status_code == 404


def test_the_history_and_the_job_records_are_not_served(harness: Harness) -> None:
    """`_history/`（旧版の退避）と `_runs/`（ジョブ記録・ロック）も配信しない。"""
    for relative in ("_history/2026-W31/1_job_x/report.xlsx", "_runs/job_x.json"):
        path = harness.store.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        assert harness.client.get(f"/files/{relative}").status_code == 404


@pytest.mark.parametrize(
    "attempt",
    [
        "../config.json",
        "..%2Fconfig.json",
        "..%252Fconfig.json",
        "%2e%2e%2fconfig.json",
        "/etc/passwd",
        "%2Fetc%2Fpasswd",
        "....//config.json",
        "..",
        ".",
        "",
        "%20weekly_ai_intelligence_report.xlsx",
    ],
)
def test_path_traversal_is_refused(harness: Harness, attempt: str) -> None:
    """⚠️ **`artifact_root` の外へ出さない。**

    ルーティング（パスパラメータは `/` に当たらない）と許可リストの2段で守る。
    どこで弾かれても応答は 404 で揃える（不正と不在を区別させない）。
    """
    harness.store.write_text(harness.store.config_path(), '{"meta": {"revision": 1}}')

    response = harness.client.get(f"/files/{attempt}")

    assert response.status_code in (404, 405), attempt
    assert "revision" not in response.text


def test_a_file_outside_the_artifact_root_is_not_reachable(
    harness: Harness, tmp_path: Path
) -> None:
    """絶対パスを渡しても `artifact_root` の外は読めない。"""
    outside = tmp_path / "secret.html"
    outside.write_text("<html>秘密</html>", encoding="utf-8")

    response = harness.client.get(f"/files/{outside}")

    assert response.status_code in (404, 405)
    assert "秘密" not in response.text


def test_only_the_expected_names_are_servable(harness: Harness) -> None:
    """許可リストの内容そのもの（除外リスト方式へ逆戻りさせない）。"""
    store = harness.store

    assert store.is_servable("weekly_ai_intelligence_report.xlsx")
    assert store.is_servable("monthly_ai_leading_cases.xlsx")
    assert store.is_servable(f"monthly_belief_{MONTHLY_PERIOD}.html")
    assert store.is_servable(
        f"weekly_ai_intelligence_newsletter_不動産_{WEEKLY_PERIOD}.html"
    )

    for denied in (
        "config.json",
        "raw_articles_2026-W31.json",
        "validation_2026-W31.json",
        "narrative_2026-W31.json",
        "weekly_ai_intelligence_report.xlsx.bak",
        "monthly_belief_2026-07.html.html",
        "monthly_belief_20260707.html",
        "weekly_ai_intelligence_newsletter_2026-W31.html",  # 業界が無い
        "../weekly_ai_intelligence_report.xlsx",
    ):
        assert not store.is_servable(denied), denied
