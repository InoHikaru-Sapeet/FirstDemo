"""判断基準ファイル `config.json` の構造定義（設計書 §2.1 ／ 仕様書 §5.2）。

`config.json` はパイプライン全体（crawl → filter → render）が参照する唯一の
判断基準であり、**ファイルが正**（設計書 §8）。このモジュールはその構造を
Pydantic で表現し、同じ定義から JSON Schema（draft 2020-12）を生成する。
`schemas/config.schema.json` は生成物であって手で書かない
（生成は `adapter.cli.export_config_schema` ／ `make config-schema`）。

固定と可変の分離（仕様書 §5.1）:

- **固定**: カテゴリ7ID・タグ10ID・軸6ID・`schema_version`・`scoring_total`・
  enum の日本語値。IDを変えると中間xlsx の互換が壊れるため `Literal` で型に
  焼き込み、そもそも別の値を持てないようにする。
- **可変**: `scoring_axes[].weight` / `exclusion_rules[].severity` /
  `exclusion_rules[].enabled` / `information_categories[].priority` /
  `tunable_thresholds.*`。admin が管理画面から編集する対象（仕様書 §7.2）。

このモジュールが見るのは **1フィールド単位の構造・型・値域** だけ。
`Σ weight == 100`・`adoption_class_score_map` の降順整合・`target_industry` の
参照整合といったクロスフィールド制約は JSON Schema 単体で表現できないため、
設計書 §2.1.1 のとおり別モジュール（T-05）が担う。ここで弾かない理由は
「JSON Schema と1:1で対応する層」に閉じておきたいから。
"""

import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import GenerateJsonSchema

SCHEMA_VERSION = "1.0"
SCORING_TOTAL = 100

JSON_SCHEMA_ID = "https://sapeet.example/schemas/config.json"
JSON_SCHEMA_TITLE = "AI Intelligence config.json"

# 件数は仕様書 §5.1 の確定値（7カテゴリ / 10タグ / 6軸 / 13除外ルール）。
INFORMATION_CATEGORY_COUNT = 7
REQUIRED_TAG_COUNT = 10
SCORING_AXIS_COUNT = 6
EXCLUSION_RULE_COUNT = 13


# --- 固定ID（仕様書 §5.1「変更すると中間xlsx互換が壊れる」）-----------------

InformationCategoryId = Literal[
    "ai_major_company_model",
    "ai_agent_automation",
    "ai_governance_risk",
    "enterprise_ai_case",
    "industry_ai_trend",
    "ai_training_org_change",
    "ai_implementation_ops",
]

RequiredTagId = Literal[
    "information_category",
    "ai_theme",
    "industry",
    "business_area",
    "info_type",
    "region",
    "reliability",
    "customer_relevance",
    "practical_usability",
    "adoption_class",
]

ScoringAxisId = Literal[
    "customer_relevance",
    "practical_usability",
    "market_impact",
    "advisory_usability",
    "reliability",
    "urgency_freshness",
]

INFORMATION_CATEGORY_IDS: tuple[str, ...] = get_args(InformationCategoryId)
REQUIRED_TAG_IDS: tuple[str, ...] = get_args(RequiredTagId)
SCORING_AXIS_IDS: tuple[str, ...] = get_args(ScoringAxisId)


# --- 共有 enum（設計書 §2.1 `$defs`）----------------------------------------


class Priority(StrEnum):
    """カテゴリ優先度。`mid_high` は xlsx 原本の「中〜高」表記に対応（仕様書 §5.3）。"""

    LOW = "low"
    MID = "mid"
    MID_HIGH = "mid_high"
    HIGH = "high"


class Severity(StrEnum):
    """除外ルールの強度。フィルタ挙動の分岐そのもの（仕様書 §5.4 / T-17）。"""

    FULL_EXCLUDE = "full_exclude"
    DEFAULT_EXCLUDE = "default_exclude"
    LOW_PRIORITY = "low_priority"
    LOW_PRIORITY_OR_EXCLUDE = "low_priority_or_exclude"
    MERGE = "merge"


# --- enum の日本語確定値（仕様書 §5.2。推測で変更しない）--------------------

Reliability = Literal["高", "中", "要確認", "低"]

CustomerRelevance = Literal[
    "直接関係", "近く応用可能", "テーマ一部参考", "一般参考", "関連薄い"
]

PracticalUsability = Literal[
    "すぐ活用", "具体例参考", "参考になる", "追加解釈が必要", "一般的", "見込み薄い"
]

AdoptionClass = Literal["次回定例で提案", "参考情報", "共有のみ", "不採用"]

Region = Literal["日本", "海外", "グローバル"]

InfoType = Literal[
    "一次情報(公式発表)",
    "主要メディア報道",
    "専門メディア報道",
    "ブログ・プレスリリース",
    "個人SNS・二次情報",
]

TagValueType = Literal["single", "multi", "enum"]

