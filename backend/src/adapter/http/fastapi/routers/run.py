"""ジョブ実行 API（T-27。設計書 §3.2・§3.3 ／ 仕様書 §6.2・§13.1）。

`POST /run/{weekly|monthly}`（202）と `GET /run/{job_id}`（状態照会）。
実行の中身は持たない——状態機械・config の固定・二重起動防止・監査ログは
すべて `RunOrchestrator`（T-26）の責務で、ここは **HTTP への変換**だけを行う。

---

⚠️ **認可の判定を新規に書かない**（TASKS.md T-27 備考）。

`require_permission` が `auth/rbac.py` の権限マトリクス（§6.2）から導出する。
`POST /run` は admin / editor / system が 202、**viewer は 403**。
`GET /run/{job_id}` は §6.2 に行が無い設計時追加分で、**`POST /run` と同じ行**に
した（根拠は `rbac.py` のコメント）。

---

⚠️ **202 を返してから 90〜100分走る**（18業界の週次フル実行の実測）。

そのため受付は2段になっている:

1. `orchestrator.prepare()` — **応答を返す前**に済ませる。ここで落ちるものが
   HTTP のエラーになる（422 = 入力の不備 / 409 = 二重起動・config を固定できない）
2. `orchestrator.execute()` — `asyncio.create_task` で**切り離して**走らせる。
   進み方は `GET /run/{job_id}` で見る

⚠️ **タイムアウトもキャンセル API も無い。** 効くのは AI 呼び出し1回ごとの
制限だけで、止めたければサーバのプロセスを落とす。落とすと走っていたジョブは
死に、記録は非終端のまま残る（次に同じ period を要求したときにロックが回収され、
記録は `Failed` になる）。詳細は `run_orchestrator` / `job_store` の⚠️。

⚠️ **プロセスをまたいだ実行状況の共有はジョブ記録（ファイル）が行う。**
`make run-weekly`（別プロセス）が走っていれば、この API も 409 で断る。
"""

import asyncio
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict

