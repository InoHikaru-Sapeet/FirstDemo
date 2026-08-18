"""Run Orchestrator（T-26。設計書 §8.2〜§8.4）。

**配線とジョブ管理のテスト**。ワーカー（T-16 / T-21 / T-22 / T-24 / T-25）は
すべてモックで、実際の `claude` は起動しない（T-45 から引き継いだ制約）。重点:

- 設計書 §8.2 の受け渡し表どおりに **crawl → filter → render** が回る
  （T-45 の配線テストをここへ移した）
- **状態機械**（§8.4）が Queued → … → Done / Failed の順に記録される
- **config は開始時 revision を固定**し、`get_pinned` 相当の経路で取る（§8.3）
- **再開ポイント**（明示指定・`auto`・前段成果物の存在確認）
- **二重起動防止**とロックの外し忘れ
- **監査ログ**（`run_start` / `run_finish` / `artifact_created`）
"""

import copy
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from adapter.storage.artifact_store import ArtifactStore
from adapter.storage.job_store import JobStore, RunAlreadyInProgressError
from application.usecases.run_orchestrator import (
    ConfigPinError,
    Pipeline,
    PreparedRun,
    RunOrchestrator,
    RunPreconditionError,
    RunRequestError,
)
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.json_document import DocumentParseError
from enterprise.entities.narrative import (
    MonthlyNarrativeDocument,
    WeeklyNarrativeDocument,
    dump_narrative,
)
from enterprise.entities.period import Period, parse_period
from enterprise.entities.run_job import (
    JobStatus,
    JobTrigger,
    ResumePoint,
    RunJob,
    RunType,
    Step,
)

INITIAL_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)

WEEKLY_PERIOD = "2026-W31"
MONTHLY_PERIOD = "2026-07"
# §5.2 の初期 config の対象業界（1件）。週刊は業界ごとに1通（T-46 Step 4）。
TARGET_INDUSTRY = "不動産"

ACTOR = "admin:usr_admin"
START = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def initial_raw() -> dict[str, Any]:
    return json.loads(INITIAL_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def config(initial_raw: dict[str, Any]) -> IntelligenceConfig:
    return IntelligenceConfig.model_validate(copy.deepcopy(initial_raw))


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path)


@pytest.fixture
def jobs(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path, tz=UTC, pid=999, is_process_alive=lambda pid: pid == 999)


# --- モック（ワーカーは1つも本物を使わない）----------------------------------


@dataclass
class FakeCrawlResult:
    path: Path
    article_count: int


class FakeCrawler:
    """T-16 の `CrawlWorker` の代わり。"""

    def __init__(self, store: ArtifactStore, *, error: Exception | None = None) -> None:
        self._store = store
        self._error = error
        self.periods: list[str] = []

    async def crawl(self, period: str) -> FakeCrawlResult:
        self.periods.append(period)
        if self._error:
            raise self._error
        path = self._store.raw_articles_path(period)
        self._store.write_text(path, "[]")
        return FakeCrawlResult(path=path, article_count=12)


@dataclass
class FakeFilterResult:
    articles: list[dict[str, Any]]
    cases: list[dict[str, Any]]
    exclusion_log: list[dict[str, Any]]
    validation_path: Path
    narrative_path: Path


class FakeFilterer:
    """T-21（＋ T-44）の `FilterWorker` の代わり。"""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        exclusions: int = 2,
        error: Exception | None = None,
    ) -> None:
        self._store = store
        self._exclusions = exclusions
        self._error = error
        self.calls: list[tuple[str, str | None]] = []

    async def run(self, period: str, *, run_id: str | None = None) -> FakeFilterResult:
        self.calls.append((period, run_id))
        if self._error:
            raise self._error
        write_narrative(self._store, parse_period(period))
        return FakeFilterResult(
            articles=[{"タイトル": "filter が返した記事"}],
            cases=[{"No": 1, "タイトル": "filter が返した事例"}],
            exclusion_log=[{"除外区分": "統合"} for _ in range(self._exclusions)],
            validation_path=self._store.validation_path(period),
            narrative_path=self._store.narrative_path(period),
        )


@dataclass
class FakeWrittenReport:
    path: Path
    rows: int


