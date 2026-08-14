"""分類・10タグ付与・6軸採点（設計書 §6.1-3/4・§6.4 ／ 仕様書 §13.3-3/4 ／ T-19）。

フィルタ段の3〜4番目。除外判定（T-17）と重複統合（T-18）を通り抜けた記事1件に
ついて、**LLM に聞くのは「分類・9タグ・6軸の点数・一言要約」だけ**を聞き、
**合計スコアと `adoption_class` はアプリ側が config から決定的に決める**。

---

**この層の責務（これ以外を持たせないこと）**

| やること | やらないこと（担当） |
|---|---|
| 情報カテゴリの分類 | 除外ルール判定（T-17 `evaluate_exclusions`） |
| 10必須タグのうち9つの付与 | 重複・統合判定（T-18 `detect_duplicate`） |
| 6軸の点数（各軸 0〜weight） | §12 フォーマットチェック（T-20 `check_article`） |
| 一言要約（2〜3文） | 採否（`min_total_score_to_publish` 等・T-21） |
| 合算・`adoption_class` 決定・降格の適用 | 行の組み立て・整列（T-21 / T-22） |

⚠️ **右列のロジックをこのモジュールへ写さないこと。** 同じ判断が2箇所にあると、
片方を直したときにもう片方が黙って古い基準で動く。

---

**LLM に「合計」と「採用区分」を出させない理由**（TASKS.md §1.1「AI利用範囲」）

LLM は「合計スコアは78点です」と書きながら6軸の和が別の値になることがある
（T-20 冒頭）。そこで:

1. **合計スコア = 6軸の和**を `total_score()` が計算する。LLM の申告値は
   **受け取る口すら作らない**（出力スキーマは `extra="forbid"` なので、
   `total_score` のようなキーを足した出力は構造的にパースに失敗する）。
2. **`adoption_class` は `adoption_class_score_map` から決める**（§6.4）。
   出力スキーマに `adoption_class` フィールドが**無い**ので、LLM は申告できない。

同じ理由で、**enum 系タグの候補は config の `enums`（＋7カテゴリID）から実行時に
組み立てる**（`build_classification_schema()`）。config に無い値は
**JSON Schema の enum に載らない＝構造的に出せない**。候補を文章で頼むだけでは
守られないことがあるので、型で閉じる。

---

**§6.1 の擬似コードとの順序の対応**（★T-17 の降格関数との関係）

    # 4) 6軸採点
    s = score_axes(a, config.scoring_axes)
    total = sum(s.values())
    tags.adoption_class = decide_adoption_class(total, tt.adoption_class_score_map)
    if verdict.action == "low_priority":
        tags.adoption_class = downgrade(tags.adoption_class)

**採点 → 合算 → 区分決定 → （`low_priority` なら降格）** の順序は
`decide_adoption()` が1本で持つ。呼び出し側（T-21）が順序を組み替えられないよう、
除外判定の結果（`ExclusionVerdict`）を渡すだけで最終的な区分が返る形にしてある。

⚠️ **降格の実装は T-17 の `downgrade_adoption_class()` をそのまま呼ぶ**
（降格幅は `low_priority` 分岐の効果そのものなので、severity 分岐と同じ場所に
置いてある。ここに写すと片方だけ変わる）。順序を入れ替える——例えば降格した後に
区分を決め直す、しきい値を下げて代用する——と §5.4 の「採用はするが下げる」から
外れるので、`test_the_order_matches_the_pseudocode` が固定している。

---

**AI 呼び出しは `AIClient`（T-15）経由の1本だけ。** 渡すのはプロンプトと出力
スキーマだけで、呼び出し先が Claude Code CLI か Anthropic API かをこの層は知らない
（差し替え口は `adapter.llm.get_ai_client()`）。

⚠️ **出力形式（「JSON だけを出せ」＋ JSON Schema）の指示は `AIClient` の実装側が
付ける。** このモジュールのプロンプトに書き足さないこと（二重指示になり、実装を
API へ差し替えたときに片方だけ残る）。

⚠️ **タイムアウトは既定（`Settings.ai_timeout_seconds` = 10分）を使う。** 30分の
`ai_crawl_timeout_seconds` は crawl（T-16）専用。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, create_model

from adapter.llm import AIClient
from adapter.llm.ai_client import AICallMeta
from enterprise.entities.config import (
    AdoptionClass,
    IntelligenceConfig,
    RequiredTag,
    RequiredTagId,
)
from enterprise.entities.config_validation import ENUM_SOURCE_PREFIX
from enterprise.entities.raw_article import RawArticle
from enterprise.entities.report_columns import axis_score_bounds
from enterprise.services.exclusion import (
    ExclusionVerdict,
    downgrade_adoption_class,
)
from enterprise.services.format_check import (
    CATEGORY_ID_SOURCE,
    FREE_VALUE_SOURCE,
    MIN_SUMMARY_SENTENCES,
)

# プロンプトの版（T-30 で `prompts/PROMPT-2.md` へ切り出し、ローダが読む予定）。
# それまではこのモジュールが唯一の置き場。⚠️ **本文を変えたら版も上げること**
# （実行時の版は `AICallMeta.prompt_version` として監査／validation メタに載る。
# 設計書 §9.2 の再現性要件）。
PROMPT_NAME = "PROMPT-2/classify_and_score"
PROMPT_VERSION = "0.1.0"

# 出力スキーマのフィールド名。タグID と軸ID は3つ重なる（`reliability` /
# `customer_relevance` / `practical_usability` はタグでも軸でもある）ため、
# 平らな1階層では表せない。`tags` / `scores` に分けて衝突を避ける。
TAGS_FIELD = "tags"
SCORES_FIELD = "scores"
SUMMARY_FIELD = "summary"

# LLM に決めさせないタグ（合計スコアから決定的に決まる。§6.4）。
# **出力スキーマからも外す**ので、LLM は申告する口を持たない。
DERIVED_TAG_ID: RequiredTagId = "adoption_class"

# 一言要約の文の数。下限は **T-20 の判定と同じ定数**を使う（§12.2 の warning と
# プロンプトの指示が食い違わないように。ずらすと「指示どおり書いたのに warning」
# になる）。上限 3 は §8.1 / T-07 の列注記「2〜3文」の上側で、T-20 は上側を
# warning にしていない（§12.2 が挙げているのは「短すぎる等」だけ）ため指示のみ。
SUMMARY_MIN_SENTENCES = MIN_SUMMARY_SENTENCES
SUMMARY_MAX_SENTENCES = 3

# 仕様書 §13.3 の PROMPT-2 冒頭の指示（**逐語**）。config を唯一の基準とし、
# 実行時点の値を主観で上書きさせない。⚠️ この一文を削らないこと。
CONFIG_AUTHORITY_INSTRUCTION = (
    "config.json のパラメータ（配点・しきい値・除外の有効/強度・対象業界）は"
    "実行時点の値をそのまま使用し、あなたの主観で上書きしないこと。"
)

# プロンプトに明記する「この依頼の対象外」。決定的 Python 側（T-17 / T-18 / T-20 /
# T-21）が持つ判断を LLM に聞かないことを、プロンプト本文でも宣言しておく。
OUT_OF_SCOPE_NOTICE = (
    "除外するかどうかの判定・重複や統合の判定・§12 のフォーマット検証・"
    "掲載可否（しきい値の適用）は、いずれもアプリ側が config から決定的に行う。"
    "これらを判断・出力しないこと。"
)


class ClassificationError(Exception):
    """分類・採点に使えない入力（候補が空の enum、軸の取り違えなど）。

    ⚠️ **AI 呼び出しの失敗はこれに包まない。** `AIClientError` とその
    サブクラス（T-15）が原因ごとに分かれており、ジョブの再実行判断に使うため
    そのまま呼び出し元へ通す。
    """


@dataclass(frozen=True, slots=True)
class AdoptionDecision:
    """6軸の点数から採用区分が決まるまでの記録（§6.4・§5.4）。

    Attributes:
        total_score: 6軸の和（**アプリ側の計算値**。LLM の申告値ではない）
        scored_class: 合計スコアと `adoption_class_score_map` から決めた区分
        adoption_class: 降格を適用した**最終**の区分（xlsx に載る値）
    """

    total_score: int
    scored_class: AdoptionClass
    adoption_class: AdoptionClass

    @property
    def is_downgraded(self) -> bool:
        """`low_priority` により1段下げられたか（§5.4）。"""
        return self.adoption_class != self.scored_class


@dataclass(frozen=True, slots=True)
class ClassifiedArticle:
    """1記事の分類・タグ・軸点・要約（T-21 が中間xlsx の行へ組み立てる素）。

    Attributes:
        article: 元の記事（crawl の出力）
        tags: タグID → 値。**10必須タグすべて**が入る（`adoption_class` は
            アプリ側が決めた値）。multi タグは `tuple[str, ...]`
        scores: 軸ID → 点数（6軸すべて）
        adoption: 合算・区分決定・降格の記録
        summary: 一言要約（2〜3文）
        meta: AI 呼び出しの出自（使用モデル・`prompt_version`。T-30 / 監査ログ）
    """

    article: RawArticle
    tags: Mapping[RequiredTagId, str | tuple[str, ...]]
    scores: Mapping[str, int]
    adoption: AdoptionDecision
    summary: str
    meta: AICallMeta

    @property
    def total_score(self) -> int:
        """合計スコア（6軸の和）。"""
        return self.adoption.total_score

    @property
    def adoption_class(self) -> AdoptionClass:
        """最終の採用区分（降格適用後）。"""
        return self.adoption.adoption_class


class ArticleClassifier:
    """記事1件を分類・採点する（AI 呼び出しは `AIClient` 経由の1本だけ）。

    出力スキーマは **config から1回だけ組み立てて持つ**（記事ごとに作り直さない）。
    実行中に config が変わっても結果が揺れないよう、`config` は実行開始時に固定
    参照しているもの（§6.3 の revision ピン留め済み）を渡すこと。
    """

    def __init__(
        self,
        *,
        client: AIClient,
        config: IntelligenceConfig,
        timeout: float | None = None,
    ) -> None:
        """
        Args:
            client: AI クライアント（`adapter.llm.get_ai_client()` の戻り値）
            config: 実行開始時に固定参照している config
            timeout: 1件あたりの制限時間（秒）。`None` なら実装の既定
                （`Settings.ai_timeout_seconds` = 10分）。⚠️ crawl 用の30分を
                ここへ渡さない
        """
        self._client = client
        self._config = config
        self._timeout = timeout
        self._output_schema = build_classification_schema(config)

    @property
    def output_schema(self) -> type[BaseModel]:
        """この config から組み立てた出力スキーマ（enum は config の実値）。"""
        return self._output_schema

    def build_prompt(self, article: RawArticle) -> str:
        """送るプロンプト（`AIClient` が出力形式の指示を後ろへ足す）。"""
        return build_classification_prompt(article, self._config)

    async def classify(
        self, article: RawArticle, *, verdict: ExclusionVerdict | None = None
    ) -> ClassifiedArticle:
        """記事1件を分類・採点し、採用区分まで決めて返す（§6.1 の 3〜4）。

        Args:
            article: 除外・重複を通り抜けた記事
            verdict: 除外判定（T-17）の結果。`low_priority` なら採点後に
                `adoption_class` を1段下げる（§5.4）。`None` なら降格しない

        Returns:
            10必須タグ・6軸点・合計・採用区分・要約が揃った1件

        Raises:
            ClassificationError: 除外済みの記事を渡された場合（本編と除外ログの
                両方に載る事故を防ぐ）、または config から候補を作れない場合
            AIClientError: AI 呼び出しの失敗（原因ごとのサブクラス。握り潰さない）
        """
        _ensure_not_excluded(verdict)

        result = await self._client.complete(
            prompt=self.build_prompt(article),
            output_schema=self._output_schema,
            prompt_version=PROMPT_VERSION,
            timeout=self._timeout,
        )

        payload = result.value.model_dump()
        tags = dict(_tag_values(payload[TAGS_FIELD], self._config))
        scores: dict[str, int] = {
            str(axis.id): int(payload[SCORES_FIELD][axis.id])
            for axis in self._config.scoring_axes
        }
        adoption = decide_adoption(scores, self._config, verdict=verdict)
        tags[DERIVED_TAG_ID] = adoption.adoption_class

        return ClassifiedArticle(
            article=article,
            tags=tags,
            scores=scores,
            adoption=adoption,
            summary=str(payload[SUMMARY_FIELD]).strip(),
            meta=result.meta,
        )


# --- 出力スキーマ（config から動的に生成）-----------------------------------


def build_classification_schema(config: IntelligenceConfig) -> type[BaseModel]:
    """LLM に守らせる出力スキーマを config から組み立てる（T-19 完了条件）。

    **config 外の値を構造的に出せない形にする**のが目的:

    - enum 系タグ → `Literal[...]`（候補は `config.enums.*` /
      `information_categories[].id` の**実行時の値**）
    - 6軸の点数 → `0 〜 その軸の weight` の整数（`axis_score_bounds()`＝実行時の
      `weight`。静的な `value_range` は見ない。admin が weight を変えたら上限も動く）
    - multi タグ → 1件以上の配列（§12.1 の「必須タグが非空」を構造で担保）
    - `adoption_class` / 合計スコア → **フィールドを作らない**（§6.4 の決定は
      アプリ側。`extra="forbid"` なので勝手に足した出力はパースに失敗する）

    Args:
        config: 実行開始時に固定参照している config

    Returns:
        Pydantic モデル（`AIClient.complete(output_schema=...)` へそのまま渡せる）

    Raises:
        ClassificationError: enum の候補が空で `Literal` を作れない場合。
            候補が無いまま自由文字列へ落とすと config 外の値が通ってしまう
    """
    tag_fields: dict[str, Any] = {}
    for tag in config.required_tags:
        if tag.id == DERIVED_TAG_ID:
            continue
        tag_fields[tag.id] = _tag_field(tag, config)

    bounds = axis_score_bounds(config)
    score_fields: dict[str, Any] = {}
    for axis in config.scoring_axes:
        low, high = bounds[axis.id]
        # ⚠️ `strict=True`。`true` を 1 点、`"9"` を 9 点と読み替えさせない
        # （T-20 の `_is_integer` も bool を整数として通さない）。ここで通すと
        # 「型が違う点数」が xlsx まで流れる。
        score_fields[axis.id] = (
            Annotated[int, Field(strict=True, ge=low, le=high)],
            Field(description=f"{axis.label}（{low}〜{high}点）: {axis.criterion}"),
        )

    tags_model = create_model(
        "ClassificationTags", __config__=_STRICT_OUTPUT, **tag_fields
    )
    scores_model = create_model("AxisScores", __config__=_STRICT_OUTPUT, **score_fields)
    return create_model(  # ty: ignore[no-matching-overload]
        "ArticleClassification",
        __config__=_STRICT_OUTPUT,
        **{
            TAGS_FIELD: (tags_model, ...),
            SCORES_FIELD: (scores_model, ...),
            SUMMARY_FIELD: (
                _NonEmptyText,
                Field(
                    description=(
                        f"一言要約。{SUMMARY_MIN_SENTENCES}〜"
                        f"{SUMMARY_MAX_SENTENCES}文の日本語"
                    )
                ),
            ),
        },
    )


def tag_candidates(
    tag: RequiredTag, config: IntelligenceConfig
) -> tuple[str, ...] | None:
    """タグの候補値を config から引く（`None` は自由記述）。

    `value_source` の記法は T-07 の列定義・T-20 のフォーマットチェックと同じ:

    - `enums.<key>` → `config.enums.<key>`
    - `information_categories.id` → 7カテゴリの ID
    - `free_controlled` → 自由記述（`ai_theme`。検査もしない）

    ⚠️ **順序は config の並びをそのまま保つ**（集合にしない）。出力スキーマの
    `enum` の並びが実行ごとに変わると、同じ config でもプロンプトが変わって
    再現性（§14）の手がかりが崩れる。
    """
    source = tag.value_source
    if source == FREE_VALUE_SOURCE:
        return None
    if source == CATEGORY_ID_SOURCE:
        return tuple(category.id for category in config.information_categories)
    if source.startswith(ENUM_SOURCE_PREFIX):
        values = getattr(config.enums, source.removeprefix(ENUM_SOURCE_PREFIX), None)
        if values is None:
            # 参照先の enum が実在するかは T-05（設計書 §2.1.1-4）の担当。
            # ここまで来た config は検証済みのはずなので、黙って自由記述へ
            # 落とさずに落とす（config 外の値が通る穴になる）。
            raise ClassificationError(
                f"タグ {tag.id!r} の value_source が config の enums にありません: "
                f"{source!r}"
            )
        return tuple(str(value) for value in values)
    raise ClassificationError(
        f"タグ {tag.id!r} の value_source を解釈できません: {source!r}"
    )


# --- 合計・採用区分（決定的。LLM の申告値を使わない）-------------------------


def total_score(scores: Mapping[str, int], config: IntelligenceConfig) -> int:
    """合計スコア＝6軸の和（設計書 §6.1 の `total = sum(s.values())`）。

    **LLM が申告した合計は使わない**（出力スキーマに口が無い）。

    Args:
        scores: 軸ID → 点数。6軸すべて揃っていること
        config: 実行時 config（軸の集合の正）

    Returns:
        6軸の和

    Raises:
        ClassificationError: 軸が欠けている／config に無い軸が混じっている場合。
            黙って和を取ると「5軸の合計」が合計スコアとして通ってしまう
    """
    axis_ids = [axis.id for axis in config.scoring_axes]
    if missing := [axis_id for axis_id in axis_ids if axis_id not in scores]:
        raise ClassificationError(f"点数が無い軸があります: {missing}")
    if unknown := sorted(set(scores) - set(axis_ids)):
        raise ClassificationError(f"config に無い軸が混じっています: {unknown}")
    return sum(scores[axis_id] for axis_id in axis_ids)


def decide_adoption_class(total: int, config: IntelligenceConfig) -> AdoptionClass:
    """合計スコアから採用区分を決める（設計書 §6.4 ／ 仕様書 §13.3-4）。

    しきい値は `tunable_thresholds.adoption_class_score_map`（admin が編集できる。
    降順整合の検証は T-05）。**境界はすべて `≥`**（しきい値ちょうどは上の区分）。

    Args:
        total: 合計スコア（6軸の和）
        config: 実行時 config

    Returns:
        `次回定例で提案` / `参考情報` / `共有のみ` / `不採用`
    """
    score_map = config.tunable_thresholds.adoption_class_score_map
    if total >= score_map.propose_next_meeting:
        return "次回定例で提案"
    if total >= score_map.reference_info:
        return "参考情報"
    if total >= score_map.share_only:
        return "共有のみ"
    return "不採用"


def decide_adoption(
    scores: Mapping[str, int],
    config: IntelligenceConfig,
    *,
    verdict: ExclusionVerdict | None = None,
) -> AdoptionDecision:
    """採点 → 合算 → 区分決定 → 降格 を §6.1 の順序で1本にまとめる。

    ⚠️ **順序を組み替えられないように1関数にしてある。** 降格（T-17 の
    `downgrade_adoption_class()`）は **合計スコアから区分を決めた後**に当てる
    （§5.4「採用はするが下げる」）。しきい値を下げて代用したり、降格後に区分を
    決め直したりすると、`min_total_score_to_publish` による採否（§13.3-5・T-21）
    との関係が変わってしまう。

    Args:
        scores: 軸ID → 点数（6軸）
        config: 実行時 config
        verdict: 除外判定（T-17）の結果。`action=low_priority` のときだけ降格する

    Returns:
        合計・降格前の区分・最終の区分

    Raises:
        ClassificationError: 除外済みの判定を渡された場合、または軸が揃わない場合
    """
    _ensure_not_excluded(verdict)

    total = total_score(scores, config)
    scored_class = decide_adoption_class(total, config)
    adoption_class = scored_class
    if verdict is not None and verdict.is_low_priority:
        adoption_class = downgrade_adoption_class(scored_class)

    return AdoptionDecision(
        total_score=total, scored_class=scored_class, adoption_class=adoption_class
    )


# --- プロンプト --------------------------------------------------------------


def build_classification_prompt(article: RawArticle, config: IntelligenceConfig) -> str:
    """PROMPT-2 の分類・採点部分を組み立てる（仕様書 §13.3-3/4）。

    **示す基準はすべて実行時の config から取る**（配点・候補値・得点帯・対象業界）。
    数値や候補をここに書き足さないこと（config を変えてもプロンプトが追随しない
    形になり、§13.3 の「実行時点の値をそのまま使用」が嘘になる）。

    ⚠️ **出力形式（JSON だけを出せ・JSON Schema）の指示は含めない。**
    `AIClient` の実装が付ける（`claude_cli_client.OUTPUT_INSTRUCTIONS`）。

    Args:
        article: 対象の記事1件
        config: 実行開始時に固定参照している config

    Returns:
        プロンプト本文
    """
    thresholds = config.tunable_thresholds
    sections = [
        "あなたはAI動向レポートの編集アナリストです。判断基準ファイル config.json を"
        "唯一の基準として、収集した記事1件を分類・タグ付与・採点してください。",
        "",
        "■ 厳守事項",
        f"- {CONFIG_AUTHORITY_INSTRUCTION}",
        "- 以下に示す候補値・配点・得点帯・対象業界が、実行時点の config の値である。",
        "- 候補として示されていない値は使わないこと。",
        "- 次の2つは**出力しない**（アプリ側が config から決定的に決める）:",
        "  - 合計スコア（6軸の点数をアプリ側で合算する）",
        "  - レポート採用区分（合計スコアと adoption_class_score_map から決まる）",
        f"- {OUT_OF_SCOPE_NOTICE}",
        "- 分からない項目を推測で埋めないこと。候補の中から記事の内容に最も合うものを"
        "選ぶ。",
        "",
        f"■ 対象業界（顧客関連度の判断基準）: {thresholds.weekly.target_industry}",
        "",
        "■ 対象記事",
        article.model_dump_json(indent=2),
        "",
        "■ 情報カテゴリ（1つ選び、id を返す）",
        *(
            f"- {category.id}: {category.label}（優先度 {category.priority.value}）"
            f" — {category.description}"
            for category in config.information_categories
        ),
        "",
        "■ 必須タグ",
        *_tag_lines(config),
        "",
        "■ 6軸採点（各軸、下の得点帯に従って整数で付ける）",
        *_axis_lines(config),
        "",
        "■ 一言要約",
        f"- {SUMMARY_MIN_SENTENCES}〜{SUMMARY_MAX_SENTENCES}文の日本語で書く。",
        "- 記事に書かれている事実だけを使い、意見・推測を混ぜない。",
    ]
    return "\n".join(sections)


def _tag_lines(config: IntelligenceConfig) -> list[str]:
    """必須タグの提示（候補は config の実値。`adoption_class` は出さない）。"""
    lines: list[str] = []
    for tag in config.required_tags:
        if tag.id == DERIVED_TAG_ID:
            lines.append(
                f"- {tag.id}: {tag.label} — **出力しない**"
                "（合計スコアからアプリ側が決定的に決める）"
            )
            continue
        candidates = tag_candidates(tag, config)
        cardinality = "複数選択可" if _is_multi(tag) else "1つ"
        allowed = (
            "自由記述（記事に即した短い語。1件以上）"
            if candidates is None
            else "候補: " + " / ".join(candidates)
        )
        lines.append(
            f"- {tag.id}: {tag.label}（{cardinality}・{tag.purpose}）— {allowed}"
        )
    return lines


def _axis_lines(config: IntelligenceConfig) -> list[str]:
    """6軸の提示（配点・評価観点・得点帯はすべて実行時の config から）。"""
    bounds = axis_score_bounds(config)
    lines: list[str] = []
    for axis in config.scoring_axes:
        low, high = bounds[axis.id]
        lines.append(f"- {axis.id}: {axis.label}（{low}〜{high}点） — {axis.criterion}")
        lines.append(f"  得点帯: {' / '.join(axis.bands)}")
    return lines


# --- 内部ヘルパ -------------------------------------------------------------

_STRICT_OUTPUT = ConfigDict(extra="forbid")
"""出力スキーマの共通設定。

