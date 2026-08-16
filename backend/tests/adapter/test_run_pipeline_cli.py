"""通し実行 CLI（T-45 ／ 設計書 §8.2・§13.1）。

**配線のテスト**。ワーカー（T-16 / T-21 / T-22 / T-24 / T-25）はすべてモックで、
実際の `claude` は起動しない（T-45 の完了条件）。重点:

- 3ステップが **crawl → filter → render の順**に呼ばれ、**戻り値が次段へ渡る**
- `--from filter` / `--from render` が**手前のステップを実行しない**
- **`run_id` が `FilterWorker.run()` へ渡る**（T-44 の履歴退避が効く形）
- 失敗時は「**どのステップで・どの例外か**」を出して**非0**で終わる。
  T-15 の例外6分類がそのまま読める
- PERIOD 省略時の解決（当週 / 前月）と、**種別と表記の食い違いを受け付けない**こと
- 各ステップの**開始・終了・所要時間**が出る（AI 呼び出しは1回数分＝T-15 備考）
"""

import copy
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from adapter.cli.run_pipeline import (
    EXIT_FAILED,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    FAILURE_HINTS,
    Kind,
    Pipeline,
    PipelineError,
    Step,
    failure_hint,
    main,
    new_run_id,
    requested_period,
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
from application.usecases.crawl import SearchNotPerformedError
from application.usecases.filter import RawArticlesNotFoundError
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.narrative import (
    MonthlyNarrativeDocument,
    WeeklyNarrativeDocument,
    dump_narrative,
)
from enterprise.entities.period import Period, PeriodError, parse_period

INITIAL_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)

WEEKLY_PERIOD = "2026-W31"
MONTHLY_PERIOD = "2026-07"
RUN_ID = "cli-20260816-090000"


@pytest.fixture(scope="session")
def initial_raw() -> dict[str, Any]:
    return json.loads(INITIAL_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def config(initial_raw: dict[str, Any]) -> IntelligenceConfig:
    return IntelligenceConfig.model_validate(copy.deepcopy(initial_raw))


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path)


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
        return FakeCrawlResult(
            path=self._store.raw_articles_path(period), article_count=12
        )


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


@dataclass
class Harness:
    """1回の実行に必要なモック一式と、その出力。"""

    pipeline: Pipeline
    crawler: FakeCrawler
    filterer: FakeFilterer
    reports: FakeReports
    weekly: FakeRenderer
    monthly: FakeRenderer
    lines: list[str] = field(default_factory=list)

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    def out(self, line: str) -> None:
        self.lines.append(line)


