"""ジョブ記録の永続化と二重起動防止のロック（T-26。設計書 §8.3・§8.4）。

重点は**ロック**。TASKS.md T-26 の備考が「外部 cron から複数回叩かれても
壊れないことが『外部cron方式』の前提条件。ロックのテストを必ず書く」と
指定している。

- 同一 `{type, period}` の2本目は拒否される
- **別の period / 別の種別は互いを塞がない**（週刊と月刊は同時に走れる）
- 外し忘れが「二度と実行できない period」を作らない
  （持ち主のプロセスが居なければ回収する）
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from adapter.storage.job_store import (
    JobStore,
    JobStoreError,
    RunAlreadyInProgressError,
)
from enterprise.entities.json_document import DocumentParseError
from enterprise.entities.run_job import JobStatus, RunJob, RunType, Step

NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)

WEEKLY_PERIOD = "2026-W31"
MONTHLY_PERIOD = "2026-07"

LIVE_PID = 4242
DEAD_PID = 4243


def alive_only(*pids: int):  # type: ignore[no-untyped-def]
    """`pids` のプロセスだけが生きていることにする（テスト用の生存確認）。"""
    return lambda pid: pid in pids


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(
        tmp_path,
        tz=UTC,
        pid=LIVE_PID,
        is_process_alive=alive_only(LIVE_PID),
    )


def make_job(job_id: str = "job_20260817-080000-abc123", **overrides: object) -> RunJob:
    base: dict[str, object] = {
        "job_id": job_id,
        "type": RunType.WEEKLY,
        "period": WEEKLY_PERIOD,
        "actor": "admin:usr_1",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return RunJob.model_validate(base | overrides)


# --- ジョブ記録 ---------------------------------------------------------------


def test_a_job_can_be_written_and_read_back(store: JobStore) -> None:
    job = make_job(revision=3).entering(Step.CRAWL, at=NOW)

    store.save(job)

    assert store.get(job.job_id) == job


def test_an_unknown_job_is_none_not_an_error(store: JobStore) -> None:
    """`GET /run/{job_id}` が 404 を返せる形（存在しないことは異常ではない）。"""
    assert store.get("job_nope") is None


def test_the_record_lives_under_the_artifact_root(
    store: JobStore, tmp_path: Path
) -> None:
    job = make_job()

    path = store.save(job)

    assert path == tmp_path / "_runs" / f"{job.job_id}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["job_id"] == job.job_id


@pytest.mark.parametrize(
    "job_id", ["../config", "a/b", "a\\b", "", " job_1", ".", ".."]
)
def test_a_job_id_cannot_escape_the_runs_directory(
    store: JobStore, job_id: str
) -> None:
    """⚠️ `job_id` は `GET /run/{job_id}` のパスパラメータ（外部入力）で届く。"""
    with pytest.raises(JobStoreError):
        store.get(job_id)


def test_a_broken_record_is_reported_not_ignored(store: JobStore) -> None:
    """半端な記録を「無い」ことにすると、走っているジョブを見失う。"""
    job = make_job()
    store.save(job)
    store.job_path(job.job_id).write_text('{"job_id": 1}', encoding="utf-8")

    with pytest.raises(DocumentParseError):
        store.get(job.job_id)


def test_a_reader_never_sees_a_half_written_record(store: JobStore) -> None:
    """書き換えは差し替え（原子的）。ポーリング中の読み手が壊れた JSON を掴まない。"""
    job = make_job()
    store.save(job)

    store.save(job.entering(Step.CRAWL, at=NOW))

    restored = store.get(job.job_id)
    assert restored is not None
    assert restored.status is JobStatus.CRAWLING
    # 一時ファイルが残っていないこと。
    assert [p.name for p in store.runs_root.iterdir() if p.is_file()] == [
        f"{job.job_id}.json"
    ]


# --- ロック（二重起動防止）----------------------------------------------------


def test_the_second_start_of_the_same_period_is_refused(store: JobStore) -> None:
    """⚠️ 外部 cron から2回叩かれても、同じ成果物を2本が同時に上書きしない。"""
    store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_first")

    with pytest.raises(RunAlreadyInProgressError) as caught:
        store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_second")

    # 誰が握っているかを返す（409 の本文に載せられる形）。
    assert caught.value.job_id == "job_first"
    assert caught.value.period == WEEKLY_PERIOD
    assert caught.value.run_type is RunType.WEEKLY


def test_a_different_period_is_not_blocked(store: JobStore) -> None:
    """先週ぶんの再実行と今週ぶんは同時に走れる（ロックの単位は period ごと）。"""
    store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_a")

    other = store.acquire(RunType.WEEKLY, "2026-W32", job_id="job_b")

    assert other.job_id == "job_b"


def test_the_weekly_and_the_monthly_do_not_block_each_other(store: JobStore) -> None:
    """月初は週刊 cron と月刊 cron が近い時刻に走る（§13.1 の 08:00 / 09:00）。"""
    store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_w")

    monthly = store.acquire(RunType.MONTHLY, MONTHLY_PERIOD, job_id="job_m")

    assert monthly.job_id == "job_m"


def test_releasing_lets_the_next_run_start(store: JobStore) -> None:
    lock = store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_first")

    store.release(lock)

    assert store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_second")
    assert store.holder_of(RunType.WEEKLY, WEEKLY_PERIOD) == "job_second"


def test_releasing_twice_is_harmless(store: JobStore) -> None:
    """終了処理を例外で止めない（`finally` から呼ばれる）。"""
    lock = store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_first")

    store.release(lock)
    store.release(lock)

    assert store.holder_of(RunType.WEEKLY, WEEKLY_PERIOD) is None


def test_no_lock_means_no_holder(store: JobStore) -> None:
    assert store.holder_of(RunType.WEEKLY, WEEKLY_PERIOD) is None


def test_an_invalid_period_cannot_be_locked(store: JobStore) -> None:
    """ロックのファイル名に period が入る（`artifact_root` の外を触らせない）。"""
    from adapter.storage.artifact_store import ArtifactStoreError

    with pytest.raises(ArtifactStoreError):
        store.acquire(RunType.WEEKLY, "../../etc", job_id="job_x")


# --- 落ちたプロセスのロック（長時間ジョブの現実）------------------------------


def test_a_lock_left_by_a_dead_process_is_reclaimed(tmp_path: Path) -> None:
    """⚠️ これが無いと「二度と実行できない period」ができる。

    90〜100分の実行中にサーバを再起動すると、ロックだけが残る。
    """
    crashed = JobStore(tmp_path, tz=UTC, pid=DEAD_PID, is_process_alive=alive_only())
    crashed.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_crashed")

    survivor = JobStore(
        tmp_path, tz=UTC, pid=LIVE_PID, is_process_alive=alive_only(LIVE_PID)
    )
    lock = survivor.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_new")

    assert lock.job_id == "job_new"


def test_reclaiming_marks_the_abandoned_job_as_failed(tmp_path: Path) -> None:
    """`GET /run/{job_id}` が「実行中」と言い続けるのを防ぐ。"""
    crashed = JobStore(tmp_path, tz=UTC, pid=DEAD_PID, is_process_alive=alive_only())
    job = make_job("job_crashed").entering(Step.FILTER, at=NOW)
    crashed.save(job)
    crashed.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id=job.job_id)

    survivor = JobStore(
        tmp_path, tz=UTC, pid=LIVE_PID, is_process_alive=alive_only(LIVE_PID)
    )
    survivor.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_new")

    abandoned = survivor.get(job.job_id)
    assert abandoned is not None
    assert abandoned.status is JobStatus.FAILED
    assert abandoned.error_type == "AbandonedRunError"
    assert "プロセスが終了しました" in (abandoned.error_message or "")


def test_reclaiming_does_not_touch_a_job_that_already_finished(tmp_path: Path) -> None:
    """終端の記録を書き換えない（成功した実行を失敗にしない）。"""
    crashed = JobStore(tmp_path, tz=UTC, pid=DEAD_PID, is_process_alive=alive_only())
    job = (
        make_job("job_done")
        .entering(Step.RENDER, at=NOW)
        .transition_to(JobStatus.DONE, at=NOW)
    )
    crashed.save(job)
    crashed.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id=job.job_id)

    survivor = JobStore(
        tmp_path, tz=UTC, pid=LIVE_PID, is_process_alive=alive_only(LIVE_PID)
    )
    survivor.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_new")

    assert survivor.get(job.job_id) == job


def test_a_lock_held_by_a_live_process_is_not_stolen(store: JobStore) -> None:
    """⚠️ 逆向きの誤りは起こさない（走っているジョブのロックを奪わない）。"""
    store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_running")

    with pytest.raises(RunAlreadyInProgressError):
        store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_new")

    assert store.holder_of(RunType.WEEKLY, WEEKLY_PERIOD) == "job_running"


def test_an_unreadable_lock_is_reclaimed(store: JobStore, tmp_path: Path) -> None:
    """中身が壊れたロックで詰まらせない（持ち主が分からない＝回収してよい）。"""
    path = store.lock_path(RunType.WEEKLY, WEEKLY_PERIOD)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("これは JSON ではない", encoding="utf-8")

    lock = store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_new")

    assert lock.job_id == "job_new"


def test_the_lock_records_the_owning_process(store: JobStore) -> None:
    """PID が無いと、落ちたプロセスのロックを回収できない。"""
    lock = store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_first")

    payload = json.loads(lock.path.read_text(encoding="utf-8"))

    assert payload["job_id"] == "job_first"
    assert payload["pid"] == LIVE_PID
    assert datetime.fromisoformat(payload["at"]).tzinfo is not None


def test_the_default_process_check_recognises_this_process(tmp_path: Path) -> None:
    """既定の生存確認（`os.kill(pid, 0)`）が自分自身を「生きている」と見ること。

    ここを取り違えると、**走っている最中に自分のロックを奪える**。
    """
    store = JobStore(tmp_path, tz=UTC)
    store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_mine")

    with pytest.raises(RunAlreadyInProgressError):
        store.acquire(RunType.WEEKLY, WEEKLY_PERIOD, job_id="job_other")

    payload = json.loads(
        store.lock_path(RunType.WEEKLY, WEEKLY_PERIOD).read_text(encoding="utf-8")
    )
    assert payload["pid"] == os.getpid()