⚠️ **`extra="forbid"` を緩めないこと。** 「合計スコアは78点」「採用区分は参考情報」
といった**申告を足した出力を、構造的に失敗させる**ためにある（T-15 の実装は
スキーマ不一致をリトライするので、指摘つきで出し直させられる）。
"""

_NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""空白だけの値を通さない文字列（§12.1「非空」を構造で担保する）。"""


def _tag_field(tag: RequiredTag, config: IntelligenceConfig) -> tuple[Any, Any]:
    """1タグぶんの (型, Field) を組み立てる。"""
    candidates = tag_candidates(tag, config)
    if candidates is None:
        item_type: Any = _NonEmptyText
        described = "自由記述"
    else:
        if not candidates:
            raise ClassificationError(
                f"タグ {tag.id!r} の候補（{tag.value_source}）が空です。"
                "候補が無いと config 外の値を防げません"
            )
        # ⚠️ `Literal` にすることが「config 外の値を構造的に出せない」の実体。
        # ここを `str` にすると候補は文章での依頼になり、守られる保証が無くなる。
        item_type = Literal[tuple(_unique(candidates))]  # ty: ignore[invalid-type-form]
        described = "／".join(candidates)

    description = f"{tag.label}（{tag.purpose}）: {described}"
    if _is_multi(tag):
        # multi タグは1件以上（§12.1「10必須タグがすべて非空」）。
        return (list[item_type], Field(min_length=1, description=description))
    return (item_type, Field(description=description))


