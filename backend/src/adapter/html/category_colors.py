"""カテゴリラベルの色マップ（設計書 §7.2 ／ 仕様書 §9.2-4・§13.4.1 ／ T-23）。

週刊メルマガの記事カードは、情報カテゴリごとに色分けしたラベルを持つ（§9.2-4）。
その色を **カテゴリID（config の `information_categories[].id`）から引く**のがこの
モジュール。ラベル文字列ではなく ID を鍵にするのは、ラベルは admin が config で
変えうるが ID は §5.2 の確定値だから。

---

**⚠️ 確定3色と補完4色は出所が違う（要確認事項 #1）**

設計書 §7.2 の検証記録のとおり、顧客提供の実サンプル
`weekly_ai_intelligence_newsletter_不動産_2026-W31.html` から機械抽出できた色は
**3種だけ**で、残り4カテゴリの色指定はサンプルに存在しない。

- `CONFIRMED_CATEGORY_COLORS`（3色）＝ **実サンプル実測の確定値**。動かさない。
- `SUPPLEMENTED_CATEGORY_COLORS`（4色）＝ **設計書 §7.2 が近縁色で補完した暫定値。
  ブランド確認が済んでいない**（TASKS.md §5 要確認事項 #1 / T-23・T-24）。
  ブランド指定が出たらこの4行だけを差し替える。

2つを別の定数に分けてあるのは、「どれが確認済みでどれが未確認か」をコードから
読めるようにするため（1つの辞書にまとめると、差し替えてよい行が分からなくなる）。
"""

import logging
from collections.abc import Mapping
from types import MappingProxyType

from enterprise.entities.config import INFORMATION_CATEGORY_IDS

logger = logging.getLogger(__name__)

# --- 実サンプル実測の確定値（設計書 §7.2 の表・出所「実サンプルHTML実測」）------
# ⚠️ **変更しないこと。** 顧客提供サンプルの体裁そのもの。
CONFIRMED_CATEGORY_COLORS: Mapping[str, str] = MappingProxyType(
    {
        "ai_agent_automation": "#0891b2",  # シアン
        "ai_major_company_model": "#7c3aed",  # バイオレット
        "ai_governance_risk": "#dc2626",  # レッド
    }
)

# --- 補完値（設計書 §7.2 の表・出所「補完（サンプル未収載・要ブランド確認）」）---
# ⚠️ **要ブランド確認（TASKS.md §5 要確認事項 #1）。** サンプルに色指定が無い
# 4カテゴリを近縁色で埋めたもので、ブランド指定が出たらここだけを差し替える。
SUPPLEMENTED_CATEGORY_COLORS: Mapping[str, str] = MappingProxyType(
    {
        "enterprise_ai_case": "#059669",  # グリーン
        "industry_ai_trend": "#d97706",  # アンバー
        "ai_training_org_change": "#db2777",  # ピンク
        "ai_implementation_ops": "#4f46e5",  # インディゴ（週刊アクセント同系）
    }
)

CATEGORY_COLORS: Mapping[str, str] = MappingProxyType(
    {**CONFIRMED_CATEGORY_COLORS, **SUPPLEMENTED_CATEGORY_COLORS}
)
"""7カテゴリぶんの色（確定3 ＋ 補完4）。"""

# 未知のカテゴリID に当てる色。**カード自体は落とさない**（記事が1件消えるより、
# 色が既定のラベルで出る方が被害が小さい）。中立なスレートグレーにしてあるのは、
# 7色のどれとも取り違えられないようにするため。
FALLBACK_CATEGORY_COLOR = "#4b5563"


def color_of(category_id: str | None) -> str:
    """カテゴリID の色を引く。

    Args:
        category_id: `information_categories[].id`（中間xlsx 列2「情報カテゴリ」）

    Returns:
        `#rrggbb`。未知のID・空なら `FALLBACK_CATEGORY_COLOR`
    """
    if not category_id:
        logger.warning("情報カテゴリが空の記事に既定色を当てました")
        return FALLBACK_CATEGORY_COLOR
    color = CATEGORY_COLORS.get(category_id)
    if color is None:
        logger.warning("未知の情報カテゴリに既定色を当てました: %r", category_id)
        return FALLBACK_CATEGORY_COLOR
    return color


def is_brand_confirmed(category_id: str) -> bool:
    """その色が実サンプル実測の確定値か（＝ブランド確認済みか）。

    補完4色（要確認事項 #1）を洗い出すのに使う。
    """
    return category_id in CONFIRMED_CATEGORY_COLORS


def unconfirmed_category_ids() -> tuple[str, ...]:
    """ブランド確認が済んでいないカテゴリID（§7.2 の補完4色）。"""
    return tuple(SUPPLEMENTED_CATEGORY_COLORS)


def missing_category_ids() -> tuple[str, ...]:
    """config の7カテゴリのうち色が定義されていないID。

    config 側にカテゴリが増えたのに色マップが追随していない、という取りこぼしを
    テストと起動時チェックで拾えるようにする。
    """
    return tuple(
        category_id
        for category_id in INFORMATION_CATEGORY_IDS
        if category_id not in CATEGORY_COLORS
    )


__all__ = [
    "CATEGORY_COLORS",
    "CONFIRMED_CATEGORY_COLORS",
    "FALLBACK_CATEGORY_COLOR",
    "SUPPLEMENTED_CATEGORY_COLORS",
    "color_of",
    "is_brand_confirmed",
    "missing_category_ids",
    "unconfirmed_category_ids",
]
