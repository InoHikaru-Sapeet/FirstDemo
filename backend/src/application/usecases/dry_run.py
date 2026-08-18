"""ドライラン再フィルタ（T-29。設計書 §3.3・§3.4 ／ 設計判断C ／ 仕様書 §7.3-5）。

未保存の config（`candidate_config_patch`）を、**既に採点し終えているデータ**へ
当て直して「この基準なら何件になるか」を試算する。出力は
`scratch/dry-run/{dry_run_id}/` にだけ書き、正規の成果物は1バイトも触らない
（設計判断C）。

---

**⚠️ 採点はやり直さない（この実装の中心的な判断）**

選択肢は2つあった:

- (A) **再採点**: `raw_articles_{period}.json` から AI 呼び出しをやり直し、
  候補 config でフィルタを丸ごと通す
- (B) **決定的再適用**: 保存済みの採点結果（中間xlsx の軸点列）へ、候補 config の
  **決定的な部分だけ**を当て直す

**選定は (B)。** 理由は3つで、最後のものが決め手:

1. **費用と時間**：初運用の実測で採点だけに **55件 × 7秒 ＋ 約$4**。§7.3-5 は
   「結果件数を**即プレビュー**」と書いており、admin がしきい値を上げ下げしながら
   何度も叩く前提の機能に、1回6分・$4 は釣り合わない。
2. **§7.3-5 の用途に足りる**：この画面で admin が動かすのは掲載しきい値と採用区分の
   しきい値で、どちらも **6軸の点数から決定的に決まる**（`rejection_reason_for_scores`
   / `decide_adoption_class`）。点数さえ残っていれば AI は要らない。
3. **★ 再採点はプレビューとして成立しない**：AI の採点は同じ記事・同じ config でも
   実行ごとに揺れる（そもそも決定的な判断を Python 側へ寄せてあるのはそのため。
   T-19・T-17 のモジュール docstring）。再採点すると
   **「しきい値を変えた効果」と「AI が違う点を付けた効果」が混ざり**、
   before → after の比較ができない。**変更点だけを動かす**のが (B)。

その代償として、**候補 config の変更内容によっては試算できない**。どの変更が
試算できるかは下の3分割（`DETERMINISTIC_PATHS` / `RESCORE_REQUIRED_PATHS` /
`NOT_PREVIEWABLE_PATHS`）が持ち、**試算できない変更は 422 で断る**——黙って
「効果ゼロ」と表示すると、admin が「この変更は件数に影響しない」と読み違える。

---

**⚠️ 試算の母集団は「その period の週次シートに残っている記事」**

保存済みの成果物のうち、採点結果（6軸の点数）を持っているのは中間xlsx の
**週次シート（22列）だけ**。除外ログ（6列）は `収集日/タイトル/URL/ソース/
除外区分/除外理由` しか持たない。したがって:

- **一度落ちた記事は戻ってこない。** しきい値を**下げた**ときの「何件増えるか」は
  試算できない（点数が残っていないので、増える記事を評価できない）。試算できるのは
  **今載っている記事のうち何件が落ちるか**と、**採用区分の分布がどう動くか**。
- **月次 period は試算できない**（`no_scored_data`）。月次実行は8列の事例しか
  書かず、採点済みの22列を成果物として残さない（`FilterWorker` → `write_monthly`）。

どちらも「AI の分析結果を成果物として残していない」ことに由来する。
`analysis_{period}.json`（全記事の軸点・タグ・事実申告）を足せば両方とも解ける
——が、成果物を増やす判断は T-29 の範囲を超える（→ TASKS.md T-38 に記録）。
"""

import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Final

