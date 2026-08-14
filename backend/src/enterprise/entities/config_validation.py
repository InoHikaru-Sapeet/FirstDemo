"""`config.json` のクロスフィールドバリデーション（設計書 §2.1.1 ／ 仕様書 §7.4）。

JSON Schema と Pydantic モデル（T-04）が見るのは **1フィールド単位** の構造・型・
値域まで。「軸weightの合計が100」「しきい値の降順整合」「参照先の enum が実在する」
といった **複数フィールドをまたぐ制約** は JSON Schema では表現できないため、
設計書 §2.1.1 のとおりこの層で担保する。

⚠️ **サーバは値を自動補正しない（設計判断A）。**
合計が100でなければ **保存を拒否** し、入力値には手を触れない。理由はスコア band が
整数レンジ（仕様書 §5.2）で採点も整数（§13.3-4）のため、按分による正規化が非整数の
weight を生み、フォーマットチェック（§12）と採点の再現性を壊すこと。監査ログの diff
（§4.4）も「入力＝保存」でないと説明できない。「比率維持で100へ補正」はフォーム値を
埋めるだけの UI 補助としてフロントに置く（T-33。保存には再度の明示操作が必要）。
**この層に正規化処理を足さないこと。**

違反は例外ではなく `ConfigIssue` のリストで返す。1回の検証で見つかった違反を
すべて返せるようにするためで、HTTP 層はそれを 422 の `issues` にそのまま載せる
（設計書 §3.3 `{"error":"validation_failed","issues":[{"path","reason"}]}`）。
"""

from enum import StrEnum
from itertools import pairwise
from typing import Any

from pydantic import BaseModel, ConfigDict

from enterprise.entities.config import (
    INFORMATION_CATEGORY_IDS,
    REQUIRED_TAG_IDS,
    SCORING_AXIS_IDS,
    ConfigEnums,
    IntelligenceConfig,
    Priority,
    Severity,
)

# `required_tags[].value_source` が enums を指すときの接頭辞（設計書 §2.1.1-4）。
ENUM_SOURCE_PREFIX = "enums."


class ConfigIssueCode(StrEnum):
    """違反の種類。UI が違反ごとの補助操作を出し分けるための機械可読キー。

    例：`WEIGHT_SUM_MISMATCH` を見て「比率維持で100へ補正」ボタンを出す（T-33）。

    ⚠️ 後半4つは **`PUT /config` の patch 検査（T-13）** が使う。この層
    （クロスフィールド検証）は生成しない。`ConfigIssue` を1種類に保つことで、
    フロント（T-34）が「モデル由来 422 / クロスフィールド 422 / patch 422」を
    **同じ方法で** `path` からフォーム欄へマッピングできるようにするため。
    """

    WEIGHT_SUM_MISMATCH = "weight_sum_mismatch"
    ADOPTION_THRESHOLD_ORDER = "adoption_threshold_order"
    UNKNOWN_INDUSTRY_REFERENCE = "unknown_industry_reference"
    UNKNOWN_ENUM_REFERENCE = "unknown_enum_reference"
    FIXED_ID_CHANGED = "fixed_id_changed"
    INITIAL_VALUE_MISMATCH = "initial_value_mismatch"

    # --- patch 検査（T-13。仕様書 §7.2 の編集可能パラメータ許可リスト）-----
    # config に存在するが編集を許していない項目（ID系・`scoring_total` 等）
    FIELD_NOT_EDITABLE = "field_not_editable"
    # config に存在しないキー（タイポ・古いフロントからの送信）
    UNKNOWN_FIELD = "unknown_field"
    # 配列要素の指定（`id` / `no`）が欠けている、または該当要素が無い
    UNKNOWN_TARGET = "unknown_target"
    # 型・値域がモデル（T-04）に反する
    INVALID_VALUE = "invalid_value"


