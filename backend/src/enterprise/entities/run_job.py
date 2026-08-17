"""ジョブの状態機械とジョブ記録（T-26。設計書 §8.3・§8.4 ／ 仕様書 §13.1・§14）。

設計書 §8.4 の状態遷移図を**そのまま**型にしたモジュール。IO は一切しない
（永続化は `adapter.storage.job_store`、
実行は `application.usecases.run_orchestrator`）。

```
[*] --> Queued
Queued --> Crawling   : resume_from<=crawl
Queued --> Filtering  : resume_from=filter（raw 存在）
Queued --> Rendering  : resume_from=render（xlsx 存在）
Crawling --> Filtering / Failed
Filtering --> Rendering / Failed
Rendering --> Done / Failed
Failed --> Queued     : 手動/自動リトライ（該当 step から）
Done --> [*]
```

---

⚠️ **遷移表をここ以外に書かないこと。**

「次はこの状態」という知識が実行側（`RunOrchestrator`）にも散ると、§8.4 の図を
直しても実行が追随しない。実行側がやるのは `job.transition_to(...)` を呼ぶことだけで、
許されない遷移は `InvalidTransitionError` で落ちる。

---

⚠️ **`job_id` は成果物の履歴退避のディレクトリ名にそのまま入る**
（`_history/{period}/{revision}_{job_id}/`。設計判断B）。したがって
**パス区切りを含められない**（`ArtifactStore._validate_segment` が拒否する）。
`new_job_id()` が返す形（`job_YYYYmmdd-HHMMSS-xxxxxx`）はその制約を満たす。

⚠️ **T-45 の CLI が発行していた `cli-YYYYmmdd-HHMMSS` は廃止**し、`job_id` に一本化した。
run_id と job_id が別物だと、監査ログの job と `_history/` のディレクトリを
突き合わせられない（どちらも「その1回の実行」を指す ID なので分ける理由が無い）。
CLI 実行か API 実行かは `RunJob.trigger` が持つ。
"""

import secrets
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field

from enterprise.entities.period import PeriodError, parse_period

# --- 種別・段・状態 -----------------------------------------------------------


class RunType(StrEnum):
    """週刊か月刊か（`POST /run/{weekly|monthly}`。設計書 §3.2・§8.1）。"""

    WEEKLY = "weekly"
    MONTHLY = "monthly"


class Step(StrEnum):
    """パイプラインの段（設計書 §8.2）。

    ⚠️ **3段のまま増やさない**（§3.1 の3プロンプトとの1:1 対応）。
    """

    CRAWL = "crawl"
    FILTER = "filter"
    RENDER = "render"


STEP_ORDER: Final[tuple[Step, ...]] = (Step.CRAWL, Step.FILTER, Step.RENDER)


class ResumePoint(StrEnum):
    """`POST /run` の `resume_from`（設計書 §3.3・§8.3）。

    `AUTO` は §8.3 の「**前段成果物の存在確認による自動スキップ**」。
    ⚠️ **自動で飛ぶのは crawl → filter だけ**（`raw_articles_{period}.json` が
    あれば crawl を省く）。§8.3 が自動スキップとして書いているのはこの1つで、
    render まで自動で飛ばすと**再実行しても filter が二度と走らない**
    （判断基準を変えて再実行したのに結果が変わらない）。render からの再開は
    `RENDER` の明示指定でだけ行う。
    """

    CRAWL = "crawl"
    FILTER = "filter"
    RENDER = "render"
    AUTO = "auto"


class JobStatus(StrEnum):
    """ジョブの状態（設計書 §8.4）。"""

    QUEUED = "queued"
    CRAWLING = "crawling"
    FILTERING = "filtering"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"


TERMINAL_STATUSES: Final[frozenset[JobStatus]] = frozenset(
    {JobStatus.DONE, JobStatus.FAILED}
)

# 「その段を実行している」状態。§8.4 の Crawling / Filtering / Rendering。
STEP_STATUS: Final[Mapping[Step, JobStatus]] = MappingProxyType(
    {
        Step.CRAWL: JobStatus.CRAWLING,
        Step.FILTER: JobStatus.FILTERING,
        Step.RENDER: JobStatus.RENDERING,
    }
)

