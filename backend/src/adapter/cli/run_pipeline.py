"""通し実行 CLI（T-45 ／ 設計書 §8.2・§13.1）。

    make run-weekly                     # 当週（Asia/Tokyo）を解決して実行
    make run-weekly PERIOD=2026-W33
    make run-monthly                    # 前月を解決して実行
    make run-monthly PERIOD=2026-07
    make run-weekly PERIOD=2026-W33 ARGS="--from filter"   # 途中から再開

crawl → filter（narrative 含む）→ render を手元で通しに実行し、生成された HTML を
目視確認するための入り口。設計書 §8.2 の受け渡し表を**そのまま配線しただけ**で、
ワーカー本体（T-16 / T-21 / T-22 / T-24 / T-25 / T-44）のロジックは一切持たない。

- **crawl**: period → `raw_articles_{period}.json`
- **filter**: `raw_articles_{period}.json` ＋ 固定した config
  → 中間xlsx `#sheet={period}` ／ 除外ログ append ／ `validation_{period}.json`
  ／ `narrative_{period}.json`（生成テキスト＝T-44）
- **render**: 中間xlsx `#sheet={period}` ＋ `narrative_{period}.json`
  → 週刊は**対象業界ごとに1通**（`target_industries` の数だけ）／月刊は1通

---

⚠️ **これは動作確認用の薄い入り口であり、P7（T-26 Run Orchestrator）の状態機械・
ジョブ管理の代替ではない。** T-26 を実装するときに、この CLI は Orchestrator を
呼ぶ薄い皮へ置き換えるか廃止する。次のものは**ここに実装しない**（T-26 の責務）:

- ジョブの状態機械（`Queued → Crawling → … → Done` / `Failed`）とジョブID の永続化
- 二重起動防止のロック（同一 `{type, period}` の同時実行）
- 前段成果物の存在確認による**自動**スキップ（ここは `--from` の明示指定だけ）
- **監査ログ（`run_start` / `run_finish`）**。T-10 のイベント基盤は実装済みだが、
  実行の監査は T-26 の完了条件なので、ここで書くと二重実装になる

---

**この CLI が持っている判断（配線の都合で決めているもの）**

1. **config は開始時に1回だけ読み、同じオブジェクトを全ステップへ渡す**（§6.3・§14
   の「固定参照」）。`FilterWorker` は渡された config の深いコピーを抱えるので、
   実行中に `config.json` を書き換えても判断基準は動かない。
2. **月次の除外ログは週次ブックへ積む**（`append_exclusions`）。§8.1 が除外ログを
   週次ブックの構成として定義しているため（T-21 備考と同じ理由）。
3. **render は xlsx と `narrative_{period}.json` を読み直す**（filter の戻り値を
   直接は渡さない）。§8.2 の受け渡しがファイル経由で、`--from render` が
   「前の実行の成果物から再開する」形になっていないと再開の確認にならないため。
"""

import asyncio
import time
from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

# ⚠️ import の副作用（`logging.basicConfig`）が目的。ワーカーの進捗ログ
# （`logger.info`：crawl started / filter finished 等）を手元の端末へ出すために要る。
# AI 呼び出しは1回数分かかるので、無言で待たせない（T-15 備考の実測）。
import common.logger  # noqa: F401
from adapter.config_repository import ConfigRepository, ConfigRepositoryError
from adapter.database.database import db_manager
from adapter.html.monthly_renderer import MonthlyNarrative, MonthlyRenderer
from adapter.html.weekly_renderer import WeeklyNarrative, WeeklyRenderer
from adapter.llm import (
    AIClientError,
    AIOutputParseError,
    AIProcessError,
    AIProtocolError,
    AIResponseError,
    AITimeoutError,
    AIUnavailableError,
    get_ai_client,
)
from adapter.storage.artifact_store import ArtifactStore, ArtifactStoreError
from adapter.xlsx.report_writer import ReportStore
from application.usecases.crawl import CrawlWorker, SearchNotPerformedError
from application.usecases.filter import FilterWorker, RawArticlesNotFoundError
from application.usecases.narrative import to_monthly_narrative, to_weekly_narrative
from config import Settings, get_settings
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.json_document import DocumentParseError
from enterprise.entities.narrative import (
    MonthlyNarrativeDocument,
    WeeklyNarrativeDocument,
    parse_narrative,
)
from enterprise.entities.period import (
    Period,
    PeriodError,
    monthly_period_of,
    parse_period,
    weekly_period_of,
)