from adapter.config_repository import ConfigRepository, ConfigRevisionConflictError
from adapter.storage.artifact_store import ArtifactStore
from adapter.xlsx.report_writer import ReportStore
from application.usecases.classify_and_score import (
    decide_adoption_class,
    total_score,
)
from application.usecases.filter import (
    CATEGORY_LOW_SCORE,
    COLUMN_TOTAL_SCORE,
    RELIABILITY_AXIS_ID,
    rejection_reason_for_scores,
)
from application.usecases.update_config import EDITABLE_PATHS, apply_patch_with_paths
from enterprise.entities.config import AdoptionClass, IntelligenceConfig
from enterprise.entities.config_validation import ConfigIssue, ConfigIssueCode
from enterprise.entities.period import Period, PeriodError, parse_period
from enterprise.entities.report_columns import WEEKLY_ARTICLE_COLUMNS
from enterprise.services.exclusion import downgrade_adoption_class

logger = logging.getLogger(__name__)

# `dry_run_id` の形。`job_id`（T-26）と同じ作り方に揃える。
# ⚠️ **パス区切りを含めないこと**（`scratch/dry-run/{dry_run_id}/` のディレクトリ名に
# なるので `ArtifactStore._validate_segment` が拒否する）。
DRY_RUN_ID_PREFIX = "dry"
DRY_RUN_ID_TIME_FORMAT = "%Y%m%d-%H%M%S"
DRY_RUN_ID_SUFFIX_BYTES = 3

# 隔離出力のファイル名（設計書 §3.3 の `scratch_url` が指す先）。
RESULT_FILENAME = "result.xlsx"

# 22列のうち採用区分を載せる列（T-07 の `tag_id` が正）。
ADOPTION_CLASS_TAG_ID = "adoption_class"


def new_dry_run_id(now: datetime) -> str:
    """`dry_20260817-080000-1a2b3c`（設計書 §3.3 の `dry_...`）。"""
    stamp = now.strftime(DRY_RUN_ID_TIME_FORMAT)
    return f"{DRY_RUN_ID_PREFIX}_{stamp}-{secrets.token_hex(DRY_RUN_ID_SUFFIX_BYTES)}"


# =============================================================================
# 編集可能パラメータの3分割（この分割が T-29 の設計そのもの）
# =============================================================================
#
# ⚠️ **`EDITABLE_PATHS`（仕様書 §7.2）を過不足なく3つに分ける。** 分け忘れた
# パスがあると「新しい編集項目を足したのに、ドライランは黙って無視する」状態に
# なる。`test_dry_run.py::test_every_editable_path_is_classified` が固定する。

DETERMINISTIC_PATHS: Final[frozenset[str]] = frozenset(
    {
        # 採否（§13.3-5）。合計スコアと信頼性点だけで決まる＝週次シートの
        # 軸点列から再判定できる（`rejection_reason_for_scores`）。
        # ⚠️ `min_total_score_to_publish` は T-17 の `default_exclude` 例外採用の
        # 条件でもあるが、その条件（合計見込み ≥ しきい値）は採否の条件と同じ
        # 向き・同じ値なので、採否の再判定に含まれている。
        "tunable_thresholds.min_total_score_to_publish",
        "tunable_thresholds.min_reliability_score_to_publish",
        # 採用区分（§6.4）。合計スコアから決まる。
        "tunable_thresholds.adoption_class_score_map.propose_next_meeting",
        "tunable_thresholds.adoption_class_score_map.reference_info",
        "tunable_thresholds.adoption_class_score_map.share_only",
    }
)
"""保存済みの採点結果だけで結果を出し直せる変更（＝ドライランが受け付ける）。"""


RESCORE_REQUIRED_PATHS: Final[frozenset[str]] = frozenset(
    {
        # 軸の配点。**軸点の上限は weight そのもの**（`axis_score_bounds()`。
        # 出力スキーマも `0〜weight` で作られる）。保存済みの点は「古い上限で
        # 付けられた点」で、新しい上限へ機械的に引き伸ばすのは点数の捏造になる。
        "scoring_axes.*.weight",
        # 対象業界は**顧客関連度の判断基準としてプロンプトに載る**
        # （`build_classification_prompt`「いずれかに関係すれば関係する」）。
        # 業界を足す／外すと、顧客関連度の点とタグが変わりうる。
        "tunable_thresholds.target_industries",
        # カテゴリ優先度も分類プロンプトに載る（`- {id}: {label}（優先度 …）`）。
        # 下流に決定的な用途は無いが、**AI へ渡している以上「変えても同じ点が
        # 出る」とは言えない**ので、安全側に倒して再採点扱いにする。
        "information_categories.*.priority",
    }
)
"""AI 採点をやり直さないと結果が分からない変更（＝ドライランでは断る）。"""