@dataclass
class FakeReports:
    """T-22 の `ReportStore` の代わり（書いた内容を記録するだけ）。"""

    store: ArtifactStore
    weekly_rows: list[dict[str, Any]] = field(default_factory=list)
    monthly_rows: list[dict[str, Any]] = field(default_factory=list)
    weekly_calls: list[dict[str, Any]] = field(default_factory=list)
    monthly_calls: list[dict[str, Any]] = field(default_factory=list)
    exclusion_calls: list[dict[str, Any]] = field(default_factory=list)

    def write_weekly(self, **kwargs: Any) -> FakeWrittenReport:
        self.weekly_calls.append(kwargs)
        return FakeWrittenReport(
            path=self.store.weekly_report_path(), rows=len(kwargs["articles"])
        )

    def write_monthly(self, **kwargs: Any) -> FakeWrittenReport:
        self.monthly_calls.append(kwargs)
        return FakeWrittenReport(
            path=self.store.monthly_cases_path(), rows=len(kwargs["cases"])
        )

    def append_exclusions(self, **kwargs: Any) -> FakeWrittenReport:
        self.exclusion_calls.append(kwargs)
        return FakeWrittenReport(
            path=self.store.weekly_report_path(), rows=len(kwargs["exclusions"])
        )

    def read_weekly(self, period: str) -> list[dict[str, Any]]:
        return list(self.weekly_rows)

    def read_monthly(self, period: str) -> list[dict[str, Any]]:
        return list(self.monthly_rows)


@dataclass
class FakeRenderedHtml:
    path: Path


class FakeRenderer:
    """T-24 / T-25 のレンダラの代わり。"""

    def __init__(self, path: Path, *, error: Exception | None = None) -> None:
        self._path = path
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def render(self, **kwargs: Any) -> FakeRenderedHtml:
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return FakeRenderedHtml(path=self._path)


class FakeConfigPin:
    """`ConfigRepository.get_pinned()` の代わり（§8.3 の固定参照）。"""

    def __init__(
        self, config: IntelligenceConfig, *, error: Exception | None = None
    ) -> None:
        self._config = config
        self._error = error
        self.calls = 0

    async def pin(self) -> IntelligenceConfig:
        self.calls += 1
        if self._error:
            raise self._error
        return self._config


@dataclass
class RecordedAudit:
    event: str
    actor: str
    period: str
    revision: int | None
    target: str
    status: JobStatus


class FakeAuditor:
    """T-10 の `AuditService` 経由の記録の代わり。"""

    def __init__(self, *, fail_on_start: Exception | None = None) -> None:
        self.records: list[RecordedAudit] = []
        self._fail_on_start = fail_on_start

    async def run_start(self, job: RunJob) -> None:
        if self._fail_on_start:
            raise self._fail_on_start
        self._add("run_start", job, job.job_id)

    async def run_finish(self, job: RunJob) -> None:
        self._add("run_finish", job, job.job_id)

    async def artifact_created(self, job: RunJob, path: str) -> None:
        self._add("artifact_created", job, path)

    def _add(self, event: str, job: RunJob, target: str) -> None:
        self.records.append(
            RecordedAudit(
                event=event,
                actor=job.actor,
                period=job.period,
                revision=job.revision,
                target=target,
                status=job.status,
            )
        )

    def events(self) -> list[str]:
        return [record.event for record in self.records]


@dataclass
class Harness:
    """1回の実行に必要なモック一式。"""

    orchestrator: RunOrchestrator
    jobs: JobStore
    store: ArtifactStore
    config: IntelligenceConfig
    crawler: FakeCrawler
    filterer: FakeFilterer
    reports: FakeReports
    weekly: FakeRenderer
    monthly: FakeRenderer
    pin: FakeConfigPin
    auditor: FakeAuditor

    def saved(self, job_id: str) -> RunJob:
        job = self.jobs.get(job_id)
        assert job is not None
        return job

    def has_lock(self, run_type: RunType, period: str) -> bool:
        return self.jobs.holder_of(run_type, period) is not None


