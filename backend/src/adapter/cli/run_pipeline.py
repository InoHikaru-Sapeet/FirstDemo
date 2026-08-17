"""通し実行 CLI（T-45 → T-26 で Orchestrator の薄い皮へ置換）。

    make run-weekly                     # 当週（Asia/Tokyo）を解決して実行
    make run-weekly PERIOD=2026-W33
    make run-monthly                    # 前月を解決して実行
    make run-monthly PERIOD=2026-07
    make run-weekly PERIOD=2026-W33 ARGS="--from filter"   # 途中から再開
    make run-weekly ARGS="--from auto"                     # 前段成果物があれば省く
    make run-weekly ARGS="--retry job_20260817-080000-1a2b3c"

---

⚠️ **配線とジョブ管理はここには無い**（T-26 で `RunOrchestrator` へ移した）。

T-45 の時点ではこのファイルが crawl → filter → render の配線を持っていたが、
P7 の申し送りどおり **`application.usecases.run_orchestrator` へ昇格・置換**した。
このファイルに残っているのは次の3つだけ:

1. **引数の解釈**（種別・PERIOD・`--from` / `--retry`）と **PERIOD の解決**
   （省略時は当週／前月。仕様書 §13.1）
2. **進捗の表示**（各段の開始・終了・所要時間。AI 呼び出しは1回数分＝T-15 実測
   なので、無言で待たせない）
3. **失敗の見せ方**（どの段で・どの例外か。`FAILURE_HINTS`）

状態機械（§8.4）・二重起動防止・config の固定・監査ログ（`run_start` /
`run_finish`）はすべて Orchestrator の責務で、**CLI 実行でも API 実行でも同じものが
効く**（同じ入り口を通るため、片方にだけ入る挙動が生まれない）。

⚠️ **`make run-weekly` は API を経由しない**（`POST /run` を叩く形にはしなかった）。
サーバを起動していなくても手元で通しに動かせることが T-45 からのこの入り口の
目的で、Orchestrator を直接呼んでも状態機械・ロック・監査は同じだから。
cron から API を叩く形は `make run-weekly-api`（T-28 ／ README）。
"""

import asyncio
import time
from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from datetime import date, datetime, timedelta

# ⚠️ import の副作用（`logging.basicConfig`）が目的。ワーカーの進捗ログ
# （`logger.info`：crawl started / filter finished 等）を手元の端末へ出すために要る。
# AI 呼び出しは1回数分かかるので、無言で待たせない（T-15 備考の実測）。
import common.logger  # noqa: F401
from adapter.database.database import db_manager
from adapter.llm import (
    AIClientError,
    AIOutputParseError,
    AIProcessError,
    AIProtocolError,
    AIResponseError,
    AITimeoutError,
    AIUnavailableError,
)
from adapter.storage.artifact_store import ArtifactStoreError
from adapter.storage.job_store import RunAlreadyInProgressError
from application.usecases.crawl import SearchNotPerformedError
from application.usecases.filter import RawArticlesNotFoundError
from application.usecases.run_orchestrator import (
    ConfigPinError,
    PreparedRun,
    RunOrchestrator,
    RunPreconditionError,
    RunRequestError,
    build_orchestrator,
)
from config import get_settings
from enterprise.entities.json_document import DocumentParseError
from enterprise.entities.period import (
    PeriodError,
    monthly_period_of,
    weekly_period_of,
)
from enterprise.entities.run_job import (
    JobStatus,
    JobTrigger,
    ResumePoint,
    RunJob,
    RunType,
    Step,
)

EXIT_OK = 0
# 実行が失敗した（どのステップで・どの例外かは標準出力に出す）。
EXIT_FAILED = 1
# 実行する前に分かる不備（period 表記・種別の不一致・config が固定できない）。
EXIT_INVALID_INPUT = 2
# 同じ period・種別が既に走っている（二重起動防止。T-26）。
EXIT_ALREADY_RUNNING = 3

