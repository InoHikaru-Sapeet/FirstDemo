"""ジョブ記録の永続化と**二重起動防止のロック**（T-26。設計書 §8.3・§8.4）。

置き場は成果物と同じ `artifact_root` の下（`_runs/`）。**ファイルが正**という
このプロジェクトの方針（TASKS.md §1.1「永続化」）に合わせてあり、DB を使わない
理由は次の3つ:

1. **プロセスをまたいで見える必要がある。** `make run-weekly`（CLI）と
   `POST /run`（API）は別プロセスで、どちらも同じ二重起動防止に従わせたい。
   SQLite にはアドバイザリロックが無い（PostgreSQL の `pg_advisory_lock` に
   当たるものが無い）ので、DB へ寄せても結局ファイルか行ロックの自作になる。
2. **ジョブ記録は監査ログではない。** 監査（`run_start` / `run_finish`）は DB の
   `audit_logs` が正で、こちらは「今どうなっているか」を返すための実行時の状態。
   消えても監査の追跡性は落ちない。
3. **マイグレーションが要らない。** 状態機械（§8.4）はまだ動く可能性があり、
   列を足すたびに migration を切るのは早い。

⚠️ **前提は「1ホスト」。** ロックは `O_EXCL` のファイル作成で取るので、
NFS 越しや複数ホストでは保証されない。AWS へ展開して複数インスタンスにする
ときは、ここを DB 行ロック（PostgreSQL のアドバイザリロック）へ差し替えること
（[`docs/future-roadmap.md`](../../../../docs/future-roadmap.md)）。

---

⚠️ **落ちたプロセスのロックをどう外すか（長時間ジョブの現実）**

週次フル実行は **18業界で90〜100分**かかる実測がある。その間にサーバを再起動
したり `Ctrl-C` を押したりすると、ロックファイルだけが残る。放置すると
「二度と実行できない period」ができるので、ロックには**持ち主のプロセスID**を
書いておき、**そのプロセスが生きていなければ stale として回収する**。

- 回収時、持ち主のジョブ記録が終端でなければ **`Failed`** へ落とす
  （`AbandonedRunError`）。`GET /run/{job_id}` が「実行中」と言い続けるのを防ぐため。
- ⚠️ **PID は再利用される。** 別のプロセスがたまたま同じ PID を持っていると、
  stale なロックを「生きている」と誤判定して回収に失敗する（＝手で消す必要が
  ある）。安全側の誤りなので許容する（逆向きの誤判定＝走っているジョブの
  ロックを奪う、は起きない）。
- 手で外す場合は `artifacts/_runs/locks/{type}_{period}.lock` を削除する。
"""

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

from pydantic import TypeAdapter

from adapter.storage.artifact_store import ENCODING, validate_period
from config import Settings, get_settings
from enterprise.entities.json_document import (
    dump_json_document,
    parse_json_document,
)
from enterprise.entities.run_job import RunJob, RunType

RUNS_DIRNAME = "_runs"
LOCKS_DIRNAME = "locks"
JOB_FILE_SUFFIX = ".json"
LOCK_FILE_SUFFIX = ".lock"

JOB_ADAPTER: TypeAdapter[RunJob] = TypeAdapter(RunJob)


class JobStoreError(Exception):
    """ジョブ記録の置き場に関する不正な要求。"""


class RunAlreadyInProgressError(JobStoreError):
    """同一 `{type, period}` のジョブが既に走っている（二重起動防止）。

    HTTP 層は **409** に変換する。外部 cron から複数回叩かれても壊れないことが
    「外部 cron 方式」の前提条件（TASKS.md T-26 備考）。

    Attributes:
        run_type: 走っている側の種別
        period: 対象期間
        job_id: 走っている側のジョブID
    """

    def __init__(self, run_type: RunType, period: str, job_id: str) -> None:
        self.run_type = run_type
        self.period = period
        self.job_id = job_id
        super().__init__(
            f"{run_type} の {period} は既に実行中です（job_id={job_id}）。"
            "同じ期間・種別のジョブは同時に走らせません"
            "（同じ成果物を2つの実行が同時に上書きするため）。"
        )


