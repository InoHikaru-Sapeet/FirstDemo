"""図解の構造化データ（T-49 ／ `enterprise.entities.diagram`）。

重点:

- **スキーマ外の図解を構造的に出せない**（3タイプの外・余計なキー・自由記述の
  レイアウト指定はすべて読み込みで落ちる）
- **件数と文字数の上限**（メールHTML の table に収まる形をスキーマ側で固定する）
- **`None`（図解なし）が正常な経路**である
- `DIAGRAM_TYPES` と実装の union が一致している（片方だけ増やせない）
"""

import pytest
from pydantic import BaseModel, ValidationError

from enterprise.entities.diagram import (
    COMPARE_MAX_POINTS,
    DIAGRAM_ADAPTER,
    DIAGRAM_TYPES,
    FLOW_MAX_STEPS,
    FLOW_MIN_STEPS,
    METRICS_MAX_ITEMS,
    METRICS_MIN_ITEMS,
    STEP_MAX_CHARS,
    TITLE_MAX_CHARS,
    VALUE_MAX_CHARS,
    CompareDiagram,
    Diagram,
    FlowDiagram,
    MetricsDiagram,
)

FLOW = {
    "type": "flow",
    "title": "契約業務の自動化",
    "steps": ["契約書を受領", "AIが下書き", "担当者が確認", "締結"],
}

COMPARE = {
    "type": "compare",
    "title": "導入前後の運用",
    "left": {"label": "従来", "points": ["担当者が全文を読む"]},
    "right": {"label": "導入後", "points": ["AIの要点を確認する"]},
}

METRICS = {
    "type": "metrics",
    "title": "導入の効果",
    "items": [
        {"value": "月120時間", "label": "問い合わせ対応の工数"},
        {"value": "-42%", "label": "一次回答までの時間"},
    ],
}


class Holder(BaseModel):
    """`Diagram | None` を持つ入れ物（AI 出力スキーマと同じ形）。"""

    diagram: Diagram | None = None


# --- 3タイプが読める -----------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "model"),
    [(FLOW, FlowDiagram), (COMPARE, CompareDiagram), (METRICS, MetricsDiagram)],
)
def test_each_declared_type_parses(payload: dict, model: type) -> None:
    assert isinstance(DIAGRAM_ADAPTER.validate_python(payload), model)


def test_the_declared_types_match_the_union() -> None:
    """`DIAGRAM_TYPES` と実装が一致している（import 時の検査と対）。"""
    assert set(DIAGRAM_TYPES) == {"flow", "compare", "metrics"}
    payloads = {FLOW["type"]: FLOW, COMPARE["type"]: COMPARE, METRICS["type"]: METRICS}
    for name in DIAGRAM_TYPES:
        assert DIAGRAM_ADAPTER.validate_python(payloads[name]).type == name


# --- スキーマ外の図解を構造的に出せない ---------------------------------------


def test_an_undeclared_diagram_type_is_rejected() -> None:
    """3種の外（例: タイムライン）は**型の段階で**通らない。"""
    with pytest.raises(ValidationError):
        DIAGRAM_ADAPTER.validate_python(
            {"type": "timeline", "title": "年表", "events": ["2024", "2025"]}
        )


def test_a_diagram_without_a_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DIAGRAM_ADAPTER.validate_python({"title": "何かの図", "steps": ["A", "B", "C"]})


def test_free_form_drawing_instructions_are_rejected() -> None:
    """⚠️ **自由描画の指定を混ぜられない**（`extra="forbid"`）。

    `svg` / `html` / `layout` のような鍵を足しても、読み込みで落ちる＝
    レンダラまで届かない。
    """
    for extra in ("svg", "html", "layout", "style"):
        with pytest.raises(ValidationError):
            DIAGRAM_ADAPTER.validate_python({**FLOW, extra: "<svg/>"})


def test_a_flow_outside_the_step_range_is_rejected() -> None:
    """流れは3〜5ステップ（横並びのマスに収まる範囲）。"""
    for count in (FLOW_MIN_STEPS - 1, FLOW_MAX_STEPS + 1):
        with pytest.raises(ValidationError):
            DIAGRAM_ADAPTER.validate_python({**FLOW, "steps": ["語"] * count})


def test_a_compare_is_fixed_to_two_panes() -> None:
    """対比は2項目に固定（3項目目を足す場所が構造的に無い）。"""
    assert set(CompareDiagram.model_fields) == {"type", "title", "left", "right"}
    with pytest.raises(ValidationError):
        DIAGRAM_ADAPTER.validate_python(
            {**COMPARE, "middle": {"label": "途中", "points": ["…"]}}
        )


def test_a_compare_pane_with_too_many_points_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DIAGRAM_ADAPTER.validate_python(
            {
                **COMPARE,
                "left": {"label": "従来", "points": ["点"] * (COMPARE_MAX_POINTS + 1)},
            }
        )


def test_metrics_outside_the_item_range_are_rejected() -> None:
    item = {"value": "1割", "label": "削減率"}
    for count in (METRICS_MIN_ITEMS - 1, METRICS_MAX_ITEMS + 1):
        with pytest.raises(ValidationError):
            DIAGRAM_ADAPTER.validate_python({**METRICS, "items": [item] * count})


@pytest.mark.parametrize(
    ("payload", "field", "value"),
    [
        (FLOW, "title", "あ" * (TITLE_MAX_CHARS + 1)),
        (FLOW, "steps", ["あ" * (STEP_MAX_CHARS + 1), "受領", "確認"]),
    ],
)
def test_text_over_the_layout_limit_is_rejected(
    payload: dict, field: str, value: object
) -> None:
    """⚠️ **上限はレンダラで切らずスキーマで課す**（黙って消さない）。"""
    with pytest.raises(ValidationError):
        DIAGRAM_ADAPTER.validate_python({**payload, field: value})


def test_a_metric_value_over_the_limit_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DIAGRAM_ADAPTER.validate_python(
            {
                **METRICS,
                "items": [
                    {"value": "9" * (VALUE_MAX_CHARS + 1), "label": "削減率"},
                    {"value": "2割", "label": "工数"},
                ],
            }
        )


def test_blank_text_is_rejected() -> None:
    """空白だけの語は通さない（空のマスを描くことになる）。"""
    with pytest.raises(ValidationError):
        DIAGRAM_ADAPTER.validate_python({**FLOW, "steps": ["受領", "   ", "確認"]})


# --- 図解なしが正常 -------------------------------------------------------------


def test_no_diagram_is_a_normal_value() -> None:
    """⚠️ **`None` は「作れなかった」ではなく「作らないのが正しい」**。"""
    assert Holder().diagram is None
    assert Holder.model_validate({"diagram": None}).diagram is None


def test_a_diagram_is_optional_but_still_validated_when_present() -> None:
    assert Holder.model_validate({"diagram": FLOW}).diagram is not None
    with pytest.raises(ValidationError):
        Holder.model_validate({"diagram": {"type": "flow", "title": "壊れた図"}})