EXIT_OK = 0
# ステップが失敗した（どのステップで・どの例外かは標準出力に出す）。
EXIT_FAILED = 1
# 実行する前に分かる不備（period 表記・種別の不一致・config が無い）。
EXIT_INVALID_INPUT = 2

# 退避先ディレクトリ名（`_history/{period}/{revision}_{run_id}/`）に入る値。
# ⚠️ `ArtifactStore._validate_segment` を通るので、パス区切りを含められない。
RUN_ID_PREFIX = "cli"
RUN_ID_TIME_FORMAT = "%Y%m%d-%H%M%S"


class Step(StrEnum):
    """パイプラインの段（設計書 §8.2）。`--from` の値でもある。"""

    CRAWL = "crawl"
    FILTER = "filter"
    RENDER = "render"


STEP_ORDER: tuple[Step, ...] = (Step.CRAWL, Step.FILTER, Step.RENDER)


class Kind(StrEnum):
    """週刊か月刊か（`make run-weekly` / `make run-monthly`）。"""

    WEEKLY = "weekly"
    MONTHLY = "monthly"


class PipelineError(Exception):
    """CLI から見た前提の不備（period の種別違い・前段成果物が無い等）。

    ⚠️ **ワーカーの例外をこれに包み直さない。** どの工程のどの原因で落ちたかを
    そのまま表示するのがこの CLI の目的で、包むと T-15 の6分類が読めなくなる。
    """


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
        PipelineError,
        "この CLI から見た前提の不備"
        "（再開ポイントの指定と成果物の状態が噛み合わない等）",
    ),
)


def failure_hint(exc: BaseException) -> str | None:
    """例外の型に対応する説明（`FAILURE_HINTS` の最初に当たった行）。"""
    return next(
        (hint for kind, hint in FAILURE_HINTS if isinstance(exc, kind)),
        None,
    )