class AbandonedRunError(Exception):
    """ロックを握ったままプロセスが消えたジョブ。

    実行中に落ちたのか成功したのかは記録から判別できないので、**失敗として
    残す**（`GET /run/{job_id}` が「実行中」のまま止まるほうが害が大きい）。
    """


@dataclass(frozen=True, slots=True)
class RunLock:
    """取得済みのロック。`JobStore.release()` へ渡して外す。

    Attributes:
        path: ロックファイル
        job_id: このロックを握っているジョブ
    """

    path: Path
    job_id: str


def _process_is_alive(pid: int) -> bool:
    """PID のプロセスが存在するか（シグナル 0 は存在確認だけで何も送らない）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 別ユーザーのプロセスが同じ PID を持っている＝生きている。
        return True
    return True


class JobStore:
    """ジョブ記録（`_runs/{job_id}.json`）とロック（`_runs/locks/*.lock`）。"""

    def __init__(
        self,
        root: Path,
        *,
        tz: tzinfo | None = None,
        pid: int | None = None,
        is_process_alive: Callable[[int], bool] = _process_is_alive,
    ) -> None:
        """
        Args:
            root: `artifact_root`。この下の `_runs/` を使う
            tz: 記録する時刻のタイムゾーン（設計書 §14 は Asia/Tokyo）
            pid: ロックへ書く自プロセスの PID（テストで差し替える）
            is_process_alive: PID の生存確認（テストで差し替える）
        """
        self.root = root
        self._tz = tz
        self._pid = os.getpid() if pid is None else pid
        self._is_process_alive = is_process_alive

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "JobStore":
        settings = settings or get_settings()
        return cls(root=settings.artifact_root, tz=settings.tzinfo)

    # --- パス -------------------------------------------------------------

    @property
    def runs_root(self) -> Path:
        return self.root / RUNS_DIRNAME

    @property
    def locks_root(self) -> Path:
        return self.runs_root / LOCKS_DIRNAME

    def job_path(self, job_id: str) -> Path:
        """`_runs/{job_id}.json`。

        ⚠️ `job_id` は `GET /run/{job_id}` のパスパラメータ（＝外部入力）として
        届く。パス区切りが混じると `_runs/` の外を読み書きできてしまうので、
        成果物と同じ検証（`ArtifactStore` の関門）を通す。
        """
        return self.runs_root / f"{_validated_job_id(job_id)}{JOB_FILE_SUFFIX}"

    def lock_path(self, run_type: RunType, period: str) -> Path:
        """`_runs/locks/{type}_{period}.lock`（二重起動防止の単位）。"""
        return (
            self.locks_root
            / f"{run_type.value}_{validate_period(period)}{LOCK_FILE_SUFFIX}"
        )

    # --- ジョブ記録 -------------------------------------------------------

    def save(self, job: RunJob) -> Path:
        """ジョブ記録を書く（原子的な差し替え。読み手が半端な JSON を見ない）。"""
        path = self.job_path(job.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, dump_json_document(JOB_ADAPTER, job))
        return path

    def get(self, job_id: str) -> RunJob | None:
        """ジョブ記録を読む。無ければ `None`。

        Raises:
            JobStoreError: `job_id` にパス区切り等が含まれる
            DocumentParseError: 記録が現行スキーマに合わない
        """
        path = self.job_path(job_id)
        if not path.exists():
            return None
        return parse_json_document(
            JOB_ADAPTER, path.read_text(encoding=ENCODING), label=path.name
        )

    # --- ロック（二重起動防止）-------------------------------------------

    def acquire(self, run_type: RunType, period: str, *, job_id: str) -> RunLock:
        """`{type, period}` のロックを取る。

        Raises:
            RunAlreadyInProgressError: 生きているプロセスが握っている
        """
        path = self.lock_path(run_type, period)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"job_id": job_id, "pid": self._pid, "at": self._now().isoformat()},
            ensure_ascii=False,
        )

        # 1回目で取れなければ stale 判定→回収し、**1回だけ**取り直す。
        # 無限に繰り返さないのは、回収と取得の競争で回り続けないため。
        for attempt in (1, 2):
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                holder = self._reclaim_if_stale(path, run_type, period)
                if attempt == 2 or holder is not None:
                    raise RunAlreadyInProgressError(
                        run_type, period, holder or "(不明)"
                    ) from None
                continue
            with os.fdopen(descriptor, "w", encoding=ENCODING) as handle:
                handle.write(payload)
            return RunLock(path=path, job_id=job_id)

        raise AssertionError("到達しない")  # pragma: no cover

    def release(self, lock: RunLock) -> None:
        """ロックを外す。**既に無くても失敗させない**（終了処理を止めない）。"""
        lock.path.unlink(missing_ok=True)

    def holder_of(self, run_type: RunType, period: str) -> str | None:
        """そのロックを握っているジョブID（取れていなければ `None`）。"""
        return _read_lock(self.lock_path(run_type, period)).get("job_id")

    # --- 内部 -------------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.now(tz=self._tz)

    def _reclaim_if_stale(
        self, path: Path, run_type: RunType, period: str
    ) -> str | None:
        """持ち主が消えていればロックを回収する。

        Returns:
            回収しなかった場合は持ち主のジョブID（＝まだ走っている）。
            回収した場合は `None`
        """
        info = _read_lock(path)
        pid = info.get("pid")
        job_id = info.get("job_id")

        if isinstance(pid, int) and self._is_process_alive(pid):
            return job_id or "(不明)"

        # 持ち主のプロセスが居ない＝落ちた。記録が実行中のままなら失敗にする。
        if isinstance(job_id, str) and job_id:
            self._abandon(job_id)
        path.unlink(missing_ok=True)
        return None

    def _abandon(self, job_id: str) -> None:
        try:
            job = self.get(job_id)
        except Exception:  # noqa: BLE001 - 読めない記録のせいで実行を止めない
            return
        if job is None or job.is_terminal:
            return
        self.save(
            job.failing(
                job.failed_step,
                AbandonedRunError(
                    f"ジョブ {job_id} を実行していたプロセスが終了しました"
                    "（タイムアウトや再起動・強制終了）。"
                    "同じ period を実行し直すと、"
                    "残っている前段の成果物から再開できます。"
                ),
                at=self._now(),
            )
        )


def _validated_job_id(job_id: str) -> str:
    if not job_id or job_id != job_id.strip():
        raise JobStoreError(f"job_id が空か前後に空白があります: {job_id!r}")
    if "/" in job_id or "\\" in job_id or "\x00" in job_id or job_id in (".", ".."):
        raise JobStoreError(f"job_id にパス区切りを含められません: {job_id!r}")
    return job_id


def _read_lock(path: Path) -> dict[str, object]:
    """ロックファイルの中身。壊れていても例外にしない（回収を止めないため）。"""
    try:
        loaded = json.loads(path.read_text(encoding=ENCODING))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _atomic_write_text(path: Path, text: str) -> None:
    """一時ファイルへ書いてから差し替える（`ArtifactStore.atomic_write` と同じ形）。

    ⚠️ ジョブ記録は**走っている最中に何度も上書きされ、同時に別プロセスから
    読まれる**（`GET /run/{job_id}` のポーリング）。素直に開いて書くと、読み手が
    書き途中の半端な JSON を掴む。
    """
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp_path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding=ENCODING) as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


__all__ = [
    "AbandonedRunError",
    "JobStore",
    "JobStoreError",
    "RunAlreadyInProgressError",
    "RunLock",
]