NOT_PREVIEWABLE_PATHS: Final[frozenset[str]] = frozenset(
    {
        # --- 事実の申告が成果物に残っていない ---------------------------------
        # ⚠️ **理屈のうえでは再適用できる。** 除外判定（T-17）は
        # `matched_rule_nos` / `is_stale` という**事実**と config だけで決まり、
        # AI にはルールの `severity` も `enabled` も見せていない（T-19）。
        # つまり有効/無効・強度を変えても事実は動かないので、事実さえ手元にあれば
        # 再採点なしで判定し直せる。**残っていないのが問題**——`ArticleFacts` は
        # `FilterResult` の中だけに在り、xlsx にも `validation_*.json` にも
        # 書かれない。除外ログの `除外理由` にはルール名が入るが、それは
        # 「最初に当たった1件」であって当たり全部ではないし、除外された記事の
        # 点数は残っていないので採否まで追えない。→ T-38。
        "exclusion_rules.*.enabled",
        "exclusion_rules.*.severity",
        # --- 判定の入力集合が成果物に残っていない -----------------------------
        # 重複判定（T-18）は決定的だが、入力は「除外を通り抜けた記事」であって
        # 「採用された記事」ではない。週次シートに残っているのは**既に重複を
        # 落とした後**の集合なので、そこへ当て直しても片側（新たに統合される分）
        # しか出ず、緩めたときに戻る記事は出てこない。片側だけの件数は誤解を
        # 招くので断る。
        "tunable_thresholds.dedup.lookback_weeks",
        "tunable_thresholds.dedup.title_similarity_threshold",
        "tunable_thresholds.dedup.treat_same_url_as_duplicate",
        "tunable_thresholds.dedup.monthly_lookback_months",
        # --- 採否より後（描画・生成）の段で効く --------------------------------
        # トピック上限は週刊 HTML の描画時の絞り（T-24）で、採否の件数は動かない。
        "tunable_thresholds.weekly.max_industry_topics",
        "tunable_thresholds.weekly.max_common_topics",
        # 生成テキスト（T-44）は AI が書く。件数の試算では扱えない。
        "tunable_thresholds.weekly.point_of_week_required",
        "tunable_thresholds.monthly.require_editorial_and_closing",
        "tunable_thresholds.monthly.chapter_count_hint",
        # --- 月次は採点済みの22列が成果物に残らない ---------------------------
        # 事例の選別（`select_cases`）自体は決定的だが、入力の22列を月次実行は
        # 書かない（書くのは8列の事例だけ）。母集団が無いので試算できない。
        "tunable_thresholds.monthly.target_case_count",
        "tunable_thresholds.monthly.min_score_for_case",
    }
)
"""試算に要るデータが残っていない／採否の件数に効かない変更（＝断る）。"""