# 手元の実行の `actor`（監査ログ §4.4 の `役割:識別子` 形式）。
# ⚠️ `system:` にしない。cron（サービストークン）の実行と混ざると、
# 監査ログから「無人で回ったのか人が回したのか」が読めなくなる。
CLI_ACTOR = "cli:run-pipeline"


# --- 失敗時の表示（T-15 の例外6分類をそのまま読める形にする）------------------
# ⚠️ **ここに判定ロジックを書かない。** 例外の型がそのまま原因の分類で（T-15 が
# 原因ごとに別の型へ分けている）、この表は「その型が何を意味するか」を人向けの
# 言葉にするだけ。型が増えたらここへ1行足す。
FAILURE_HINTS: tuple[tuple[type[BaseException], str], ...] = (
    # T-15 の6分類（すべて `AIClientError` を基底に持つ）
    (
        AIUnavailableError,
        "AI を呼ぶ前提が満たされていない（`claude` が PATH に無い／実行できない）。"
        "⚠️ 再実行では直らない。CLI の導入と `claude` のログインを確認すること",
    ),
    (
        AITimeoutError,
        "制限時間内に終わらなかった（プロセスは kill 済み）。"
        "crawl は AI_CRAWL_TIMEOUT_SECONDS（既定30分）、"
        "分類・採点系は AI_TIMEOUT_SECONDS（既定10分）",
    ),
    (
        AIProcessError,
        "`claude` が非0で終了した。**未ログインはここに出る想定**"
        "（標準エラー出力の内容が下の『内容』に載っている）",
    ),
    (
        AIProtocolError,
        "応答の形が想定と違う（封筒として読めない／必要なフィールドが無い）。"
        "CLI のバージョンが上がって出力形式が変わった可能性がある（T-15 備考の実測）",
    ),
    (
        AIResponseError,
        "応答を成功として扱えなかった（`is_error` / `stop_reason=refusal` / "
        "ツールの拒否記録 `permission_denials` 等）",
    ),
    (
        AIOutputParseError,
        "出力が求めたスキーマに合わないまま、リトライ上限まで続いた（AI_MAX_ATTEMPTS）",
    ),
    # 以降は AI 以外。原因がプロンプトでも通信でもないものを混ぜて読まないための行。
    (
        SearchNotPerformedError,
        "web 検索が実施されていない／確認できないので収集結果を受け取らなかった"
        "（記憶からの推測と区別が付かないため。T-16）。"
        "**成果物は書いていない**",
    ),
    (
        RawArticlesNotFoundError,
        "crawl の出力（`raw_articles_{period}.json`）が無い。"
        "`--from` を付けずに crawl から実行すること",
    ),
    (
        DocumentParseError,
        "読み込んだ JSON がスキーマに合わない（どのパスがなぜ駄目かは下の『内容』）",
    ),
    (
        ArtifactStoreError,
        "成果物の置き場への要求が不正（period / industry をファイル名へ埋められない）",
    ),
    (
        RunPreconditionError,
        "再開ポイントの指定と、残っている成果物が噛み合わない"
        "（前段の出力が無い状態で途中から始めようとしている）",
    ),
    (
        ConfigPinError,
        "開始時の config revision を固定できない（§8.3）。"
        "`config.json` と改訂履歴（DB）が食い違っている可能性がある",
    ),
)


def failure_hint(exc: BaseException) -> str | None:
    """例外の型に対応する説明（`FAILURE_HINTS` の最初に当たった行）。"""
    return next(
        (hint for kind, hint in FAILURE_HINTS if isinstance(exc, kind)),
        None,
    )


def hint_for_error_type(name: str | None) -> str | None:
    """ジョブ記録の `error_type`（型名の文字列）から説明を引く。

    ⚠️ Orchestrator は例外を状態へ落として返すので（`execute()` は投げない）、
    CLI が見るのは**型名の文字列**になる。`FAILURE_HINTS` は型のまま持ちたい
    （新しい例外型を足したときに、名前の綴りではなく型で照合したい）ので、
    突き合わせはここで名前へ落として行う。
    """
    return next(
        (hint for kind, hint in FAILURE_HINTS if kind.__name__ == name),
        None,
    )