def _is_multi(tag: RequiredTag) -> bool:
    """複数値のタグか（config の `required_tags[].type`）。"""
    return tag.type == "multi"


def _tag_values(
    payload: Mapping[str, Any], config: IntelligenceConfig
) -> dict[RequiredTagId, str | tuple[str, ...]]:
    """LLM 出力のタグを xlsx へ載せられる形へ整える。

    multi タグは `tuple` にし、**同じ値の重複は畳む**（`業界` 欄が
    `不動産;不動産` になるのを防ぐ。順序は出力順のまま）。
    """
    values: dict[RequiredTagId, str | tuple[str, ...]] = {}
    for tag in config.required_tags:
        if tag.id == DERIVED_TAG_ID:
            continue
        raw = payload[tag.id]
        if _is_multi(tag):
            values[tag.id] = _unique(str(item).strip() for item in raw)
        else:
            values[tag.id] = str(raw).strip()
    return values


def _unique(values: Any) -> tuple[str, ...]:
    """順序を保った重複除去。"""
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return tuple(seen)


def _ensure_not_excluded(verdict: ExclusionVerdict | None) -> None:
    """除外済みの記事を分類・採点へ持ち込ませない（§6.1 は `continue` する）。

    Raises:
        ClassificationError: `action=exclude` の判定を渡された場合
    """
    if verdict is not None and verdict.is_excluded:
        raise ClassificationError(
            "除外された記事は分類・採点の対象外です"
            f"（除外区分={verdict.category!r} / 除外理由={verdict.reason!r}）。"
            "§6.1 は除外ログへ回して次の記事へ進む"
        )


__all__ = [
    "CONFIG_AUTHORITY_INSTRUCTION",
    "PROMPT_NAME",
    "PROMPT_VERSION",
    "SUMMARY_MAX_SENTENCES",
    "SUMMARY_MIN_SENTENCES",
    "AdoptionDecision",
    "ArticleClassifier",
    "ClassificationError",
    "ClassifiedArticle",
    "build_classification_prompt",
    "build_classification_schema",
    "decide_adoption",
    "decide_adoption_class",
    "tag_candidates",
    "total_score",
]
