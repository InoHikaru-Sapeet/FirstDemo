"""`narrative_{period}.json` のスキーマ（T-44 ／ 決定3）。

重点:

- **段落・文は要素の列で持ち、連結は読み出し側**（`\\n\\n` は T-07 の定義だけ）
- **空の narrative が書ける**（採用記事0件の実行。必須かどうかを決めるのは config）
- **対象期間の食い違いを落とす**（先週の生成テキストで今週の HTML を作らせない）
- **示唆の鍵は生の URL**（T-24 の `insight_for()` が引く形）
- **図解も同じファイルに置く**（T-49。週次は URL・月次は `No` が鍵。無いのが正常）
- **週次は業界で分けない**（業界版の廃止＝T-52。旧い入れ子のファイルは落ちる）
- **今週のポイントは見出し＋詳細**（鍵は見出しの文。索引で対応づけない＝T-52）
"""

import json

import pytest

from enterprise.entities.diagram import FlowDiagram
from enterprise.entities.json_document import DocumentParseError
from enterprise.entities.narrative import (
    MonthlyNarrativeDocument,
    WeeklyNarrativeDocument,
    case_diagram_key,
    diagram_by_key,
    dump_narrative,
    empty_narrative,
    parse_monthly_narrative,
    parse_narrative,
    parse_weekly_narrative,
    text_by_key,
)
from enterprise.entities.period import parse_period
from enterprise.entities.report_columns import PARAGRAPH_SEPARATOR

WEEKLY_PERIOD = "2026-W31"
MONTHLY_PERIOD = "2026-07"

URL = "https://example.com/news/1"
INDUSTRY = "不動産"

FLOW_DIAGRAM = FlowDiagram(
    type="flow", title="契約業務の自動化", steps=["受領", "AIが下書き", "確認"]
)


HEADINGS = [
    "今週はAIエージェントの実務投入が相次いだ。",
    "契約業務など定型度の高い領域から広がっている。",
    "現場の運用設計が論点になっている。",
]


def weekly(**overrides: object) -> WeeklyNarrativeDocument:
    payload: dict[str, object] = {
        "period": WEEKLY_PERIOD,
        "point_of_week_sentences": list(HEADINGS),
        "point_of_week_details": {HEADINGS[0]: "実務投入の詳細を述べる段落。"},
        "insights": {URL: "自社では契約書レビューの前段から試すのが現実的である。"},
    }
    payload.update(overrides)
    return WeeklyNarrativeDocument.model_validate(payload)


def monthly(**overrides: object) -> MonthlyNarrativeDocument:
    payload: dict[str, object] = {
        "period": MONTHLY_PERIOD,
        "editorial_subtitle": "『導入したか』ではなく『作り直したか』が問われた月",
        "editorial_paragraphs": [
            "俯瞰の段落。",
            "共通する変化の段落。",
            "視点の段落。",
        ],
        "chapter_intros": {"第1章 業務への組み込み": "この章には…を集めた。"},
        "closing_paragraphs": ["今月の総括。", "来月への視点。"],
    }
    payload.update(overrides)
    return MonthlyNarrativeDocument.model_validate(payload)


# --- 連結（レンダラへ渡す形）--------------------------------------------------


def test_the_sentences_join_into_one_point_of_week() -> None:
    """§9.2-2 は「3〜4文」＝段落ではないので、文の間に区切りを挟まない。"""
    point_of_week = weekly().point_of_week

    assert point_of_week is not None
    assert point_of_week.startswith("今週はAIエージェント")
    assert PARAGRAPH_SEPARATOR not in point_of_week


def test_the_paragraphs_join_with_the_shared_separator() -> None:
    """区切りは T-07 の `PARAGRAPH_SEPARATOR`（写しを持たない）。"""
    document = monthly()

    assert document.editorial == PARAGRAPH_SEPARATOR.join(document.editorial_paragraphs)
    assert document.closing == PARAGRAPH_SEPARATOR.join(document.closing_paragraphs)


def test_nothing_generated_reads_as_none_not_an_empty_string() -> None:
    """レンダラは「空なら出さない／落とす」を `None` と空文字の両方で見る。"""
    empty_week = weekly(point_of_week_sentences=[], point_of_week_details={})
    assert empty_week.point_of_week is None
    empty = monthly(editorial_paragraphs=[], closing_paragraphs=[])
    assert empty.editorial is None
    assert empty.closing is None