class ConfigIssue(BaseModel):
    """「どのパスがなぜダメか」1件。

    `path` は Pydantic の `ValidationError.loc` と同じドット区切り
    （`scoring_axes.0.weight`）。モデル由来の 422 とこの層の 422 をフロントが
    同じ方法でフィールドへマッピングできるようにするため（T-34）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    reason: str
    code: ConfigIssueCode


class ConfigValidationError(Exception):
    """クロスフィールド制約の違反。HTTP 層は 422 へ変換する（設計書 §3.3）。

    移行 CLI（T-14）は「検証失敗時は書き込まず中断」なので、例外で止められる
    入口も用意しておく。
    """

    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = issues
        super().__init__(
            "; ".join(f"{issue.path}: {issue.reason}" for issue in issues)
            or "config のバリデーションに失敗しました"
        )


# --- 仕様書 §5.2 の初期値（§2.1.1-6 の一致チェック用）-----------------------
# 移行 CLI（T-14）が xlsx から起こした結果がこの値と一致するかを確かめる。
# 件数（7 / 10 / 6 / 13）はモデル側の min/max_length で担保済み（T-04）なので
# ここでは扱わない。

INITIAL_SCORING_WEIGHTS: dict[str, int] = {
    "customer_relevance": 25,
    "practical_usability": 20,
    "market_impact": 20,
    "advisory_usability": 15,
    "reliability": 10,
    "urgency_freshness": 10,
}

INITIAL_CATEGORY_PRIORITIES: dict[str, Priority] = {
    "ai_major_company_model": Priority.MID_HIGH,
    "ai_agent_automation": Priority.HIGH,
    "ai_governance_risk": Priority.HIGH,
    "enterprise_ai_case": Priority.HIGH,
    "industry_ai_trend": Priority.HIGH,
    "ai_training_org_change": Priority.MID,
    "ai_implementation_ops": Priority.MID,
}

INITIAL_EXCLUSION_SEVERITIES: dict[int, Severity] = {
    1: Severity.FULL_EXCLUDE,
    2: Severity.FULL_EXCLUDE,
    3: Severity.DEFAULT_EXCLUDE,
    4: Severity.DEFAULT_EXCLUDE,
    5: Severity.DEFAULT_EXCLUDE,
    6: Severity.DEFAULT_EXCLUDE,
    7: Severity.DEFAULT_EXCLUDE,
    8: Severity.DEFAULT_EXCLUDE,
    9: Severity.DEFAULT_EXCLUDE,
    10: Severity.DEFAULT_EXCLUDE,
    11: Severity.LOW_PRIORITY,
    12: Severity.MERGE,
    13: Severity.LOW_PRIORITY_OR_EXCLUDE,
}

# キーは config ルートからのドット区切りパス。`ConfigIssue.path` と同じ文字列を
# そのまま使えるようにしてある。
INITIAL_TUNABLE_THRESHOLDS: dict[str, Any] = {
    "tunable_thresholds.min_total_score_to_publish": 60,
    "tunable_thresholds.adoption_class_score_map.propose_next_meeting": 85,
    "tunable_thresholds.adoption_class_score_map.reference_info": 70,
    "tunable_thresholds.adoption_class_score_map.share_only": 60,
    "tunable_thresholds.min_reliability_score_to_publish": 5,
    "tunable_thresholds.weekly.target_industry": "不動産",
    "tunable_thresholds.weekly.max_industry_topics": 5,
    "tunable_thresholds.weekly.max_common_topics": 6,
    "tunable_thresholds.weekly.point_of_week_required": True,
    "tunable_thresholds.monthly.target_case_count": 15,
    "tunable_thresholds.monthly.chapter_count_hint": 5,
    "tunable_thresholds.monthly.min_score_for_case": 80,
    "tunable_thresholds.monthly.require_editorial_and_closing": True,
    "tunable_thresholds.dedup.lookback_weeks": 8,
    "tunable_thresholds.dedup.title_similarity_threshold": 0.85,
    "tunable_thresholds.dedup.treat_same_url_as_duplicate": True,
}

# 採用区分しきい値の降順チェーン（設計書 §2.1.1-2 ／ 仕様書 §7.4）。
# ラベルは仕様書 §5.2 `enums.adoption_class` と §7.2 の UI 項目名に合わせる。
_ADOPTION_THRESHOLD_CHAIN: tuple[tuple[str, str], ...] = (
    (
        "tunable_thresholds.adoption_class_score_map.propose_next_meeting",
        "次回定例で提案",
    ),
    ("tunable_thresholds.adoption_class_score_map.reference_info", "参考情報"),
    ("tunable_thresholds.adoption_class_score_map.share_only", "共有のみ"),
    ("tunable_thresholds.min_total_score_to_publish", "掲載最低スコア"),
)
_ADOPTION_ORDER_TEXT = " ≥ ".join(label for _, label in _ADOPTION_THRESHOLD_CHAIN)


def _attr(root: object, path: str) -> Any:
    """`tunable_thresholds.weekly.target_industry` のようなドット区切りで値を引く。"""
    value: Any = root
    for name in path.split("."):
        value = getattr(value, name)
    return value


# --- §2.1.1-1: Σ scoring_axes[].weight == 100（設計判断A: 保存拒否）---------


def check_weight_sum(config: IntelligenceConfig) -> list[ConfigIssue]:
    """軸配点の合計が `scoring_total` と一致するか。

    **一致しなければ保存を拒否する。按分による自動正規化はしない（設計判断A）。**
    path をセクション（`scoring_axes`）にしているのは、合計のズレを特定の1軸へ
    帰属させられないため（設計書 §3.3 の 422 例も `"path":"scoring_axes"`）。
    """
    total = sum(axis.weight for axis in config.scoring_axes)
    if total == config.scoring_total:
        return []

    breakdown = " + ".join(f"{axis.id}={axis.weight}" for axis in config.scoring_axes)
    return [
        ConfigIssue(
            path="scoring_axes",
            reason=(
                f"weight合計が{config.scoring_total}でない"
                f"（現在 {total}: {breakdown}）。"
                "自動補正はしないので、各軸の weight を調整して合計を"
                f"{config.scoring_total}に合わせてください"
            ),
            code=ConfigIssueCode.WEIGHT_SUM_MISMATCH,
        )
    ]


# --- §2.1.1-2: 採用区分しきい値の降順整合 -----------------------------------


def check_adoption_threshold_order(config: IntelligenceConfig) -> list[ConfigIssue]:
    """`propose_next_meeting ≥ reference_info ≥ share_only ≥ min_total…` を確かめる。

    崩れている隣接ペアごとに1件返す。path は「高すぎる側」（チェーンの下位）に
    置く。上位を下げるより下位を下げるほうが admin の意図に沿うことが多く、
    フォーム上でも直すべき欄が1つに定まるため。
    """
    links = [
        (path, label, _attr(config, path)) for path, label in _ADOPTION_THRESHOLD_CHAIN
    ]

    issues: list[ConfigIssue] = []
    for (_, upper_label, upper), (lower_path, lower_label, lower) in pairwise(links):
        if lower <= upper:
            continue
        issues.append(
            ConfigIssue(
                path=lower_path,
                reason=(
                    f"{lower_label}({lower}) が {upper_label}({upper}) を上回っている。"
                    f"しきい値は {_ADOPTION_ORDER_TEXT} の降順である必要がある"
                ),
                code=ConfigIssueCode.ADOPTION_THRESHOLD_ORDER,
            )
        )
    return issues


# --- §2.1.1-3: weekly.target_industry ∈ enums.industry ----------------------


def check_target_industry_reference(config: IntelligenceConfig) -> list[ConfigIssue]:
    """週刊メルマガの対象業界が `enums.industry` に実在するか（参照整合）。

    `target_industry` は出力ファイル名にも入る（`weekly_..._{industry}_{period}.html`）
    ので、enum 外の値を通すとレンダラ（T-24）が誰も選べない業界版を出してしまう。
    """
    industry = config.tunable_thresholds.weekly.target_industry
    if industry in config.enums.industry:
        return []

    return [
        ConfigIssue(
            path="tunable_thresholds.weekly.target_industry",
            reason=(
                f"enums.industry に存在しない業界 {industry!r}。"
                f"選択できるのは: {' / '.join(config.enums.industry)}"
            ),
            code=ConfigIssueCode.UNKNOWN_INDUSTRY_REFERENCE,
        )
    ]


# --- §2.1.1-4: required_tags[*].value_source の参照先 enum が実在 -----------


def check_value_source_references(config: IntelligenceConfig) -> list[ConfigIssue]:
    """`value_source` が `enums.*` を指すとき、その enum キーが実在するか。

    分類・採点（T-19）は出力スキーマの enum を config の `enums` から動的に
    生成する。参照先が無いタグを通すと、その段でタグの取り得る値を組み立て
    られない。`enums.` 以外（`free_controlled` / `information_categories.id`）は
    設計書 §2.1 で自由文字列なので触らない。
    """
    available = tuple(ConfigEnums.model_fields)

    issues: list[ConfigIssue] = []
    for index, tag in enumerate(config.required_tags):
        if not tag.value_source.startswith(ENUM_SOURCE_PREFIX):
            continue
        key = tag.value_source.removeprefix(ENUM_SOURCE_PREFIX)
        if key in available:
            continue
        issues.append(
            ConfigIssue(
                path=f"required_tags.{index}.value_source",
                reason=(
                    f"enums に {key!r} というキーが無い（{tag.id} タグの参照先）。"
                    f"参照できるのは: {' / '.join(available)}"
                ),
                code=ConfigIssueCode.UNKNOWN_ENUM_REFERENCE,
            )
        )
    return issues


# --- §2.1.1-5: ID系が現行値と一致 -------------------------------------------


def check_fixed_identities(config: IntelligenceConfig) -> list[ConfigIssue]:
    """カテゴリ / タグ / 軸の `id` と除外ルールの `no` が定義どおり揃っているか。

    「現行値と不一致なら 422」（設計書 §2.1.1-5）の現行値＝`config.py` が
    `Literal` で固定している正準 ID 列。保存済み config はすべてこの検証を
    通っているので、正準列との比較は「ひとつ前の revision との比較」と同じ
    ことになる。

    モデル（T-04）が弾けるのは「未知の ID」と「件数違い」まで。ここで拾うのは
    **重複と欠落の組み合わせ**（7件だが同じ ID が2つ）と **並び順の変更**。
    除外ルールの `no` を含めるのは、`no` がルールの同一性そのもので、重複すると
    除外判定（T-17。`no` 昇順で評価）が非決定的になるため。
    """
    expected_rule_numbers = tuple(range(1, len(INITIAL_EXCLUSION_SEVERITIES) + 1))

    issues: list[ConfigIssue] = []
    for section, field, label, actual, expected in (
        (
            "information_categories",
            "id",
            "情報カテゴリ",
            tuple(category.id for category in config.information_categories),
            INFORMATION_CATEGORY_IDS,
        ),
        (
            "required_tags",
            "id",
            "必須タグ",
            tuple(tag.id for tag in config.required_tags),
            REQUIRED_TAG_IDS,
        ),
        (
            "scoring_axes",
            "id",
            "スコアリング軸",
            tuple(axis.id for axis in config.scoring_axes),
            SCORING_AXIS_IDS,
        ),
        (
            "exclusion_rules",
            "no",
            "除外ルール",
            tuple(rule.no for rule in config.exclusion_rules),
            expected_rule_numbers,
        ),
    ):
        issues.extend(_check_identity_sequence(section, field, label, actual, expected))
    return issues


def _check_identity_sequence(
    section: str,
    field: str,
    label: str,
    actual: tuple[Any, ...],
    expected: tuple[Any, ...],
) -> list[ConfigIssue]:
    if actual == expected:
        return []

    missing = [value for value in expected if value not in actual]
    duplicated = [value for value in dict.fromkeys(actual) if actual.count(value) > 1]

    if missing or duplicated:
        problems = []
        if missing:
            problems.append(f"不足: {', '.join(str(v) for v in missing)}")
        if duplicated:
            problems.append(f"重複: {', '.join(str(v) for v in duplicated)}")
        detail = "／".join(problems)
    else:
        # 集合は同じで並びだけ違うケース
        detail = f"順序が異なる（期待: {', '.join(str(v) for v in expected)}）"

    return [
        ConfigIssue(
            path=section,
            reason=(
                f"{label}の {field} が定義と一致しない（{detail}）。"
                "ID系は中間xlsx の互換が壊れるため変更できない（仕様書 §5.1）"
            ),
            code=ConfigIssueCode.FIXED_ID_CHANGED,
        )
    ]


# --- §2.1.1-6: 初期値が §5.2 実データと一致（移行時に使用）------------------


def validate_initial_config(config: IntelligenceConfig) -> list[ConfigIssue]:
    """移行直後の config が仕様書 §5.2 の初期値どおりかを確かめる（設計書 §2.1.1-6）。

    xlsx → config.json の初期マイグレーション（T-14 手順5）の受け入れ判定用。
    日本語 → ID の正規化（`中〜高` → `mid_high`。仕様書 §5.3）を取り違えると
    ここで落ちる。

    **通常の保存経路では呼ばない。** admin が weight やしきい値を変えたあとの
    config は当然 §5.2 と一致しないため。保存時の検証は `validate_config`。

    Args:
        config: 検証する config（モデル検証済み）

    Returns:
        違反のリスト。空なら §5.2 の初期値と一致している
    """
    issues: list[ConfigIssue] = []

    for index, axis in enumerate(config.scoring_axes):
        issues.extend(
            _check_initial_value(
                f"scoring_axes.{index}.weight",
                actual=axis.weight,
                expected=INITIAL_SCORING_WEIGHTS[axis.id],
            )
        )

    for index, category in enumerate(config.information_categories):
        issues.extend(
            _check_initial_value(
                f"information_categories.{index}.priority",
                actual=category.priority,
                expected=INITIAL_CATEGORY_PRIORITIES[category.id],
            )
        )

    for index, rule in enumerate(config.exclusion_rules):
        issues.extend(
            _check_initial_value(
                f"exclusion_rules.{index}.severity",
                actual=rule.severity,
                expected=INITIAL_EXCLUSION_SEVERITIES[rule.no],
            )
        )
        # §5.2 では13ルールすべて有効
        issues.extend(
            _check_initial_value(
                f"exclusion_rules.{index}.enabled", actual=rule.enabled, expected=True
            )
        )

    for path, expected in INITIAL_TUNABLE_THRESHOLDS.items():
        issues.extend(
            _check_initial_value(path, actual=_attr(config, path), expected=expected)
        )

    return issues


def _check_initial_value(path: str, *, actual: Any, expected: Any) -> list[ConfigIssue]:
    if actual == expected:
        return []
    return [
        ConfigIssue(
            path=path,
            reason=(
                "初期値が仕様書 §5.2 の実データと一致しない"
                f"（期待 {expected!r} / 実際 {actual!r}）"
            ),
            code=ConfigIssueCode.INITIAL_VALUE_MISMATCH,
        )
    ]


# --- 入口 --------------------------------------------------------------------


def validate_config(config: IntelligenceConfig) -> list[ConfigIssue]:
    """保存前に必ず通すクロスフィールド検証（設計書 §2.1.1 の 1〜5）。

    早期 return せず全項目を評価する。admin が一度の保存で複数の違反を直せる
    ようにするため（フロントは `issues[].path` を該当フィールドへマッピングして
    まとめて表示する。T-34）。

    §2.1.1-6（初期値の一致）は移行専用なので含まない（→ `validate_initial_config`）。

    Args:
        config: 検証する config（モデル検証済み）

    Returns:
        違反のリスト。空なら保存してよい
    """
    return [
        *check_weight_sum(config),
        *check_adoption_threshold_order(config),
        *check_target_industry_reference(config),
        *check_value_source_references(config),
        *check_fixed_identities(config),
    ]


def raise_for_issues(issues: list[ConfigIssue]) -> None:
    """違反があれば `ConfigValidationError` を投げる。"""
    if issues:
        raise ConfigValidationError(issues)


def ensure_valid_config(config: IntelligenceConfig) -> IntelligenceConfig:
    """検証を通ったときだけ config を返す。書き込み前の関門（T-11 / T-14）。

    **返すのは受け取った config そのまま。** 補正した別物を返さない（設計判断A）。

    Raises:
        ConfigValidationError: クロスフィールド制約に違反している場合
    """
    raise_for_issues(validate_config(config))
    return config
