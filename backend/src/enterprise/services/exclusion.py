"""除外ルール判定エンジン（設計書 §6.2 ／ 仕様書 §5.4・§13.3-1 ／ T-17）。

フィルタ段の1番目。13の除外ルールを **`no` 昇順**で当て、最初に当たったルールの
`severity` に従って「除外 / 低優先 / 統合 / 採用」を決める。

---

**このモジュールが「決定的 Python で強制」の中核である理由**（TASKS.md §1.1）

パイプラインは分類・採点を LLM に任せる（T-19）。その LLM に「この記事は除外で
よいですか」まで聞くと、**同じ config・同じ記事で実行のたびに採否が変わる**。
§14 の再現性要件と、config を「唯一の判断基準」とする §5.1 の原則が同時に崩れる。

そこで、この層は次の2つしか見ない:

1. `config`（`exclusion_rules` の `no` / `severity` / `enabled`、`tunable_thresholds`）
2. `ScreenedArticle`（記事そのものと、**事実の申告**だけ）

**`ScreenedArticle` に「除外すべき」「採用すべき」に相当するフィールドを足さないこと。**
足した瞬間に上流（＝LLM）が分岐を上書きできるようになり、このモジュールを置いた
意味が消える。持ってよいのは「どのルールに当たるか」「顧客関連度は何か」「合計は
何点の見込みか」「鮮度が低いか」という**観測**だけで、そこから何が起きるかは
config と下の `evaluate_exclusions()` だけが決める。
`test_screened_article_carries_facts_only` がこの境界を固定している。

---

**なぜ「どのルールに当たるか」を外から受け取るのか**

ルールの中身（例: 「アフィリエイト・広告色の強いツール紹介記事」）は自然文で、
`examples` も判断材料の列挙にすぎない。当たり判定そのものは意味理解が要るため
決定的には書けず、上流の選別（crawl / 分類。§13.3-1）が申告する。

**申告できるのは「当たったルール番号」までで、そこから先は渡さない。** 同じ
`matched_rule_nos` でも、admin が `severity` を変えれば結果は変わる（→
`test_the_same_signals_follow_the_config_severity`）。これが「LLM の判断で
severity 分岐を上書きさせない」の具体的な形。

---

**`is_stale`（鮮度が低いか）を日付から計算しない理由**

`low_priority_or_exclude`（ルール13「古い情報の再掲・まとめ記事」）の分岐に要る
情報だが、そのルールの `examples` は **「同一日付は新しいが中身が古いまとめ」** と
明記している。つまり **日付を見ても判定できない**のが仕様の前提であり、
`published_at` から算出すると仕様の例をそのまま取り違える。加えて config に鮮度の
しきい値は無く（`tunable_thresholds` に該当キーが無い）、ここで日数を決め打ちすると
「config に無いしきい値」が生まれる。よって内容の鮮度は事実として受け取る。
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, get_args

from pydantic import BaseModel, ConfigDict, Field

from enterprise.entities.config import (
    EXCLUSION_RULE_COUNT,
    SCORING_TOTAL,
    AdoptionClass,
    CustomerRelevance,
    ExclusionRule,
    IntelligenceConfig,
    Severity,
)
from enterprise.entities.raw_article import RawArticle
from enterprise.entities.report_columns import EXCLUSION_LOG_COLUMNS, format_row

# `default_exclude` の例外採用条件（仕様書 §5.4「顧客関連度が『直接関係』かつ
# 総合スコアがしきい値超なら例外採用可」）。値は §5.2 の確定 enum。型注釈を
# `CustomerRelevance` にしてあるので、書き間違えれば型チェックで落ちる
# （`test_direct_relevance_is_a_config_enum_value` が実データとも突き合わせる）。
DIRECT_CUSTOMER_RELEVANCE: CustomerRelevance = "直接関係"

# 除外ログ（6列）の `除外区分`。severity 由来の語彙は設計書 §6.2 の文字列に合わせる。
# ⚠️ §2.2.2 は除外区分を「等」と書いて閉じていないため enum にしない。
# `統合` は重複統合（T-18 / §11.3）、`フォーマット不備` は検証（T-20 / §12.2）、
# `低スコア/信頼性不足` は採否（T-21 / §13.3-5）がそれぞれ持つ。
CATEGORY_FULL_EXCLUDE = "完全除外"
CATEGORY_DEFAULT_EXCLUDE = "原則除外"
CATEGORY_LOW_PRIORITY_OR_EXCLUDE = "低優先/除外"

# `default_exclude` を例外採用したときに残す理由（設計書 §6.2 の note 形式）。
# 仕様書 §5.4 が「要理由記載」としているので、採用側にも必ず文字列を残す。
DEFAULT_EXCLUDE_EXCEPTION_NOTE = "default_exclude例外採用: "

# 採用区分の強い順（仕様書 §5.2 `enums.adoption_class` の並び）。降格はこの並びを
# 1つ下げる（→ `downgrade_adoption_class`）。
ADOPTION_CLASS_DESCENDING: tuple[AdoptionClass, ...] = get_args(AdoptionClass)


class ExclusionError(Exception):
    """判定に使えない入力（config に無いルール番号など）。"""


class ExclusionAction(StrEnum):
    """判定結果。§13.3 のパイプラインが次に何をするかを決める。"""

    KEEP = "keep"
    """採用（そのまま次段へ）。"""

    EXCLUDE = "exclude"
    """除外。**除外ログ行を必ず作る**（仕様書 §5.4・§8.1）。"""

    LOW_PRIORITY = "low_priority"
    """採用するが `adoption_class` を降格する（§5.4。降格は採点後に T-21 が適用）。"""

    MERGE = "merge"
    """除外ではなく統合。重複判定（T-18 / §11）と連動する。"""


class ScreenedArticle(BaseModel):
    """判定に必要な**事実**だけを載せた記事（上流の選別結果）。

    ⚠️ **ここに「除外する / 採用する」に相当するフィールドを足さないこと。**
    理由はモジュール docstring のとおり。足すと上流が分岐を上書きできてしまう。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    article: RawArticle
    """収集した記事そのもの（除外ログの `収集日/タイトル/URL/ソース` の出どころ）。"""

    matched_rule_nos: frozenset[int] = Field(default_factory=frozenset)
    """当たった除外ルールの `no`。空なら何にも当たらない。

    値域は config の `exclusion_rules[].no` と同じ 1〜13。上流が範囲外の番号を
    申告した場合は構造的に弾く（config に無い判断を持ち込ませない）。
    """

    customer_relevance: CustomerRelevance | None = None
    """顧客関連度の見込み（`default_exclude` の例外採用条件・§5.4）。

    未判定なら `None`。**`None` は例外採用の条件を満たさない**（安全側＝除外）。
    """

    estimated_total_score: int | None = Field(default=None, ge=0, le=SCORING_TOTAL)
    """合計スコアの見込み（同じく例外採用条件の `合計見込み`）。

    確定値ではない。確定した合計は採点後に6軸の和として計算される（T-19）。
    未判定なら `None` で、やはり例外採用の条件を満たさない。
    """

    is_stale: bool = False
    """内容の鮮度が低いか（`low_priority_or_exclude` の分岐・§5.4）。

    日付から計算しない（理由はモジュール docstring）。既定は「古くない」。
    """

    def model_post_init(self, _context: Any) -> None:
        """ルール番号の値域を確かめる。

        Pydantic は `frozenset[int]` の要素に `Field(ge=...)` を掛けにくいので
        ここで見る。config の `no` は 1〜13（T-04）で、欠落・重複が無いことは
        T-05 が保証している。
        """
        out_of_range = sorted(
            no for no in self.matched_rule_nos if not 1 <= no <= EXCLUSION_RULE_COUNT
        )
        if out_of_range:
            raise ValueError(
                f"除外ルール番号が範囲外です: {out_of_range}"
                f"（1〜{EXCLUSION_RULE_COUNT}）"
            )