# 設計書 §8.4 の矢印をそのまま写したもの。⚠️ **ここを緩めない。**
ALLOWED_TRANSITIONS: Final[Mapping[JobStatus, frozenset[JobStatus]]] = MappingProxyType(
    {
        # Queued からは「再開ポイントの段」へ直接入る（3本の矢印）。
        JobStatus.QUEUED: frozenset(
            {
                JobStatus.CRAWLING,
                JobStatus.FILTERING,
                JobStatus.RENDERING,
                # 実行に入る前に落ちた場合（前段成果物が無い・ロックが取れない）。
                JobStatus.FAILED,
            }
        ),
        JobStatus.CRAWLING: frozenset({JobStatus.FILTERING, JobStatus.FAILED}),
        JobStatus.FILTERING: frozenset({JobStatus.RENDERING, JobStatus.FAILED}),
        JobStatus.RENDERING: frozenset({JobStatus.DONE, JobStatus.FAILED}),
        # Failed --> Queued: 手動/自動リトライ（該当 step から）。
        JobStatus.FAILED: frozenset({JobStatus.QUEUED}),
        # Done --> [*]。**Done から動かせない**（同じ period をもう一度回すのは
        # 新しいジョブ。冪等性は成果物側の upsert が担う＝設計判断B）。
        JobStatus.DONE: frozenset(),
    }
)


class InvalidTransitionError(Exception):
    """設計書 §8.4 に無い状態遷移を要求された。

    Attributes:
        current: 現在の状態
        requested: 要求された状態
    """

    def __init__(self, current: JobStatus, requested: JobStatus) -> None:
        self.current = current
        self.requested = requested
        allowed = sorted(ALLOWED_TRANSITIONS[current]) or ["（無し・終端）"]
        super().__init__(
            f"{current} から {requested} への遷移は設計書 §8.4 にありません"
            f"（{current} から行けるのは {', '.join(map(str, allowed))}）。"
        )


def steps_from(start: Step) -> tuple[Step, ...]:
    """`start` 以降に実行する段（手前の段は**実行しない**）。"""
    return STEP_ORDER[STEP_ORDER.index(start) :]


def period_matches_type(period: str, run_type: RunType) -> bool:
    """period の表記と種別（週刊/月刊）が噛み合っているか。

    ⚠️ **取り違えを通すと、週刊のつもりで月刊の成果物を上書きする**
    （正規名は period 由来）。表記の妥当性そのものは
    `enterprise.entities.period.parse_period()` が見る。
    """
    try:
        parsed = parse_period(period)
    except PeriodError:
        return False
    return parsed.is_weekly == (run_type is RunType.WEEKLY)


def run_type_of(period: str) -> RunType:
    """period の表記から種別を決める（`2026-W31` → weekly / `2026-07` → monthly）。

    Raises:
        PeriodError: どちらの表記でもない／実在しない期間
    """
    return RunType.WEEKLY if parse_period(period).is_weekly else RunType.MONTHLY


# `job_id` の形。⚠️ **パス区切りを含めないこと**
# （`_history/{period}/{revision}_{job_id}/` のディレクトリ名になるので、
# `ArtifactStore._validate_segment` が拒否する）。
JOB_ID_PREFIX = "job"
JOB_ID_TIME_FORMAT = "%Y%m%d-%H%M%S"
JOB_ID_SUFFIX_BYTES = 3


def new_job_id(now: datetime) -> str:
    """`job_20260817-080000-1a2b3c`。

    秒までの時刻だけだと**同じ秒に別 period のジョブが2つ**来たときに衝突する
    （二重起動防止は同一 `{type, period}` にしか効かない）。ランダムな接尾辞を
    足して、ジョブ記録ファイル（`_runs/{job_id}.json`）の上書きを防ぐ。
    """
    stamp = now.strftime(JOB_ID_TIME_FORMAT)
    return f"{JOB_ID_PREFIX}_{stamp}-{secrets.token_hex(JOB_ID_SUFFIX_BYTES)}"


# --- ジョブ記録 ---------------------------------------------------------------


class JobTrigger(StrEnum):
    """誰が起動したか（`actor` とは別に、経路そのものを残す）。

    `CRON` と `API` はどちらも `POST /run` だが、監査で「定期実行が動いたか」を
    見るときに人手の実行と混ざると読めない。判別は呼び出し元のロール
    （`system` = cron のサービストークン）で行う。
    """

    CLI = "cli"
    API = "api"
    CRON = "cron"