# --- period の解決（仕様書 §13.1 と同じ規則）---------------------------------


def resolve_period(run_type: RunType, *, today: date) -> str:
    """PERIOD 省略時の対象期間（設計書 §8.1 の「period 解決」列）。

    週次は `{{ISO_WEEK}}`＝**実行日が属する週**（毎週月曜 08:00 に当週を回す）、
    月次は `{{PREV_MONTH}}`＝**実行日の前月**（毎月1日 09:00 に前月ぶんを作る）。

    ⚠️ **基準は Asia/Tokyo**（呼び出し側が `Settings.tzinfo` の今日を渡す）。
    UTC の今日で解決すると、日本時間の月曜早朝・月初に1つ前の期間を指す。
    """
    if run_type is RunType.WEEKLY:
        return weekly_period_of(today)
    return monthly_period_of(today.replace(day=1) - timedelta(days=1))


# --- 実行 --------------------------------------------------------------------


async def run(
    orchestrator: RunOrchestrator,
    prepared: PreparedRun,
    *,
    out: Callable[[str], None] = print,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """受付済みの実行を走らせ、進捗と結果を表示する。終了コードを返す。

    Args:
        orchestrator: 実行の本体（状態機械・監査・ロックはこちらが持つ）
        prepared: `prepare()` 済みの実行（**ロックを握っている**）
        out: 出力先（テストで差し替える）
        clock: 所要時間の計測（テストで差し替える）

    Returns:
        `EXIT_OK` / `EXIT_FAILED`
    """
    job = prepared.job
    period = prepared.period
    steps = job.remaining_steps

    out(f"=== 通し実行: {period.text} ===")
    out(f"種別        : {'週刊' if period.is_weekly else '月刊'}（{job.type}）")
    out(f"対象期間    : {period.start} 〜 {period.end}")
    out(f"config      : revision={job.revision}（実行中は固定）")
    out(f"job_id      : {job.job_id}")
    out(f"成果物の置場: {prepared.pipeline.store.root}")
    out(f"実行ステップ: {' → '.join(step.value for step in steps)}")
    if job.start_step is not Step.CRAWL:
        out("（手前のステップは実行しません。前の実行の成果物をそのまま使います）")
    out("")

    started = clock()
    finished = await orchestrator.execute(prepared)
    elapsed = clock() - started

    if finished.status is JobStatus.FAILED:
        out(_failure_report(finished, elapsed=elapsed))
        _report_artifacts(finished, out=out)
        return EXIT_FAILED

    out(f"=== 完了（{elapsed:.1f}秒）===")
    _report_artifacts(finished, out=out)
    return EXIT_OK


def _failure_report(job: RunJob, *, elapsed: float) -> str:
    """「どのステップで・どの例外か」（T-45 から引き継いだ完了条件）。

    ⚠️ **例外の型名をそのまま出す。** T-15 は原因ごとに型を分けており
    （`AIProcessError` なら未ログイン、`AITimeoutError` なら時間切れ …）、
    人向けの文言へ言い換えると、その分類がここで潰れる。
    """
    where = job.failed_step or "（実行前）"
    lines = [
        f"✗ {where} で失敗しました（{elapsed:.1f}秒）",
        f"    job_id: {job.job_id}",
        f"    例外 : {job.error_type}",
    ]
    if hint := hint_for_error_type(job.error_type):
        lines.append(f"    分類 : {hint}")
    if _is_ai_error(job.error_type):
        lines.append(
            "    ※ AI 呼び出しの失敗（T-15 の6分類："
            "AIUnavailableError / AITimeoutError / AIProcessError / "
            "AIProtocolError / AIResponseError / AIOutputParseError）"
        )
    lines.append(f"    内容 : {job.error_message}")
    lines.append(
        f"    再開 : make run-{job.type.value} PERIOD={job.period} "
        f'ARGS="--retry {job.job_id}"'
    )
    return "\n".join(lines)


def _is_ai_error(name: str | None) -> bool:
    return any(
        issubclass(kind, AIClientError) and kind.__name__ == name
        for kind, _ in FAILURE_HINTS
    )


def _report_artifacts(job: RunJob, *, out: Callable[[str], None]) -> None:
    """生成物のパスを列挙する（失敗時も、そこまでに作れたぶんを出す）。"""
    if not job.artifacts:
        return
    label = (
        "失敗するまでに書き出した成果物" if job.status is JobStatus.FAILED else "生成物"
    )
    out(f"--- {label} {len(job.artifacts)} 件 ---")
    for path in job.artifacts:
        out(f"  {path}")


def _build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="run-pipeline",
        description=(
            "crawl → filter → render を通しで実行する（T-26 Run Orchestrator の入口）。"
            "状態機械・二重起動防止・監査ログは POST /run と同じものが効く。"
        ),
    )
    parser.add_argument(
        "run_type",
        metavar="type",
        type=RunType,
        choices=list(RunType),
        help="weekly（週刊メルマガ）または monthly（月刊ビリーフ）",
    )
    parser.add_argument(
        "--period",
        default=None,
        help=(
            "対象期間（週次 YYYY-Www / 月次 YYYY-MM）。"
            "省略すると Asia/Tokyo の当週（weekly）／前月（monthly）"
        ),
    )
    parser.add_argument(
        "--from",
        dest="resume_from",
        type=ResumePoint,
        choices=list(ResumePoint),
        default=ResumePoint.CRAWL,
        help=(
            "開始ステップ（既定 crawl）。filter なら収集済みの raw_articles を、"
            "render なら書き出し済みの中間xlsx と narrative を使って再開する。"
            "auto は raw_articles があれば crawl だけを省く（§8.3）"
        ),
    )
    parser.add_argument(
        "--retry",
        dest="retry_job_id",
        default=None,
        help=(
            "失敗したジョブを失敗した段からやり直す（設計書 §8.4 の Failed→Queued）。"
            "--period / --from とは併用しない"
        ),
    )
    return parser


