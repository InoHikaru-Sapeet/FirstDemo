"""通し実行 CLI（T-45 → T-26 で Orchestrator の薄い皮へ置換）。

⚠️ **配線のテストはここには無い。** crawl → filter → render の順序・成果物の
受け渡し・再開ポイント・二重起動防止・監査ログは
`tests/application/test_run_orchestrator.py` が担当する（T-26 で
`RunOrchestrator` へ移したため）。**同じことを2箇所で検査しない。**

ここが見るのは CLI に残った3つだけ:

1. **引数の解釈**（`--period` / `--from` / `--retry`）と PERIOD の解決（§13.1）
2. **進捗と結果の表示**（job_id・実行ステップ・生成物）
3. **失敗の見せ方と終了コード**（どの段で・どの例外か。T-15 の6分類がそのまま
   読めること）
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from adapter.cli import run_pipeline
from adapter.cli.run_pipeline import (
    CLI_ACTOR,
    EXIT_ALREADY_RUNNING,
    EXIT_FAILED,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    FAILURE_HINTS,
    failure_hint,
    hint_for_error_type,
    main,
    resolve_period,
    run,
)
from adapter.llm import (
    AIOutputParseError,
    AIProcessError,
    AIProtocolError,
    AIResponseError,
    AITimeoutError,
    AIUnavailableError,
)
from adapter.storage.artifact_store import ArtifactStore
from adapter.storage.job_store import RunAlreadyInProgressError
from application.usecases.crawl import SearchNotPerformedError
from application.usecases.filter import RawArticlesNotFoundError
from application.usecases.run_orchestrator import (
    ConfigPinError,
    Pipeline,
    PreparedRun,
    RunPreconditionError,
    RunRequestError,
)
from enterprise.entities.period import parse_period
from enterprise.entities.run_job import (
    JobStatus,
    JobTrigger,
    ResumePoint,
    RunJob,
    RunType,
    Step,
)

WEEKLY_PERIOD = "2026-W31"
MONTHLY_PERIOD = "2026-07"
JOB_ID = "job_20260817-080000-abc123"
NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)


# --- モック（Orchestrator は本物を使わない）----------------------------------


def make_job(**overrides: Any) -> RunJob:
    base: dict[str, Any] = {
        "job_id": JOB_ID,
        "type": RunType.WEEKLY,
        "period": WEEKLY_PERIOD,
        "actor": CLI_ACTOR,
        "revision": 3,
        "trigger": JobTrigger.CLI,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return RunJob.model_validate(base | overrides)


def make_prepared(store: ArtifactStore, job: RunJob) -> PreparedRun:
    """`run()` が触るのは `job` / `period` / `pipeline.store.root` だけ。"""
    dummy = object()
    pipeline = Pipeline(
        config=dummy,  # type: ignore[arg-type]
        store=store,
        crawler=dummy,  # type: ignore[arg-type]
        filterer=dummy,  # type: ignore[arg-type]
        reports=dummy,  # type: ignore[arg-type]
        weekly_renderer=dummy,  # type: ignore[arg-type]
        monthly_renderer=dummy,  # type: ignore[arg-type]
    )
    return PreparedRun(
        job=job,
        pipeline=pipeline,
        period=parse_period(job.period),
        lock=dummy,  # type: ignore[arg-type]
    )


@dataclass
class FakeOrchestrator:
    """`prepare()` / `prepare_retry()` / `execute()` だけを持つ代役。"""

    store: ArtifactStore
    finished: RunJob | None = None
    prepare_error: Exception | None = None
    prepare_calls: list[dict[str, Any]] = field(default_factory=list)
    retry_calls: list[str] = field(default_factory=list)

    async def prepare(self, **kwargs: Any) -> PreparedRun:
        self.prepare_calls.append(kwargs)
        if self.prepare_error:
            raise self.prepare_error
        return make_prepared(
            self.store,
            make_job(
                type=kwargs["run_type"],
                period=kwargs["period"],
                resume_from=kwargs["resume_from"],
                start_step=Step(
                    kwargs["resume_from"].value
                    if kwargs["resume_from"] is not ResumePoint.AUTO
                    else "crawl"
                ),
                actor=kwargs["actor"],
                trigger=kwargs["trigger"],
            ),
        )

    async def prepare_retry(
        self, job_id: str, *, actor: str | None = None
    ) -> PreparedRun:
        self.retry_calls.append(job_id)
        if self.prepare_error:
            raise self.prepare_error
        return make_prepared(self.store, make_job(job_id=job_id))

    async def execute(self, prepared: PreparedRun) -> RunJob:
        return self.finished or prepared.job.model_copy(
            update={"status": JobStatus.DONE}
        )


@dataclass
class Lines:
    lines: list[str] = field(default_factory=list)

    def out(self, line: str) -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path)


def ticking_clock(step: float = 1.5) -> Any:
    ticks = iter(range(0, 10_000))
    return lambda: next(ticks) * step


# --- 表示（無言で待たせない）--------------------------------------------------


async def test_the_header_names_the_job_the_period_and_the_pinned_revision(
    store: ArtifactStore,
) -> None:
    """AI 呼び出しは1回数分・週次フルは90〜100分（実測）。何が走るかを先に見せる。"""
    printed = Lines()
    orchestrator = FakeOrchestrator(store)
    prepared = make_prepared(store, make_job())

    code = await run(orchestrator, prepared, out=printed.out)  # type: ignore[arg-type]

    assert code == EXIT_OK
    assert f"job_id      : {JOB_ID}" in printed.text
    assert "revision=3（実行中は固定）" in printed.text
    assert "対象期間    : 2026-07-27 〜 2026-08-02" in printed.text
    assert "実行ステップ: crawl → filter → render" in printed.text
    assert str(store.root) in printed.text


async def test_a_resumed_run_says_that_the_earlier_steps_are_skipped(
    store: ArtifactStore,
) -> None:
    printed = Lines()
    prepared = make_prepared(
        store, make_job(start_step=Step.RENDER, resume_from=ResumePoint.RENDER)
    )

    await run(FakeOrchestrator(store), prepared, out=printed.out)  # type: ignore[arg-type]

    assert "実行ステップ: render" in printed.text
    assert "手前のステップは実行しません" in printed.text


async def test_the_elapsed_time_is_reported(store: ArtifactStore) -> None:
    printed = Lines()

    await run(
        FakeOrchestrator(store),  # type: ignore[arg-type]
        make_prepared(store, make_job()),
        out=printed.out,
        clock=ticking_clock(step=1.5),
    )

    assert "=== 完了（1.5秒）===" in printed.text


async def test_the_generated_paths_are_listed(store: ArtifactStore) -> None:
    printed = Lines()
    done = make_job(status=JobStatus.RENDERING).model_copy(
        update={"status": JobStatus.DONE, "artifacts": ["/a/x.xlsx", "/a/y.html"]}
    )

    await run(
        FakeOrchestrator(store, finished=done),  # type: ignore[arg-type]
        make_prepared(store, make_job()),
        out=printed.out,
    )

    assert "--- 生成物 2 件 ---" in printed.text
    assert "/a/x.xlsx" in printed.text
    assert "/a/y.html" in printed.text


# --- 失敗の見せ方（どの段で・どの例外か）--------------------------------------


def failed_job(error_type: str, message: str, *, step: Step = Step.CRAWL) -> RunJob:
    return make_job().model_copy(
        update={
            "status": JobStatus.FAILED,
            "failed_step": step,
            "error_type": error_type,
            "error_message": message,
        }
    )


async def test_a_failure_names_the_step_and_the_exception_class(
    store: ArtifactStore,
) -> None:
    """⚠️ 型名をそのまま出す（言い換えると T-15 の6分類が潰れる）。"""
    printed = Lines()
    job = failed_job("AITimeoutError", "1800秒で打ち切りました", step=Step.CRAWL)

    code = await run(
        FakeOrchestrator(store, finished=job),  # type: ignore[arg-type]
        make_prepared(store, make_job()),
        out=printed.out,
    )

    assert code == EXIT_FAILED
    assert "crawl で失敗しました" in printed.text
    assert "AITimeoutError" in printed.text
    assert "1800秒で打ち切りました" in printed.text
    assert "T-15 の6分類" in printed.text


async def test_a_failure_tells_how_to_retry(store: ArtifactStore) -> None:
    """設計書 §8.4 の `Failed → Queued`。次に打つコマンドを出す。"""
    printed = Lines()
    job = failed_job("ValueError", "今週のポイントが空です", step=Step.RENDER)

    await run(
        FakeOrchestrator(store, finished=job),  # type: ignore[arg-type]
        make_prepared(store, make_job()),
        out=printed.out,
    )

    assert f'make run-weekly PERIOD={WEEKLY_PERIOD} ARGS="--retry {JOB_ID}"' in (
        printed.text
    )


async def test_the_artifacts_written_before_the_failure_are_reported(
    store: ArtifactStore,
) -> None:
    """どこまで進んだかが分かると、`--retry` / `--from` で再開できる。"""
    printed = Lines()
    job = failed_job("AIProcessError", "未ログイン", step=Step.FILTER).model_copy(
        update={"artifacts": ["/a/raw_articles.json"]}
    )

    await run(
        FakeOrchestrator(store, finished=job),  # type: ignore[arg-type]
        make_prepared(store, make_job()),
        out=printed.out,
    )

    assert "失敗するまでに書き出した成果物 1 件" in printed.text
    assert "/a/raw_articles.json" in printed.text


async def test_a_non_ai_failure_is_not_labelled_as_an_ai_failure(
    store: ArtifactStore,
) -> None:
    """AI の失敗と混ぜて読まない（原因も対処も別）。"""
    printed = Lines()
    job = failed_job("ValueError", "今週のポイントが空です", step=Step.RENDER)

    await run(
        FakeOrchestrator(store, finished=job),  # type: ignore[arg-type]
        make_prepared(store, make_job()),
        out=printed.out,
    )

    assert "T-15 の6分類" not in printed.text


# --- 失敗の分類表 -------------------------------------------------------------


def ai_errors() -> list[Exception]:
    """T-15 の6分類（この6つで打ち止め＝`AIClientError` の直接の子）。"""
    return [
        AIUnavailableError("claude が見つかりません", command="claude"),
        AITimeoutError("1800秒で打ち切りました", timeout_seconds=1800.0),
        AIProcessError(
            "終了コード 1: not logged in", exit_code=1, stderr="not logged in"
        ),
        AIProtocolError("封筒として読めません", stdout="{", stderr=""),
        AIResponseError(
            "permission_denials: WebSearch", reasons=["WebSearch"], stderr=""
        ),
        AIOutputParseError("スキーマに合いません", attempts=3, issues=[], payload="{}"),
    ]


def test_every_ai_error_class_has_a_hint() -> None:
    """6分類のどれが起きても「何が起きたか」が読める（表の抜けを検出する）。"""
    for error in ai_errors():
        assert failure_hint(error), type(error).__name__
        assert hint_for_error_type(type(error).__name__), type(error).__name__


def test_the_non_ai_failures_have_their_own_hints() -> None:
    assert "検索" in (failure_hint(SearchNotPerformedError("", requested=0)) or "")
    assert "crawl" in (failure_hint(RawArticlesNotFoundError("")) or "")


def test_the_run_specific_failures_have_hints() -> None:
    """T-26 で増えた2つ（再開ポイントの不一致・config の固定失敗）。"""
    assert hint_for_error_type(RunPreconditionError.__name__)
    assert hint_for_error_type(ConfigPinError.__name__)


def test_the_hint_table_has_no_duplicated_rows() -> None:
    """同じ型を2行書くと後ろの行が死ぬ（最初に当たった行を使うため）。"""
    kinds = [kind for kind, _ in FAILURE_HINTS]
    assert len(kinds) == len(set(kinds))


def test_an_unknown_error_type_has_no_hint() -> None:
    assert hint_for_error_type("SomethingElseError") is None
    assert hint_for_error_type(None) is None


# --- period の解決（仕様書 §13.1）---------------------------------------------


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 8, 16), "2026-W33"),  # 日曜（ISO週は月曜始まり）
        (date(2026, 8, 17), "2026-W34"),  # 翌日の月曜は次の週
        (date(2027, 1, 1), "2026-W53"),  # 年をまたぐ ISO 週
    ],
)
def test_the_weekly_period_defaults_to_the_current_iso_week(
    today: date, expected: str
) -> None:
    assert resolve_period(RunType.WEEKLY, today=today) == expected


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 8, 1), "2026-07"),  # 月初（cron は毎月1日 09:00）
        (date(2026, 8, 16), "2026-07"),
        (date(2026, 1, 5), "2025-12"),  # 年をまたぐ
    ],
)
def test_the_monthly_period_defaults_to_the_previous_month(
    today: date, expected: str
) -> None:
    assert resolve_period(RunType.MONTHLY, today=today) == expected


# --- main（引数の解釈と終了コード）--------------------------------------------


@pytest.fixture
def fake_build(
    monkeypatch: pytest.MonkeyPatch, store: ArtifactStore
) -> FakeOrchestrator:
    orchestrator = FakeOrchestrator(store)
    monkeypatch.setattr(
        run_pipeline, "build_orchestrator", lambda *a, **k: orchestrator
    )
    return orchestrator


def test_main_hands_the_arguments_to_the_orchestrator(
    fake_build: FakeOrchestrator, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["weekly", "--period", WEEKLY_PERIOD, "--from", "filter"]) == EXIT_OK

    call = fake_build.prepare_calls[0]
    assert call["run_type"] is RunType.WEEKLY
    assert call["period"] == WEEKLY_PERIOD
    assert call["resume_from"] is ResumePoint.FILTER
    assert call["trigger"] is JobTrigger.CLI
    # ⚠️ cron（`system:`）の実行と混ぜない（監査で区別が付かなくなる）。
    assert call["actor"] == CLI_ACTOR
    capsys.readouterr()


def test_main_resolves_the_period_when_it_is_omitted(
    fake_build: FakeOrchestrator, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["monthly"]) == EXIT_OK

    period = fake_build.prepare_calls[0]["period"]
    assert len(period) == 7 and period[4] == "-"  # YYYY-MM（前月）
    capsys.readouterr()


def test_main_defaults_to_starting_from_the_crawl(
    fake_build: FakeOrchestrator, capsys: pytest.CaptureFixture[str]
) -> None:
    """§3.3「`resume_from` 省略時は crawl から」。"""
    assert main(["weekly", "--period", WEEKLY_PERIOD]) == EXIT_OK

    assert fake_build.prepare_calls[0]["resume_from"] is ResumePoint.CRAWL
    capsys.readouterr()


def test_main_routes_retry_to_the_orchestrator(
    fake_build: FakeOrchestrator, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["weekly", "--retry", JOB_ID]) == EXIT_OK

    assert fake_build.retry_calls == [JOB_ID]
    assert fake_build.prepare_calls == []
    capsys.readouterr()


def test_main_reports_a_bad_request_with_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    store: ArtifactStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """入力の不備は「実行して失敗した」と別の終了コードで返す。"""
    orchestrator = FakeOrchestrator(
        store, prepare_error=RunRequestError("weekly に 2026-07 は使えません")
    )
    monkeypatch.setattr(
        run_pipeline, "build_orchestrator", lambda *a, **k: orchestrator
    )

    assert main(["weekly", "--period", MONTHLY_PERIOD]) == EXIT_INVALID_INPUT
    assert "中止しました" in capsys.readouterr().out


def test_main_reports_a_double_start_with_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    store: ArtifactStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """⚠️ cron から重ねて叩かれたときに「失敗」と読めてしまわないよう分ける。"""
    orchestrator = FakeOrchestrator(
        store,
        prepare_error=RunAlreadyInProgressError(
            RunType.WEEKLY, WEEKLY_PERIOD, "job_running"
        ),
    )
    monkeypatch.setattr(
        run_pipeline, "build_orchestrator", lambda *a, **k: orchestrator
    )

    assert main(["weekly", "--period", WEEKLY_PERIOD]) == EXIT_ALREADY_RUNNING
    assert "job_running" in capsys.readouterr().out


def test_main_reports_a_config_that_cannot_be_pinned(
    monkeypatch: pytest.MonkeyPatch,
    store: ArtifactStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    orchestrator = FakeOrchestrator(
        store, prepare_error=ConfigPinError("revision=3 のスナップショットがありません")
    )
    monkeypatch.setattr(
        run_pipeline, "build_orchestrator", lambda *a, **k: orchestrator
    )

    assert main(["weekly", "--period", WEEKLY_PERIOD]) == EXIT_INVALID_INPUT
    assert "スナップショット" in capsys.readouterr().out


def test_main_returns_failed_when_the_run_fails(
    monkeypatch: pytest.MonkeyPatch,
    store: ArtifactStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    orchestrator = FakeOrchestrator(
        store, finished=failed_job("AIProcessError", "未ログイン")
    )
    monkeypatch.setattr(
        run_pipeline, "build_orchestrator", lambda *a, **k: orchestrator
    )

    assert main(["weekly", "--period", WEEKLY_PERIOD]) == EXIT_FAILED
    assert "crawl で失敗しました" in capsys.readouterr().out


def test_the_exit_codes_are_distinct() -> None:
    """cron のログから「何が起きたか」を終了コードだけで切り分けられること。"""
    codes = [EXIT_OK, EXIT_FAILED, EXIT_INVALID_INPUT, EXIT_ALREADY_RUNNING]
    assert len(set(codes)) == len(codes)