# --- ステップの相手（実体は T-16 / T-21 / T-22 / T-24 / T-25）-----------------
# ⚠️ **戻り値のうちこの CLI が使うものだけを書く。** 具象クラスを型に書くと
# 「配線テストのためにワーカーを本物として組み立てる」ことになり、テストが
# 実際の `claude` を起動しかねない（起動しないことは T-45 の完了条件）。


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
    """1回の実行で使う相手一式（組み立ては `build_pipeline()`）。

    Attributes:
        config: 実行開始時に読んで**固定参照**する config（§6.3・§14）
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


def build_pipeline(
    config: IntelligenceConfig, *, settings: Settings | None = None
) -> Pipeline:
    """本番の相手で組み立てる（テストはここを通らず `Pipeline` を直接作る）。

    ⚠️ **AI クライアントは用途ごとに取る。** crawl だけが web 検索を要求する
    （`get_ai_client(web_search=True)`）。filter 側へ web 検索を渡さないのは、
    収集はもう済んでいて、この段で検索させる必要が無いため（T-16 / T-21）。
    """
    settings = settings or get_settings()
    store = ArtifactStore.from_settings(settings)
    reports = ReportStore(store)
    return Pipeline(
        config=config,
        store=store,
        crawler=CrawlWorker(
            client=get_ai_client(web_search=True),
            store=store,
            config=config,
            settings=settings,
        ),
        filterer=FilterWorker(
            client=get_ai_client(),
            store=store,
            config=config,
            history_reader=reports,
        ),
        reports=reports,
        weekly_renderer=WeeklyRenderer(store),
        monthly_renderer=MonthlyRenderer(store),
    )


# --- period の解決（仕様書 §13.1 と同じ規則）---------------------------------


def resolve_period(kind: Kind, *, today: date) -> str:
    """PERIOD 省略時の対象期間（設計書 §8.1 の「period 解決」列）。

    週次は `{{ISO_WEEK}}`＝**実行日が属する週**（毎週月曜 08:00 に当週を回す）、
    月次は `{{PREV_MONTH}}`＝**実行日の前月**（毎月1日 09:00 に前月ぶんを作る）。

    ⚠️ **基準は Asia/Tokyo**（呼び出し側が `Settings.tzinfo` の今日を渡す）。
    UTC の今日で解決すると、日本時間の月曜早朝・月初に1つ前の期間を指す。
    """
    if kind is Kind.WEEKLY:
        return weekly_period_of(today)
    return monthly_period_of(today.replace(day=1) - timedelta(days=1))


def requested_period(kind: Kind, period: str) -> Period:
    """明示された PERIOD を検証する（表記・実在・**種別の一致**）。

    ⚠️ **種別の取り違えをここで落とす。** `make run-weekly PERIOD=2026-07` を
    通すと、週刊のつもりで月刊の成果物を上書きする（正規名は period 由来）。

    Raises:
        PeriodError: 表記が不正／実在しない期間
        PipelineError: 種別（weekly / monthly）と表記が食い違う場合
    """
    parsed = parse_period(period)
    if (kind is Kind.WEEKLY) != parsed.is_weekly:
        expected = "YYYY-Www（週次）" if kind is Kind.WEEKLY else "YYYY-MM（月次）"
        raise PipelineError(
            f"{kind} の実行に {period!r} は使えません（{expected} が必要です）"
        )
    return parsed


def new_run_id(now: datetime) -> str:
    """この実行の ID（履歴退避のディレクトリ名に入る）。

    T-26 が発行するジョブID の代わりに、**手元の実行だと分かる形**で採番する
    （`_history/{period}/{revision}_{run_id}/`）。秒までで十分なのは、同じ period を
    1秒以内に2回実行することが手元では起きないため（同時実行の防止は T-26）。
    """
    return f"{RUN_ID_PREFIX}-{now.strftime(RUN_ID_TIME_FORMAT)}"


# --- 各ステップ（配線だけ。ワーカーのロジックは持たない）----------------------


async def run_crawl(
    pipeline: Pipeline, period: Period, *, out: Callable[[str], None]
) -> list[Path]:
    """crawl（T-16）。`raw_articles_{period}.json` を書く。"""
    result = await pipeline.crawler.crawl(period.text)
    out(f"    収集 {result.article_count} 件")
    return [result.path]


async def run_filter(
    pipeline: Pipeline, period: Period, *, run_id: str, out: Callable[[str], None]
) -> list[Path]:
    """filter（T-21 ＋ 生成テキスト T-44）と中間xlsx への書き出し（T-22）。

    ⚠️ **`run_id` を `FilterWorker.run()` へ渡す**（`narrative_{period}.json` の
    退避が効く形。T-44 は `run_id=None` だと退避しない）。
    """
    result = await pipeline.filterer.run(period.text, run_id=run_id)
    revision = pipeline.revision
    written: list[Path] = []

    if period.is_weekly:
        report = pipeline.reports.write_weekly(
            period=period.text,
            articles=result.articles,
            exclusions=result.exclusion_log,
            revision=revision,
            run_id=run_id,
        )
        out(f"    採用 {report.rows} 件 / 除外 {len(result.exclusion_log)} 件")
        written.append(report.path)
    else:
        report = pipeline.reports.write_monthly(
            period=period.text,
            cases=result.cases,
            revision=revision,
            run_id=run_id,
        )
        out(f"    事例 {report.rows} 件 / 除外 {len(result.exclusion_log)} 件")
        written.append(report.path)
        # ⚠️ 除外ログは月次ブックではなく**週次ブック**へ積む（§8.1 が除外ログを
        # 週次ブックの構成として定義している。T-21 備考）。0件のときに呼ばないのは、
        # 書くものが無いのに週次ブックを上書き（＝退避を1世代消費）しないため。
        if result.exclusion_log:
            log = pipeline.reports.append_exclusions(
                period=period.text,
                exclusions=result.exclusion_log,
                revision=revision,
                run_id=run_id,
            )
            written.append(log.path)

    return [*written, result.validation_path, result.narrative_path]


def run_render(
    pipeline: Pipeline, period: Period, *, run_id: str, out: Callable[[str], None]
) -> list[Path]:
    """render（T-24 / T-25）。**入力は中間xlsx と narrative ファイル**（§8.2）。

    ⚠️ **週刊は対象業界ごとに1通**（T-46 Step 4。`weekly_..._{industry}_
    {period}.html`）。config の `target_industries` を順に回し、生成テキストも
    その業界ぶんを渡す。月刊は業界別ではない（1通）。

    Raises:
        PipelineError: `narrative_{period}.json` が無い場合
            （filter を通していない／別 period の成果物しか無い）
    """
    narrative_path = pipeline.store.narrative_path(period.text)
    if not pipeline.store.exists(narrative_path):
        raise PipelineError(
            f"{narrative_path} がありません。"
            "生成テキストは filter が書くので（T-44）、"
            "--from filter で filter からやり直してください"
        )
    document = parse_narrative(pipeline.store.read_text(narrative_path), period=period)

    if period.is_weekly and isinstance(document, WeeklyNarrativeDocument):
        articles = pipeline.reports.read_weekly(period.text)
        industries = pipeline.config.tunable_thresholds.weekly.industries
        out(f"    週次シートの記事 {len(articles)} 件 / 対象業界 {len(industries)} 件")
        # ⚠️ **業界ごとに1通**（正規名に業界が入る＝T-46 Step 4）。1回の実行で
        # 業界数ぶんの HTML が出るので、生成物の列挙もその数だけ増える。
        # 入力（当週シート）は共通で、業界ごとに変わるのは**振り分けと生成テキスト**。
        return [
            pipeline.weekly_renderer.render(
                period=period.text,
                articles=articles,
                config=pipeline.config,
                narrative=to_weekly_narrative(document, industry),
                industry=industry,
                revision=pipeline.revision,
                run_id=run_id,
            ).path
            for industry in industries
        ]
    elif isinstance(document, MonthlyNarrativeDocument):
        cases = pipeline.reports.read_monthly(period.text)
        out(f"    月次シートの事例 {len(cases)} 件")
        rendered = pipeline.monthly_renderer.render(
            period=period.text,
            cases=cases,
            config=pipeline.config,
            narrative=to_monthly_narrative(document),
            revision=pipeline.revision,
            run_id=run_id,
        )
    else:  # pragma: no cover - parse_narrative が period で型を決めている
        raise PipelineError(
            f"{narrative_path} の種別が対象期間 {period.text} と噛み合いません"
        )

    return [rendered.path]


# --- 実行 --------------------------------------------------------------------


async def run(
    pipeline: Pipeline,
    period: Period,
    *,
    run_id: str,
    start_from: Step = Step.CRAWL,
    out: Callable[[str], None] = print,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """通しで実行する。終了コードを返す（例外で落ちない）。

    Args:
        pipeline: 使う相手一式
        period: 対象期間（表記の検証済み）
        run_id: この実行の ID（履歴退避の名前）
        start_from: 開始ステップ（`--from`）。手前のステップは**実行しない**
        out: 出力先（テストで差し替える）
        clock: 所要時間の計測（テストで差し替える）

    Returns:
        `EXIT_OK` / `EXIT_FAILED`
    """
    steps = STEP_ORDER[STEP_ORDER.index(start_from) :]
    out(f"=== 通し実行（T-45）: {period.text} ===")
    out(f"種別        : {'週刊' if period.is_weekly else '月刊'}（{period.kind}）")
    out(f"対象期間    : {period.start} 〜 {period.end}")
    out(f"config      : revision={pipeline.revision}（実行中は固定）")
    out(f"run_id      : {run_id}")
    out(f"成果物の置場: {pipeline.store.root}")
    out(f"実行ステップ: {' → '.join(step.value for step in steps)}")
    if start_from is not Step.CRAWL:
        skipped = STEP_ORDER[: STEP_ORDER.index(start_from)]
        out(
            f"（{' / '.join(step.value for step in skipped)} は --from の指定により"
            "実行しません。前の実行の成果物をそのまま使います）"
        )
    out("")

    artifacts: list[Path] = []
    total = len(steps)
    for number, step in enumerate(steps, start=1):
        out(f"[{number}/{total}] {step} 開始 …")
        started = clock()
        try:
            produced = await _run_step(step, pipeline, period, run_id=run_id, out=out)
        except Exception as exc:  # noqa: BLE001 - どの例外でも工程と型を見せて落とす
            out(_failure_report(step, exc, elapsed=clock() - started))
            _report_artifacts(artifacts, out=out, complete=False)
            return EXIT_FAILED

        artifacts.extend(produced)
        out(f"[{number}/{total}] {step} 完了（{clock() - started:.1f}秒）")
        out("")

    _report_artifacts(artifacts, out=out, complete=True)
    return EXIT_OK


async def _run_step(
    step: Step,
    pipeline: Pipeline,
    period: Period,
    *,
    run_id: str,
    out: Callable[[str], None],
) -> list[Path]:
    """1ステップ実行する（例外はそのまま呼び出し元へ通す）。"""
    if step is Step.CRAWL:
        return await run_crawl(pipeline, period, out=out)
    if step is Step.FILTER:
        return await run_filter(pipeline, period, run_id=run_id, out=out)
    return run_render(pipeline, period, run_id=run_id, out=out)


def _failure_report(step: Step, exc: BaseException, *, elapsed: float) -> str:
    """「どのステップで・どの例外か」（T-45 の完了条件）。

    ⚠️ **例外の型名をそのまま出す。** T-15 は原因ごとに型を分けており
    （`AIProcessError` なら未ログイン、`AITimeoutError` なら時間切れ …）、
    人向けの文言へ言い換えると、その分類がここで潰れる。
    """
    lines = [
        f"✗ {step} で失敗しました（{elapsed:.1f}秒）",
        f"    例外 : {type(exc).__name__}（{type(exc).__module__}）",
    ]
    if hint := failure_hint(exc):
        lines.append(f"    分類 : {hint}")
    if isinstance(exc, AIClientError):
        lines.append(
            "    ※ AI 呼び出しの失敗（T-15 の6分類："
            "AIUnavailableError / AITimeoutError / AIProcessError / "
            "AIProtocolError / AIResponseError / AIOutputParseError）"
        )
    lines.append(f"    内容 : {exc}")
    return "\n".join(lines)


def _report_artifacts(
    artifacts: Sequence[Path], *, out: Callable[[str], None], complete: bool
) -> None:
    """生成物のパスを列挙する（失敗時も、そこまでに作れたぶんを出す）。"""
    if complete:
        out(f"=== 完了: 生成物 {len(artifacts)} 件 ===")
    elif not artifacts:
        return
    else:
        out(f"=== 失敗するまでに書き出した成果物 {len(artifacts)} 件 ===")
    for path in artifacts:
        out(f"  {path}")


def _build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="run-pipeline",
        description=(
            "crawl → filter → render を通しで実行する（T-45 / 設計書 §8.2）。"
            "P7（T-26）の Run Orchestrator が入るまでの手動確認用。"
        ),
    )
    parser.add_argument(
        "kind",
        type=Kind,
        choices=list(Kind),
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
        dest="start_from",
        type=Step,
        choices=list(Step),
        default=Step.CRAWL,
        help=(
            "開始ステップ（既定 crawl）。filter なら収集済みの "
            "raw_articles を、render なら書き出し済みの中間xlsx と "
            "narrative を使って途中から再開する"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args: Namespace = _build_parser().parse_args(argv)
    settings = get_settings()
    now = datetime.now(tz=settings.tzinfo)

    try:
        period = (
            requested_period(args.kind, args.period)
            if args.period
            else parse_period(resolve_period(args.kind, today=now.date()))
        )
    except (PeriodError, PipelineError) as exc:
        print(f"中止しました: {exc}")
        return EXIT_INVALID_INPUT

    async def _run() -> int:
        # config は開始時に1回だけ読む（§6.3・§14 の固定参照）。**正はファイル**
        # なので `ConfigRepository.load()`。DB セッションを開くのは、この口が
        # 改訂履歴も扱うため（読むだけなら DB へは書かない。T-11・T-14 と同じ形）。
        async with db_manager.session() as db:
            repo = ConfigRepository.from_settings(db, settings)
            try:
                config = repo.load()
            except (ConfigRepositoryError, DocumentParseError) as exc:
                print(f"中止しました: {exc}")
                return EXIT_INVALID_INPUT

        return await run(
            build_pipeline(config, settings=settings),
            period,
            run_id=new_run_id(now),
            start_from=args.start_from,
        )

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("中止しました: 中断されました。")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXIT_FAILED",
    "EXIT_INVALID_INPUT",
    "EXIT_OK",
    "FAILURE_HINTS",
    "STEP_ORDER",
    "Kind",
    "Pipeline",
    "PipelineError",
    "Step",
    "build_pipeline",
    "failure_hint",
    "main",
    "new_run_id",
    "requested_period",
    "resolve_period",
    "run",
    "run_crawl",
    "run_filter",
    "run_render",
]