from adapter.http.fastapi.auth.dependencies import (
    get_session_factory,
    require_permission,
)
from adapter.storage.artifact_store import ArtifactStore
from adapter.storage.job_store import JobStore, JobStoreError, RunAlreadyInProgressError
from application.usecases.run_orchestrator import (
    ConfigPinError,
    PreparedRun,
    RunOrchestrator,
    RunRequestError,
    SessionFactory,
    build_orchestrator,
)
from config import Settings, get_settings
from enterprise.entities.principal import Principal, Role
from enterprise.entities.run_job import (
    JobStatus,
    JobTrigger,
    ResumePoint,
    RunJob,
    RunType,
    Step,
    resolve_period,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/run", tags=["run"])

# 切り離したジョブへの強い参照。⚠️ **これが無いと GC される。**
# `asyncio.create_task()` の戻り値を捨てると、イベントループは弱い参照しか
# 持たないため、走っている最中のタスクが回収されうる（CPython の既知の挙動）。
_RUNNING: set[asyncio.Task[RunJob]] = set()


# --- DI -----------------------------------------------------------------------


def get_job_store(settings: Annotated[Settings, Depends(get_settings)]) -> JobStore:
    return JobStore.from_settings(settings)


def get_orchestrator(
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    jobs: Annotated[JobStore, Depends(get_job_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RunOrchestrator:
    return build_orchestrator(session_factory, settings=settings, jobs=jobs)


class RunLauncher:
    """受付済みの実行を**応答と切り離して**走らせる。

    ⚠️ **差し替え口として分けてある。** テストは同期に走らせる実装へ差し替えて
    「202 を返した後に本当に走るか」を待ち合わせなしで確かめる。将来ジョブを
    別プロセス／別ワーカーへ出すときも、ここを差し替えれば済む。
    """

    async def launch(
        self, orchestrator: RunOrchestrator, prepared: PreparedRun
    ) -> None:
        task = asyncio.create_task(
            orchestrator.execute(prepared), name=f"run:{prepared.job.job_id}"
        )
        _RUNNING.add(task)
        task.add_done_callback(_RUNNING.discard)


def get_run_launcher() -> RunLauncher:
    return RunLauncher()


# --- I/O（設計書 §3.3）--------------------------------------------------------


class RunRequest(BaseModel):
    """`POST /run/{type}` のリクエスト（設計書 §3.3）。

    ⚠️ **`period` を省略できるようにした**（§3.3 の例は必須の形）。cron の
    コマンドを固定文字列にするため（T-28）。省略時は仕様書 §13.1 の規則で
    解決する＝週次は当週 ISO 週・月次は前月（Asia/Tokyo）。CLI の PERIOD 省略と
    **同じ関数**を使う（`enterprise.entities.run_job.resolve_period`）ので、
    叩く口によって対象期間がずれることはない。→ §3.3 への追記が必要（T-38）。
    """

    model_config = ConfigDict(extra="forbid")

    period: str | None = None
    resume_from: ResumePoint = ResumePoint.CRAWL


class RunAccepted(BaseModel):
    """`POST /run/{type}` → 202（設計書 §3.3）。"""

    job_id: str
    type: RunType
    period: str
    status: JobStatus


class RunStatusResponse(BaseModel):
    """`GET /run/{job_id}`（設計時追加。フロントのポーリング用）。

    ⚠️ **成果物は絶対パスではなく配信 URL で返す。** ジョブ記録が持っているのは
    サーバのファイルパス（`/srv/app/artifacts/...`）で、そのまま返すと配置を
    外へ漏らすうえ、フロントから開けない。配信できるものだけ `/files/...` に
    直し、それ以外（`raw_articles` / `validation` / `narrative`）は
    **件数にだけ数える**。
    """

    job_id: str
    type: RunType
    period: str
    status: JobStatus
    resume_from: ResumePoint
    start_step: Step
    revision: int | None
    trigger: JobTrigger
    attempts: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    completed_steps: list[Step]
    artifact_count: int
    artifact_urls: list[str]
    failed_step: Step | None
    error_type: str | None
    error_message: str | None


def to_status_response(job: RunJob, store: ArtifactStore) -> RunStatusResponse:
    from adapter.http.fastapi.routers.files import file_url

    return RunStatusResponse(
        job_id=job.job_id,
        type=job.type,
        period=job.period,
        status=job.status,
        resume_from=job.resume_from,
        start_step=job.start_step,
        revision=job.revision,
        trigger=job.trigger,
        attempts=job.attempts,
        created_at=job.created_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
        completed_steps=list(job.completed_steps),
        artifact_count=len(job.artifacts),
        artifact_urls=[
            file_url(name)
            for name in dict.fromkeys(_basename(path) for path in job.artifacts)
            if store.is_servable(name)
        ],
        failed_step=job.failed_step,
        error_type=job.error_type,
        error_message=job.error_message,
    )


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _error(code: int, error: str, message: str) -> HTTPException:
    """プロジェクト共通の `detail` 封筒（T-13・T-40・T-42 と同じ形）。"""
    return HTTPException(status_code=code, detail={"error": error, "message": message})


# --- エンドポイント -------------------------------------------------------------


@router.post("/{run_type}", status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    run_type: Annotated[RunType, Path(description="weekly / monthly")],
    principal: Annotated[Principal, Depends(require_permission)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_orchestrator)],
    launcher: Annotated[RunLauncher, Depends(get_run_launcher)],
    settings: Annotated[Settings, Depends(get_settings)],
    # ⚠️ **本文ごと省略できる。** cron の `curl -X POST` に JSON を書かせない
    # （period も resume_from も既定で足りる＝T-28 のコマンド例）。
    body: RunRequest | None = None,
) -> RunAccepted:
    """パイプラインを起動する（**admin / editor / system**。viewer は 403）。

    - **202** `{job_id, type, period, status}` — 受け付けた（まだ終わっていない）
    - **409** `already_running` — 同じ `{type, period}` が実行中（二重起動防止）
    - **409** `config_not_pinnable` — 開始時 revision を固定できない（§8.3）
    - **422** `invalid_request` — period の表記・種別違い／再開ポイントの前段が無い

    ⚠️ **`period` を省略すると §13.1 の規則で解決する**（週次＝当週 / 月次＝前月）。
    cron はこれを使う（T-28）。
    """
    request = body or RunRequest()
    period = request.period or resolve_period(
        run_type, today=datetime.now(tz=settings.tzinfo).date()
    )
    try:
        prepared = await orchestrator.prepare(
            run_type=run_type,
            period=period,
            actor=principal.actor,
            resume_from=request.resume_from,
            trigger=_trigger_for(principal),
        )
    except RunAlreadyInProgressError as exc:
        raise _error(status.HTTP_409_CONFLICT, "already_running", str(exc)) from exc
    except ConfigPinError as exc:
        raise _error(status.HTTP_409_CONFLICT, "config_not_pinnable", str(exc)) from exc
    except RunRequestError as exc:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_request", str(exc)
        ) from exc

    await launcher.launch(orchestrator, prepared)
    logger.info(
        "run accepted: job=%s type=%s period=%s actor=%s",
        prepared.job.job_id,
        run_type,
        period,
        principal.actor,
    )
    return RunAccepted(
        job_id=prepared.job.job_id,
        type=prepared.job.type,
        period=prepared.job.period,
        status=prepared.job.status,
    )


@router.get("/{job_id}")
async def get_run(
    job_id: Annotated[str, Path(description="POST /run が返した job_id")],
    _caller: Annotated[Principal, Depends(require_permission)],
    jobs: Annotated[JobStore, Depends(get_job_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RunStatusResponse:
    """ジョブの状態を返す（フロントのポーリング用。T-27 の完了条件）。

    ⚠️ **ポーリング前提なので軽く保つ。** 読むのはジョブ記録1ファイルだけで、
    成果物の実在確認や xlsx の読み込みはしない（`GET /reports/{period}` の担当）。

    - **200** ジョブの状態
    - **404** そのジョブが無い（`job_id` の形が不正な場合も同じ 404。
      「不正」と「無い」を区別させない）
    """
    try:
        job = jobs.get(job_id)
    except JobStoreError as exc:
        # パス区切りを含む job_id 等。**404 と同じ扱い**にする（形の妥当性から
        # 「どんな ID なら存在しうるか」を探らせない）。
        logger.warning("不正な job_id への照会を拒否した: %r", job_id)
        raise _error(
            status.HTTP_404_NOT_FOUND, "job_not_found", "ジョブが見つかりません。"
        ) from exc

    if job is None:
        raise _error(
            status.HTTP_404_NOT_FOUND, "job_not_found", "ジョブが見つかりません。"
        )
    return to_status_response(job, ArtifactStore.from_settings(settings))


def _trigger_for(principal: Principal) -> JobTrigger:
    """`system`（サービストークン）＝ cron、それ以外は人（API）。

    ⚠️ **監査ログで「無人で回ったのか人が回したのか」を分けるため。** ロールは
    認可の入力（T-09）だが、ここで見ているのは認可ではなく**記録の分類**。
    """
    return JobTrigger.CRON if principal.role is Role.SYSTEM else JobTrigger.API


__all__ = [
    "RunAccepted",
    "RunLauncher",
    "RunRequest",
    "RunStatusResponse",
    "get_job_store",
    "get_orchestrator",
    "get_run_launcher",
    "router",
]