_REASONS: Final[Mapping[str, tuple[ConfigIssueCode, str]]] = {
    "scoring_axes.*.weight": (
        ConfigIssueCode.RESCORE_REQUIRED,
        "軸の配点は各軸の得点上限そのものです。保存済みの点数は古い上限で"
        "付けられているため、再採点しないと新しい配点での結果は出せません。",
    ),
    "tunable_thresholds.target_industries": (
        ConfigIssueCode.RESCORE_REQUIRED,
        "対象業界は顧客関連度の判断基準として採点プロンプトに載ります。"
        "変更すると点数とタグが変わりうるため、再採点が必要です。",
    ),
    "information_categories.*.priority": (
        ConfigIssueCode.RESCORE_REQUIRED,
        "カテゴリ優先度は分類プロンプトに載ります。変更後も同じ分類になるとは"
        "言えないため、再採点が必要です。",
    ),
    "exclusion_rules.*.enabled": (
        ConfigIssueCode.NOT_PREVIEWABLE,
        "除外ルールの当たり判定に使った事実（該当ルール番号・鮮度）は成果物に"
        "残っていないため、保存済みの結果からは試算できません。",
    ),
    "exclusion_rules.*.severity": (
        ConfigIssueCode.NOT_PREVIEWABLE,
        "除外ルールの当たり判定に使った事実（該当ルール番号・鮮度）は成果物に"
        "残っていないため、保存済みの結果からは試算できません。",
    ),
    "tunable_thresholds.monthly.target_case_count": (
        ConfigIssueCode.NOT_PREVIEWABLE,
        "月次実行は採点済みの記事一覧を成果物に残さない（残るのは事例8列だけ）"
        "ため、試算の母集団がありません。",
    ),
    "tunable_thresholds.monthly.min_score_for_case": (
        ConfigIssueCode.NOT_PREVIEWABLE,
        "月次実行は採点済みの記事一覧を成果物に残さない（残るのは事例8列だけ）"
        "ため、試算の母集団がありません。",
    ),
}
"""パスごとの理由。未登録のパスには種類ごとの既定文を使う。"""

_DEDUP_REASON = (
    "重複判定の入力は「除外を通り抜けた記事」ですが、成果物に残っているのは"
    "重複を落とした後の一覧です。緩めたときに戻る記事を試算できません。"
)
_DOWNSTREAM_REASON = (
    "この項目が効くのは採否より後（HTML 描画・生成テキスト）の段なので、"
    "採用・除外の件数は変わりません。"
)


def _issue_for(path: str) -> ConfigIssue:
    """試算できないパス1件の `ConfigIssue`（フロントは `path` を欄へ対応づける）。"""
    if path in _REASONS:
        code, reason = _REASONS[path]
    elif path.startswith("tunable_thresholds.dedup."):
        code, reason = ConfigIssueCode.NOT_PREVIEWABLE, _DEDUP_REASON
    else:
        code, reason = ConfigIssueCode.NOT_PREVIEWABLE, _DOWNSTREAM_REASON
    return ConfigIssue(path=path, reason=reason, code=code)


class DryRunError(Exception):
    """ドライランを実行できない要求。HTTP 層がステータスへ変換する。"""


class DryRunNotPreviewableError(DryRunError):
    """候補 config の変更を保存済みの結果から試算できない（→ 422）。

    `issues` は `ConfigIssue`（T-05 / T-13 と同じ形）なので、フロントは
    patch の 422 と同じ方法で `path` をフォーム欄へ対応づけられる。
    """

    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(f"{issue.path}: {issue.reason}" for issue in issues))


class DryRunPeriodError(DryRunError):
    """period の表記が不正／実在しない期間（→ 422）。"""


class NoScoredDataError(DryRunError):
    """その period に採点済みのデータが無い（→ 404）。"""


@dataclass(frozen=True, slots=True)
class DryRunCounts:
    """件数サマリ（設計書 §3.3 の `summary`）。"""

    adopted: int
    excluded: int


@dataclass(frozen=True, slots=True)
class DryRunResult:
    """ドライラン1回の結果（HTTP 層が 202 の本文へ写す）。

    Attributes:
        dry_run_id: `dry_...`
        period: 対象期間
        base_revision: 試算の基にした現行 config の revision
        directory: 隔離出力のディレクトリ（`scratch/dry-run/{id}/`）
        result_path: 明細 xlsx
        summary: 候補 config を当てた後の件数
        baseline: 当てる前（＝いま成果物に残っている）件数
        ttl_hours: この出力が消えるまでの時間（`Settings.scratch_ttl_hours`）
        changed_paths: 候補 config が触れたパス（監査・ログ用）
    """

    dry_run_id: str
    period: Period
    base_revision: int
    directory: Path
    result_path: Path
    summary: DryRunCounts
    baseline: DryRunCounts
    ttl_hours: int
    changed_paths: tuple[str, ...]