def harness(
    store: ArtifactStore,
    config: IntelligenceConfig,
    *,
    crawl_error: Exception | None = None,
    filter_error: Exception | None = None,
    render_error: Exception | None = None,
    exclusions: int = 2,
) -> Harness:
    crawler = FakeCrawler(store, error=crawl_error)
    filterer = FakeFilterer(store, exclusions=exclusions, error=filter_error)
    reports = FakeReports(store=store)
    weekly = FakeRenderer(store.root / "weekly.html", error=render_error)
    monthly = FakeRenderer(store.root / "monthly.html", error=render_error)
    return Harness(
        pipeline=Pipeline(
            config=config,
            store=store,
            crawler=crawler,
            filterer=filterer,
            reports=reports,
            weekly_renderer=weekly,
            monthly_renderer=monthly,
        ),
        crawler=crawler,
        filterer=filterer,
        reports=reports,
        weekly=weekly,
        monthly=monthly,
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


def ticking_clock(step: float = 1.5) -> Any:
    """呼ぶたびに `step` 秒進む時計（所要時間の表示を固定するため）。"""
    ticks = iter(range(0, 10_000))
    return lambda: next(ticks) * step


# --- 通し実行 -----------------------------------------------------------------


async def test_the_three_steps_run_in_the_order_of_the_handover_table(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """設計書 §8.2 の順（crawl → filter → render）で1回ずつ呼ばれる。"""
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config)
    write_narrative(store, period)

    code = await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    assert code == EXIT_OK
    assert test.crawler.periods == [WEEKLY_PERIOD]
    assert test.filterer.calls == [(WEEKLY_PERIOD, RUN_ID)]
    assert len(test.weekly.calls) == 1
    assert test.monthly.calls == []
    assert test.output.index("crawl 開始") < test.output.index("filter 開始")
    assert test.output.index("filter 開始") < test.output.index("render 開始")


async def test_the_run_id_reaches_the_filter_worker(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """T-44 は `run_id=None` だと narrative を退避しない。渡し忘れを固定する。"""
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config)
    write_narrative(store, period)

    await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    assert test.filterer.calls[0][1] == RUN_ID
    assert test.reports.weekly_calls[0]["run_id"] == RUN_ID
    assert test.weekly.calls[0]["run_id"] == RUN_ID


async def test_the_pinned_revision_goes_to_every_writer(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """退避先の名前は `{revision}_{run_id}`。全ステップで同じ revision を使う。"""
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config)
    write_narrative(store, period)

    await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    revision = config.meta.revision
    assert test.reports.weekly_calls[0]["revision"] == revision
    assert test.weekly.calls[0]["revision"] == revision
    assert f"revision={revision}" in test.output


async def test_the_weekly_filter_writes_the_articles_and_the_exclusions(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config, exclusions=3)
    write_narrative(store, period)

    await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    call = test.reports.weekly_calls[0]
    assert call["period"] == WEEKLY_PERIOD
    assert call["articles"] == [{"タイトル": "filter が返した記事"}]
    assert len(call["exclusions"]) == 3
    assert test.reports.monthly_calls == []


async def test_a_monthly_run_writes_the_cases_and_appends_to_the_weekly_book(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """除外ログは月次ブックではなく週次ブックへ積む（§8.1・T-21 備考）。"""
    period = parse_period(MONTHLY_PERIOD)
    test = harness(store, config, exclusions=2)
    write_narrative(store, period)

    code = await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    assert code == EXIT_OK
    assert test.reports.monthly_calls[0]["cases"] == [
        {"No": 1, "タイトル": "filter が返した事例"}
    ]
    assert test.reports.weekly_calls == []
    assert len(test.reports.exclusion_calls[0]["exclusions"]) == 2
    assert len(test.monthly.calls) == 1
    assert test.weekly.calls == []


async def test_a_monthly_run_without_exclusions_leaves_the_weekly_book_alone(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """書くものが無いのに週次ブックを上書きしない（退避の世代を無駄に消費する）。"""
    period = parse_period(MONTHLY_PERIOD)
    test = harness(store, config, exclusions=0)
    write_narrative(store, period)

    await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    assert test.reports.exclusion_calls == []


async def test_the_render_reads_the_files_instead_of_the_filter_result(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """§8.2 の受け渡しはファイル経由。filter の戻り値を直接は渡さない。"""
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config)
    test.reports.weekly_rows = [{"タイトル": "xlsx から読み戻した記事"}]
    write_narrative(store, period)

    await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    assert test.weekly.calls[0]["articles"] == [{"タイトル": "xlsx から読み戻した記事"}]


async def test_the_narrative_file_is_handed_to_the_renderer(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """生成テキスト（T-44）は narrative ファイルから読んでレンダラへ渡す。"""
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config)
    store.write_text(
        store.narrative_path(period.text),
        dump_narrative(
            WeeklyNarrativeDocument(
                period=period.text,
                point_of_week_sentences=["今週はエージェントの週だった。"],
                insights={"https://example.com/1": "自社では検証から始める。"},
            )
        ),
    )

    await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    narrative = test.weekly.calls[0]["narrative"]
    assert narrative.point_of_week == "今週はエージェントの週だった。"
    assert narrative.insights == {"https://example.com/1": "自社では検証から始める。"}


async def test_the_generated_paths_are_listed_at_the_end(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config)
    write_narrative(store, period)

    await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    for path in (
        store.raw_articles_path(WEEKLY_PERIOD),
        store.weekly_report_path(),
        store.validation_path(WEEKLY_PERIOD),
        store.narrative_path(WEEKLY_PERIOD),
        store.root / "weekly.html",
    ):
        assert str(path) in test.output


async def test_each_step_reports_its_start_and_its_duration(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """AI 呼び出しは1回数分（T-15 実測）。無言で待たせない。"""
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config)
    write_narrative(store, period)

    await run(
        test.pipeline,
        period,
        run_id=RUN_ID,
        out=test.out,
        clock=ticking_clock(step=1.5),
    )

    assert "[1/3] crawl 開始 …" in test.output
    assert "[1/3] crawl 完了（1.5秒）" in test.output
    assert "[3/3] render 完了（1.5秒）" in test.output


# --- 再開ポイント（--from。§14 の簡易版）--------------------------------------


async def test_from_filter_does_not_crawl_again(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config)
    write_narrative(store, period)

    code = await run(
        test.pipeline, period, run_id=RUN_ID, start_from=Step.FILTER, out=test.out
    )

    assert code == EXIT_OK
    assert test.crawler.periods == []
    assert test.filterer.calls == [(WEEKLY_PERIOD, RUN_ID)]
    assert len(test.weekly.calls) == 1
    assert "[1/2] filter 開始 …" in test.output


async def test_from_render_runs_neither_the_crawl_nor_the_filter(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config)
    write_narrative(store, period)

    code = await run(
        test.pipeline, period, run_id=RUN_ID, start_from=Step.RENDER, out=test.out
    )

    assert code == EXIT_OK
    assert test.crawler.periods == []
    assert test.filterer.calls == []
    assert len(test.weekly.calls) == 1


async def test_from_render_without_a_narrative_file_fails_and_says_what_to_do(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """生成テキストは filter が書く（T-44）。無いまま描くと本文が抜ける。"""
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config)

    code = await run(
        test.pipeline, period, run_id=RUN_ID, start_from=Step.RENDER, out=test.out
    )

    assert code == EXIT_FAILED
    assert test.weekly.calls == []
    assert "render で失敗しました" in test.output
    assert "PipelineError" in test.output
    assert "--from filter" in test.output


async def test_a_narrative_from_another_period_is_refused(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """本文だけ先週のままの HTML を配信に回さない（T-44 の read 側ガード）。"""
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config)
    store.write_text(
        store.narrative_path(period.text),
        dump_narrative(WeeklyNarrativeDocument(period="2026-W30")),
    )

    code = await run(
        test.pipeline, period, run_id=RUN_ID, start_from=Step.RENDER, out=test.out
    )

    assert code == EXIT_FAILED
    assert "DocumentParseError" in test.output
    assert test.weekly.calls == []


# --- 失敗の扱い（どのステップで・どの例外か）----------------------------------


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


@pytest.mark.parametrize("error", ai_errors(), ids=lambda error: type(error).__name__)
async def test_an_ai_failure_names_the_step_and_the_exception_class(
    store: ArtifactStore, config: IntelligenceConfig, error: Exception
) -> None:
    """T-15 の6分類がそのまま読める形で出る（言い換えて潰さない）。"""
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config, crawl_error=error)

    code = await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    assert code == EXIT_FAILED
    assert "crawl で失敗しました" in test.output
    assert type(error).__name__ in test.output
    assert str(error) in test.output
    assert "T-15 の6分類" in test.output


def test_every_ai_error_class_has_a_hint() -> None:
    """6分類のどれが起きても「何が起きたか」が読める（表の抜けを検出する）。"""
    for error in ai_errors():
        assert failure_hint(error), type(error).__name__


def test_the_non_ai_failures_have_their_own_hints() -> None:
    """AI の失敗と混ぜて読まないための行（原因も対処も別）。"""
    assert "検索" in (failure_hint(SearchNotPerformedError("", requested=0)) or "")
    assert "crawl" in (failure_hint(RawArticlesNotFoundError("")) or "")


def test_the_hint_table_has_no_duplicated_rows() -> None:
    """同じ型を2行書くと後ろの行が死ぬ（最初に当たった行を使うため）。"""
    kinds = [kind for kind, _ in FAILURE_HINTS]
    assert len(kinds) == len(set(kinds))


async def test_a_failing_filter_does_not_reach_the_render(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    period = parse_period(WEEKLY_PERIOD)
    test = harness(
        store,
        config,
        filter_error=AITimeoutError("600秒で打ち切り", timeout_seconds=600.0),
    )
    write_narrative(store, period)

    code = await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    assert code == EXIT_FAILED
    assert "filter で失敗しました" in test.output
    assert test.weekly.calls == []


async def test_the_artifacts_written_before_the_failure_are_reported(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """どこまで進んだかが分かると、次に `--from` で再開できる。"""
    period = parse_period(WEEKLY_PERIOD)
    test = harness(
        store,
        config,
        filter_error=AIProcessError("未ログイン", exit_code=1, stderr="not logged in"),
    )

    code = await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    assert code == EXIT_FAILED
    assert str(store.raw_articles_path(WEEKLY_PERIOD)) in test.output


async def test_a_render_failure_is_reported_too(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """レンダラの例外（`point_of_week_required` で今週のポイントが空 等）。"""
    period = parse_period(WEEKLY_PERIOD)
    test = harness(store, config, render_error=ValueError("今週のポイントが空です"))
    write_narrative(store, period)

    code = await run(test.pipeline, period, run_id=RUN_ID, out=test.out)

    assert code == EXIT_FAILED
    assert "render で失敗しました" in test.output
    assert "ValueError" in test.output


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
    assert resolve_period(Kind.WEEKLY, today=today) == expected


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
    assert resolve_period(Kind.MONTHLY, today=today) == expected


def test_an_explicit_period_must_match_the_kind() -> None:
    """`make run-weekly PERIOD=2026-07` は月刊の成果物を上書きしてしまう。"""
    with pytest.raises(PipelineError):
        requested_period(Kind.WEEKLY, MONTHLY_PERIOD)
    with pytest.raises(PipelineError):
        requested_period(Kind.MONTHLY, WEEKLY_PERIOD)


def test_an_explicit_period_must_exist() -> None:
    """表記が合っているだけの期間は通さない（実日付へ開けるかまで見る）。"""
    assert requested_period(Kind.WEEKLY, WEEKLY_PERIOD).text == WEEKLY_PERIOD
    with pytest.raises(PeriodError):
        requested_period(Kind.MONTHLY, "2026-13")
    with pytest.raises(PeriodError):
        requested_period(Kind.WEEKLY, "2026/W31")


def test_main_refuses_a_period_that_does_not_match_the_kind(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """入力の不備は「実行できなかった」＝失敗と別の終了コードで返す。"""
    assert main(["weekly", "--period", MONTHLY_PERIOD]) == EXIT_INVALID_INPUT
    assert "中止しました" in capsys.readouterr().out


def test_main_refuses_an_unparsable_period(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["monthly", "--period", "2026-13"]) == EXIT_INVALID_INPUT
    assert "中止しました" in capsys.readouterr().out


# --- run_id ------------------------------------------------------------------


def test_the_run_id_can_name_a_history_generation(
    store: ArtifactStore, tmp_path: Path
) -> None:
    """退避先は `_history/{period}/{revision}_{run_id}/`。区切り文字を含めない。"""
    from datetime import datetime

    run_id = new_run_id(datetime(2026, 8, 16, 9, 0, 0))
    target = store.root / "weekly_report.xlsx"
    store.write_text(target, "x")

    archived = store.archive(target, period=WEEKLY_PERIOD, revision=3, run_id=run_id)

    assert archived is not None
    assert archived.parent.name == f"3_{run_id}"
    assert run_id == "cli-20260816-090000"