# --- 空の narrative（採用記事0件の実行）--------------------------------------


def test_an_empty_narrative_can_be_written_for_either_period() -> None:
    """⚠️ 件数の下限をスキーマで課さない（課すと0件の実行がファイルを書けない）。"""
    week = empty_narrative(parse_period(WEEKLY_PERIOD))
    month = empty_narrative(parse_period(MONTHLY_PERIOD))

    assert isinstance(week, WeeklyNarrativeDocument)
    assert isinstance(month, MonthlyNarrativeDocument)
    assert week.point_of_week is None
    assert week.point_of_week_items == []
    assert month.editorial is None


# --- 今週のポイント（見出し＋詳細。T-52 Step 1）-------------------------------


def test_the_headings_and_details_pair_up_by_the_sentence_itself() -> None:
    """⚠️ **鍵は見出しの文**（索引ではない）。

    索引で対応づけると、詳細が1件欠けたときに**別の見出しへずれて付く**——
    文章としては読めてしまうので、内容が入れ替わっても誰も気づけない。
    """
    document = weekly(
        point_of_week_details={HEADINGS[1]: "2番目の詳細。"},
    )

    items = document.point_of_week_items

    assert [item.heading for item in items] == HEADINGS
    assert [item.detail for item in items] == [None, "2番目の詳細。", None]


def test_a_heading_without_a_detail_is_kept() -> None:
    """⚠️ 詳細が無くても見出しは落とさない（総括の1文として意味を持つ）。"""
    document = weekly(point_of_week_details={})

    assert [item.heading for item in document.point_of_week_items] == HEADINGS
    assert document.point_of_week == "".join(HEADINGS)


def test_a_detail_is_not_part_of_the_joined_point_of_week() -> None:
    """HTML が描くのは見出しの連結だけ（詳細は閲覧ページの展開の中）。"""
    document = weekly(point_of_week_details={HEADINGS[0]: "詳細の段落。"})

    assert "詳細の段落。" not in (document.point_of_week or "")


def test_the_old_per_industry_shape_no_longer_loads() -> None:
    """⚠️ T-46 Step 4 の入れ子（`industries`）は `extra="forbid"` で落ちる。

    **成果物なので移行はしない**（`--from render` で前の実行の narrative を
    使い回すと落ちるので、そのときは `--from filter` からやり直す）。
    """
    with pytest.raises(DocumentParseError):
        parse_weekly_narrative(
            json.dumps(
                {
                    "period": WEEKLY_PERIOD,
                    "industries": {INDUSTRY: {"point_of_week_sentences": ["文。"]}},
                },
                ensure_ascii=False,
            )
        )


# --- 読み書き -----------------------------------------------------------------


def test_the_document_round_trips_through_the_file() -> None:
    document = weekly()

    assert parse_weekly_narrative(dump_narrative(document)) == document


def test_the_monthly_document_round_trips_through_the_file() -> None:
    document = monthly()

    assert parse_monthly_narrative(dump_narrative(document)) == document


def test_japanese_is_written_as_is() -> None:
    """人が開いて読める形で書く（§14 の UTF-8・T-06 と同じ扱い）。"""
    assert "今週はAIエージェント" in dump_narrative(weekly())


def test_a_narrative_for_another_period_is_rejected() -> None:
    """⚠️ 先週の生成テキストで今週の HTML を作らせない。"""
    text = dump_narrative(weekly(period="2026-W30"))

    with pytest.raises(DocumentParseError) as error:
        parse_narrative(text, period=parse_period(WEEKLY_PERIOD))

    assert "対象期間が違います" in str(error.value)


def test_a_weekly_narrative_cannot_be_read_as_monthly() -> None:
    """`extra="forbid"` なので、種別を取り違えたファイルはそこで落ちる。"""
    text = dump_narrative(weekly())

    with pytest.raises(DocumentParseError):
        parse_narrative(text, period=parse_period(MONTHLY_PERIOD))


