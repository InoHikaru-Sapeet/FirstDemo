"""ジョブの状態機械（T-26。設計書 §8.4）。

**設計書 §8.4 の状態遷移図との1:1 突き合わせ**が主眼。図に無い矢印を後から
足せてしまうと、「実行中のまま Done になる」「Done から走り直す」といった、
記録を見ても何が起きたか分からない状態が生まれる。
"""

from datetime import UTC, datetime

import pytest

from enterprise.entities.period import PeriodError
from enterprise.entities.run_job import (
    ALLOWED_TRANSITIONS,
    STEP_ORDER,
    InvalidTransitionError,
    JobStatus,
    JobTrigger,
    ResumePoint,
    RunJob,
    RunType,
    Step,
    new_job_id,
    period_matches_type,
    run_type_of,
    steps_from,
)

NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 17, 9, 30, tzinfo=UTC)

WEEKLY_PERIOD = "2026-W31"
MONTHLY_PERIOD = "2026-07"


def make_job(**overrides: object) -> RunJob:
    base: dict[str, object] = {
        "job_id": "job_20260817-080000-abc123",
        "type": RunType.WEEKLY,
        "period": WEEKLY_PERIOD,
        "actor": "admin:usr_1",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return RunJob.model_validate(base | overrides)


# =============================================================================
# 1. 遷移表が設計書 §8.4 と一致しているか
# =============================================================================

# 設計書 §8.4 の mermaid をそのまま書き写したもの。
#   [*] --> Queued
#   Queued --> Crawling  : resume_from<=crawl
#   Queued --> Filtering : resume_from=filter (raw存在)
#   Queued --> Rendering : resume_from=render (xlsx存在)
#   Crawling --> Filtering / Crawling --> Failed
#   Filtering --> Rendering / Filtering --> Failed
#   Rendering --> Done / Rendering --> Failed
#   Failed --> Queued
#   Done --> [*]
DESIGN_8_4: dict[JobStatus, set[JobStatus]] = {
    JobStatus.QUEUED: {
        JobStatus.CRAWLING,
        JobStatus.FILTERING,
        JobStatus.RENDERING,
        # 図に無い1本。受付の後・1段目に入る前に落ちた場合（前段成果物が無い等）を
        # 「実行中でも完了でもない」まま残さないために足してある。
        JobStatus.FAILED,
    },
    JobStatus.CRAWLING: {JobStatus.FILTERING, JobStatus.FAILED},
    JobStatus.FILTERING: {JobStatus.RENDERING, JobStatus.FAILED},
    JobStatus.RENDERING: {JobStatus.DONE, JobStatus.FAILED},
    JobStatus.FAILED: {JobStatus.QUEUED},
    JobStatus.DONE: set(),
}


@pytest.mark.parametrize("status", sorted(JobStatus, key=lambda s: s.value))
def test_the_transition_table_matches_design_8_4(status: JobStatus) -> None:
    """⚠️ **設計書 §8.4 の確定値。ここを緩めない。**"""
    assert set(ALLOWED_TRANSITIONS[status]) == DESIGN_8_4[status]


def test_every_status_has_a_row() -> None:
    """黙って「どこへでも行ける」状態を作らない。"""
    assert set(ALLOWED_TRANSITIONS) == set(JobStatus)


def test_done_is_terminal() -> None:
    """同じ period をもう一度回すのは**新しいジョブ**（冪等性は成果物側の upsert）。"""
    done = make_job(status=JobStatus.DONE)

    assert done.is_terminal
    for status in JobStatus:
        with pytest.raises(InvalidTransitionError):
            done.transition_to(status, at=LATER)


def test_a_step_cannot_be_skipped() -> None:
    """Crawling から Rendering へ飛ばない（filter を通っていない成果物を描かない）。"""
    job = make_job(status=JobStatus.CRAWLING)

    with pytest.raises(InvalidTransitionError) as caught:
        job.transition_to(JobStatus.RENDERING, at=LATER)

    assert "§8.4" in str(caught.value)
    assert caught.value.current is JobStatus.CRAWLING


def test_a_failed_job_goes_back_to_queued_not_straight_into_a_step() -> None:
    """`Failed --> Queued` の1本だけ（§8.4）。失敗から直接走り出さない。"""
    failed = make_job(status=JobStatus.FAILED)

    assert failed.transition_to(JobStatus.QUEUED, at=LATER).status is JobStatus.QUEUED
    with pytest.raises(InvalidTransitionError):
        failed.transition_to(JobStatus.FILTERING, at=LATER)


def test_reaching_a_terminal_status_stamps_the_finish_time() -> None:
    job = make_job(status=JobStatus.RENDERING)

    assert job.transition_to(JobStatus.DONE, at=LATER).finished_at == LATER
    assert job.transition_to(JobStatus.FAILED, at=LATER).finished_at == LATER


# =============================================================================
# 2. 段の進み方
# =============================================================================


def test_entering_a_step_uses_the_matching_status() -> None:
    job = make_job()

    crawling = job.entering(Step.CRAWL, at=LATER)
    filtering = crawling.entering(Step.FILTER, at=LATER)

    assert crawling.status is JobStatus.CRAWLING
    assert filtering.status is JobStatus.FILTERING
    assert filtering.is_running


def test_a_resumed_job_enters_its_start_step_directly() -> None:
    """`Queued --> Rendering`（§8.4 の3本目の矢印）。"""
    job = make_job(start_step=Step.RENDER, resume_from=ResumePoint.RENDER)

    assert job.remaining_steps == (Step.RENDER,)
    assert job.entering(Step.RENDER, at=LATER).status is JobStatus.RENDERING


def test_completing_a_step_records_its_artifacts() -> None:
    job = make_job().entering(Step.CRAWL, at=LATER)

    done = job.completing(Step.CRAWL, artifacts=["/a/raw.json"], at=LATER)

    assert done.completed_steps == [Step.CRAWL]
    assert done.artifacts == ["/a/raw.json"]
    # 状態は次の段に入るときに進める（完了だけでは動かさない）。
    assert done.status is JobStatus.CRAWLING


def test_remaining_steps_skips_what_is_already_done() -> None:
    job = make_job().completing(Step.CRAWL, artifacts=[], at=LATER)

    assert job.remaining_steps == (Step.FILTER, Step.RENDER)


def test_failing_keeps_the_exception_class_name() -> None:
    """⚠️ T-15 は原因ごとに例外の型を分けている。言い換えると分類が潰れる。"""
    job = make_job().entering(Step.CRAWL, at=LATER)

    failed = job.failing(Step.CRAWL, TimeoutError("1800秒で打ち切り"), at=LATER)

    assert failed.status is JobStatus.FAILED
    assert failed.failed_step is Step.CRAWL
    assert failed.error_type == "TimeoutError"
    assert failed.error_message == "1800秒で打ち切り"


# =============================================================================
# 3. リトライ（Failed --> Queued・該当 step から）
# =============================================================================


def test_a_retry_restarts_from_the_failed_step() -> None:
    job = (
        make_job()
        .entering(Step.CRAWL, at=NOW)
        .completing(Step.CRAWL, artifacts=["/a/raw.json"], at=NOW)
        .entering(Step.FILTER, at=NOW)
        .failing(Step.FILTER, RuntimeError("落ちた"), at=NOW)
    )

    retried = job.requeued(start_step=Step.FILTER, at=LATER)

    assert retried.status is JobStatus.QUEUED
    assert retried.start_step is Step.FILTER
    assert retried.remaining_steps == (Step.FILTER, Step.RENDER)
    # 前回の失敗の記録は消す（「今どうなっているか」が読めなくなる）。
    assert retried.failed_step is None
    assert retried.error_type is None
    assert retried.finished_at is None
    # crawl の成果物は残す（再開の材料）。
    assert retried.artifacts == ["/a/raw.json"]


def test_a_retry_from_an_earlier_step_takes_back_the_later_completions() -> None:
    """⚠️ 取り消さないと `remaining_steps` がその段を飛ばす。"""
    job = (
        make_job()
        .entering(Step.CRAWL, at=NOW)
        .completing(Step.CRAWL, artifacts=[], at=NOW)
        .entering(Step.FILTER, at=NOW)
        .completing(Step.FILTER, artifacts=[], at=NOW)
        .entering(Step.RENDER, at=NOW)
        .failing(Step.RENDER, RuntimeError("落ちた"), at=NOW)
    )

    retried = job.requeued(start_step=Step.CRAWL, at=LATER)

    assert retried.completed_steps == []
    assert retried.remaining_steps == STEP_ORDER


def test_starting_counts_the_attempt() -> None:
    job = make_job()

    assert job.starting(at=LATER).attempts == 1
    assert job.starting(at=LATER).starting(at=LATER).attempts == 2


# =============================================================================
# 4. period と種別
# =============================================================================


def test_a_period_must_match_its_run_type() -> None:
    """⚠️ 取り違えると週刊のつもりで月刊の成果物を上書きする（正規名は period 由来）。"""
    assert period_matches_type(WEEKLY_PERIOD, RunType.WEEKLY)
    assert period_matches_type(MONTHLY_PERIOD, RunType.MONTHLY)
    assert not period_matches_type(MONTHLY_PERIOD, RunType.WEEKLY)
    assert not period_matches_type(WEEKLY_PERIOD, RunType.MONTHLY)


def test_an_unparsable_period_matches_nothing() -> None:
    assert not period_matches_type("2026/W31", RunType.WEEKLY)
    assert not period_matches_type("2026-13", RunType.MONTHLY)


def test_the_run_type_can_be_read_from_the_period() -> None:
    assert run_type_of(WEEKLY_PERIOD) is RunType.WEEKLY
    assert run_type_of(MONTHLY_PERIOD) is RunType.MONTHLY
    with pytest.raises(PeriodError):
        run_type_of("2026-13")


def test_steps_from_never_reruns_an_earlier_step() -> None:
    assert steps_from(Step.CRAWL) == STEP_ORDER
    assert steps_from(Step.FILTER) == (Step.FILTER, Step.RENDER)
    assert steps_from(Step.RENDER) == (Step.RENDER,)


# =============================================================================
# 5. job_id
# =============================================================================


def test_the_job_id_can_name_a_history_generation() -> None:
    """退避先は `_history/{period}/{revision}_{job_id}/`（設計判断B）。

    ⚠️ パス区切りが入ると `ArtifactStore._validate_segment` が拒否する。
    """
    job_id = new_job_id(NOW)

    assert job_id.startswith("job_20260817-080000-")
    for forbidden in ("/", "\\", "..", " "):
        assert forbidden not in job_id


def test_two_job_ids_in_the_same_second_do_not_collide() -> None:
    """秒までしか無いと、別 period の同時受付でジョブ記録を上書きする。"""
    assert new_job_id(NOW) != new_job_id(NOW)


# =============================================================================
# 6. ジョブ記録の往復（ファイルへ書いて読み戻せるか）
# =============================================================================


def test_a_job_survives_a_round_trip_through_json() -> None:
    job = (
        make_job(trigger=JobTrigger.CRON, revision=7, resume_from=ResumePoint.AUTO)
        .entering(Step.FILTER, at=LATER)
        .completing(Step.FILTER, artifacts=["/a/report.xlsx"], at=LATER)
    )

    restored = RunJob.model_validate_json(job.model_dump_json())

    assert restored == job


def test_a_job_record_rejects_unknown_fields() -> None:
    """⚠️ 綴り違いのフィールドを黙って捨てない（状態が静かに欠ける）。"""
    payload = make_job().model_dump(mode="json") | {"statuss": "done"}

    with pytest.raises(ValueError):
        RunJob.model_validate(payload)
