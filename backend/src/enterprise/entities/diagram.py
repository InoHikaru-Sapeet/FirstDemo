"""図解の構造化データ（T-49）。

**「内容」は filter 段の AI が構造化データとして申告し、「描画」は決定的 Python が
行う。** render に AI を足さない方針（TASKS.md §1.1「render = 決定的 Python
テンプレート」）は変わっていない——この型は AI の出力スキーマであると同時に、
`narrative_{period}.json` に落ちる生成テキストの一部で、レンダラはこの型を
受け取って**あらかじめ決めた形で描く**だけ。

---

**⚠️ 図解タイプは3種だけ**（自由描画をさせない）

| type | 何を表すか | 構造 |
|---|---|---|
| `flow` | 3〜5ステップの流れ | `steps`（3〜5件の短い語） |
| `compare` | 2項目の対比 | `left` / `right`（見出し＋1〜3点） |
| `metrics` | 数値ハイライト2〜4個 | `items`（値＋ラベル） |

タイプごとに構造を固定してあるので、**AI は「どこに何を書くか」しか選べない**。
「SVG を書け」「レイアウトを考えろ」の類は構造的に出せない（`extra="forbid"` ＋
判別子つき union なので、`type` が3種の外なら読み込みで落ちる）。

⚠️ **図解は 0〜1個で、無くてよい。** 該当するタイプが無い記事・事例に無理やり
figure を作らせると、内容の薄い図が並ぶだけになる。呼び出し側（T-44 の出力
スキーマ）は `Diagram | None` で受け、`None` を**正常な経路**として扱う。

---

**⚠️ 文字数の上限は「体裁のための確定値」**

メールHTML は table レイアウトで、外枠 680px・カード左右余白 30px なので図解に
使える幅は約 620px しかない。`flow` の5ステップなら1マス約 110px（全角10字ぶん）で、
長い語を入れるとマスの高さが揃わずに崩れる。そこで**スキーマ側で上限を課す**
（レンダラで切り詰めると、AI が書いたものが黙って消える）。

⚠️ **数える単位は「文字数」**（`truncate_fullwidth()` の全角換算ではない）。
Pydantic の `max_length` は文字数しか見られず、ここで課したいのは「短く書かせる」
ことなので、半角混じりで多少ぶれても構わない。**見た目の幅で切るのは描画側の
責務**（T-48 の `mail_html.truncate_fullwidth()`）。

---

**⚠️ 確定値（xlsx の22列・8列）には混ぜない。**

図解は `narrative_{period}.json` 側の持ち物（2026-08-16 の決定3 と同じ扱い）。
中間xlsx の列は §8.1・§8.2 の確定値なので増やせない。
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

DIAGRAM_LABEL = "diagram"

DIAGRAM_TYPES: tuple[str, ...] = ("flow", "compare", "metrics")
"""AI が申告できる図解タイプ（**この3種だけ**）。"""

# --- 体裁のための上限（モジュール docstring）----------------------------------

TITLE_MAX_CHARS = 30
"""図解の見出し。"""

STEP_MAX_CHARS = 18
"""`flow` の1ステップ。5マスに割ると1マス約110px なので、これ以上は崩れる。"""

PANE_LABEL_MAX_CHARS = 20
"""`compare` の左右の見出し。"""

POINT_MAX_CHARS = 40
"""`compare` の1点。2列表の1セルに収まる長さ。"""

VALUE_MAX_CHARS = 12
"""`metrics` の数値そのもの（`月120時間` `-42%` `1.8億円` など）。"""

LABEL_MAX_CHARS = 20
"""`metrics` の数値が何を指すか。"""

# --- 件数の上限（依頼の確定値）------------------------------------------------

FLOW_MIN_STEPS = 3
FLOW_MAX_STEPS = 5
COMPARE_MIN_POINTS = 1
COMPARE_MAX_POINTS = 3
METRICS_MIN_ITEMS = 2
METRICS_MAX_ITEMS = 4

_STRICT = ConfigDict(extra="forbid")


def _text(limit: int) -> object:
    """前後の空白を落とし、空文字を通さず、上限文字数で切る型。"""
    return Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=limit)
    ]


_Title = _text(TITLE_MAX_CHARS)
_Step = _text(STEP_MAX_CHARS)
_PaneLabel = _text(PANE_LABEL_MAX_CHARS)
_Point = _text(POINT_MAX_CHARS)
_Value = _text(VALUE_MAX_CHARS)
_Label = _text(LABEL_MAX_CHARS)


class _DiagramBase(BaseModel):
    """3タイプに共通する骨格。"""

    model_config = _STRICT

    title: _Title  # ty: ignore[invalid-type-form]
    """図解の見出し（何を表した図かの1行）。"""


class FlowDiagram(_DiagramBase):
    """流れ図（3〜5ステップ）。

    描画は「横並びのマス＋矢印」（メールHTML は table なので `→` の文字で繋ぐ）。

    Attributes:
        steps: 各ステップの短い語（順序がそのまま流れの向き）
    """

    type: Literal["flow"]
    steps: list[_Step] = Field(  # ty: ignore[invalid-type-form]
        min_length=FLOW_MIN_STEPS, max_length=FLOW_MAX_STEPS
    )


class ComparePane(BaseModel):
    """`compare` の片側。

    Attributes:
        label: その側の見出し（`従来` / `導入後` など）
        points: その側の要点（1〜3点）
    """

    model_config = _STRICT

    label: _PaneLabel  # ty: ignore[invalid-type-form]
    points: list[_Point] = Field(  # ty: ignore[invalid-type-form]
        min_length=COMPARE_MIN_POINTS, max_length=COMPARE_MAX_POINTS
    )


class CompareDiagram(_DiagramBase):
    """対比図（2項目）。描画は2列表。

    ⚠️ **2項目に固定**（3項目以上は表が横に潰れる）。
    """

    type: Literal["compare"]
    left: ComparePane
    right: ComparePane


class MetricItem(BaseModel):
    """`metrics` の1個。

    Attributes:
        value: 数値そのもの（単位まで含めた表示用の文字列）
        label: その数値が何を指すか
    """

    model_config = _STRICT

    value: _Value  # ty: ignore[invalid-type-form]
    label: _Label  # ty: ignore[invalid-type-form]


class MetricsDiagram(_DiagramBase):
    """数値ハイライト（2〜4個）。描画は大きめのボックスの横並び。"""

    type: Literal["metrics"]
    items: list[MetricItem] = Field(
        min_length=METRICS_MIN_ITEMS, max_length=METRICS_MAX_ITEMS
    )


type Diagram = Annotated[
    FlowDiagram | CompareDiagram | MetricsDiagram, Field(discriminator="type")
]
"""図解1件。**`type` で判別する union**（3種の外は読み込みで落ちる）。"""

DIAGRAM_ADAPTER: TypeAdapter[Diagram] = TypeAdapter(Diagram)


def _check_types_match_the_union() -> None:
    """`DIAGRAM_TYPES` と実際の union が食い違っていないか（import 時に落とす）。

    ⚠️ タイプを増やすときに片方だけ直すと、プロンプト（T-44）が案内する種類と
    受け取れる種類がずれる。両方が同じ集合であることをここで固定する。
    """
    declared = {
        model.model_fields["type"].annotation.__args__[0]  # ty: ignore
        for model in (FlowDiagram, CompareDiagram, MetricsDiagram)
    }
    if declared != set(DIAGRAM_TYPES):
        raise ValueError(
            f"DIAGRAM_TYPES と図解モデルの type が一致しません: "
            f"{sorted(DIAGRAM_TYPES)} != {sorted(declared)}"
        )


_check_types_match_the_union()


__all__ = [
    "COMPARE_MAX_POINTS",
    "COMPARE_MIN_POINTS",
    "DIAGRAM_ADAPTER",
    "DIAGRAM_LABEL",
    "DIAGRAM_TYPES",
    "FLOW_MAX_STEPS",
    "FLOW_MIN_STEPS",
    "LABEL_MAX_CHARS",
    "METRICS_MAX_ITEMS",
    "METRICS_MIN_ITEMS",
    "PANE_LABEL_MAX_CHARS",
    "POINT_MAX_CHARS",
    "STEP_MAX_CHARS",
    "TITLE_MAX_CHARS",
    "VALUE_MAX_CHARS",
    "ComparePane",
    "CompareDiagram",
    "Diagram",
    "FlowDiagram",
    "MetricItem",
    "MetricsDiagram",
]