class RunJob(BaseModel):
    """1回のパイプライン実行（設計書 §8.4 の状態＋再開に要る情報）。

    **不変**。状態を進めるメソッドは新しい `RunJob` を返す。ジョブ記録は
    ファイルへ書き出す（`adapter.storage.job_store`）ので、そのまま JSON にできる形。

    Attributes:
        job_id: この実行の ID。**履歴退避のディレクトリ名にもなる**（設計判断B）
        type: 週刊か月刊か
        period: 対象期間（`2026-W31` / `2026-07`）
        status: 現在の状態（§8.4）
        resume_from: 要求された再開ポイント（`auto` を含む・記録用）
        start_step: 実際に開始した段（`auto` を解決した後の値）
        revision: **開始時に固定した config の revision**（§6.3・§14）
        trigger: 起動経路
        actor: 監査ログと同じ `役割:識別子` 表記（設計書 §4.4）
        created_at: 受付時刻
        updated_at: 最後に状態が変わった時刻
        finished_at: 終端（Done / Failed）に入った時刻
        completed_steps: 完了した段
        artifacts: 生成物のパス（文字列）
        failed_step: 失敗した段（`status=failed` のときだけ）
        error_type: 失敗した例外の型名（T-15 の6分類がそのまま読める形）
        error_message: 失敗した例外の内容
        attempts: 実行回数（リトライで増える）
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    type: RunType
    period: str
    status: JobStatus = JobStatus.QUEUED
    resume_from: ResumePoint = ResumePoint.CRAWL
    start_step: Step = Step.CRAWL
    revision: int | None = None
    trigger: JobTrigger = JobTrigger.API
    actor: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    completed_steps: list[Step] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    failed_step: Step | None = None
    error_type: str | None = None
    error_message: str | None = None
    attempts: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_running(self) -> bool:
        """走っている（＝ロックを握っているはずの）状態か。"""
        return self.status in STEP_STATUS.values()

    @property
    def remaining_steps(self) -> tuple[Step, ...]:
        """`start_step` 以降のうち、まだ完了していない段。"""
        done = set(self.completed_steps)
        return tuple(step for step in steps_from(self.start_step) if step not in done)

    def transition_to(self, status: JobStatus, *, at: datetime) -> Self:
        """§8.4 の矢印に沿って状態を進める。

        Raises:
            InvalidTransitionError: §8.4 に無い遷移
        """
        if status not in ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTransitionError(self.status, status)
        return self.model_copy(
            update={
                "status": status,
                "updated_at": at,
                "finished_at": at if status in TERMINAL_STATUSES else None,
            }
        )

    def entering(self, step: Step, *, at: datetime) -> Self:
        """その段の実行に入る（`Crawling` / `Filtering` / `Rendering`）。"""
        return self.transition_to(STEP_STATUS[step], at=at)

    def completing(self, step: Step, *, artifacts: list[str], at: datetime) -> Self:
        """その段が終わった（状態は次の段に入るときに進める）。"""
        return self.model_copy(
            update={
                "completed_steps": [*self.completed_steps, step],
                "artifacts": [*self.artifacts, *artifacts],
                "updated_at": at,
            }
        )

    def failing(self, step: Step | None, exc: BaseException, *, at: datetime) -> Self:
        """失敗させる。

        ⚠️ **例外の型名をそのまま残す。** T-15 は原因ごとに例外の型を分けており
        （`AIProcessError` なら未ログイン、`AITimeoutError` なら時間切れ …）、
        人向けの言葉へ言い換えるとその分類が潰れる（T-45 と同じ方針）。
        """
        return self.transition_to(JobStatus.FAILED, at=at).model_copy(
            update={
                "failed_step": step,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
        )

    def requeued(self, *, start_step: Step, at: datetime) -> Self:
        """`Failed → Queued`（該当 step からのリトライ。§8.4）。

        前回の失敗の記録は消す（残すと「今どうなっているか」が読めない）。
        `attempts` は `starting()` で増える。
        """
        return self.transition_to(JobStatus.QUEUED, at=at).model_copy(
            update={
                "start_step": start_step,
                "failed_step": None,
                "error_type": None,
                "error_message": None,
                "finished_at": None,
                # ⚠️ 再開する段より後の「完了済み」は取り消す。取り消さないと
                # `remaining_steps` がその段を飛ばしてしまう。
                "completed_steps": [
                    step
                    for step in self.completed_steps
                    if STEP_ORDER.index(step) < STEP_ORDER.index(start_step)
                ],
            }
        )

    def starting(self, *, at: datetime) -> Self:
        """実行に入る直前（試行回数を1つ増やす）。状態は変えない。"""
        return self.model_copy(update={"attempts": self.attempts + 1, "updated_at": at})


__all__ = [
    "ALLOWED_TRANSITIONS",
    "JOB_ID_PREFIX",
    "STEP_ORDER",
    "STEP_STATUS",
    "TERMINAL_STATUSES",
    "InvalidTransitionError",
    "JobStatus",
    "JobTrigger",
    "ResumePoint",
    "RunJob",
    "RunType",
    "Step",
    "new_job_id",
    "period_matches_type",
    "run_type_of",
    "steps_from",
]