class ExclusionVerdict(BaseModel):
    """判定結果1件（設計書 §6.2 の戻り値）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ExclusionAction

    rule_no: int | None = None
    """判定の根拠になったルールの `no`。どれにも当たらなければ `None`。"""

    rule_name: str = ""
    """根拠ルールの `name`（除外理由の元）。"""

    category: str = ""
    """除外ログの `除外区分`。**除外時のみ埋まる**。"""

    reason: str = ""
    """除外ログの `除外理由`。ルール名をそのまま使う（§2.2.2）。"""

    note: str = ""
    """採用側に残す理由。いまは `default_exclude` の例外採用だけが使う（§5.4）。"""

    @property
    def is_excluded(self) -> bool:
        """除外ログ行を作るべきか。"""
        return self.action is ExclusionAction.EXCLUDE

    @property
    def is_low_priority(self) -> bool:
        """採点後に `adoption_class` を降格すべきか（§5.4・§6.1）。"""
        return self.action is ExclusionAction.LOW_PRIORITY


def enabled_rules_in_order(config: IntelligenceConfig) -> list[ExclusionRule]:
    """評価順（`no` 昇順）に並べた有効ルール（`enabled=true` のみ）。

    **順序がそのまま結果を決める**（先に当たったルールで打ち切る）ので、config の
    配列順ではなく `no` で並べ替える。config.json の要素順が入れ替わっても判定が
    変わらないようにするため（§13.3-1「No 順」）。
    """
    return sorted(
        (rule for rule in config.exclusion_rules if rule.enabled), key=lambda r: r.no
    )


def evaluate_exclusions(
    screened: ScreenedArticle, config: IntelligenceConfig
) -> ExclusionVerdict:
    """除外ルールを `no` 昇順に当て、最初に当たったルールで判定する（設計書 §6.2）。

    **最初に当たった1件で打ち切る。** 後ろのルールがより強い severity を持って
    いても評価しない（§13.3-1 が「No 順」と決めているため、順序の意味は
    「先に書いてあるルールが優先」）。

    Args:
        screened: 記事と、上流が申告した事実
        config: 実行開始時に固定参照している config（§6.3 の revision ピン留め済み）

    Returns:
        判定結果。どのルールにも当たらなければ `action=KEEP`

    Raises:
        ExclusionError: 申告されたルール番号が config に存在しない場合。
            握り潰すと「当たったはずのルールが黙って無視される」ため落とす
    """
    known_nos = {rule.no for rule in config.exclusion_rules}
    if unknown := sorted(screened.matched_rule_nos - known_nos):
        raise ExclusionError(
            f"config に存在しない除外ルール番号です: {unknown}"
            f"（config の no: {sorted(known_nos)}）"
        )

    thresholds = config.tunable_thresholds

    for rule in enabled_rules_in_order(config):
        if rule.no not in screened.matched_rule_nos:
            continue

        match rule.severity:
            case Severity.FULL_EXCLUDE:
                return _excluded(rule, CATEGORY_FULL_EXCLUDE)

            case Severity.DEFAULT_EXCLUDE:
                if _qualifies_for_exception(
                    screened, thresholds.min_total_score_to_publish
                ):
                    return ExclusionVerdict(
                        action=ExclusionAction.KEEP,
                        rule_no=rule.no,
                        rule_name=rule.name,
                        note=DEFAULT_EXCLUDE_EXCEPTION_NOTE + rule.name,
                    )
                return _excluded(rule, CATEGORY_DEFAULT_EXCLUDE)

            case Severity.LOW_PRIORITY:
                return ExclusionVerdict(
                    action=ExclusionAction.LOW_PRIORITY,
                    rule_no=rule.no,
                    rule_name=rule.name,
                    reason=rule.name,
                )

            case Severity.LOW_PRIORITY_OR_EXCLUDE:
                if screened.is_stale:
                    return _excluded(rule, CATEGORY_LOW_PRIORITY_OR_EXCLUDE)
                return ExclusionVerdict(
                    action=ExclusionAction.LOW_PRIORITY,
                    rule_no=rule.no,
                    rule_name=rule.name,
                    reason=rule.name,
                )

            case Severity.MERGE:
                # 除外ではない。統合されるかどうかは重複判定（T-18）が決めるので、
                # ここでは `除外区分` を埋めない（§11.3 の `統合` は T-18 が付ける）。
                return ExclusionVerdict(
                    action=ExclusionAction.MERGE,
                    rule_no=rule.no,
                    rule_name=rule.name,
                    reason=rule.name,
                )

    return ExclusionVerdict(action=ExclusionAction.KEEP)


def downgrade_adoption_class(adoption_class: AdoptionClass) -> AdoptionClass:
    """`low_priority` の記事の採用区分を1段下げる（仕様書 §5.4「`共有のみ`寄り」）。

    §5.4 は **「採用はするが」**下げる、と書いている。したがって降格で `不採用`
    にはしない（下限は `共有のみ`）。既に `不採用` のものはそのまま
    （スコアが `share_only` に届いていない＝採否判定・§13.3-5 で落ちる記事）。

    ⚠️ 適用するのは**採点後**（`adoption_class` が決まった後）で、呼ぶのは T-21。
    この関数をここに置いてあるのは、降格が `low_priority` 分岐の効果そのもので、
    分岐と降格幅が離れると片方だけ変わるため。

    Args:
        adoption_class: `adoption_class_score_map` から決まった採用区分（§6.4）

    Returns:
        1段下げた採用区分
    """
    floor = len(ADOPTION_CLASS_DESCENDING) - 2  # `不採用` の1つ上＝`共有のみ`
    index = ADOPTION_CLASS_DESCENDING.index(adoption_class)
    if index >= floor:
        return adoption_class
    return ADOPTION_CLASS_DESCENDING[index + 1]


def exclusion_log_entry(
    screened: ScreenedArticle, verdict: ExclusionVerdict
) -> dict[str, Any]:
    """除外ログ1行を列名つきで組み立てる（設計書 §2.2.2 の6列）。

    列名・列順は T-07 の `EXCLUSION_LOG_COLUMNS` だけが定義を持つ。ここで名前を
    書き間違えれば `format_row()`（→ `exclusion_log_row`）が落ちる。

    Args:
        screened: 除外された記事
        verdict: `action=EXCLUDE` の判定結果

    Returns:
        列名 → 値

    Raises:
        ExclusionError: 除外以外の判定を渡した場合。採用した記事を除外ログへ
            書くと本編と食い違うため、呼び出し側の取り違えをここで落とす
    """
    if not verdict.is_excluded:
        raise ExclusionError(
            f"除外ログ行を作れるのは action=exclude のときだけです"
            f"（渡されたのは {verdict.action}）"
        )

    article = screened.article
    return {
        "収集日": article.collected_at,
        "タイトル": article.title,
        "URL": article.url,
        "ソース": article.source,
        "除外区分": verdict.category,
        "除外理由": verdict.reason,
    }


def exclusion_log_row(
    screened: ScreenedArticle, verdict: ExclusionVerdict
) -> list[str | int | None]:
    """除外ログ1行を xlsx の列順（6列）で組み立てる（T-22 のライタへ渡す形）。"""
    return format_row(EXCLUSION_LOG_COLUMNS, exclusion_log_entry(screened, verdict))


def _excluded(rule: ExclusionRule, category: str) -> ExclusionVerdict:
    """除外の判定結果。`除外理由` はルール名そのもの（§2.2.2）。"""
    return ExclusionVerdict(
        action=ExclusionAction.EXCLUDE,
        rule_no=rule.no,
        rule_name=rule.name,
        category=category,
        reason=rule.name,
    )


def _qualifies_for_exception(
    screened: ScreenedArticle, min_total_score_to_publish: int
) -> bool:
    """`default_exclude` を例外採用してよいか（仕様書 §5.4・設計書 §6.2）。

    条件は **顧客関連度=「直接関係」かつ 合計見込み ≥ `min_total_score_to_publish`**。
    どちらか一方でも未判定（`None`）なら満たさない＝原則どおり除外する（安全側）。
    """
    if screened.customer_relevance != DIRECT_CUSTOMER_RELEVANCE:
        return False
    if screened.estimated_total_score is None:
        return False
    return screened.estimated_total_score >= min_total_score_to_publish


def matched_rule_names(
    config: IntelligenceConfig, rule_nos: Sequence[int] | frozenset[int]
) -> list[str]:
    """ルール番号からルール名を引く（監査ログ・デバッグ表示用）。

    判定そのものには使わない。`no` 昇順で返す。
    """
    by_no = {rule.no: rule.name for rule in config.exclusion_rules}
    return [by_no[no] for no in sorted(rule_nos) if no in by_no]