async def _prepare(
    orchestrator: RunOrchestrator, args: Namespace, *, today: date
) -> PreparedRun:
    if args.retry_job_id:
        return await orchestrator.prepare_retry(args.retry_job_id, actor=CLI_ACTOR)
    period = args.period or resolve_period(args.run_type, today=today)
    return await orchestrator.prepare(
        run_type=args.run_type,
        period=period,
        actor=CLI_ACTOR,
        resume_from=args.resume_from,
        trigger=JobTrigger.CLI,
    )


def main(argv: list[str] | None = None) -> int:
    args: Namespace = _build_parser().parse_args(argv)
    settings = get_settings()
    today = datetime.now(tz=settings.tzinfo).date()

    async def _run() -> int:
        orchestrator = build_orchestrator(db_manager.session, settings=settings)
        try:
            prepared = await _prepare(orchestrator, args, today=today)
        except RunAlreadyInProgressError as exc:
            print(f"中止しました: {exc}")
            return EXIT_ALREADY_RUNNING
        except (PeriodError, RunRequestError, ConfigPinError) as exc:
            print(f"中止しました: {exc}")
            return EXIT_INVALID_INPUT
        return await run(orchestrator, prepared)

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        # `execute()` の `finally` が回るので、Ctrl-C ではロックは外れる
        # （ジョブ記録も `Failed` になる）。**強制終了（SIGKILL）や電源断だけは
        # ロックが残る**が、次に同じ period を要求したときに持ち主のプロセスが
        # 居ないことを検出して回収する（`adapter.storage.job_store` の⚠️）。
        print("中止しました: 中断されました。")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLI_ACTOR",
    "EXIT_ALREADY_RUNNING",
    "EXIT_FAILED",
    "EXIT_INVALID_INPUT",
    "EXIT_OK",
    "FAILURE_HINTS",
    "failure_hint",
    "hint_for_error_type",
    "main",
    "resolve_period",
    "run",
]
