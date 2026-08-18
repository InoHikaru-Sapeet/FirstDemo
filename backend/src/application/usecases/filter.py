"""filter オーケストレーション（設計書 §6.1 ／ 仕様書 §13.3 ／ T-21）。

`raw_articles_{period}.json`（T-16 の出力）を入力に、除外・重複統合・分類採点・
採否・§12 検証を通し、**中間xlsx へ書ける形の行**（週次22列 / 月次8列）と
**除外ログ行**（6列）と `validation_{period}.json` と `narrative_{period}.json` を
作る。

**xlsx を書くのは T-22。** この層は「何を書くか」までを決め、ファイルとして書くのは
`validation_{period}.json`（T-20 申し送り③）と `narrative_{period}.json`（決定3・
T-44）だけ（シリアライズは T-06、置き場は T-02）。

---

**⚠️ 生成テキストもこの段で作る**（2026-08-16 の決定3 ／ T-44）

設計書 §7.3・§7.4 が「（生成テキスト）」としている今週のポイント・示唆ボックス・
巻頭言・章導入文・むすびは、**中間xlsx の列（§8 の確定値）には入らない**。
render は §1.1 により AI を呼べないので、**採用記事・事例が確定した後**に
この層で生成し、`narrative_{period}.json` として渡す
（`application.usecases.narrative`）。

⚠️ **段は増やしていない**（crawl → filter → render の3段構成のまま。§3.1 の
3プロンプトとの1:1 対応・T-26 の状態機械 §8.4）。増えるのは **period ごとに
AI 往復1回**だけで、記事ごとの往復は足していない（T-15 備考の CLI オーバーヘッド）。

---

**⚠️ 手順の順序（2026-08-16 の決定1。§6.1 の擬似コードとの差分）**

    1. 分類・10タグ・6軸採点 ＋ 除外判定に要る事実の申告（AI 1往復・T-19）
    2. 除外判定（T-17）              → 除外なら除外ログへ
    3. 重複・統合判定（T-18）        → 重複なら代表へ統合し除外ログへ
    4. 採否（§13.3-5）               → 低スコア／信頼性不足なら除外ログへ
       （合計・区分決定・`low_priority` の降格は T-19 の `decide_adoption()`）
    5. フォーマットチェック（T-20）  → error なら本編から外し除外ログへ
    6. 合計スコア降順で整列 → 週次22列 ／ 月次は事例へ昇格して8列

§6.1 は除外判定（1）を分類（3）より前に置いているが、**除外判定に要る事実
（当たったルール番号・鮮度）を作れるのは AI だけ**で、その AI 呼び出しが分類・採点
そのもの。選別用の呼び出しを別に立てると1記事あたりの往復が2倍になる（1回数分。
T-15 備考）ので、同じ1往復にまとめた（→ TASKS.md T-21 備考・T-38）。

この順序では `default_exclude` の例外採用（§5.4）の判定に **確定した合計点**を
渡せる（§6.2 の `estimate_total(a)`＝合計見込みの代用が要らない）。

---

**⚠️ 診断はログにだけ出す**（T-46 Step 2）

実行の終わりに **採用記事のカテゴリ分布**と**スコア分布（最高 / 中央値 / 最低）**、
月次はさらに**事例昇格の内訳**（`enterprise_ai_case` 該当 / `min_score_for_case`
以上 / `target_case_count` の絞り）を INFO で出す。初運用（2026-W33・2026-07）で
「事例0件」「業界関連トピック0件」の原因を後から診断できなかったのは、
**採用記事の一覧がどの成果物にも残らない**ため（月次実行は月次8列の cases しか
書かない）。

⚠️ **成果物は増やさない。** 週次22列 / 月次8列は §8 の確定値、
`validation_{period}.json` は §2.4 のスキーマで、どちらも診断のために広げない。

---

**config は実行開始時に固定する**（§6.3・§14）

`FilterWorker` は渡された config を**深いコピーで抱え込む**。実行中に admin が
保存しても、また呼び出し元が同じオブジェクトを書き換えても、判断基準は動かない
（`test_the_config_is_pinned_for_the_whole_run`）。revision は結果に載せて監査へ
渡す（§9.2 の「`prompt_version` と `config.revision` を記録」の後者）。

---

**⚠️ 既知の割り切り**

- **`validation_{period}.json` の行番号は「整列後・不備除外前」の並びで数える**
  （T-20 の `check_articles()` の仕様）。フォーマット不備で外れた記事があると、
  実際の週次シートの行とその件数ぶんずれる。ずれを消すには検証レポート側に記事の
  識別子が要るが、§2.4 のスキーマは `{row, field, reason}` しか持たない（→ T-38）。
- **除外ログは週次ブックの `除外ログ` シートに一本化する**（§8.1 が除外ログを
  週次ブックの構成として定義しているため）。月次実行で出た除外もここへ積む。
  分けると重複判定（§11.1）の参照元が2箇所に割れる。
- **月次の参照範囲から対象月そのものを外す**。§11.1 は当月を含むと書いているが、
  当月の cases は**この実行の出力**なので、含めると再実行で全件が「既出」になる
  （§14 冪等性。T-18 が呼び出し側の責任としている点）。
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Protocol

from adapter.llm import AIClient
from adapter.llm.ai_client import AICallMeta
from adapter.storage.artifact_store import ArtifactStore
from application.usecases.classify_and_score import (
    AnalyzedArticle,
    ArticleClassifier,
    ClassifiedArticle,
    total_score,
)
from application.usecases.monthly_cases import (
    CASE_CATEGORY_ID,
    CaseCandidate,
    MonthlyCase,
    MonthlyCaseBuilder,
    select_cases,
)
from application.usecases.narrative import NarrativeBuilder
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.narrative import (
    NarrativeDocument,
    dump_narrative,
)
from enterprise.entities.period import Period, PeriodError, parse_period
from enterprise.entities.raw_article import RawArticle, parse_raw_articles
from enterprise.entities.report_columns import (
    EXCLUSION_LOG_COLUMNS,
    MULTI_VALUE_SEPARATOR,
    WEEKLY_ARTICLE_COLUMNS,
    format_row,
)
from enterprise.entities.validation_report import (
    ValidationReport,
    dump_validation_report,
)
from enterprise.services.dedup import (
    DedupHistory,
    deduplicate,
    duplicate_log_entry,
    monthly_periods_in_scope,
    weekly_periods_in_scope,
)
from enterprise.services.exclusion import (
    ExclusionVerdict,
    ScreenedArticle,
    evaluate_exclusions,
    exclusion_log_entry,
)
from enterprise.services.format_check import (
    check_articles,
    format_error_log_entry,
)

logger = logging.getLogger(__name__)

# 採否（§13.3-5）で外した記事の `除外区分`。§2.2.2 は除外区分を「等」と書いて
# 閉じていないので enum にしない（`完全除外` 等は T-17、`統合` は T-18、
# `フォーマット不備` は T-20 がそれぞれ持つ）。
CATEGORY_LOW_SCORE = "低スコア/信頼性不足"

# 採否の理由（除外ログの `除外理由`）。**しきい値と実測値の両方を残す**
# （config を変えたときに「どの基準で落ちたか」が後から読めるように）。
REASON_TOTAL_BELOW = "合計スコア {total} < {threshold}"
REASON_RELIABILITY_BELOW = "信頼性点 {score} < {threshold}"
REASON_SEPARATOR = " / "

# 信頼性の軸ID（§13.3-5 の「信頼性点」）。config の `scoring_axes[].id`。
RELIABILITY_AXIS_ID = "reliability"

# 参照する週次22列（列名の正は T-07。ここは「どの列を見るか」だけを持つ）。
COLUMN_CATEGORY = "情報カテゴリ"
COLUMN_TOTAL_SCORE = "合計スコア"
COLUMN_INDUSTRY = "業界"
COLUMN_URL = "URL"

# 診断ログの区切り（T-46 Step 2）。
DIAGNOSTIC_SEPARATOR = " / "


class FilterError(Exception):
    """フィルタに使えない入力（period 表記の誤り・入力ファイルが無い等）。

    ⚠️ **AI 呼び出しの失敗はこれに包まない。** `AIClientError` とその
    サブクラス（T-15）が原因ごとに分かれており、ジョブの再実行判断に使うため
    そのまま呼び出し元へ通す。
    """


class RawArticlesNotFoundError(FilterError):
    """`raw_articles_{period}.json` が無い（crawl が未実行）。"""


class HistoryReader(Protocol):
    """既出記事（過去週のシート＋除外ログ）を読む口（§11.1 の参照範囲）。

    実体は中間xlsx のリーダ（T-22）。**この層は xlsx の構造を知らない**
    （enterprise 層が読み書きの形を持たないのと同じ理由で、差し替え口にしてある）。
    """

    def read_history(self, periods: Sequence[str]) -> DedupHistory:
        """指定 period の既出記事を、**新しい period から順**に返す。

        順序がそのまま代表の優先順になる（設計書 §6.3。T-18 申し送り②）。
        """
        ...


@dataclass(frozen=True, slots=True)
class FilterResult:
    """filter の結果（T-22 が xlsx へ、T-26 が監査ログへ使う）。

    Attributes:
        period: 対象期間
        config_revision: 実行中に固定していた config の revision（§9.2）
        articles: 週次22列の行（**合計スコア降順**・§12 を通ったものだけ）
        cases: 月次8列の行（`No` 昇順＝章グルーピング順）。週次実行では空
        exclusion_log: 除外ログ6列の行（発生順）
        validation: `validation_{period}.json` の中身
        validation_path: 実際に書き出した先
        narrative: `narrative_{period}.json` の中身（生成テキスト。決定3・T-44）
        narrative_path: 実際に書き出した先
        classified: `articles` と同じ並びの分類結果（監査・T-24 の手がかり）
        monthly_cases: `cases` の組み立て前の形（章・段落を持つ）
        ai_calls: 行った AI 呼び出しのメタ（記事1件につき1回＋月次の事例ぶん
            ＋生成テキストの1回）
    """

    period: Period
    config_revision: int
    articles: list[dict[str, Any]]
    cases: list[dict[str, Any]]
    exclusion_log: list[dict[str, Any]]
    validation: ValidationReport
    validation_path: Path
    narrative: NarrativeDocument
    narrative_path: Path
    classified: list[ClassifiedArticle] = field(default_factory=list)
    monthly_cases: list[MonthlyCase] = field(default_factory=list)
    ai_calls: tuple[AICallMeta, ...] = ()

    @property
    def article_count(self) -> int:
        return len(self.articles)

    @property
    def excluded_count(self) -> int:
        return len(self.exclusion_log)

    def exclusion_log_rows(self) -> list[list[str | int | None]]:
        """除外ログを xlsx の列順（6列）に並べる（T-22 のライタへ渡す形）。"""
        return [
            format_row(EXCLUSION_LOG_COLUMNS, entry) for entry in self.exclusion_log
        ]

    def article_rows(self) -> list[list[str | int | None]]:
        """週次22列を xlsx の列順に並べる（T-22 のライタへ渡す形）。"""
        return [format_row(WEEKLY_ARTICLE_COLUMNS, record) for record in self.articles]


@dataclass(frozen=True, slots=True)
class _Kept:
    """除外を通り抜けた記事1件（重複判定の前）。"""

    analyzed: AnalyzedArticle
    verdict: ExclusionVerdict


class FilterWorker:
    """1つの period を通しでフィルタする（設計書 §6.1 の手順）。

    組み立て方（T-26 のジョブから使う想定）:

        worker = FilterWorker(
            client=get_ai_client(),
            store=ArtifactStore.from_settings(),
            config=await repo.get_pinned(revision),   # ← 実行開始時に固定
            history_reader=reader,                    # ← 中間xlsx のリーダ（T-22）
        )
        result = await worker.run("2026-W31")
    """

    def __init__(
        self,
        *,
        client: AIClient,
        store: ArtifactStore,
        config: IntelligenceConfig,
        history_reader: HistoryReader | None = None,
        timeout: float | None = None,
    ) -> None:
        """
        Args:
            client: AI クライアント（`adapter.llm.get_ai_client()`）。
                ⚠️ web 検索は要らない（収集は済んでいる）
            store: 成果物の置き場（T-02）
            config: 実行開始時に固定参照している config。**深いコピーを取る**
            history_reader: 既出記事を読む口。`None` なら履歴なしで動く
                （初回実行。過去週との重複が検出できない旨を警告に出す）
            timeout: 1回の AI 呼び出しの制限時間（秒）。`None` なら実装の既定
                （`Settings.ai_timeout_seconds` = 10分）。⚠️ crawl 用の30分ではない
        """
        # ⚠️ 深いコピー。呼び出し元が同じオブジェクトを持ち回して書き換えても、
        # 実行中に判断基準が変わらないようにする（§6.3・§14 の「固定参照」）。
        self._config = config.model_copy(deep=True)
        self._store = store
        self._history_reader = history_reader
        self._classifier = ArticleClassifier(
            client=client, config=self._config, timeout=timeout
        )
        self._case_builder = MonthlyCaseBuilder(
            client=client, config=self._config, timeout=timeout
        )
        self._narrator = NarrativeBuilder(
            client=client, config=self._config, timeout=timeout
        )

    @property
    def config(self) -> IntelligenceConfig:
        """固定参照している config（実行中に差し替わらない）。"""
        return self._config

    @property
    def revision(self) -> int:
        """固定参照している config の revision（§9.2 の記録対象）。"""
        return self._config.meta.revision

    async def run(self, period: str, *, run_id: str | None = None) -> FilterResult:
        """1つの period をフィルタする（モジュール冒頭の手順）。

        Args:
            period: `2026-W31`（週次）または `2026-07`（月次）
            run_id: ジョブ実行ID（T-26）。`narrative_{period}.json` を上書きする
                前の退避先の名前に使う（設計判断B）。**`None` なら退避しない**
                （手元での単発実行。退避先のディレクトリ名を作れないため）

        Returns:
            中間xlsx へ書ける行・除外ログ・検証レポート

        Raises:
            FilterError: period の表記が不正／実在しない期間
            RawArticlesNotFoundError: crawl の出力が無い
            DocumentParseError: `raw_articles_{period}.json` が壊れている
            AIClientError: AI 呼び出しの失敗（原因ごとのサブクラス。握り潰さない）
        """
        parsed = self._parse(period)
        articles = self._load_articles(parsed)
        history = self._load_history(parsed)

        logger.info(
            "filter started (period=%s, revision=%d, articles=%d, history=%d)",
            parsed.text,
            self.revision,
            len(articles),
            len(history),
        )

        exclusion_log: list[dict[str, Any]] = []
        metas: list[AICallMeta] = []

        # 1〜2) 分類・採点＋事実申告（AI 1往復）→ 除外判定
        kept: list[_Kept] = []
        for article in articles:
            analyzed = await self._classifier.analyze(article)
            metas.append(analyzed.meta)
            screened = screened_article(analyzed, self._config)
            verdict = evaluate_exclusions(screened, self._config)
            if verdict.is_excluded:
                exclusion_log.append(exclusion_log_entry(screened, verdict))
                continue
            kept.append(_Kept(analyzed=analyzed, verdict=verdict))

        # 3) 重複・統合判定
        dedup_result = deduplicate(
            [item.analyzed.article for item in kept],
            history,
            self._config.tunable_thresholds.dedup,
            period=parsed.text,
        )
        for duplicate in dedup_result.duplicates:
            exclusion_log.append(duplicate_log_entry(duplicate))

        # ⚠️ 代表 → 分析結果の対応は**オブジェクトの同一性**で引く。同じ内容の
        # 記事が2件あっても（crawl は重複を落とさない）取り違えない。
        analyzed_by_article = {id(item.analyzed.article): item for item in kept}

        # 4) 採否（合計・区分決定・降格は T-19 の `decide_adoption()` 経由）
        pairs: list[tuple[dict[str, Any], ClassifiedArticle]] = []
        for representative in dedup_result.representatives:
            item = analyzed_by_article[id(representative.article)]
            classified = self._classifier.finalize(item.analyzed, verdict=item.verdict)
            if reason := rejection_reason(classified, self._config):
                exclusion_log.append(
                    low_score_log_entry(representative.article, reason)
                )
                continue
            pairs.append(
                (
                    weekly_record(classified, source_text=representative.source_text),
                    classified,
                )
            )

        # 6) 合計スコア降順で整列（§8.1）。**5) の前に行う**のは、§12 の行番号が
        # 週次シートの行位置だから（T-20 申し送り①「降順に整列した後の一覧を渡す」）。
        # 同点は入力順＝収集順のまま（安定ソート）。
        pairs.sort(key=lambda pair: -int(pair[0][COLUMN_TOTAL_SCORE]))

        # 5) フォーマットチェック（error のある記事は本編から外す）
        check = check_articles([record for record, _ in pairs], self._config)
        rejected_ids = {id(rejected.record) for rejected in check.rejected}
        for rejected in check.rejected:
            exclusion_log.append(format_error_log_entry(rejected.record))

        accepted = [pair for pair in pairs if id(pair[0]) not in rejected_ids]
        records = [record for record, _ in accepted]
        classified_articles = [classified for _, classified in accepted]

        cases = await self._build_cases(records, classified_articles, parsed)
        metas.extend(self._case_builder.ai_calls)
        # ⚠️ **業界タグは事例へ昇格した行から写す**（T-52 Step 2）。月次8列に
        # 「業界」の列は無く（§8.2 の確定値）、月次実行は22列を1行も書かないので、
        # ここで拾わないと閲覧ページの業界チップの材料がどこにも残らない。
        # **AI には聞かない**（列19 は T-19 が config の候補から選んだ確定値）。
        case_industries = self._case_industries(records, cases)

        # ⚠️ **生成テキストは採用が確定した後**（決定3・T-44）。採否・重複・
        # フォーマット不備で外れた記事の示唆を書かせない（無駄な出力であるうえ、
        # 載らない記事の文章がファイルに残ると混乱する）。
        narrative = await self._build_narrative(
            records, cases, parsed, industries=case_industries
        )
        metas.extend(self._narrator.ai_calls)

        validation_path = self._write_validation(check.report, parsed)
        narrative_path = self._write_narrative(narrative, parsed, run_id=run_id)

        logger.info(
            "filter finished (period=%s, adopted=%d, cases=%d, excluded=%d, ok=%s)",
            parsed.text,
            len(records),
            len(cases),
            len(exclusion_log),
            check.report.ok,
        )
        # ⚠️ **診断は成果物ではなくログで出す**（T-46 Step 2）。xlsx の列（§8 の
        # 確定値）も `validation_*.json` のスキーマ（§2.4）も増やさない。
        logger.info(
            "filter category distribution (period=%s, adopted=%d, %s)",
            parsed.text,
            len(records),
            format_category_distribution(records, self._config),
        )
        logger.info(
            "filter score distribution (period=%s, adopted=%d, %s)",
            parsed.text,
            len(records),
            format_score_distribution(records),
        )
        return FilterResult(
            period=parsed,
            config_revision=self.revision,
            articles=records,
            cases=[case.to_row() for case in cases],
            exclusion_log=exclusion_log,
            validation=check.report,
            validation_path=validation_path,
            narrative=narrative,
            narrative_path=narrative_path,
            classified=classified_articles,
            monthly_cases=cases,
            ai_calls=tuple(metas),
        )

    # --- 内部 -------------------------------------------------------------

    def _parse(self, period: str) -> Period:
        try:
            return parse_period(period)
        except PeriodError as exc:
            raise FilterError(str(exc)) from exc

    def _load_articles(self, period: Period) -> list[RawArticle]:
        """crawl の出力を読む（**この層は直接 `open()` しない**。T-02 経由）。"""
        path = self._store.raw_articles_path(period.text)
        if not self._store.exists(path):
            raise RawArticlesNotFoundError(
                f"{path} がありません。先に crawl（T-16）を実行してください"
            )
        return parse_raw_articles(self._store.read_text(path))

    def _load_history(self, period: Period) -> DedupHistory:
        """§11.1 の参照範囲ぶんの既出記事を読む。

        ⚠️ **対象 period は含めない。** 週次は `weekly_periods_in_scope()` が
        そもそも手前しか返さない。月次は §11.1 が当月を含むと書いているが、当月の
        cases はこの実行の出力なので外す（モジュール冒頭「既知の割り切り」）。
        """
        dedup = self._config.tunable_thresholds.dedup
        if period.is_weekly:
            periods = weekly_periods_in_scope(period.text, dedup.lookback_weeks)
        else:
            periods = [
                candidate
                for candidate in monthly_periods_in_scope(
                    period.text, dedup.monthly_lookback_months
                )
                if candidate != period.text
            ]

        if self._history_reader is None:
            logger.warning(
                "履歴リーダが無いので過去期間との重複を検出しません"
                "（period=%s / 参照予定 %d 期間）",
                period.text,
                len(periods),
            )
            return DedupHistory()
        return self._history_reader.read_history(periods)

    async def _build_cases(
        self,
        records: Sequence[Mapping[str, Any]],
        classified: Sequence[ClassifiedArticle],
        period: Period,
    ) -> list[MonthlyCase]:
        """月次だけ、採用記事を事例へ昇格させる（§13.3 出力1）。"""
        if not period.is_monthly:
            return []

        monthly = self._config.tunable_thresholds.monthly
        selection = select_cases(
            records, [item.article for item in classified], self._config
        )
        candidates = [
            CaseCandidate(
                article=classified[index].article,
                total_score=classified[index].total_score,
                summary=classified[index].summary,
            )
            for index in selection.indexes
        ]
        # ⚠️ **3条件の内訳を出す**（T-46 Step 2）。初運用（2026-07）で事例が0件に
        # なったとき、カテゴリ該当が0件なのか・しきい値落ちなのか・件数の絞りなのかを
        # 後から診断できなかった（採用記事の一覧はどの成果物にも残らない）。
        logger.info(
            "monthly case selection (period=%s, adopted=%d, %s=%d, >=min_score_for_case"
            "(%d)=%d, dropped_by_target_case_count(%d)=%d, promoted=%d)",
            period.text,
            len(records),
            CASE_CATEGORY_ID,
            selection.category_matched,
            monthly.min_score_for_case,
            selection.above_min_score,
            monthly.target_case_count,
            selection.dropped_by_target_count,
            len(candidates),
        )
        if not candidates:
            logger.warning(
                "事例へ昇格できる記事がありません（period=%s。"
                "情報カテゴリ=%s かつ 合計スコア >= %d が条件）",
                period.text,
                CASE_CATEGORY_ID,
                monthly.min_score_for_case,
            )
        return await self._case_builder.build(candidates, period=period.text)

    def _case_industries(
        self,
        records: Sequence[Mapping[str, Any]],
        cases: Sequence[MonthlyCase],
    ) -> dict[int, list[str]]:
        """事例の `No` → 業界タグ（週次22列 列19 の値。T-52 Step 2）。

        ⚠️ **引き当ては URL**。事例の行（月次8列）は22列の行から作られており、
        列22「URL」は §12.1 の非空必須項目で記事を一意に指せる唯一の列
        （示唆の鍵と同じ理由＝T-44）。`No` は事例側の通し番号なので22列からは引けない。

        ⚠️ **見つからない事例は鍵ごと置かない**（空の配列を持たせない）。
        """
        by_url: dict[str, list[str]] = {}
        for record in records:
            url = str(record.get(COLUMN_URL) or "").strip()
            if url:
                by_url.setdefault(url, industry_tags(record))
        return {
            case.no: tags
            for case in cases
            if (tags := by_url.get(case.url.strip(), []))
        }

    async def _build_narrative(
        self,
        records: Sequence[Mapping[str, Any]],
        cases: Sequence[MonthlyCase],
        period: Period,
        *,
        industries: Mapping[int, Sequence[str]] | None = None,
    ) -> NarrativeDocument:
        """生成テキストを作る（決定3・T-44。**period ごとに AI 1往復**）。

        週次は当週シートの行から（今週のポイント＋記事ごとの示唆）、月次は
        事例から（巻頭言・章導入文・むすび）。**どちらか一方だけ**を作る
        （週次実行に巻頭言は要らず、月次実行に今週のポイントは要らない）。
        """
        if period.is_weekly:
            return await self._narrator.build_weekly(records, period=period)
        return await self._narrator.build_monthly(
            cases, period=period, industries=industries
        )

    def _write_validation(self, report: ValidationReport, period: Period) -> Path:
        """`validation_{period}.json` を書く（T-20 申し送り③）。"""
        path = self._store.validation_path(period.text)
        self._store.write_text(path, dump_validation_report(report))
        return path

    def _write_narrative(
        self, document: NarrativeDocument, period: Period, *, run_id: str | None
    ) -> Path:
        """`narrative_{period}.json` を書く（決定3・T-44）。

        ⚠️ **退避が先**（設計判断B。T-22 の `_save()`・T-24 の `render()` と同じ
        順序）。上書きしてから退避すると、退避されるのは新しい内容になる。
        """
        path = self._store.narrative_path(period.text)
        archived = (
            self._store.archive(
                path, period=period.text, revision=self.revision, run_id=run_id
            )
            if run_id is not None
            else None
        )
        self._store.write_text(path, dump_narrative(document))

        logger.info("narrative written (path=%s, archived=%s)", path, archived)
        return path


# --- 各層のつなぎ（この決定が T-21 の担当）----------------------------------


def screened_article(
    analyzed: AnalyzedArticle, config: IntelligenceConfig
) -> ScreenedArticle:
    """分類・採点の結果を除外判定（T-17）の入力へ写す。

    ⚠️ **`estimated_total_score` に渡すのは確定した合計点**（6軸の和）。§6.2 の
    擬似コードは `estimate_total(a)`＝合計見込みだが、決定1 の順序では採点が
    先に終わっているので見積もる理由が無い。§5.4 の条件（「総合スコアがしきい値超
    なら例外採用」）にはこちらの方が忠実（→ TASKS.md T-21 備考）。

    ⚠️ **顧客関連度も確定したタグの値**（`enums.customer_relevance` のいずれか）。

    Args:
        analyzed: `ArticleClassifier.analyze()` の結果
        config: 実行時 config（軸の集合の正）

    Returns:
        事実だけを載せた `ScreenedArticle`
    """
    return ScreenedArticle(
        article=analyzed.article,
        matched_rule_nos=analyzed.facts.matched_rule_nos,
        customer_relevance=analyzed.tags.get("customer_relevance"),  # ty: ignore[invalid-argument-type]
        estimated_total_score=total_score(analyzed.scores, config),
        is_stale=analyzed.facts.is_stale,
    )


def weekly_record(
    classified: ClassifiedArticle, *, source_text: str | None = None
) -> dict[str, Any]:
    """週次22列の1行を組み立てる（列名 → 値）。

    ⚠️ **列名と順序をここに書かない。** どの列がどの軸・どのタグかは T-07 の
    `axis_id` / `tag_id` が持っている（`WEEKLY_ARTICLE_COLUMNS`）。列が増減しても
    この関数は追随する。

    Args:
        classified: 分類・採点・採用区分まで決まった1件
        source_text: `ソース` 欄。統合した代表なら `A / B(統合)`（§11.3・T-18）。
            `None` なら記事の媒体名そのまま

    Returns:
        列名 → 値（multi タグは `list`。xlsx への変換は T-07 の `format_cell`）

    Raises:
        FilterError: 列に対応する値が無い場合（列定義と組み立ての食い違い）
    """
    article = classified.article
    fixed: dict[str, Any] = {
        "収集日": article.collected_at,
        "タイトル": article.title,
        "一言要約": classified.summary,
        "合計スコア": classified.total_score,
        "ソース": article.source if source_text is None else source_text,
        "URL": article.url,
    }

    record: dict[str, Any] = {}
    for column in WEEKLY_ARTICLE_COLUMNS:
        if column.axis_id is not None:
            record[column.name] = classified.scores[column.axis_id]
        elif column.tag_id is not None:
            value = classified.tags[column.tag_id]
            record[column.name] = list(value) if isinstance(value, tuple) else value
        elif column.name in fixed:
            record[column.name] = fixed[column.name]
        else:  # pragma: no cover - 列が増えたら実装を足すべき、という合図
            raise FilterError(
                f"週次22列の {column.name!r} に入れる値が決まっていません。"
                "T-07 の列定義を変えたら、この組み立ても合わせてください"
            )
    return record


def rejection_reason(
    classified: ClassifiedArticle, config: IntelligenceConfig
) -> str | None:
    """採否（§13.3-5）で外すなら、その理由を返す。

    Returns:
        除外理由。採用するなら `None`
    """
    return rejection_reason_for_scores(
        classified.total_score, classified.scores.get(RELIABILITY_AXIS_ID), config
    )


def rejection_reason_for_scores(
    total: int, reliability: int | None, config: IntelligenceConfig
) -> str | None:
    """採否（§13.3-5）を**点数だけ**から判定する。

    条件は2つで、**どちらか一方でも下回れば除外**:

    - 合計スコア < `min_total_score_to_publish`
    - 信頼性点 < `min_reliability_score_to_publish`

    境界は `≥` が採用（しきい値ちょうどは載せる）。T-17 の例外採用・T-19 の区分
    決定と同じ向きに揃えてある。

    ⚠️ **採否の判定はこの関数1つが正。** ドライラン（T-29）は保存済みの採点結果
    （中間xlsx の軸点列）へ新しいしきい値を当て直すので、`ClassifiedArticle` を
    持たない。**そちら側に判定を写さない**ため、点数だけを入口にしてある
    （写すと「保存では落ちるのにプレビューでは通る」が起きる）。

    Args:
        total: 合計スコア（6軸の和）
        reliability: 信頼性軸の点数。**`None` なら信頼性の条件は見ない**
        config: 判定に使う config（ドライランでは候補 config）

    Returns:
        除外理由。採用するなら `None`
    """
    thresholds = config.tunable_thresholds
    reasons: list[str] = []

    if total < thresholds.min_total_score_to_publish:
        reasons.append(
            REASON_TOTAL_BELOW.format(
                total=total, threshold=thresholds.min_total_score_to_publish
            )
        )

    if (
        reliability is not None
        and reliability < thresholds.min_reliability_score_to_publish
    ):
        reasons.append(
            REASON_RELIABILITY_BELOW.format(
                score=reliability,
                threshold=thresholds.min_reliability_score_to_publish,
            )
        )

    return REASON_SEPARATOR.join(reasons) if reasons else None


# --- 診断（T-46 Step 2。**ログにだけ出す**）-----------------------------------


def category_distribution(
    records: Sequence[Mapping[str, Any]], config: IntelligenceConfig
) -> dict[str, int]:
    """採用記事のカテゴリ分布（カテゴリID → 件数）。

    ⚠️ **0件のカテゴリも 0 として残す**（config の7カテゴリを起点に数える）。
    初運用（2026-W33）で問題になったのは「`enterprise_ai_case` が **0件**」という
    事実そのもので、出現したカテゴリだけを並べるとそれが読めない。

    config に無いカテゴリID が行に入っていたら末尾へ足す（黙って落とさない。
    分類は config の候補から選ばせている＝T-19 ので、本来は起きない）。
    """
    counts: dict[str, int] = {
        str(category.id): 0 for category in config.information_categories
    }
    for record in records:
        key = str(record.get(COLUMN_CATEGORY) or "")
        counts[key] = counts.get(key, 0) + 1
    return counts


def format_category_distribution(
    records: Sequence[Mapping[str, Any]], config: IntelligenceConfig
) -> str:
    """カテゴリ分布をログ1行の形へ（件数降順 → カテゴリID の定義順）。"""
    counts = category_distribution(records, config)
    order = {key: index for index, key in enumerate(counts)}
    ranked = sorted(counts.items(), key=lambda item: (-item[1], order[item[0]]))
    return DIAGNOSTIC_SEPARATOR.join(f"{key}={count}" for key, count in ranked)


def score_distribution(records: Sequence[Mapping[str, Any]]) -> list[int]:
    """採用記事の合計スコア（読めない値は数えない）。"""
    scores: list[int] = []
    for record in records:
        value = record.get(COLUMN_TOTAL_SCORE)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        scores.append(value)
    return scores


def format_score_distribution(records: Sequence[Mapping[str, Any]]) -> str:
    """スコア分布（最高 / 中央値 / 最低）をログ1行の形へ。

    §5.2 のしきい値（`min_score_for_case` = 80 / `propose_next_meeting` = 85）に
    届く記事が構造的に無いことは、この3つの値があれば実行ログから読める
    （初運用の実測は 60〜73 に密集していた）。
    """
    scores = score_distribution(records)
    if not scores:
        return "スコアなし（採用0件）"
    return DIAGNOSTIC_SEPARATOR.join(
        [
            f"max={max(scores)}",
            f"median={_number(median(scores))}",
            f"min={min(scores)}",
        ]
    )


def _number(value: float) -> str:
    """中央値の表示（偶数件では .5 になるので、整数のときだけ整数で出す）。"""
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def industry_tags(record: Mapping[str, Any]) -> list[str]:
    """週次22列 列19「業界」（multi）を読む（T-52 Step 2）。

    リーダは `list[str]` を返すが、xlsx から直に読んだ場合は `;` 区切りの文字列に
    なりうるので両方を受ける（区切りは T-07 の定義だけを使う）。
    """
    value = record.get(COLUMN_INDUSTRY)
    if value is None:
        return []
    if isinstance(value, str):
        parts: list[str] = value.split(MULTI_VALUE_SEPARATOR)
    elif isinstance(value, (list, tuple)):
        parts = [str(part) for part in value]
    else:
        parts = [str(value)]
    return [part.strip() for part in parts if part.strip()]


def low_score_log_entry(article: RawArticle, reason: str) -> dict[str, Any]:
    """採否で外した記事の除外ログ1行（§13.3-5 `除外区分=低スコア/信頼性不足`）。"""
    return {
        "収集日": article.collected_at,
        "タイトル": article.title,
        "URL": article.url,
        "ソース": article.source,
        "除外区分": CATEGORY_LOW_SCORE,
        "除外理由": reason,
    }


__all__ = [
    "CATEGORY_LOW_SCORE",
    "COLUMN_CATEGORY",
    "COLUMN_INDUSTRY",
    "COLUMN_TOTAL_SCORE",
    "COLUMN_URL",
    "FilterError",
    "FilterResult",
    "FilterWorker",
    "HistoryReader",
    "RawArticlesNotFoundError",
    "category_distribution",
    "format_category_distribution",
    "format_score_distribution",
    "industry_tags",
    "low_score_log_entry",
    "score_distribution",
    "rejection_reason",
    "rejection_reason_for_scores",
    "screened_article",
    "weekly_record",
]
