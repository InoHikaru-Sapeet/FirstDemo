"""Run Orchestrator（T-26。設計書 §8.2〜§8.4 ／ 仕様書 §13.1・§14）。

crawl → filter → render の**段取り**を持つ唯一の場所。ワーカー本体
（T-16 / T-21 / T-22 / T-24 / T-25 / T-44）のロジックは1行も持たず、やるのは

1. **config を開始時 revision で固定参照する**（§6.3・§8.3）
2. **再開ポイントを決める**（`resume_from` の明示指定／前段成果物の存在確認）
3. **状態機械を進める**（§8.4。表は `enterprise.entities.run_job`）
4. **二重起動を防ぐ**（同一 `{type, period}` のロック。`adapter.storage.job_store`）
5. **監査ログを残す**（`run_start` / `run_finish` / `artifact_created`。T-10）

の5つだけ。

---

⚠️ **T-45 の通し実行 CLI はここへ昇格・置換した**（T-45 備考の申し送り）。

`adapter.cli.run_pipeline` にあった3段の配線（`run_crawl` / `run_filter` /
`run_render` と `Pipeline`）を**そのまま**移し、CLI は Orchestrator を呼ぶ薄い皮に
した。並べて残さなかったのは、配線が2つあると「`--from render` の前段確認」の
ような判断が片方にだけ入って食い違うため。CLI 側にしか無かった挙動
（narrative が無ければ落とす等）はここへ引き継いである。

**T-45 との差分**は3つ:

| | T-45（CLI） | T-26（Orchestrator） |
|---|---|---|
| config | `load()`（ファイル） | **`get_pinned(rev)`（DB スナップショット）** |
| run_id | `cli-YYYYmmdd-HHMMSS` | **`job_id`（`job_...`）に一本化** |
| スキップ | `--from` の明示のみ | ＋ **`auto`（前段成果物の存在確認）** |

---

⚠️ **`get_pinned()` を使う理由と、その代償**（§8.3 の完了条件）

`load()` は `config.json` を読む＝**実行中に admin が保存すると次のステップから
別の基準になりうる**。`get_pinned(revision)` は保存時に取った DB スナップショットを
返すので、ファイルが書き換わっても実行中の判断基準は動かない（§14 の再現性）。

代償は、**`config.json` を手で書き換えている環境では DB と食い違う**こと
（`ConfigRepository.get_pinned` の⚠️）。固定できない場合は**黙って `load()` へ
落とさず**、`ConfigPinError` で実行前に落とす。落とさないと「ファイルを直したのに
反映されない実行」が静かに走る。

---

⚠️ **長時間ジョブ中のプロセス管理（タイムアウト・キャンセル）**

週次フル実行は **18業界で90〜100分**（実測）。この前提で次のように割り切っている。

- **ジョブ全体のタイムアウトは無い。** 効くのは AI 呼び出し1回ごとの制限
  （`AI_TIMEOUT_SECONDS` / `AI_CRAWL_TIMEOUT_SECONDS`）だけで、超えると
  `AITimeoutError` でその段が `Failed` になる。
- **キャンセル API は無い。** 止めるにはプロセスを落とす（CLI なら `Ctrl-C`、
  API ならサーバを止める）。
- **落とした後**：ジョブ記録は非終端のまま残り、ロックも残る。次に同じ period を
  要求したときに、ロックの持ち主プロセスが居ないことを検出して回収し、記録を
  `Failed` にする（`job_store` の⚠️）。
- **DB セッションを握り続けない。** config の固定と監査ログの書き込みで
  **そのつど開いて閉じる**。90分開けたままにすると、その間ずっと接続を専有し、
  SQLite では他の書き込みを待たせる。
- **再開**：落ちた実行の成果物は残っているので、`resume_from` で途中から回せる
  （§14）。

⚠️ **`asyncio.create_task()` で走らせる場合、サーバのプロセスが落ちればジョブも
死ぬ。** ジョブ記録が「実行中」のまま残るのはこのためで、上の回収がその後始末。
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from adapter.config_repository import ConfigRepository, ConfigRepositoryError
from adapter.database.models.audit_log import AuditEventType
from adapter.html.monthly_renderer import MonthlyNarrative
from adapter.html.weekly_renderer import WeeklyNarrative
from adapter.storage.artifact_store import ArtifactStore
from adapter.storage.job_store import JobStore, RunLock
from application.usecases.audit import AuditService
from application.usecases.narrative import to_monthly_narrative, to_weekly_narrative
from config import Settings, get_settings
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.json_document import DocumentParseError
from enterprise.entities.narrative import (
    MonthlyNarrativeDocument,
    WeeklyNarrativeDocument,
    parse_narrative,
)
from enterprise.entities.period import Period, PeriodError, parse_period
from enterprise.entities.run_job import (
    JobStatus,
    JobTrigger,
    ResumePoint,
    RunJob,
    RunType,
    Step,
    new_job_id,
    period_matches_type,
)

logger = logging.getLogger(__name__)


class RunRequestError(Exception):
    """実行を始める前に分かる不備（period の表記・種別違い・前段成果物が無い）。

    HTTP 層は **422**（形式・整合）へ変換する。
    """


class RunPreconditionError(RunRequestError):
    """再開ポイントの指定と、実際に残っている成果物が噛み合わない。

    例：`resume_from=render` なのに `narrative_{period}.json` が無い。
    ⚠️ **黙って前の段から回さない。** 指定した段から回るつもりの呼び出し元に
    「本文の無い HTML」や「思っていたより長い実行」を返さないため。
    """


class ConfigPinError(Exception):
    """開始時 revision の固定に失敗した（§8.3）。

    HTTP 層は **409**（実行できる状態にない）へ変換する。
    """


# --- ステップの相手（実体は T-16 / T-21 / T-22 / T-24 / T-25）-----------------
# ⚠️ **戻り値のうち Orchestrator が使うものだけを書く。** 具象クラスを型に書くと
# 「配線テストのためにワーカーを本物として組み立てる」ことになり、テストが実際の
# `claude` を起動しかねない（T-45 から引き継いだ制約）。


class CrawlOutcome(Protocol):
    """`application.usecases.crawl.CrawlResult` が満たす形。"""

    @property
    def path(self) -> Path: ...

    @property
    def article_count(self) -> int: ...


class FilterOutcome(Protocol):
    """`application.usecases.filter.FilterResult` が満たす形。"""

    @property
    def articles(self) -> list[dict[str, Any]]: ...

    @property
    def cases(self) -> list[dict[str, Any]]: ...

    @property
    def exclusion_log(self) -> list[dict[str, Any]]: ...

    @property
    def validation_path(self) -> Path: ...

    @property
    def narrative_path(self) -> Path: ...


class WriteOutcome(Protocol):
    """`adapter.xlsx.report_writer.WrittenReport` が満たす形。"""

    @property
    def path(self) -> Path: ...

    @property
    def rows(self) -> int: ...


class RenderOutcome(Protocol):
    """週刊／月刊レンダラの `RenderedHtml` が満たす形。"""

    @property
    def path(self) -> Path: ...


class Crawler(Protocol):
    async def crawl(self, period: str) -> CrawlOutcome: ...


class Filterer(Protocol):
    async def run(self, period: str, *, run_id: str | None = None) -> FilterOutcome: ...


class Reports(Protocol):
    """中間xlsx の読み書き（T-22）。"""

    def write_weekly(
        self,
        *,
        period: str,
        articles: Sequence[Mapping[str, Any]],
        exclusions: Sequence[Mapping[str, Any]] = (),
        revision: int,
        run_id: str,
    ) -> WriteOutcome: ...

    def write_monthly(
        self,
        *,
        period: str,
        cases: Sequence[Mapping[str, Any]],
        revision: int,
        run_id: str,
    ) -> WriteOutcome: ...

    def append_exclusions(
        self,
        *,
        period: str,
        exclusions: Sequence[Mapping[str, Any]],
        revision: int,
        run_id: str,
    ) -> WriteOutcome: ...

    def read_weekly(self, period: str) -> list[dict[str, Any]]: ...

    def read_monthly(self, period: str) -> list[dict[str, Any]]: ...


class WeeklyRender(Protocol):
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
    ) -> RenderOutcome: ...


class MonthlyRender(Protocol):
    def render(
        self,
        *,
        period: str,
        cases: Sequence[Mapping[str, Any]],
        config: IntelligenceConfig,
        narrative: MonthlyNarrative | None = None,
        revision: int,
        run_id: str,
    ) -> RenderOutcome: ...


@dataclass(frozen=True, slots=True)
class Pipeline:
    """1回の実行で使う相手一式（組み立ては `adapter.pipeline_factory`）。

    Attributes:
        config: 実行開始時に**固定参照**している config（§6.3・§14）
        store: 成果物の置き場（narrative の読み込みに使う。T-02）
        crawler: crawl ワーカー（T-16）
        filterer: filter ワーカー（T-21・narrative は T-44）
        reports: 中間xlsx の読み書き（T-22）
        weekly_renderer: 週刊レンダラ（T-24）
        monthly_renderer: 月刊レンダラ（T-25）
    """

    config: IntelligenceConfig
    store: ArtifactStore
    crawler: Crawler
    filterer: Filterer
    reports: Reports
    weekly_renderer: WeeklyRender
    monthly_renderer: MonthlyRender

    @property
    def revision(self) -> int:
        """固定参照している config の revision（退避先の名前・監査の素）。"""
        return self.config.meta.revision


# --- config の固定と監査（どちらも DB を短く開いて閉じる）--------------------

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class ConfigPin(Protocol):
    """開始時 revision を固定した config を返す（§6.3・§8.3）。"""

    async def pin(self) -> IntelligenceConfig: ...


class RunAuditor(Protocol):
    """実行の監査（設計書 §4.4 の `run_start` / `run_finish` / `artifact_created`）。"""

    async def run_start(self, job: RunJob) -> None: ...

    async def run_finish(self, job: RunJob) -> None: ...

    async def artifact_created(self, job: RunJob, path: str) -> None: ...


class RepositoryConfigPin:
    """`config.json` の現行 revision を、DB のスナップショットで固定する。

    手順は「ファイルを読んで **revision だけ**決める → その revision の
    スナップショットを DB から取る」。revision の正はファイル（T-11）で、
    実行中に動かしたくないのは**中身**なので、この2段になる。
    """

    def __init__(
        self, session_factory: SessionFactory, *, settings: Settings | None = None
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def pin(self) -> IntelligenceConfig:
        """Raises: ConfigPinError: config が無い／履歴に該当 revision が無い等。"""
        async with self._session_factory() as db:
            repo = ConfigRepository.from_settings(db, self._settings)
            try:
                revision = repo.load().meta.revision
            except (ConfigRepositoryError, DocumentParseError) as exc:
                raise ConfigPinError(f"config を読めません: {exc}") from exc
            try:
                return await repo.get_pinned(revision)
            except (ConfigRepositoryError, DocumentParseError) as exc:
                # ⚠️ ここで `load()` の結果へ落とさない。落とすと「DB と食い違う
                # config で走った」ことが誰にも分からなくなる（§14 の再現性）。
                raise ConfigPinError(
                    f"revision={revision} のスナップショットを固定できません: {exc}"
                    "（config.json を手で編集した場合に起きます。"
                    "`make config-record ARGS='--apply'` を実行してください"
                    "（または管理画面から保存）。履歴と監査ログがファイルに追いつきます）"
                ) from exc


class AuditingRunAuditor:
    """T-10 の `AuditService` 経由で実行の監査ログを積む。

    ⚠️ **1件ごとにセッションを開いて commit する。** 90分のジョブの間ずっと
    トランザクションを開けたままにすると、その間の監査ログが1件も見えず
    （commit されていない）、SQLite では他の書き込みも待たされる。

    ⚠️ **監査の失敗は握り潰さない**（`AuditService` の方針）。`run_start` が
    書けなければジョブを始めない＝「誰が動かしたか分からない実行」を作らない。
    """

    def __init__(
        self, session_factory: SessionFactory, *, tz: tzinfo | None = None
    ) -> None:
        self._session_factory = session_factory
        self._tz = tz

    async def run_start(self, job: RunJob) -> None:
        await self._record(job, AuditEventType.RUN_START, target=job.job_id)

    async def run_finish(self, job: RunJob) -> None:
        await self._record(job, AuditEventType.RUN_FINISH, target=job.job_id)

    async def artifact_created(self, job: RunJob, path: str) -> None:
        await self._record(job, AuditEventType.ARTIFACT_CREATED, target=path)

    async def _record(
        self, job: RunJob, event_type: AuditEventType, *, target: str
    ) -> None:
        async with self._session_factory() as db:
            AuditService(db).record(
                event_type=event_type,
                actor=job.actor,
                at=datetime.now(tz=self._tz),
                revision=job.revision,
                diff=_run_detail(job, event_type),
                target=target,
                period=job.period,
            )
            await db.commit()


def _run_detail(job: RunJob, event_type: AuditEventType) -> dict[str, Any]:
    """監査ログの `diff` に載せる実行の要点。

    ⚠️ **config の中身は入れない**（`config_update` と違い、実行系のイベントで
    判断基準そのものを残す理由が無い。固定した `revision` があれば
    `config_revisions` から引ける）。
    """
    detail: dict[str, Any] = {
        "job_id": job.job_id,
        "type": job.type.value,
        "status": job.status.value,
        "trigger": job.trigger.value,
        "start_step": job.start_step.value,
        "resume_from": job.resume_from.value,
        "attempts": job.attempts,
    }
    if event_type is AuditEventType.RUN_FINISH:
        detail["completed_steps"] = [step.value for step in job.completed_steps]
        detail["artifact_count"] = len(job.artifacts)
        if job.status is JobStatus.FAILED:
            detail["failed_step"] = job.failed_step.value if job.failed_step else None
            detail["error_type"] = job.error_type
    return detail


# --- 実行の受付から完了まで ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedRun:
    """受付が済んで**ロックを握った**実行（あとは走らせるだけ）。

    ⚠️ **`prepare()` と `execute()` を分けているのは HTTP のため。**
    `POST /run` は 202 を即返す必要があるが、二重起動（409）と入力の不備（422）は
    **応答を返す前に**判定しなければならない。ロックの取得までを `prepare()` で
    同期に済ませ、90分かかる `execute()` を背後に回す。

    ⚠️ **`prepare()` したら必ず `execute()` すること。** ロックは `execute()` の
    `finally` で外れる。呼び忘れるとロックが残る（プロセスが生きている間は
    stale 判定にもならない）。
    """

    job: RunJob
    pipeline: Pipeline
    period: Period
    lock: RunLock


class RunOrchestrator:
    """設計書 §8.4 の状態機械を回す。"""

    def __init__(
        self,
        *,
        jobs: JobStore,
        config_pin: ConfigPin,
        auditor: RunAuditor,
        build_pipeline: Callable[[IntelligenceConfig], Pipeline],
        clock: Callable[[], datetime] | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        """
        Args:
            jobs: ジョブ記録とロック（`adapter.storage.job_store`）
            config_pin: 開始時 revision の固定（§8.3）
            auditor: 実行の監査（T-10）
            build_pipeline: 固定した config から相手一式を組む
                （本番は `adapter.pipeline_factory.build_pipeline`）
            clock: 現在時刻（テストで差し替える）
            tz: `clock` 未指定時のタイムゾーン
        """
        self._jobs = jobs
        self._config_pin = config_pin
        self._auditor = auditor
        self._build_pipeline = build_pipeline
        self._tz = tz
        self._clock = clock or (lambda: datetime.now(tz=tz))

    # --- 受付 -------------------------------------------------------------

    async def prepare(
        self,
        *,
        run_type: RunType,
        period: str,
        actor: str,
        resume_from: ResumePoint = ResumePoint.CRAWL,
        trigger: JobTrigger = JobTrigger.API,
    ) -> PreparedRun:
        """入力を検証し、ロックを取り、config を固定してジョブを作る。

        **まだ1ステップも走らせない。** ここまでが同期に返せる部分で、
        続きは `execute()`。

        Raises:
            RunRequestError: period の表記・種別が不正（422）
            RunPreconditionError: 再開ポイントの前段成果物が無い（422）
            RunAlreadyInProgressError: 同一 `{type, period}` が実行中（409）
            ConfigPinError: 開始時 revision を固定できない（409）
        """
        parsed = self._parse_period(run_type, period)

        # ⚠️ **ロックが先。** config の固定（DB 往復）や成果物の存在確認より前に
        # 取らないと、同じ period の2本が両方とも検証を抜けてから衝突する。
        job_id = new_job_id(self._clock())
        lock = self._jobs.acquire(run_type, parsed.text, job_id=job_id)
        try:
            config = await self._config_pin.pin()
            pipeline = self._build_pipeline(config)
            start_step = self._resolve_start_step(pipeline, parsed, resume_from)
            self._ensure_inputs_exist(pipeline, parsed, start_step)

            now = self._clock()
            job = RunJob(
                job_id=job_id,
                type=run_type,
                period=parsed.text,
                status=JobStatus.QUEUED,
                resume_from=resume_from,
                start_step=start_step,
                revision=config.meta.revision,
                trigger=trigger,
                actor=actor,
                created_at=now,
                updated_at=now,
            )
            self._jobs.save(job)
        except BaseException:
            # 受付で落ちたらロックを残さない（次の要求が 409 で詰まる）。
            self._jobs.release(lock)
            raise

        return PreparedRun(job=job, pipeline=pipeline, period=parsed, lock=lock)

    async def prepare_retry(
        self, job_id: str, *, actor: str | None = None
    ) -> PreparedRun:
        """`Failed → Queued`（該当 step からのリトライ。§8.4）。

        **同じ `job_id` を使い回す**（§8.4 の矢印が同じジョブの遷移なので）。
        失敗した段から再開し、その先の「完了済み」は取り消す。

        Raises:
            RunRequestError: そのジョブが無い／失敗していない
            RunAlreadyInProgressError: 同一 `{type, period}` が実行中（409）
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise RunRequestError(f"ジョブ {job_id} がありません。")
        if job.status is not JobStatus.FAILED:
            raise RunRequestError(
                f"ジョブ {job_id} は {job.status} です。"
                "リトライできるのは failed のジョブだけです（設計書 §8.4）。"
            )

        parsed = self._parse_period(job.type, job.period)
        lock = self._jobs.acquire(job.type, job.period, job_id=job.job_id)
        try:
            config = await self._config_pin.pin()
            pipeline = self._build_pipeline(config)
            # 失敗した段から。段が記録されていない（受付で落ちた）なら最初から。
            start_step = job.failed_step or job.start_step
            self._ensure_inputs_exist(pipeline, parsed, start_step)

            retried = job.requeued(start_step=start_step, at=self._clock()).model_copy(
                update={
                    # ⚠️ **config は取り直す。** リトライは新しい実行なので、
                    # そのときの現行 revision で固定する（§6.3 は「実行開始時点」）。
                    "revision": config.meta.revision,
                    "actor": actor or job.actor,
                }
            )
            self._jobs.save(retried)
        except BaseException:
            self._jobs.release(lock)
            raise

        return PreparedRun(job=retried, pipeline=pipeline, period=parsed, lock=lock)

    # --- 実行 -------------------------------------------------------------

    async def execute(self, prepared: PreparedRun) -> RunJob:
        """段を順に回す。**例外で終わらせず、`Failed` のジョブを返す。**

        呼び出し元（HTTP の背景タスク / CLI）が「どこで落ちたか」を記録から
        読めるようにするため、ステップの例外はここで受け止めて状態へ落とす。
        受付の時点で分かる不備は `prepare()` が例外で返している。
        """
        job = prepared.job.starting(at=self._clock())
        self._jobs.save(job)
        try:
            await self._auditor.run_start(job)
            job = await self._run_steps(job, prepared)
        except BaseException as exc:
            # ここへ来るのは**段の外**で落ちた場合（監査ログが書けない・
            # 中断された等）。段の中の失敗は `_run_steps` が状態へ落として返す。
            # ⚠️ 記録を `Failed` にしてから再送出する。握り潰すと「実行中のまま
            # 止まったジョブ」が残る。
            if not job.is_terminal:
                job = self._fail(job, None, exc)
                await self._finish(job)
            raise
        finally:
            self._jobs.release(prepared.lock)
        return job

    async def run(
        self,
        *,
        run_type: RunType,
        period: str,
        actor: str,
        resume_from: ResumePoint = ResumePoint.CRAWL,
        trigger: JobTrigger = JobTrigger.API,
    ) -> RunJob:
        """`prepare()` → `execute()`（通しで待つ呼び出し元＝CLI 用）。"""
        prepared = await self.prepare(
            run_type=run_type,
            period=period,
            actor=actor,
            resume_from=resume_from,
            trigger=trigger,
        )
        return await self.execute(prepared)

    # --- 内部：段の駆動 ---------------------------------------------------

    async def _run_steps(self, job: RunJob, prepared: PreparedRun) -> RunJob:
        for step in job.remaining_steps:
            job = job.entering(step, at=self._clock())
            self._jobs.save(job)
            logger.info(
                "run step started: job=%s step=%s period=%s",
                job.job_id,
                step,
                job.period,
            )
            try:
                produced = await self._run_step(step, prepared)
            except Exception as exc:  # noqa: BLE001 - どの例外でも段と型を残す
                logger.warning(
                    "run step failed: job=%s step=%s error=%s",
                    job.job_id,
                    step,
                    type(exc).__name__,
                )
                job = self._fail(job, step, exc)
                await self._finish(job)
                return job

            job = job.completing(
                step, artifacts=[str(path) for path in produced], at=self._clock()
            )
            self._jobs.save(job)
            for path in produced:
                await self._auditor.artifact_created(job, str(path))
            logger.info("run step finished: job=%s step=%s", job.job_id, step)

        job = job.transition_to(JobStatus.DONE, at=self._clock())
        await self._finish(job)
        return job

    async def _run_step(self, step: Step, prepared: PreparedRun) -> list[Path]:
        if step is Step.CRAWL:
            return await self._crawl(prepared)
        if step is Step.FILTER:
            return await self._filter(prepared)
        return self._render(prepared)

    async def _crawl(self, prepared: PreparedRun) -> list[Path]:
        """crawl（T-16）。`raw_articles_{period}.json` を書く。"""
        result = await prepared.pipeline.crawler.crawl(prepared.period.text)
        return [result.path]

    async def _filter(self, prepared: PreparedRun) -> list[Path]:
        """filter（T-21 ＋ 生成テキスト T-44）と中間xlsx への書き出し（T-22）。

        ⚠️ **`run_id` に `job_id` を渡す**（`_history/{period}/{revision}_{job_id}/`
        へ退避される＝設計判断B。T-44 は `run_id=None` だと退避しない）。
        """
        pipeline = prepared.pipeline
        period = prepared.period
        job_id = prepared.job.job_id
        result = await pipeline.filterer.run(period.text, run_id=job_id)
        revision = pipeline.revision
        written: list[Path] = []

        if period.is_weekly:
            report = pipeline.reports.write_weekly(
                period=period.text,
                articles=result.articles,
                exclusions=result.exclusion_log,
                revision=revision,
                run_id=job_id,
            )
            written.append(report.path)
        else:
            report = pipeline.reports.write_monthly(
                period=period.text,
                cases=result.cases,
                revision=revision,
                run_id=job_id,
            )
            written.append(report.path)
            # ⚠️ 除外ログは月次ブックではなく**週次ブック**へ積む（§8.1 が除外ログを
            # 週次ブックの構成として定義している。T-21 備考）。0件のときに呼ばないのは、
            # 書くものが無いのに週次ブックを上書き（＝退避を1世代消費）しないため。
            if result.exclusion_log:
                log = pipeline.reports.append_exclusions(
                    period=period.text,
                    exclusions=result.exclusion_log,
                    revision=revision,
                    run_id=job_id,
                )
                written.append(log.path)

        return [*written, result.validation_path, result.narrative_path]

    def _render(self, prepared: PreparedRun) -> list[Path]:
        """render（T-24 / T-25）。**入力は中間xlsx と narrative ファイル**（§8.2）。

        ⚠️ **週刊は対象業界ごとに1通**（T-46 Step 4。`weekly_..._{industry}_
        {period}.html`）。回すのは呼び出し側の責務で、レンダラは1回1業界のまま
        （T-46 Step 4 備考の申し送り「T-26 でも同じ形にすること」）。
        """
        pipeline = prepared.pipeline
        period = prepared.period
        job_id = prepared.job.job_id

        narrative_path = pipeline.store.narrative_path(period.text)
        # ⚠️ **無ければ落とす。黙って本文の無い HTML を出さない**（T-45 から
        # 引き継いだ判断）。`_ensure_inputs_exist` が受付でも見ているが、
        # crawl から通した実行でも通る道なのでここでも確かめる。
        if not pipeline.store.exists(narrative_path):
            raise RunPreconditionError(
                f"{narrative_path} がありません。生成テキストは filter が書くので"
                "（T-44）、resume_from=filter で filter からやり直してください。"
            )
        document = parse_narrative(
            pipeline.store.read_text(narrative_path), period=period
        )

        if period.is_weekly and isinstance(document, WeeklyNarrativeDocument):
            articles = pipeline.reports.read_weekly(period.text)
            industries = pipeline.config.tunable_thresholds.industries
            return [
                pipeline.weekly_renderer.render(
                    period=period.text,
                    articles=articles,
                    config=pipeline.config,
                    narrative=to_weekly_narrative(document, industry),
                    industry=industry,
                    revision=pipeline.revision,
                    run_id=job_id,
                ).path
                for industry in industries
            ]

        if isinstance(document, MonthlyNarrativeDocument):
            cases = pipeline.reports.read_monthly(period.text)
            return [
                pipeline.monthly_renderer.render(
                    period=period.text,
                    cases=cases,
                    config=pipeline.config,
                    narrative=to_monthly_narrative(document),
                    revision=pipeline.revision,
                    run_id=job_id,
                ).path
            ]

        raise RunPreconditionError(  # pragma: no cover - parse_narrative が型を決める
            f"{narrative_path} の種別が対象期間 {period.text} と噛み合いません"
        )

    # --- 内部：受付の検証 -------------------------------------------------

    def _parse_period(self, run_type: RunType, period: str) -> Period:
        """表記・実在・**種別の一致**を見る。

        ⚠️ **種別の取り違えをここで落とす。** `POST /run/weekly` に `2026-07` を
        通すと、週刊のつもりで月刊の成果物を上書きする（正規名は period 由来）。
        """
        try:
            parsed = parse_period(period)
        except PeriodError as exc:
            raise RunRequestError(str(exc)) from exc
        if not period_matches_type(period, run_type):
            expected = (
                "YYYY-Www（週次）" if run_type is RunType.WEEKLY else "YYYY-MM（月次）"
            )
            raise RunRequestError(
                f"{run_type} の実行に {period!r} は使えません（{expected} が必要です）"
            )
        return parsed

    def _resolve_start_step(
        self, pipeline: Pipeline, period: Period, resume_from: ResumePoint
    ) -> Step:
        """`resume_from` を実際の開始段へ解決する（§8.3）。

        ⚠️ **`auto` が飛ばすのは crawl だけ。** §8.3 が自動スキップとして書いて
        いるのは「`raw_articles_{period}.json` があれば crawl をスキップし filter
        から再開」の1つで、render まで自動で飛ばすと**判断基準を変えて再実行しても
        filter が走らない**。render からの再開は明示指定でだけ行う。
        """
        if resume_from is not ResumePoint.AUTO:
            return Step(resume_from.value)
        raw = pipeline.store.raw_articles_path(period.text)
        if pipeline.store.exists(raw):
            logger.info("auto resume: %s があるので crawl を省きます", raw)
            return Step.FILTER
        return Step.CRAWL

    def _ensure_inputs_exist(
        self, pipeline: Pipeline, period: Period, start_step: Step
    ) -> None:
        """その段の入力が残っているか（§8.3 の「前段成果物の存在確認」）。

        Raises:
            RunPreconditionError: 前の実行の成果物が無い
        """
        store = pipeline.store
        if start_step is Step.FILTER:
            raw = store.raw_articles_path(period.text)
            if not store.exists(raw):
                raise RunPreconditionError(
                    f"{raw} がありません。crawl の出力が無い状態では filter から"
                    "始められません"
                    "（resume_from を外して crawl から実行してください）。"
                )
        elif start_step is Step.RENDER:
            narrative = store.narrative_path(period.text)
            if not store.exists(narrative):
                raise RunPreconditionError(
                    f"{narrative} がありません。生成テキストは filter が書くので"
                    "（T-44）、resume_from=filter で filter からやり直してください。"
                )

    # --- 内部：終了処理 ---------------------------------------------------

    def _fail(self, job: RunJob, step: Step | None, exc: BaseException) -> RunJob:
        failed = job.failing(step, exc, at=self._clock())
        self._jobs.save(failed)
        return failed

    async def _finish(self, job: RunJob) -> None:
        self._jobs.save(job)
        await self._auditor.run_finish(job)
        logger.info(
            "run finished: job=%s status=%s artifacts=%d",
            job.job_id,
            job.status,
            len(job.artifacts),
        )


def build_orchestrator(
    session_factory: SessionFactory,
    *,
    settings: Settings | None = None,
    jobs: JobStore | None = None,
) -> RunOrchestrator:
    """本番の相手で組み立てる（CLI と API が共有する1箇所）。

    ⚠️ `build_pipeline` はここで遅延 import する。`adapter.pipeline_factory` は
    このモジュールを import しているので（`Pipeline` の定義元）、先頭で読むと
    循環になる。
    """
    from adapter.pipeline_factory import build_pipeline

    settings = settings or get_settings()
    return RunOrchestrator(
        jobs=jobs or JobStore.from_settings(settings),
        config_pin=RepositoryConfigPin(session_factory, settings=settings),
        auditor=AuditingRunAuditor(session_factory, tz=settings.tzinfo),
        build_pipeline=lambda config: build_pipeline(config, settings=settings),
        tz=settings.tzinfo,
    )


__all__ = [
    "AuditingRunAuditor",
    "ConfigPin",
    "ConfigPinError",
    "Pipeline",
    "PreparedRun",
    "RepositoryConfigPin",
    "RunAuditor",
    "RunOrchestrator",
    "RunPreconditionError",
    "RunRequestError",
    "build_orchestrator",
]
