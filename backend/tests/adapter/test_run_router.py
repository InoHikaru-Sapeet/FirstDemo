"""ジョブ実行 API（T-27。設計書 §3.2・§3.3）。

**HTTP への変換のテスト**。実行の中身（状態機械・config の固定・ロック・監査）は
`tests/application/test_run_orchestrator.py` が担当し、ここは:

- **202 で `job_id` を返し、実行は応答と切り離される**（90〜100分走る前提）
- **応答を返す前に判定するもの**：二重起動（409）・config が固定できない（409）・
  period の不備（422）
- `period` 省略時の解決（§13.1。cron が固定文字列で叩けること）
- `GET /run/{job_id}` の内容と 404
- ⚠️ **ジョブ記録の絶対パスを外へ出さない**（配信 URL と件数に落とす）

認可（viewer は 403 等）は `test_rbac.py` の網羅テストが担当する。
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.database.base import Base
from adapter.database.models.user import User
from adapter.http.fastapi.auth.dependencies import get_db_session, get_session_factory
from adapter.http.fastapi.main import app
from adapter.http.fastapi.routers import run as run_router
from adapter.storage.job_store import JobStore, RunAlreadyInProgressError
from application.usecases.run_orchestrator import (
    ConfigPinError,
    PreparedRun,
    RunPreconditionError,
    RunRequestError,
)
from config import get_settings
from enterprise.entities.period import parse_period
from enterprise.entities.principal import Role
from enterprise.entities.run_job import (
    JobTrigger,
    ResumePoint,
    RunJob,
    RunType,
    Step,
)
from enterprise.services.password import hash_password
from enterprise.services.service_token import hash_service_token

PASSWORD = "correct horse battery staple"
SERVICE_TOKEN = "service-token-for-tests"
WEEKLY_PERIOD = "2026-W31"
MONTHLY_PERIOD = "2026-07"
JOB_ID = "job_20260817-080000-abc123"
NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)


# --- 代役 ---------------------------------------------------------------------


def make_job(**overrides: Any) -> RunJob:
    base: dict[str, Any] = {
        "job_id": JOB_ID,
        "type": RunType.WEEKLY,
        "period": WEEKLY_PERIOD,
        "actor": "admin:usr_admin",
        "revision": 3,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return RunJob.model_validate(base | overrides)


@dataclass
class FakeOrchestrator:
    """`prepare()` だけを持つ代役（`execute()` は launcher が呼ぶ）。"""

    job: RunJob | None = None
    error: Exception | None = None
    prepare_calls: list[dict[str, Any]] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)

    async def prepare(self, **kwargs: Any) -> PreparedRun:
        self.prepare_calls.append(kwargs)
        if self.error:
            raise self.error
        job = (self.job or make_job()).model_copy(
            update={
                "type": kwargs["run_type"],
                "period": kwargs["period"],
                "resume_from": kwargs["resume_from"],
                "actor": kwargs["actor"],
                "trigger": kwargs["trigger"],
            }
        )
        return PreparedRun(
            job=job, pipeline=None, period=parse_period(job.period), lock=None
        )  # type: ignore[arg-type]

    async def execute(self, prepared: PreparedRun) -> RunJob:
        self.executed.append(prepared.job.job_id)
        return prepared.job


class InlineLauncher:
    """テスト用：切り離さずその場で走らせる（待ち合わせを不要にする）。"""

    async def launch(self, orchestrator: Any, prepared: PreparedRun) -> None:
        await orchestrator.execute(prepared)


@dataclass
class Harness:
    client: TestClient
    orchestrator: FakeOrchestrator
    jobs: JobStore
    root: Path


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("SERVICE_TOKEN_HASH", hash_service_token(SERVICE_TOKEN))
    get_settings.cache_clear()

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'run.db'}", poolclass=NullPool
    )

    async def create_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    orchestrator = FakeOrchestrator()
    jobs = JobStore(tmp_path / "artifacts", tz=UTC)

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_session_factory] = lambda: maker
    app.dependency_overrides[run_router.get_orchestrator] = lambda: orchestrator
    app.dependency_overrides[run_router.get_run_launcher] = InlineLauncher
    app.dependency_overrides[run_router.get_job_store] = lambda: jobs

    with TestClient(app) as client:
        client._maker = maker  # type: ignore[attr-defined]
        yield Harness(
            client=client,
            orchestrator=orchestrator,
            jobs=jobs,
            root=tmp_path / "artifacts",
        )

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def login(harness: Harness, role: Role = Role.ADMIN) -> None:
    email = f"{role.value}@sapeet.com"

    async def insert() -> None:
        async with harness.client._maker() as session:  # type: ignore[attr-defined]
            session.add(
                User(
                    user_id=f"usr_{role.value}",
                    email=email,
                    display_name="テスト 花子",
                    password_hash=hash_password(PASSWORD),
                    role=role,
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
    response = harness.client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


# --- POST /run/{type} ----------------------------------------------------------


def test_a_run_is_accepted_with_202_and_a_job_id(harness: Harness) -> None:
    """設計書 §3.3：`202 { job_id, type, period, status }`。"""
    login(harness)

    response = harness.client.post("/run/weekly", json={"period": WEEKLY_PERIOD})

    assert response.status_code == 202
    assert response.json() == {
        "job_id": JOB_ID,
        "type": "weekly",
        "period": WEEKLY_PERIOD,
        "status": "queued",
    }


def test_the_run_is_started_after_the_response_is_shaped(harness: Harness) -> None:
    """⚠️ **202 は「受け付けた」であって「終わった」ではない。**

    ここでは launcher を差し替えて同期に走らせているが、本番は
    `asyncio.create_task` で切り離す（90〜100分かかるため）。
    """
    login(harness)

    harness.client.post("/run/weekly", json={"period": WEEKLY_PERIOD})

    assert harness.orchestrator.executed == [JOB_ID]


def test_the_period_is_resolved_when_it_is_omitted(harness: Harness) -> None:
    """§13.1 の規則（週次＝当週）。cron が固定文字列で叩けるようにするため。"""
    login(harness)

    response = harness.client.post("/run/weekly", json={})

    assert response.status_code == 202
    period = harness.orchestrator.prepare_calls[0]["period"]
    assert parse_period(period).is_weekly


def test_the_monthly_period_is_resolved_to_the_previous_month(
    harness: Harness,
) -> None:
    login(harness)

    response = harness.client.post("/run/monthly", json={})

    assert response.status_code == 202
    assert parse_period(harness.orchestrator.prepare_calls[0]["period"]).is_monthly


def test_a_body_is_not_required(harness: Harness) -> None:
    """cron の `curl -X POST` に本文を書かせない。"""
    login(harness)

    assert harness.client.post("/run/weekly").status_code == 202


def test_the_resume_point_reaches_the_orchestrator(harness: Harness) -> None:
    login(harness)

    harness.client.post(
        "/run/weekly", json={"period": WEEKLY_PERIOD, "resume_from": "auto"}
    )

    assert harness.orchestrator.prepare_calls[0]["resume_from"] is ResumePoint.AUTO


def test_the_resume_point_defaults_to_crawl(harness: Harness) -> None:
    """§3.3「`resume_from` 省略時は crawl から」。"""
    login(harness)

    harness.client.post("/run/weekly", json={"period": WEEKLY_PERIOD})

    assert harness.orchestrator.prepare_calls[0]["resume_from"] is ResumePoint.CRAWL


def test_the_actor_is_the_caller(harness: Harness) -> None:
    """監査ログの `actor`（§4.4 の `役割:識別子`）は呼び出し元から取る。"""
    login(harness, Role.EDITOR)

    harness.client.post("/run/weekly", json={"period": WEEKLY_PERIOD})

    assert harness.orchestrator.prepare_calls[0]["actor"] == "editor:usr_editor"
    assert harness.orchestrator.prepare_calls[0]["trigger"] is JobTrigger.API


def test_a_service_token_is_recorded_as_a_cron_run(harness: Harness) -> None:
    """⚠️ 監査で「無人で回ったのか人が回したのか」を分けるため。"""
    harness.client.headers["Authorization"] = f"Bearer {SERVICE_TOKEN}"

    response = harness.client.post("/run/weekly", json={"period": WEEKLY_PERIOD})

    assert response.status_code == 202
    assert harness.orchestrator.prepare_calls[0]["trigger"] is JobTrigger.CRON


def test_an_unknown_run_type_is_rejected(harness: Harness) -> None:
    """`POST /run/quarterly` のような種別は受け付けない。"""
    login(harness)

    assert harness.client.post("/run/quarterly", json={}).status_code == 422


def test_an_unknown_body_field_is_rejected(harness: Harness) -> None:
    """綴り違いを黙って無視しない（`resume` と書いて crawl から回るのを防ぐ）。"""
    login(harness)

    assert (
        harness.client.post("/run/weekly", json={"resume": "filter"}).status_code == 422
    )


# --- 応答を返す前に判定するもの ------------------------------------------------


def test_a_double_start_is_a_conflict(harness: Harness) -> None:
    """⚠️ 外部 cron から重ねて叩かれても 202 を返さない（二重起動防止・T-26）。"""
    login(harness)
    harness.orchestrator.error = RunAlreadyInProgressError(
        RunType.WEEKLY, WEEKLY_PERIOD, "job_running"
    )

    response = harness.client.post("/run/weekly", json={"period": WEEKLY_PERIOD})

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "already_running"
    assert "job_running" in response.json()["detail"]["message"]
    # 走り出していないこと（409 なのに背後で動いている、を作らない）。
    assert harness.orchestrator.executed == []


def test_a_config_that_cannot_be_pinned_is_a_conflict(harness: Harness) -> None:
    """§8.3。実行できる状態にない（入力の不備ではない）ので 409。"""
    login(harness)
    harness.orchestrator.error = ConfigPinError("revision=3 のスナップショットが無い")

    response = harness.client.post("/run/weekly", json={"period": WEEKLY_PERIOD})

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "config_not_pinnable"


def test_a_period_of_the_wrong_kind_is_unprocessable(harness: Harness) -> None:
    """⚠️ `POST /run/weekly` に `2026-07` を通すと月刊の成果物を上書きする。"""
    login(harness)
    harness.orchestrator.error = RunRequestError("weekly に 2026-07 は使えません")

    response = harness.client.post("/run/weekly", json={"period": MONTHLY_PERIOD})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_request"


def test_a_missing_prerequisite_is_unprocessable(harness: Harness) -> None:
    """`resume_from=render` なのに narrative が無い（§8.3 の前段成果物の確認）。"""
    login(harness)
    harness.orchestrator.error = RunPreconditionError("narrative がありません")

    response = harness.client.post(
        "/run/weekly", json={"period": WEEKLY_PERIOD, "resume_from": "render"}
    )

    assert response.status_code == 422
    assert "narrative" in response.json()["detail"]["message"]


# --- GET /run/{job_id} ---------------------------------------------------------


def test_the_job_status_can_be_polled(harness: Harness) -> None:
    """T-27 の完了条件「フロントがポーリングできる」。"""
    login(harness)
    harness.jobs.save(make_job().entering(Step.FILTER, at=NOW))

    response = harness.client.get(f"/run/{JOB_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == JOB_ID
    assert body["status"] == "filtering"
    assert body["period"] == WEEKLY_PERIOD
    assert body["revision"] == 3


def test_a_failed_job_reports_the_step_and_the_exception_class(
    harness: Harness,
) -> None:
    """⚠️ T-15 の6分類がそのまま読める（言い換えて潰さない）。"""
    login(harness)
    harness.jobs.save(
        make_job()
        .entering(Step.CRAWL, at=NOW)
        .failing(Step.CRAWL, TimeoutError("1800秒で打ち切り"), at=NOW)
    )

    body = harness.client.get(f"/run/{JOB_ID}").json()

    assert body["status"] == "failed"
    assert body["failed_step"] == "crawl"
    assert body["error_type"] == "TimeoutError"
    assert body["error_message"] == "1800秒で打ち切り"


def test_the_status_does_not_leak_server_paths(harness: Harness) -> None:
    """⚠️ **ジョブ記録の絶対パスを外へ出さない。**

    サーバ上の配置を漏らすうえ、フロントから開けない。配信できるものは
    `/files/...` に直し、それ以外（raw / validation / narrative）は件数だけ。
    """
    login(harness)
    root = harness.root
    harness.jobs.save(
        make_job()
        .entering(Step.RENDER, at=NOW)
        .completing(
            Step.RENDER,
            artifacts=[
                str(root / "raw_articles_2026-W31.json"),
                str(root / "narrative_2026-W31.json"),
                str(root / "weekly_ai_intelligence_report.xlsx"),
                str(root / f"weekly_ai_intelligence_newsletter_{WEEKLY_PERIOD}.html"),
            ],
            at=NOW,
        )
    )

    body = harness.client.get(f"/run/{JOB_ID}").json()

    assert body["artifact_count"] == 4
    assert body["artifact_urls"] == [
        "/files/weekly_ai_intelligence_report.xlsx",
        "/files/weekly_ai_intelligence_newsletter_2026-W31.html",
    ]
    assert str(root) not in response_text(body)
    assert "raw_articles" not in response_text(body)


def response_text(body: dict[str, Any]) -> str:
    import json

    return json.dumps(body, ensure_ascii=False)


def test_an_unknown_job_is_404(harness: Harness) -> None:
    login(harness)

    response = harness.client.get("/run/job_nope")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "job_not_found"


def test_a_traversing_job_id_never_reaches_the_runs_directory(
    harness: Harness,
) -> None:
    """⚠️ `job_id` は外部入力。`_runs/` の外（`config.json` 等）を読ませない。

    2段で守っている：ルーティングが `/` を含むパスパラメータに当たらないこと
    （ここで 404 になる）と、`JobStore.job_path()` の検証（下のテスト）。
    """
    login(harness)

    for job_id in ("..%2F..%2Fconfig", "../../config.json", "..%252F..%252Fconfig"):
        assert harness.client.get(f"/run/{job_id}").status_code == 404


def test_a_malformed_job_id_is_404_not_500(harness: Harness) -> None:
    """検証で弾いた `job_id` も「無い」と同じ 404 にする。

    「不正」と「無い」を区別できると、どんな ID なら存在しうるかを探れる。
    """
    login(harness)

    response = harness.client.get("/run/%20job_1")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "job_not_found"


def test_the_poll_does_not_need_the_artifacts_to_exist(harness: Harness) -> None:
    """ポーリングは軽く保つ（記録1ファイルだけを読む）。"""
    login(harness)
    harness.jobs.save(make_job())

    assert harness.client.get(f"/run/{JOB_ID}").status_code == 200