def reapply_adoption_class(
    stored: Any,
    total: int,
    current: IntelligenceConfig,
    candidate: IntelligenceConfig,
) -> AdoptionClass:
    """採用区分を候補 config で決め直す（§6.4）。**降格は引き継ぐ。**

    `adoption_class` は合計スコアと `adoption_class_score_map` から決まるが、
    除外ルールが `low_priority` だった記事は**そこから1段下げてある**（§5.4）。
    その「下げた」という事実は成果物に残っていない（残るのは下げた後の値だけ）。

    そこで **現行 config で決め直した区分と、保存されている区分を突き合わせて
    降格の有無を復元する**：ちょうど1段下がっていれば降格が当たっていたと読み、
    候補 config での区分にも同じ1段を当てる。

    ⚠️ **前提は「保存されている行を出したのが現行 revision である」こと。**
    ドライランは保存前のプレビューなので普通は成り立つが、その実行の後に別の
    改訂が入っていると崩れる。**説明が付かない場合は降格を当てない**
    （分からないものを推測で下げるより、素直に決め直した値を見せる）。

    Args:
        stored: 週次シートに載っている `レポート採用区分`
        total: 合計スコア（6軸の和）
        current: 現行 config（保存されている行を出した基準のはず）
        candidate: 候補 config

    Returns:
        候補 config での採用区分
    """
    scored_now = decide_adoption_class(total, current)
    scored_next = decide_adoption_class(total, candidate)
    if stored == scored_now:
        return scored_next
    if stored == downgrade_adoption_class(scored_now):
        return downgrade_adoption_class(scored_next)

    logger.warning(
        "保存されている採用区分を現行 config から説明できません"
        "（保存値=%r / 現行 config での区分=%r / 合計=%d）。"
        "降格は当てずに決め直した値を使います",
        stored,
        scored_now,
        total,
    )
    return scored_next