AdminRole = Literal["admin"]


class _StrictModel(BaseModel):
    """未知キーを拒否する（設計書 §2.1 の `additionalProperties: false`）。

    タイポした設定キーが黙って無視されると、管理者は「設定したつもり」なのに
    挙動が変わらない事故になる。config はパイプライン全体の判断基準なので、
    知らないキーは受け取った時点で落とす。
    """

    model_config = ConfigDict(extra="forbid")


# --- 各セクション ------------------------------------------------------------


class ConfigMeta(_StrictModel):
    """config 自体のメタ情報。`revision` は楽観ロックの比較対象（設計書 §4.3）。"""

    config_name: Literal["ai_intelligence_requirements"]
    source_of_truth_xlsx: Literal["weekly_ai_intelligence_requirements.xlsx"]
    editable_by: list[AdminRole] = Field(min_length=1)
    visible_to: list[AdminRole] = Field(min_length=1)
    # 初期マイグレーション投入時は updated_by が null（設計書 §10.3 手順6）。
    updated_at: datetime | None = None
    updated_by: str | None = None
    revision: int = Field(ge=1)


class InformationCategory(_StrictModel):
    """情報カテゴリ。`priority` のみ可変（仕様書 §7.2）。"""

    id: InformationCategoryId
    label: str
    priority: Priority
    description: str


class RequiredTag(_StrictModel):
    """必須タグ定義。10件すべて必須なので `required` は `true` 固定。

    `value_source` が `enums.*` を指すときに当該キーが実在するかの検証は
    クロスフィールド制約なので T-05（設計書 §2.1.1-4）。
    """

    id: RequiredTagId
    label: str
    type: TagValueType
    required: Literal[True]
    purpose: str
    value_source: str


class ScoringAxis(_StrictModel):
    """スコアリング軸。`weight` が可変（仕様書 §7.2）。

    `weight` は 0-100 の整数まで。合計100 の担保は T-05（設計判断A: 保存拒否）。
    """

    id: ScoringAxisId
    label: str
    weight: int = Field(ge=0, le=SCORING_TOTAL)
    criterion: str
    bands: list[str] = Field(min_length=1)


class ExclusionRule(_StrictModel):
    """除外ルール。`severity` と `enabled` が可変（仕様書 §7.2）。

    評価は `no` 昇順（T-17）。
    """

    no: int = Field(ge=1, le=EXCLUSION_RULE_COUNT)
    severity: Severity
    enabled: bool
    name: str
    examples: str


class ConfigEnums(_StrictModel):
    """タグの取り得る値。分類・採点プロンプトの出力スキーマ生成元になる（T-19）。

    `industry` / `business_area` だけは運用で増減しうるため自由文字列。
    それ以外は仕様書 §5.2 の確定値を `Literal` で固定する。
    """

    priority: list[Priority]
    severity: list[Severity]
    reliability: list[Reliability]
    customer_relevance: list[CustomerRelevance]
    practical_usability: list[PracticalUsability]
    adoption_class: list[AdoptionClass]
    region: list[Region]
    info_type: list[InfoType]
    industry: list[str]
    business_area: list[str]


class AdoptionClassScoreMap(_StrictModel):
    """採用区分のしきい値。降順整合（≥ の連鎖）の検証は T-05（設計書 §2.1.1-2）。"""

    propose_next_meeting: int = Field(ge=0, le=SCORING_TOTAL)
    reference_info: int = Field(ge=0, le=SCORING_TOTAL)
    share_only: int = Field(ge=0, le=SCORING_TOTAL)


class WeeklyThresholds(_StrictModel):
    """週刊メルマガの可変パラメータ（仕様書 §7.2）。"""

    # enums.industry のいずれかであること（参照整合）は T-05（設計書 §2.1.1-3）。
    target_industry: str
    max_industry_topics: int = Field(ge=0)
    max_common_topics: int = Field(ge=0)
    point_of_week_required: bool

    @property
    def industries(self) -> tuple[str, ...]:
        """対象業界（**必ず1件以上**）。

        収集の重点（T-16 / T-46）・顧客関連度の採点（T-19）・生成テキスト（T-44）・
        描画（T-24）が参照する読み出し口を**1つ**にしてある。フィールドの形
        （単数 / 複数）が変わっても、参照側はここだけを見ていれば追随できる。
        """
        return (self.target_industry,)


class MonthlyThresholds(_StrictModel):
    """月刊ビリーフの可変パラメータ（仕様書 §7.2）。"""

    target_case_count: int = Field(ge=0)
    chapter_count_hint: int = Field(ge=0)
    min_score_for_case: int = Field(ge=0, le=SCORING_TOTAL)
    require_editorial_and_closing: bool