def harness(
    store: ArtifactStore,
    jobs: JobStore,
    config: IntelligenceConfig,
    *,
    crawl_error: Exception | None = None,
    filter_error: Exception | None = None,
    render_error: Exception | None = None,
    pin_error: Exception | None = None,
    audit_start_error: Exception | None = None,
    exclusions: int = 2,
) -> Harness:
    crawler = FakeCrawler(store, error=crawl_error)
    filterer = FakeFilterer(store, exclusions=exclusions, error=filter_error)
    reports = FakeReports(store=store)
    weekly = FakeRenderer(store.root / "weekly.html", error=render_error)
    monthly = FakeRenderer(store.root / "monthly.html", error=render_error)
    pin = FakeConfigPin(config, error=pin_error)
    auditor = FakeAuditor(fail_on_start=audit_start_error)

    ticks = iter(range(0, 10_000))
    return Harness(
        orchestrator=RunOrchestrator(
            jobs=jobs,
            config_pin=pin,
            auditor=auditor,
            build_pipeline=lambda pinned: Pipeline(
                config=pinned,
                store=store,
                crawler=crawler,
                filterer=filterer,
                reports=reports,
                weekly_renderer=weekly,
                monthly_renderer=monthly,
            ),
            clock=lambda: START + timedelta(seconds=next(ticks)),
        ),
        jobs=jobs,
        store=store,
        config=config,
        crawler=crawler,
        filterer=filterer,
        reports=reports,
        weekly=weekly,
        monthly=monthly,
        pin=pin,
        auditor=auditor,
    )


def write_narrative(store: ArtifactStore, period: Period) -> Path:
    """filter が書いたはずの `narrative_{period}.json` を置く（render の入力）。"""
    document = (
        WeeklyNarrativeDocument(period=period.text)
        if period.is_weekly
        else MonthlyNarrativeDocument(period=period.text)
    )
    path = store.narrative_path(period.text)
    store.write_text(path, dump_narrative(document))
    return path


async def run_weekly(test: Harness, **kwargs: Any) -> RunJob:
    return await test.orchestrator.run(
        run_type=RunType.WEEKLY, period=WEEKLY_PERIOD, actor=ACTOR, **kwargs
    )


# =============================================================================
# 1. 段の配線（設計書 §8.2 の受け渡し表）
# =============================================================================


async def test_the_three_steps_run_in_the_order_of_the_handover_table(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config)

    job = await run_weekly(test)

    assert job.status is JobStatus.DONE
    assert job.completed_steps == [Step.CRAWL, Step.FILTER, Step.RENDER]
    assert test.crawler.periods == [WEEKLY_PERIOD]
    assert test.filterer.calls == [(WEEKLY_PERIOD, job.job_id)]
    assert len(test.weekly.calls) == 1
    assert test.monthly.calls == []