class DryRunUsecase:
    """`POST /config/dry-run`：候補 config を採点済みデータへ決定的に再適用する。

    ⚠️ **認可はここではしない。** dry-run が config ファミリ（admin 限定）で
    あることは HTTP 層（`require_admin`）の責務（設計書 §3.4）。
    """

    def __init__(
        self,
        *,
        repo: ConfigRepository,
        store: ArtifactStore,
        reports: ReportStore,
        tz: tzinfo | None = None,
    ) -> None:
        """
        Args:
            repo: 現行 config の読み出し口（T-11）
            store: 成果物の置き場（T-02）
            reports: 中間xlsx の読み書き口（T-22）
            tz: `dry_run_id` の時刻部分に使うタイムゾーン（`Settings.tzinfo`）
        """
        self._repo = repo
        self._store = store
        self._reports = reports
        self._tz = tz

    def _now(self) -> datetime:
        return datetime.now(tz=self._tz)

    def execute(
        self,
        *,
        period: str,
        patch: Mapping[str, Any],
        base_revision: int | None = None,
    ) -> DryRunResult:
        """候補 config を当てた結果を試算し、隔離パスへ書く。

        Args:
            period: 対象の週次 period（`2026-W31`）
            patch: 未保存の編集値（§7.2 の編集可能パラメータのみ）
            base_revision: 指定すると現行 revision と突合する（設計書 §3.3）

        Returns:
            件数サマリと隔離出力の場所

        Raises:
            ConfigNotFoundError: `config.json` が無い（404）
            ConfigRevisionConflictError: `base_revision` の不一致（409）
            ConfigPatchError: patch が許可リスト・型に反する（422）
            DryRunPeriodError: period の表記が不正／週次でない（422）
            DryRunNotPreviewableError: 試算できない変更（422）
            NoScoredDataError: その period に採点済みデータが無い（404）
        """
        current = self._repo.load()
        if base_revision is not None and base_revision != current.meta.revision:
            raise ConfigRevisionConflictError(base_revision, current.meta.revision)

        candidate, changed = apply_patch_with_paths(current, patch)
        _ensure_previewable(changed)

        parsed = self._parse(period)
        rows = self._reports.read_weekly(parsed.text)
        if not rows:
            raise NoScoredDataError(
                f"{parsed.text} に採点済みのデータがありません。"
                "先にパイプラインを実行してください"
            )

        adopted, newly_excluded = reapply(rows, current=current, candidate=candidate)
        # 元からの除外（その period ぶん）へ、今回落ちた分を足す。⚠️ 一度落ちた
        # 記事は戻ってこない（点数が残っていないため。モジュール冒頭の⚠️）。
        existing_exclusions = self._reports.read_exclusions(parsed.text)
        exclusions = [*existing_exclusions, *newly_excluded]

        # ⚠️ **TTL 超過分の掃除はここで行う**（設計判断C ／ T-02）。専用の
        # スケジューラを立てずに済ませるため、書く直前に一度だけ掃く。
        removed = self._store.purge_expired_scratch()
        if removed:
            logger.info("期限切れのドライラン出力を %d 件削除しました", len(removed))

        dry_run_id = new_dry_run_id(self._now())
        directory = self._store.dry_run_dir(dry_run_id)
        result_path = directory / RESULT_FILENAME
        self._reports.write_dry_run(
            result_path,
            period=parsed.text,
            dry_run_id=dry_run_id,
            revision=current.meta.revision,
            articles=adopted,
            exclusions=exclusions,
        )

        result = DryRunResult(
            dry_run_id=dry_run_id,
            period=parsed,
            base_revision=current.meta.revision,
            directory=directory,
            result_path=result_path,
            summary=DryRunCounts(adopted=len(adopted), excluded=len(exclusions)),
            baseline=DryRunCounts(adopted=len(rows), excluded=len(existing_exclusions)),
            ttl_hours=self._store.scratch_ttl_hours,
            changed_paths=changed,
        )
        logger.info(
            "dry-run finished (id=%s, period=%s, revision=%d, paths=%s,"
            " adopted %d→%d, excluded %d→%d)",
            dry_run_id,
            parsed.text,
            current.meta.revision,
            list(changed),
            result.baseline.adopted,
            result.summary.adopted,
            result.baseline.excluded,
            result.summary.excluded,
        )
        return result

    def _parse(self, period: str) -> Period:
        try:
            parsed = parse_period(period)
        except PeriodError as exc:
            raise DryRunPeriodError(str(exc)) from exc
        if not parsed.is_weekly:
            # ⚠️ 月次は採点済みの22列を成果物として残さない（モジュール冒頭の⚠️）。
            raise DryRunPeriodError(
                f"ドライランは週次 period だけが対象です: {period!r}。"
                "月次実行は採点済みの記事一覧を成果物に残さないため試算できません"
            )
        return parsed


def _ensure_previewable(changed: Sequence[str]) -> None:
    """試算できない変更が混ざっていたら 422 にする。

    ⚠️ **「効果ゼロ」として黙って通さない。** 通すと admin は「この変更は件数に
    影響しない」と読む。実際には「この機能では分からない」であって、意味が逆。

    Raises:
        DryRunNotPreviewableError: 1つでも試算できない変更がある場合
    """
    issues = [_issue_for(path) for path in changed if path not in DETERMINISTIC_PATHS]
    if issues:
        raise DryRunNotPreviewableError(issues)


