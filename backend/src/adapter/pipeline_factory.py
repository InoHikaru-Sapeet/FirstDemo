"""パイプラインの相手一式を本番の実装で組み立てる（T-26。設計書 §8.2）。

`RunOrchestrator`（`application.usecases.run_orchestrator`）は3段の**段取り**だけを
持ち、相手は `Pipeline`（Protocol の束）として受け取る。ここはその Protocol を
**具象アダプタで満たす1箇所**で、CLI（`make run-weekly`）と API（`POST /run`）の
両方がこれを使う。

⚠️ **T-45 の `adapter.cli.run_pipeline.build_pipeline()` をここへ移した。**
CLI の中に置いたままだと、HTTP のルーターが `adapter.cli` を import することに
なる（運用コマンドが API の依存に入る）。中身は変えていない。

⚠️ **AI クライアントは用途ごとに取る。** crawl だけが web 検索を要求する
（`get_ai_client(web_search=True)`）。filter 側へ web 検索を渡さないのは、収集は
もう済んでいて、この段で検索させる必要が無いため（T-16 / T-21）。
"""

from adapter.html.monthly_renderer import MonthlyRenderer
from adapter.html.weekly_renderer import WeeklyRenderer
from adapter.llm import get_ai_client
from adapter.storage.artifact_store import ArtifactStore
from adapter.xlsx.report_writer import ReportStore
from application.usecases.crawl import CrawlWorker
from application.usecases.filter import FilterWorker
from application.usecases.run_orchestrator import Pipeline
from config import Settings, get_settings
from enterprise.entities.config import IntelligenceConfig


def build_pipeline(
    config: IntelligenceConfig, *, settings: Settings | None = None
) -> Pipeline:
    """固定した config でパイプラインを組む。

    Args:
        config: **実行開始時に固定した** config（§6.3・§14）。ワーカーはこの
            オブジェクトを抱えるので、実行中に `config.json` が書き換わっても
            判断基準は動かない
        settings: 差し替え用（既定はプロセスの設定）

    Returns:
        3段の相手一式
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


__all__ = ["build_pipeline"]