async def test_the_job_id_is_the_run_id_handed_to_every_writer(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """⚠️ T-44 は `run_id=None` だと narrative を退避しない。渡し忘れを固定する。

    T-45 の `cli-...` を廃して `job_id` に一本化したので、`_history/` の
    ディレクトリ名から監査ログのジョブを引ける（`{revision}_{job_id}`）。
    """
    test = harness(store, jobs, config)

    job = await run_weekly(test)

    assert test.filterer.calls[0][1] == job.job_id
    assert test.reports.weekly_calls[0]["run_id"] == job.job_id
    assert test.weekly.calls[0]["run_id"] == job.job_id
    assert job.job_id.startswith("job_")


async def test_the_pinned_revision_goes_to_every_writer(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """退避先の名前は `{revision}_{job_id}`。全ステップで同じ revision を使う。"""
    test = harness(store, jobs, config)

    job = await run_weekly(test)

    revision = config.meta.revision
    assert job.revision == revision
    assert test.reports.weekly_calls[0]["revision"] == revision
    assert test.weekly.calls[0]["revision"] == revision


async def test_the_weekly_filter_writes_the_articles_and_the_exclusions(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config, exclusions=3)

    await run_weekly(test)

    call = test.reports.weekly_calls[0]
    assert call["period"] == WEEKLY_PERIOD
    assert call["articles"] == [{"タイトル": "filter が返した記事"}]
    assert len(call["exclusions"]) == 3
    assert test.reports.monthly_calls == []


async def test_a_monthly_run_writes_the_cases_and_appends_to_the_weekly_book(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """除外ログは月次ブックではなく週次ブックへ積む（§8.1・T-21 備考）。"""
    test = harness(store, jobs, config, exclusions=2)

    job = await test.orchestrator.run(
        run_type=RunType.MONTHLY, period=MONTHLY_PERIOD, actor=ACTOR
    )

    assert job.status is JobStatus.DONE
    assert test.reports.monthly_calls[0]["cases"] == [
        {"No": 1, "タイトル": "filter が返した事例"}
    ]
    assert test.reports.weekly_calls == []
    assert len(test.reports.exclusion_calls[0]["exclusions"]) == 2
    assert len(test.monthly.calls) == 1
    assert test.weekly.calls == []


async def test_a_monthly_run_without_exclusions_leaves_the_weekly_book_alone(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """書くものが無いのに週次ブックを上書きしない（退避の世代を無駄に消費する）。"""
    test = harness(store, jobs, config, exclusions=0)

    await test.orchestrator.run(
        run_type=RunType.MONTHLY, period=MONTHLY_PERIOD, actor=ACTOR
    )

    assert test.reports.exclusion_calls == []


async def test_the_render_reads_the_files_instead_of_the_filter_result(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """§8.2 の受け渡しはファイル経由。filter の戻り値を直接は渡さない。"""
    test = harness(store, jobs, config)
    test.reports.weekly_rows = [{"タイトル": "xlsx から読み戻した記事"}]

    await run_weekly(test)

    assert test.weekly.calls[0]["articles"] == [{"タイトル": "xlsx から読み戻した記事"}]


async def test_one_weekly_html_is_rendered_whatever_the_target_industries_are(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """T-52 Step 1：**週刊は1通**（業界版の廃止）。

    T-46 Step 4 は「業界の数だけ HTML を出す」ループを呼び出し側（ここ）に
    置いていた。業界版が無くなったので、対象業界が何件あっても1通しか出ない
    ——**ループが残っていないこと**をここで固定する。
    """
    config.tunable_thresholds.target_industries = ["不動産", "金融"]
    test = harness(store, jobs, config)
    store.write_text(
        store.narrative_path(WEEKLY_PERIOD),
        dump_narrative(
            WeeklyNarrativeDocument(
                period=WEEKLY_PERIOD, point_of_week_sentences=["今週の総括。"]
            )
        ),
    )

    job = await run_weekly(test, resume_from=ResumePoint.RENDER)

    assert len(test.weekly.calls) == 1
    assert "industry" not in test.weekly.calls[0]
    assert test.weekly.calls[0]["narrative"].point_of_week == "今週の総括。"
    assert sum("weekly.html" in path for path in job.artifacts) == 1


async def test_every_artifact_is_recorded_on_the_job(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config)

    job = await run_weekly(test)

    for path in (
        store.raw_articles_path(WEEKLY_PERIOD),
        store.weekly_report_path(),
        store.validation_path(WEEKLY_PERIOD),
        store.narrative_path(WEEKLY_PERIOD),
        store.root / "weekly.html",
    ):
        assert str(path) in job.artifacts


# =============================================================================
# 2. 状態機械（設計書 §8.4）
# =============================================================================


async def test_the_job_walks_the_states_in_order(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """Queued → Crawling → Filtering → Rendering → Done（§8.4）。

    ⚠️ 状態は**その都度ファイルへ書く**（`GET /run/{job_id}` のポーリングが
    「今どの段か」を返せるように）。ここでは書かれた状態を順に集める。
    """
    test = harness(store, jobs, config)
    seen: list[JobStatus] = []
    original_save = jobs.save

    def spy(job: RunJob) -> Path:
        if not seen or seen[-1] is not job.status:
            seen.append(job.status)
        return original_save(job)

    jobs.save = spy  # type: ignore[method-assign]

    await run_weekly(test)

    assert seen == [
        JobStatus.QUEUED,
        JobStatus.CRAWLING,
        JobStatus.FILTERING,
        JobStatus.RENDERING,
        JobStatus.DONE,
    ]


async def test_a_failing_step_stops_the_pipeline_and_names_the_step(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config, filter_error=TimeoutError("600秒で打ち切り"))

    job = await run_weekly(test)

    assert job.status is JobStatus.FAILED
    assert job.failed_step is Step.FILTER
    assert job.error_type == "TimeoutError"
    assert job.error_message == "600秒で打ち切り"
    # 後続は走らない。
    assert test.weekly.calls == []
    # そこまでに書けた成果物は残す（次の再開の材料）。
    assert str(store.raw_articles_path(WEEKLY_PERIOD)) in job.artifacts


async def test_a_failure_is_persisted_so_a_poller_can_see_it(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config, crawl_error=RuntimeError("落ちた"))

    job = await run_weekly(test)

    assert test.saved(job.job_id).status is JobStatus.FAILED


async def test_the_orchestrator_does_not_raise_on_a_step_failure(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """呼び出し元（背景タスク・CLI）が記録から読めるよう、状態へ落として返す。"""
    test = harness(store, jobs, config, render_error=ValueError("今週のポイントが空"))

    job = await run_weekly(test)

    assert job.status is JobStatus.FAILED
    assert job.error_type == "ValueError"


# =============================================================================
# 3. config の固定参照（設計書 §8.3・§6.3）
# =============================================================================


async def test_the_config_is_pinned_once_at_the_start(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """⚠️ 実行中に admin が保存しても途中で基準が切り替わらない（§14 の再現性）。"""
    test = harness(store, jobs, config)

    job = await run_weekly(test)

    assert test.pin.calls == 1
    assert test.filterer.calls  # 全段が同じ Pipeline（＝同じ config）を使う
    assert job.revision == config.meta.revision


async def test_a_config_that_cannot_be_pinned_stops_the_run(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """⚠️ 黙って `load()` へ落とさない（食い違ったまま走ったことが分からなくなる）。"""
    test = harness(store, jobs, config, pin_error=ConfigPinError("履歴に無い"))

    with pytest.raises(ConfigPinError):
        await run_weekly(test)

    assert test.crawler.periods == []
    # 受付で落ちたらロックを残さない（次の要求が詰まる）。
    assert not test.has_lock(RunType.WEEKLY, WEEKLY_PERIOD)


# =============================================================================
# 4. 再開ポイント（設計書 §8.3）
# =============================================================================


async def test_resume_from_filter_does_not_crawl_again(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config)
    store.write_text(store.raw_articles_path(WEEKLY_PERIOD), "[]")

    job = await run_weekly(test, resume_from=ResumePoint.FILTER)

    assert job.status is JobStatus.DONE
    assert job.start_step is Step.FILTER
    assert test.crawler.periods == []
    assert test.filterer.calls == [(WEEKLY_PERIOD, job.job_id)]


async def test_resume_from_render_runs_neither_the_crawl_nor_the_filter(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config)
    write_narrative(store, parse_period(WEEKLY_PERIOD))

    job = await run_weekly(test, resume_from=ResumePoint.RENDER)

    assert job.status is JobStatus.DONE
    assert test.crawler.periods == []
    assert test.filterer.calls == []
    assert len(test.weekly.calls) == 1


async def test_auto_skips_the_crawl_when_the_raw_articles_are_there(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """§8.3「`raw_articles_{period}.json` があれば crawl をスキップ」。"""
    test = harness(store, jobs, config)
    store.write_text(store.raw_articles_path(WEEKLY_PERIOD), "[]")

    job = await run_weekly(test, resume_from=ResumePoint.AUTO)

    assert job.start_step is Step.FILTER
    assert job.resume_from is ResumePoint.AUTO  # 要求そのものも残す
    assert test.crawler.periods == []


async def test_auto_starts_from_the_crawl_when_nothing_is_there(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config)

    job = await run_weekly(test, resume_from=ResumePoint.AUTO)

    assert job.start_step is Step.CRAWL
    assert test.crawler.periods == [WEEKLY_PERIOD]


async def test_auto_never_skips_the_filter(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """⚠️ ここが render まで飛ぶと、**判断基準を変えて再実行しても結果が変わらない**。

    §8.3 が自動スキップとして書いているのは crawl の1つだけ。
    """
    test = harness(store, jobs, config)
    store.write_text(store.raw_articles_path(WEEKLY_PERIOD), "[]")
    write_narrative(store, parse_period(WEEKLY_PERIOD))

    job = await run_weekly(test, resume_from=ResumePoint.AUTO)

    assert job.start_step is Step.FILTER
    assert test.filterer.calls  # filter は必ず走る
    assert len(test.weekly.calls) == 1


async def test_resume_from_filter_without_the_raw_articles_is_refused(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """§8.3 の「前段成果物の存在確認」。受付の時点で落とす。"""
    test = harness(store, jobs, config)

    with pytest.raises(RunPreconditionError) as caught:
        await run_weekly(test, resume_from=ResumePoint.FILTER)

    assert "raw_articles" in str(caught.value)
    assert not test.has_lock(RunType.WEEKLY, WEEKLY_PERIOD)


async def test_resume_from_render_without_a_narrative_says_what_to_do(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """生成テキストは filter が書く（T-44）。無いまま描くと本文が抜ける。"""
    test = harness(store, jobs, config)

    with pytest.raises(RunPreconditionError) as caught:
        await run_weekly(test, resume_from=ResumePoint.RENDER)

    assert "resume_from=filter" in str(caught.value)
    assert test.weekly.calls == []


async def test_a_narrative_from_another_period_is_refused(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """本文だけ先週のままの HTML を配信に回さない（T-44 の read 側ガード）。"""
    test = harness(store, jobs, config)
    store.write_text(
        store.narrative_path(WEEKLY_PERIOD),
        dump_narrative(WeeklyNarrativeDocument(period="2026-W30")),
    )

    job = await run_weekly(test, resume_from=ResumePoint.RENDER)

    assert job.status is JobStatus.FAILED
    assert job.error_type == DocumentParseError.__name__
    assert test.weekly.calls == []


# =============================================================================
# 5. period と種別の検証
# =============================================================================


async def test_a_period_of_the_wrong_kind_is_refused(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """⚠️ `POST /run/weekly` に `2026-07` を通すと月刊の成果物を上書きする。"""
    test = harness(store, jobs, config)

    with pytest.raises(RunRequestError):
        await test.orchestrator.run(
            run_type=RunType.WEEKLY, period=MONTHLY_PERIOD, actor=ACTOR
        )
    with pytest.raises(RunRequestError):
        await test.orchestrator.run(
            run_type=RunType.MONTHLY, period=WEEKLY_PERIOD, actor=ACTOR
        )


async def test_an_unparsable_period_is_refused_before_the_lock(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """表記が合っているだけの期間も通さない（実日付へ開けるかまで見る）。"""
    test = harness(store, jobs, config)

    with pytest.raises(RunRequestError):
        await test.orchestrator.run(
            run_type=RunType.MONTHLY, period="2026-13", actor=ACTOR
        )

    assert test.pin.calls == 0
    assert not jobs.locks_root.exists() or list(jobs.locks_root.iterdir()) == []


# =============================================================================
# 6. 二重起動防止（TASKS.md T-26 備考「ロックのテストを必ず書く」）
# =============================================================================


async def test_the_same_period_cannot_run_twice_at_once(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """⚠️ 外部 cron から複数回叩かれても壊れないことが「外部cron方式」の前提。"""
    test = harness(store, jobs, config)
    first = await test.orchestrator.prepare(
        run_type=RunType.WEEKLY, period=WEEKLY_PERIOD, actor=ACTOR
    )

    with pytest.raises(RunAlreadyInProgressError) as caught:
        await test.orchestrator.prepare(
            run_type=RunType.WEEKLY, period=WEEKLY_PERIOD, actor=ACTOR
        )

    assert caught.value.job_id == first.job.job_id


async def test_the_lock_is_released_after_a_successful_run(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config)

    await run_weekly(test)

    assert not test.has_lock(RunType.WEEKLY, WEEKLY_PERIOD)
    # 同じ period をもう一度回せる（冪等性は成果物側の upsert が担う）。
    assert (await run_weekly(test)).status is JobStatus.DONE


async def test_the_lock_is_released_after_a_failed_run(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """⚠️ 失敗でロックが残ると「二度と実行できない period」になる。"""
    test = harness(store, jobs, config, crawl_error=RuntimeError("落ちた"))

    await run_weekly(test)

    assert not test.has_lock(RunType.WEEKLY, WEEKLY_PERIOD)


async def test_the_lock_is_released_even_when_the_audit_log_fails(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """監査ログが書けない場合も、ロックを残さずジョブは `Failed` で残す。"""
    test = harness(store, jobs, config, audit_start_error=RuntimeError("DB が落ちた"))
    prepared = await test.orchestrator.prepare(
        run_type=RunType.WEEKLY, period=WEEKLY_PERIOD, actor=ACTOR
    )

    with pytest.raises(RuntimeError):
        await test.orchestrator.execute(prepared)

    assert not test.has_lock(RunType.WEEKLY, WEEKLY_PERIOD)
    assert test.saved(prepared.job.job_id).status is JobStatus.FAILED
    assert test.crawler.periods == []


async def test_a_different_period_can_run_while_another_is_locked(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config)
    await test.orchestrator.prepare(
        run_type=RunType.WEEKLY, period=WEEKLY_PERIOD, actor=ACTOR
    )

    other = await test.orchestrator.prepare(
        run_type=RunType.WEEKLY, period="2026-W32", actor=ACTOR
    )

    assert other.job.period == "2026-W32"


# =============================================================================
# 7. 監査ログ（T-10 / 設計書 §4.4）
# =============================================================================


async def test_a_successful_run_is_audited_from_start_to_finish(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config)

    job = await run_weekly(test)

    events = test.auditor.events()
    assert events[0] == "run_start"
    assert events[-1] == "run_finish"
    assert events.count("artifact_created") == len(job.artifacts)


async def test_the_audit_carries_the_actor_period_and_pinned_revision(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config)

    await run_weekly(test)

    for record in test.auditor.records:
        assert record.actor == ACTOR
        assert record.period == WEEKLY_PERIOD
        assert record.revision == config.meta.revision


async def test_a_failed_run_is_audited_too(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """⚠️ 失敗した実行こそ記録が要る（何が動いて何が動かなかったか）。"""
    test = harness(store, jobs, config, crawl_error=RuntimeError("落ちた"))

    await run_weekly(test)

    assert test.auditor.events() == ["run_start", "run_finish"]
    assert test.auditor.records[-1].status is JobStatus.FAILED


async def test_the_trigger_is_recorded(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """cron の定期実行と人手の実行を混ぜて読まないため。"""
    test = harness(store, jobs, config)

    job = await run_weekly(test, trigger=JobTrigger.CRON)

    assert job.trigger is JobTrigger.CRON


# =============================================================================
# 8. リトライ（設計書 §8.4 の Failed --> Queued）
# =============================================================================


async def test_a_retry_resumes_from_the_failed_step(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config, render_error=ValueError("今週のポイントが空"))
    failed = await run_weekly(test)
    assert failed.status is JobStatus.FAILED

    # 2回目はレンダラを直した状態で（同じ job_id を使い回す＝§8.4 の矢印）。
    test.weekly._error = None  # type: ignore[attr-defined]
    retried = await test.orchestrator.execute(
        await test.orchestrator.prepare_retry(failed.job_id)
    )

    assert retried.job_id == failed.job_id
    assert retried.status is JobStatus.DONE
    assert retried.attempts == 2
    # 手前の段は回さない（crawl / filter は1回ずつのまま）。
    assert test.crawler.periods == [WEEKLY_PERIOD]
    assert len(test.filterer.calls) == 1


async def test_only_a_failed_job_can_be_retried(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config)
    done = await run_weekly(test)

    with pytest.raises(RunRequestError) as caught:
        await test.orchestrator.prepare_retry(done.job_id)

    assert "failed" in str(caught.value)


async def test_retrying_an_unknown_job_is_refused(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    test = harness(store, jobs, config)

    with pytest.raises(RunRequestError):
        await test.orchestrator.prepare_retry("job_nope")


async def test_a_retry_pins_the_config_again(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """§6.3 は「**実行開始時点**の revision」。リトライは新しい実行。"""
    test = harness(store, jobs, config, crawl_error=RuntimeError("落ちた"))
    failed = await run_weekly(test)

    await test.orchestrator.prepare_retry(failed.job_id)

    assert test.pin.calls == 2


# =============================================================================
# 9. 受付と実行の分離（HTTP が 202 を即返すための形）
# =============================================================================


async def test_prepare_does_not_run_anything_yet(
    store: ArtifactStore, jobs: JobStore, config: IntelligenceConfig
) -> None:
    """`POST /run` は 202 を返してから走る。受付の時点では1段も回さない。"""
    test = harness(store, jobs, config)

    prepared = await test.orchestrator.prepare(
        run_type=RunType.WEEKLY, period=WEEKLY_PERIOD, actor=ACTOR
    )

    assert isinstance(prepared, PreparedRun)
    assert prepared.job.status is JobStatus.QUEUED
    assert test.crawler.periods == []
    # 受付の時点で照会できる（フロントがポーリングを始められる）。
    assert test.saved(prepared.job.job_id).status is JobStatus.QUEUED
    # ロックは既に握っている（409 は 202 を返す前に判定する）。
    assert test.has_lock(RunType.WEEKLY, WEEKLY_PERIOD)