def test_parse_narrative_picks_the_schema_from_the_period() -> None:
    assert (
        parse_narrative(dump_narrative(monthly()), period=parse_period(MONTHLY_PERIOD))
        == monthly()
    )


@pytest.mark.parametrize("period", ["2026-13", "2025-W53", "31週", ""])
def test_an_unusable_period_is_rejected(period: str) -> None:
    """period はファイル名にもなる（T-02）。表記の検証を1箇所に寄せてある。"""
    with pytest.raises(ValueError):
        WeeklyNarrativeDocument(period=period)


def test_blank_text_is_rejected() -> None:
    """空白だけの段落を通すと、描画したときに空の `<p>` になる。"""
    with pytest.raises(ValueError):
        weekly(point_of_week_sentences=["   "])
    with pytest.raises(ValueError):
        weekly(insights={URL: "  "})


def test_an_unknown_field_is_rejected() -> None:
    """列（§8 の確定値）と同じで、勝手なキーを足したファイルは通さない。"""
    with pytest.raises(DocumentParseError):
        parse_weekly_narrative('{"period": "2026-W31", "point_of_week": "x"}')


# --- 宛先 → 文章の対応表 -----------------------------------------------------


def test_the_first_text_for_a_key_wins() -> None:
    """言い直しが本文を上書きしない（T-21 の章の割り当てと同じ向き）。"""
    assert text_by_key([("a", "先"), ("a", "後")]) == {"a": "先"}


def test_blank_pairs_are_dropped_and_whitespace_is_trimmed() -> None:
    assert text_by_key([(" a ", " 文 "), ("", "x"), ("b", "  ")]) == {"a": "文"}


# --- 図解（T-49）-------------------------------------------------------------


def test_a_weekly_narrative_holds_diagrams_keyed_by_the_article_url() -> None:
    """図解の鍵は**示唆と同じ生の URL**（当週シート列22 の値）。"""
    document = weekly(diagrams={URL: FLOW_DIAGRAM})

    assert document.diagrams[URL].type == "flow"
    assert parse_weekly_narrative(dump_narrative(document)) == document


def test_a_monthly_narrative_holds_diagrams_keyed_by_the_case_number() -> None:
    """月次の鍵は列1「No」を文字列にしたもの（URL は非空が保証されない）。"""
    document = monthly(case_diagrams={case_diagram_key(3): FLOW_DIAGRAM})

    assert document.case_diagrams["3"].type == "flow"
    assert parse_monthly_narrative(dump_narrative(document)) == document


def test_a_case_diagram_key_that_is_not_a_number_is_rejected() -> None:
    """⚠️ 章ラベルや URL を鍵にしたファイルを黙って受け取らない。"""
    with pytest.raises(ValueError):
        MonthlyNarrativeDocument(
            period=MONTHLY_PERIOD,
            case_diagrams={"第1章 業務への組み込み": FLOW_DIAGRAM},
        )


def test_no_diagram_is_the_normal_case() -> None:
    """⚠️ **鍵が無い＝図解なし**。空の対応表で読み書きできる。"""
    assert weekly().diagrams == {}
    assert monthly().case_diagrams == {}


def test_a_diagram_outside_the_schema_is_rejected_in_the_file() -> None:
    """narrative ファイル経由でもスキーマ外の図解は入らない。"""
    with pytest.raises(DocumentParseError):
        parse_monthly_narrative(
            json.dumps(
                {
                    "period": MONTHLY_PERIOD,
                    "case_diagrams": {"1": {"type": "timeline", "title": "年表"}},
                },
                ensure_ascii=False,
            )
        )


def test_a_diagram_with_no_target_is_dropped() -> None:
    """`diagram_by_key()`：`None`（図解なし）と空の宛先は落とす。"""
    assert diagram_by_key([(" a ", FLOW_DIAGRAM), ("b", None), ("", FLOW_DIAGRAM)]) == {
        "a": FLOW_DIAGRAM
    }


def test_the_first_diagram_for_a_key_wins() -> None:
    other = FlowDiagram(type="flow", title="別の図", steps=["A", "B", "C"])

    assert diagram_by_key([("a", FLOW_DIAGRAM), ("a", other)]) == {"a": FLOW_DIAGRAM}