class DedupThresholds(_StrictModel):
    """重複判定パラメータ（仕様書 §11.2 / T-18）。"""

    lookback_weeks: int = Field(ge=0)
    title_similarity_threshold: float = Field(ge=0, le=1)
    treat_same_url_as_duplicate: bool
    # ⚠️ **仕様書 §5.2 の確定 JSON に無いキー**（2026-08-16 の決定2 で追加）。
    # §11.1 の月次「当月＋直近数ヶ月」の月数が仕様書・設計書のどちらにも無く、
    # T-18 は「config に無いしきい値を作らない」ために月数を呼び出し側から受け取る
    # 形にしてあった。その受け取り先が T-21 なので、決め打ちの代わりに config へ
    # 足す（→ TASKS.md T-21 備考・T-38 の改訂対象）。
    #
    # **既定値を持たせてあるのは、この鍵を持たない既存の `config.json` を読めなく
    # しないため**（`ConfigRepository.load()` が落ちると admin が管理画面から
    # 直せなくなる＝T-11 の load が検証を緩めているのと同じ理由）。
    # 下限は 1（0 だと「当月だけ」になり、`lookback_months=0` を渡すのと区別が
    # 付かないうえ、§11.1 の「直近数ヶ月」を満たさない）。
    monthly_lookback_months: int = Field(default=3, ge=1)


class TunableThresholds(_StrictModel):
    """管理画面が編集するしきい値群（仕様書 §7.2 の編集可能パラメータ）。"""

    min_total_score_to_publish: int = Field(ge=0, le=SCORING_TOTAL)
    adoption_class_score_map: AdoptionClassScoreMap
    # 信頼性は6軸中 0-10 点なので上限は 10（仕様書 §5.2 scoring_axes.reliability）。
    min_reliability_score_to_publish: int = Field(ge=0, le=10)
    weekly: WeeklyThresholds
    monthly: MonthlyThresholds
    dedup: DedupThresholds


class IntelligenceConfig(_StrictModel):
    """`config.json` 全体（設計書 §2.1）。

    フィールドの宣言順は仕様書 §5.2 のキー順と一致させている。書き出した
    `config.json` のキー順が実データと揃い、revision 間の diff が読める形に
    なるため（監査ログの diff は設計書 §4.4）。
    """

    schema_version: Literal["1.0"]
    meta: ConfigMeta
    information_categories: list[InformationCategory] = Field(
        min_length=INFORMATION_CATEGORY_COUNT, max_length=INFORMATION_CATEGORY_COUNT
    )
    required_tags: list[RequiredTag] = Field(
        min_length=REQUIRED_TAG_COUNT, max_length=REQUIRED_TAG_COUNT
    )
    scoring_axes: list[ScoringAxis] = Field(
        min_length=SCORING_AXIS_COUNT, max_length=SCORING_AXIS_COUNT
    )
    scoring_total: Literal[100]
    exclusion_rules: list[ExclusionRule] = Field(
        min_length=EXCLUSION_RULE_COUNT, max_length=EXCLUSION_RULE_COUNT
    )
    enums: ConfigEnums
    tunable_thresholds: TunableThresholds
    source_whitelist_hint: list[str] = Field(default_factory=list)


# --- JSON Schema 生成 --------------------------------------------------------


class ConfigJsonSchemaGenerator(GenerateJsonSchema):
    """`$defs` のキーをスネークケースへ寄せる生成器。

    設計書 §2.1 が `#/$defs/priority` / `#/$defs/severity` を参照しているため、
    生成物の参照名を設計書と一致させる。IDはすべて英小文字スネークケース、が
    仕様書 §5.1 の原則でもある。
    """

    def normalize_name(self, name: str) -> str:
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", super().normalize_name(name))
        return snake.lower()


def config_json_schema() -> dict[str, Any]:
    """`IntelligenceConfig` から JSON Schema（draft 2020-12）を組み立てる。

    Pydantic は `$schema` を付けず `title` を先頭に置かないので、設計書 §2.1 と
    同じ並び（`$schema` → `$id` → `title` → 本体 → `$defs`）へ整えて返す。

    Returns:
        JSON Schema 相当の dict
    """
    body = IntelligenceConfig.model_json_schema(
        schema_generator=ConfigJsonSchemaGenerator
    )
    defs = body.pop("$defs", {})
    body.pop("title", None)
    return {
        "$schema": ConfigJsonSchemaGenerator.schema_dialect,
        "$id": JSON_SCHEMA_ID,
        "title": JSON_SCHEMA_TITLE,
        **body,
        "$defs": defs,
    }


def config_json_schema_text() -> str:
    """`schemas/config.schema.json` として書き出す文字列。

    生成コマンドとドリフト検知テストが同じ関数を使うことで、「生成し直したら
    差分が出る」状態をテストで検出できる。日本語 enum を含むので
    `ensure_ascii=False`（入出力は UTF-8。設計書 §14）。
    """
    return json.dumps(config_json_schema(), indent=2, ensure_ascii=False) + "\n"