def reapply(
    rows: Sequence[Mapping[str, Any]],
    *,
    current: IntelligenceConfig,
    candidate: IntelligenceConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """保存済みの週次22列へ候補 config の**決定的な部分**を当て直す。

    やることは2つだけ（それ以上は AI の領分）:

    1. 採否（§13.3-5）を `rejection_reason_for_scores()` で判定し直す
    2. 残った記事の採用区分（§6.4）を決め直す（降格は引き継ぐ）

    ⚠️ **合計スコアは列の値をそのまま使わず6軸の和を取り直す。** 「合計は
    アプリ側が6軸から決める」（T-19）という約束をここでも守るためで、
    壊れた行（和が合わない列）を黙って通さない効果もある。軸の配点変更は
    `RESCORE_REQUIRED_PATHS` で断ってあるので、和は保存時と同じ値になる。

    Args:
        rows: 週次シートの行（列名 → 値）
        current: 現行 config
        candidate: 候補 config

    Returns:
        (採用として残る行, 今回落ちた記事の除外ログ行)。採用側は入力の並び
        （合計スコア降順）を保つ
    """
    adopted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for row in rows:
        scores = _axis_scores(row)
        total = total_score(scores, candidate)
        reason = rejection_reason_for_scores(
            total, scores.get(RELIABILITY_AXIS_ID), candidate
        )
        if reason is not None:
            excluded.append(_low_score_row(row, reason))
            continue

        updated = dict(row)
        updated[COLUMN_TOTAL_SCORE] = total
        updated[_ADOPTION_CLASS_COLUMN] = reapply_adoption_class(
            row.get(_ADOPTION_CLASS_COLUMN), total, current, candidate
        )
        adopted.append(updated)

    return adopted, excluded


def _axis_scores(row: Mapping[str, Any]) -> dict[str, int]:
    """週次22列の軸点列を「軸ID → 点数」へ。

    ⚠️ **どの列がどの軸かは T-07 の `axis_id` が持つ**（列名をここへ書かない）。
    """
    return {
        str(column.axis_id): int(row[column.name])
        for column in WEEKLY_ARTICLE_COLUMNS
        if column.axis_id is not None
    }


def _low_score_row(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """新しいしきい値で落ちた記事の除外ログ1行（§13.3-5）。

    区分・理由の文言は `application.usecases.filter` のものをそのまま使う
    （本番の除外ログと同じ語彙で読めるように。写しを作らない）。
    """
    return {
        "収集日": row.get("収集日"),
        "タイトル": row.get("タイトル"),
        "URL": row.get("URL"),
        "ソース": row.get("ソース"),
        "除外区分": CATEGORY_LOW_SCORE,
        "除外理由": reason,
    }


def _adoption_class_column() -> str:
    """採用区分を載せる列名（T-07 の `tag_id` から引く）。"""
    for column in WEEKLY_ARTICLE_COLUMNS:
        if column.tag_id == ADOPTION_CLASS_TAG_ID:
            return column.name
    raise DryRunError(  # pragma: no cover - T-07 の列定義が壊れたときだけ
        f"週次22列に {ADOPTION_CLASS_TAG_ID} の列がありません"
    )


_ADOPTION_CLASS_COLUMN: Final[str] = _adoption_class_column()


def unclassified_editable_paths() -> frozenset[str]:
    """3分割から漏れている編集可能パラメータ（テストが空であることを固定）。"""
    return frozenset(EDITABLE_PATHS) - (
        DETERMINISTIC_PATHS | RESCORE_REQUIRED_PATHS | NOT_PREVIEWABLE_PATHS
    )


__all__ = [
    "DETERMINISTIC_PATHS",
    "NOT_PREVIEWABLE_PATHS",
    "RESCORE_REQUIRED_PATHS",
    "RESULT_FILENAME",
    "DryRunCounts",
    "DryRunError",
    "DryRunNotPreviewableError",
    "DryRunPeriodError",
    "DryRunResult",
    "DryRunUsecase",
    "NoScoredDataError",
    "new_dry_run_id",
    "reapply",
    "reapply_adoption_class",
    "unclassified_editable_paths",
]
