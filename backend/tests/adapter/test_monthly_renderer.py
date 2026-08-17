"""月刊ビリーフ レンダラ（T-25 ／ 設計書 §7.4 ／ 仕様書 §10）。

重点（§7.4 のマッピング表の各行）:

- **`No` 昇順＝章グルーピング順**（並べ替えて救わない。T-22 と同じ理由）
- 章ヘッダ（`第N章` バッジ＋章タイトル＋導入文・下端 `2px solid #4FA8DB`）
- 事例カード（`CASE NN ／ 〈企業〉`／タイトル `<a>` ネイビー／解説の `\\n\\n` を
  `<p>` 分割／出典行）
- 目次（章一覧＋件数＋`全N事例・M章`）とフッタのバッジ（`収録事例 N 件` ほか）
- **生成テキスト**（巻頭言・章導入文・むすび）は渡された分だけ出す。
  `require_editorial_and_closing=true` で空なら落とす
- **§7.1 の禁止事項が出力に混ざらない**／ゴールデンファイル比較
"""

import copy
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from adapter.html import mail_html
from adapter.html.monthly_renderer import (
    ACCENT,
    ACCENT_LIGHT,
    BRAND_TITLE,
    CASE_LABEL_FORMAT,
    CASE_LABEL_SEPARATOR,
    CASE_NUMBER_FORMAT,
    CHAPTER_BADGE_FORMAT,
    CHAPTER_BAND_BACKGROUND,
    CLOSING_HEADING,
    EDITORIAL_HEADING,
    HEADER_EYEBROW,
    INVERSE_TEXT,
    KEY_FIGURE_QUOTE_MAX_FULLWIDTH_CHARS,
    NAVY,
    PAGE_BACKGROUND,
    PANEL_BACKGROUND,
    QUOTE_ELLIPSIS,
    REFERENCED_COLUMNS,
    MonthlyNarrative,
    MonthlyRenderer,
    MonthlyRenderError,
    ensure_ascending_numbers,
    group_into_chapters,
    key_figure_quote,
    organizations_of,
    render_monthly_html,
    split_chapter_label,
)
from adapter.storage.artifact_store import ArtifactStore
from application.usecases.monthly_cases import CHAPTER_LABEL_FORMAT
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.report_columns import (
    MONTHLY_CASE_COLUMNS,
    MONTHLY_CASE_COLUMNS_BY_NAME,
    ORGANIZATION_SEPARATOR,
    PARAGRAPH_SEPARATOR,
)

INITIAL_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)
GOLDEN_PATH = Path(__file__).parent / "golden" / "monthly_belief_2026-07.html"

PERIOD = "2026-07"
REVISION = 3
RUN_ID = "run-0001"


@pytest.fixture
def config() -> IntelligenceConfig:
    raw = json.loads(INITIAL_CONFIG_PATH.read_text(encoding="utf-8"))
    return IntelligenceConfig.model_validate(copy.deepcopy(raw))


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path)


def chapter_label(number: int, title: str) -> str:
    return CHAPTER_LABEL_FORMAT.format(number=number, title=title)


def case(
    *,
    no: int = 1,
    chapter: str = "第1章 業務への組み込み",
    organizations: tuple[str, ...] = ("大手不動産",),
    title: str = "契約業務をAIエージェントで組み替えた",
    url: str = "https://example.com/case/1",
    source: str = "ITmedia（2026-07-08）",
    month: str = PERIOD,
    paragraphs: tuple[str, ...] = (
        "契約業務の一部をAIエージェントへ移した。",
        "ひな型のある書類から着手し、確認は担当者が担う形にした。",
        "自社では書式の揃った領域から試すのが早い。",
    ),
) -> dict[str, Any]:
    """当月シートの1行（列名 → 値。T-22 のリーダが返す形）。"""
    values: dict[str, Any] = {
        "No": no,
        "トピック(章)": chapter,
        "企業・組織": list(organizations),
        "タイトル": title,
        "URL": url,
        "出典": source,
        "掲載月": month,
        "解説": list(paragraphs),
    }
    missing = [c.name for c in MONTHLY_CASE_COLUMNS if c.name not in values]
    assert not missing, f"テスト側の行が8列を満たしていません: {missing}"
    return values


def narrative(**overrides: Any) -> MonthlyNarrative:
    defaults: dict[str, Any] = {
        "editorial_subtitle": "『導入したか』ではなく『作り直したか』が問われた月",
        "editorial": "第1段落。\n\n第2段落。\n\n第3段落。",
        "closing": "今月の総括。\n\n来月の視点。",
    }
    defaults.update(overrides)
    return MonthlyNarrative(**defaults)


def render(
    cases: list[dict[str, Any]],
    config: IntelligenceConfig,
    text: MonthlyNarrative | None = None,
) -> str:
    return render_monthly_html(
        period=PERIOD, cases=cases, config=config, narrative=text or narrative()
    )


# --- 列定義との接続 -----------------------------------------------------------


def test_every_referenced_column_exists_in_the_shared_definition() -> None:
    assert all(name in MONTHLY_CASE_COLUMNS_BY_NAME for name in REFERENCED_COLUMNS)


def test_the_chapter_label_is_parsed_from_the_format_used_to_build_it() -> None:
    """体裁の正は T-21 の `CHAPTER_LABEL_FORMAT`（写しを持たない）。"""
    assert split_chapter_label(chapter_label(3, "規程と体制")) == (3, "規程と体制")


def test_a_chapter_label_that_does_not_match_still_renders(
    config: IntelligenceConfig,
) -> None:
    """手編集した xlsx でも章ごと落とさない（バッジだけ出さない）。"""
    assert split_chapter_label("章タイトルだけ") == (None, "章タイトルだけ")

    markup = render([case(chapter="章タイトルだけ")], config)

    assert "章タイトルだけ" in markup


# --- 並び順と章のグルーピング（§8.2・§10.3）--------------------------------


def test_cases_out_of_order_are_refused_instead_of_being_sorted(
    config: IntelligenceConfig,
) -> None:
    """`No` の順序は章の束ね方そのもの。黙って直すと構成が変わる（T-22 と同じ）。"""
    out_of_order = [case(no=2), case(no=1)]

    with pytest.raises(MonthlyRenderError, match="昇順"):
        render(out_of_order, config)


def test_a_case_without_a_number_is_refused(config: IntelligenceConfig) -> None:
    broken = case()
    broken["No"] = None

    with pytest.raises(MonthlyRenderError, match="No"):
        render([broken], config)


def test_ascending_numbers_pass_the_check() -> None:
    ensure_ascending_numbers([case(no=1), case(no=2), case(no=7)])


def test_cases_are_grouped_by_chapter_in_first_appearance_order() -> None:
    chapters = group_into_chapters(
        [
            case(no=1, chapter=chapter_label(1, "業務への組み込み")),
            case(no=2, chapter=chapter_label(1, "業務への組み込み")),
            case(no=3, chapter=chapter_label(2, "規程と体制")),
        ]
    )

    assert [c.title for c in chapters] == ["業務への組み込み", "規程と体制"]
    assert [len(c.cases) for c in chapters] == [2, 1]
    assert [c.badge for c in chapters] == ["第1章", "第2章"]


def test_a_chapter_that_reappears_later_does_not_get_a_second_header(
    config: IntelligenceConfig,
) -> None:
    """目次の件数と本編の見出し数が食い違わないようにする。"""
    chapters = group_into_chapters(
        [
            case(no=1, chapter=chapter_label(1, "A")),
            case(no=2, chapter=chapter_label(2, "B")),
            case(no=3, chapter=chapter_label(1, "A")),
        ]
    )

    assert [c.title for c in chapters] == ["A", "B"]
    assert [len(c.cases) for c in chapters] == [2, 1]


# --- ヘッダ（§10.2-1）--------------------------------------------------------


def test_the_header_carries_the_brand_issue_and_target_period(
    config: IntelligenceConfig,
) -> None:
    markup = render([case()], config)

    assert HEADER_EYEBROW in markup
    assert BRAND_TITLE in markup
    assert "2026年7月号" in markup
    assert "対象期間：2026年7月1日 〜 7月31日" in markup


def test_the_last_day_of_the_month_comes_from_the_calendar(
    config: IntelligenceConfig,
) -> None:
    markup = render_monthly_html(
        period="2026-02",
        cases=[case(month="2026-02")],
        config=config,
        narrative=narrative(),
    )

    assert "対象期間：2026年2月1日 〜 2月28日" in markup


def test_the_outer_frame_follows_the_monthly_palette(
    config: IntelligenceConfig,
) -> None:
    markup = render([case()], config)

    assert "background-color:#EEF2F6" in markup
    assert "width:680px" in markup
    assert "max-width:100%" in markup
    assert "border-radius:8px" in markup
    assert "background-color:#1F4E78" in markup


# --- 目次（§10.2-3）----------------------------------------------------------


def test_the_contents_lists_every_chapter_with_its_case_count(
    config: IntelligenceConfig,
) -> None:
    markup = render(
        [
            case(no=1, chapter=chapter_label(1, "業務への組み込み")),
            case(no=2, chapter=chapter_label(1, "業務への組み込み")),
            case(no=3, chapter=chapter_label(2, "規程と体制")),
        ],
        config,
    )

    assert "業務への組み込み" in markup
    assert "規程と体制" in markup
    assert "2件" in markup
    assert "1件" in markup
    assert "全3事例・2章" in markup


# --- 本編（§10.2-4・§10.3）--------------------------------------------------


def test_a_chapter_header_is_underlined_with_the_accent(
    config: IntelligenceConfig,
) -> None:
    markup = render([case()], config)

    assert "border-bottom:2px solid #4FA8DB" in markup


def test_the_chapter_introduction_is_rendered_when_it_is_given(
    config: IntelligenceConfig,
) -> None:
    label = chapter_label(1, "業務への組み込み")

    markup = render(
        [case(chapter=label)],
        config,
        narrative(chapter_intros={label: "この章では業務そのものの作り替えを見る。"}),
    )

    assert "この章では業務そのものの作り替えを見る。" in markup


def test_a_case_is_labelled_with_a_zero_padded_number_and_its_organizations(
    config: IntelligenceConfig,
) -> None:
    """T-48 Step 2 でバッジと企業名に割れた（区切りの `／` は描かない）。"""
    markup = render([case(no=1, organizations=("大手不動産", "AIベンダ"))], config)

    joined = ORGANIZATION_SEPARATOR.join(["大手不動産", "AIベンダ"])
    assert "CASE 01" in markup
    assert joined in markup
    assert f"CASE 01{CASE_LABEL_SEPARATOR}" not in markup


def test_the_case_badge_and_organizations_are_derived_from_the_fixed_wording() -> None:
    """⚠️ **確定文言の正は `CASE_LABEL_FORMAT` 1つ**（§10.2-4）。

    バッジ（前半）と区切りを別に持っているので、確定文言を直したときに片方だけ
    古いまま食い違わないよう、組み直して一致を固定する（実装側も import 時に
    同じ検査をしている）。
    """
    composed = CASE_NUMBER_FORMAT + CASE_LABEL_SEPARATOR + "{organizations}"

    assert composed == CASE_LABEL_FORMAT
    assert CASE_NUMBER_FORMAT.format(no=7) == "CASE 07"


def test_the_case_number_comes_from_the_no_column(
    config: IntelligenceConfig,
) -> None:
    """表と HTML で番号が食い違わないよう、レンダラ側で数え直さない。"""
    markup = render([case(no=7), case(no=12)], config)

    assert "CASE 07" in markup
    assert "CASE 12" in markup


def test_the_case_title_links_to_the_url_in_navy(
    config: IntelligenceConfig,
) -> None:
    markup = render([case(url="https://example.com/case/42")], config)

    assert 'href="https://example.com/case/42"' in markup
    assert "color:#1F4E78" in markup


def test_the_commentary_paragraphs_become_separate_p_elements(
    config: IntelligenceConfig,
) -> None:
    """§10.3「`解説` の `\\n\\n` 段落を `<p>` に分割」。"""
    markup = render([case(paragraphs=("①事実。", "②詳細。", "③示唆。"))], config)

    assert '<p style="margin:0;font-size:13px' in markup
    for text in ("①事実。", "②詳細。", "③示唆。"):
        assert f">{text}</p>" in markup


def test_the_commentary_also_accepts_a_joined_string(
    config: IntelligenceConfig,
) -> None:
    """リーダは `list[str]` を返すが、`\\n\\n` 連結でも同じ結果になる。"""
    joined = PARAGRAPH_SEPARATOR.join(["①事実。", "②詳細。", "③示唆。"])
    record = case()
    record["解説"] = joined

    markup = render([record], config)

    assert ">③示唆。</p>" in markup


def test_the_last_commentary_paragraph_is_set_apart(
    config: IntelligenceConfig,
) -> None:
    """§10.3「最終段落は示唆／持ち帰りトーン」。この層は見え方だけ分ける。"""
    markup = render([case(paragraphs=("事実。", "詳細。", "示唆。"))], config)

    assert "border-left:3px solid #9FD4F2" in markup


def test_the_source_line_sits_under_a_rule(config: IntelligenceConfig) -> None:
    markup = render([case(source="日経クロステック（2026-07-10）")], config)

    assert "日経クロステック（2026-07-10）" in markup
    assert "border-top:1px solid #DCE7F0" in markup


def test_multiple_organizations_are_joined_with_the_shared_separator() -> None:
    assert organizations_of({"企業・組織": ["A", "B"]}) == f"A{ORGANIZATION_SEPARATOR}B"
    assert organizations_of({"企業・組織": "A・B"}) == "A・B"
    assert organizations_of({}) == ""


# --- 視覚強化（T-48 Step 2。装飾のみ・本文は不変）-----------------------------


def test_the_chapter_number_badge_is_a_filled_navy_chip(
    config: IntelligenceConfig,
) -> None:
    markup = render([case(chapter=chapter_label(1, "基幹業務の作り替え"))], config)

    assert f"background-color:{NAVY};border-radius:4px" in markup
    assert f"color:{INVERSE_TEXT}" in markup
    assert CHAPTER_BADGE_FORMAT.format(number=1) in markup


def test_the_chapter_header_carries_a_color_band(config: IntelligenceConfig) -> None:
    """章色帯（左端 `6px` の帯＋地色）。⚠️ **確定値の下端罫は残す**。"""
    markup = render([case()], config)

    assert f"border-left:6px solid {ACCENT}" in markup
    assert f"background-color:{CHAPTER_BAND_BACKGROUND}" in markup
    assert f"border-bottom:2px solid {ACCENT}" in markup  # §10.2-4 の確定値


def test_the_chapter_band_does_not_invent_a_new_colour() -> None:
    """帯の地色は確定パレット内から選ぶ（新しい色を作らない）。"""
    assert CHAPTER_BAND_BACKGROUND == PAGE_BACKGROUND
    assert CHAPTER_BAND_BACKGROUND != PANEL_BACKGROUND


def test_a_chapter_without_a_badge_still_renders_its_title(
    config: IntelligenceConfig,
) -> None:
    """書式に合わない章ラベルは**バッジ無しでタイトルだけ**（T-25 の方針のまま）。"""
    markup = render([case(chapter="章の書式に合わないラベル")], config)

    assert "章の書式に合わないラベル" in markup
    assert f"background-color:{NAVY};border-radius:4px" not in markup


def test_the_case_number_is_shown_as_a_light_blue_badge(
    config: IntelligenceConfig,
) -> None:
    """事例バッジは水色地（章のネイビー地と地色を分けて階層を出す）。

    ⚠️ 地色だけを探さない（§10.2-1 の号バッジも同じ水色）。`CASE 04` を囲む
    チップそのものが出ていることを見る。
    """
    markup = render([case(no=4)], config)

    chip = re.compile(
        re.escape(f'style="background-color:{ACCENT_LIGHT};border-radius:3px">')
        + r"<tr><td[^>]*><p[^>]*>CASE 04</p>"
    )
    assert chip.search(markup) is not None


# --- キーとなる数値の引用ボックス（T-48 Step 2）-------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        "問い合わせ対応の工数を月120時間削減した。",
        "作業時間を40％短縮した。",
        "残業を3割減らせた。",
        "外注費を年1,200万円抑えた。",
        "処理量は5倍になった。",
        "立ち上げは3か月で済んだ。",
        "調達コストは50万ドル下がった。",
        "応答時間は30秒まで縮んだ。",
        "工数は月20人時ぶん空いた。",
    ],
)
def test_a_sentence_with_a_key_figure_is_quoted(sentence: str) -> None:
    assert key_figure_quote(f"導入した。{sentence}確認は担当者が担う。") == sentence


@pytest.mark.parametrize(
    "commentary",
    [
        "方針を見直した。手順ごと組み替えた。",
        "3件の事例を集めた。担当は5名だった。",  # 数え上げは対象にしない
        "",
        None,
    ],
)
def test_a_commentary_without_a_key_figure_is_not_quoted(commentary: object) -> None:
    assert key_figure_quote(commentary) is None


def test_the_first_key_figure_in_the_commentary_wins() -> None:
    text = "工数を月120時間削減した。\n\n外注費も年800万円下がった。"

    assert key_figure_quote(text) == "工数を月120時間削減した。"


def test_a_long_quote_is_cut_at_the_limit() -> None:
    long_sentence = "あ" * KEY_FIGURE_QUOTE_MAX_FULLWIDTH_CHARS + "で30％削減した。"

    quote = key_figure_quote(long_sentence)

    assert quote is not None
    assert quote.endswith(QUOTE_ELLIPSIS)


def test_a_sentence_without_a_trailing_period_is_still_quoted() -> None:
    assert key_figure_quote("工数を120時間削減") == "工数を120時間削減"


def test_the_quote_box_appears_before_the_body(config: IntelligenceConfig) -> None:
    quoted = "問い合わせ対応の工数を月120時間削減した。"
    markup = render(
        [
            case(
                paragraphs=(
                    "契約業務をAIエージェントへ移した。",
                    quoted,
                    "自社では書式の揃った領域から試すのが早い。",
                )
            )
        ],
        config,
    )

    box = f"background-color:{PANEL_BACKGROUND};border-left:4px solid {ACCENT}"
    assert box in markup
    assert markup.index(box) < markup.index("契約業務をAIエージェントへ移した。")


def test_quoting_does_not_remove_the_sentence_from_the_body(
    config: IntelligenceConfig,
) -> None:
    """⚠️ **本文は不変**（引用は抜き書きで、段落は全段そのまま出る）。"""
    quoted = "工数を月120時間削減した。"
    paragraphs = ("前置き。", quoted, "むすび。")

    markup = render([case(paragraphs=paragraphs)], config)

    assert markup.count(quoted) == 2  # 引用ボックス＋本文
    for paragraph in paragraphs:
        assert f">{paragraph}</p>" in markup


def test_a_case_without_a_key_figure_has_no_empty_box(
    config: IntelligenceConfig,
) -> None:
    markup = render([case(paragraphs=("数値の無い解説。",))], config)

    assert f"border-left:4px solid {ACCENT}" not in markup


# --- 生成テキスト（§10.2-2・§10.2-5）---------------------------------------


def test_the_editorial_and_closing_are_rendered_when_they_are_given(
    config: IntelligenceConfig,
) -> None:
    markup = render([case()], config)

    assert EDITORIAL_HEADING in markup
    assert CLOSING_HEADING in markup
    assert "『導入したか』ではなく『作り直したか』が問われた月" in markup
    assert ">第1段落。</p>" in markup
    assert ">来月の視点。</p>" in markup
    # ⚠️ 地色だけを数えない（T-48 Step 2 の引用ボックスも `#F7FAFC` を使う）。
    # §10.2-2・§10.2-5 の囲みは**枠線つき**（`border:1px solid #DCE7F0`）なので、
    # そこまで含めた署名で数える。
    assert markup.count("background-color:#F7FAFC;border:1px solid #DCE7F0") == 2


def test_rendering_fails_when_the_required_editorial_is_missing(
    config: IntelligenceConfig,
) -> None:
    assert config.tunable_thresholds.monthly.require_editorial_and_closing is True

    with pytest.raises(MonthlyRenderError, match="巻頭言"):
        render([case()], config, narrative(editorial=None))


def test_rendering_fails_when_the_required_closing_is_missing(
    config: IntelligenceConfig,
) -> None:
    with pytest.raises(MonthlyRenderError, match="むすび"):
        render([case()], config, narrative(closing="   "))


def test_both_sections_may_be_omitted_when_config_does_not_require_them(
    config: IntelligenceConfig,
) -> None:
    config.tunable_thresholds.monthly.require_editorial_and_closing = False

    markup = render_monthly_html(
        period=PERIOD, cases=[case()], config=config, narrative=None
    )

    assert EDITORIAL_HEADING not in markup
    assert CLOSING_HEADING not in markup
    assert "CASE 01" in markup


# --- フッタ（§10.2-6）--------------------------------------------------------


def test_the_footer_carries_the_count_badges(config: IntelligenceConfig) -> None:
    markup = render(
        [
            case(no=1, chapter=chapter_label(1, "A")),
            case(no=2, chapter=chapter_label(2, "B")),
            case(no=3, chapter=chapter_label(2, "B")),
        ],
        config,
    )

    assert "収録事例 3 件" in markup
    assert "トピック 2 章" in markup


def test_the_publication_date_defaults_to_the_last_day_of_the_month(
    config: IntelligenceConfig,
) -> None:
    """⚠️ `date.today()` を既定にしない（§14 再現性）。"""
    markup = render([case()], config)

    assert "発行日：2026年7月31日" in markup


def test_the_publication_date_can_be_given_explicitly(
    config: IntelligenceConfig,
) -> None:
    markup = render_monthly_html(
        period=PERIOD,
        cases=[case()],
        config=config,
        narrative=narrative(),
        issued_on=date(2026, 8, 1),
    )

    assert "発行日：2026年8月1日" in markup


# --- §7.1 の制約とエスケープ --------------------------------------------------


def test_the_rendered_document_contains_no_forbidden_construct(
    config: IntelligenceConfig,
) -> None:
    markup = render([case(no=1), case(no=2, chapter=chapter_label(2, "B"))], config)

    assert mail_html.forbidden_constructs(markup) == []
    assert '<meta charset="utf-8">' in markup


def test_markup_inside_a_case_is_escaped(config: IntelligenceConfig) -> None:
    markup = render(
        [
            case(
                title='<script>alert("x")</script>',
                organizations=("<b>企業</b>",),
                source="媒体 & 通信",
                paragraphs=("<i>事実</i>", "詳細", "示唆"),
            )
        ],
        config,
    )

    assert mail_html.forbidden_constructs(markup) == []
    assert "&lt;script&gt;" in markup
    assert "&lt;b&gt;企業&lt;/b&gt;" in markup
    assert "&amp;" in markup


def test_a_case_whose_url_is_not_http_keeps_its_card_without_a_link(
    config: IntelligenceConfig,
) -> None:
    markup = render([case(title="怪しいURL", url="javascript:alert(1)")], config)

    assert "怪しいURL" in markup
    assert mail_html.forbidden_constructs(markup) == []
    assert "javascript:" not in markup


# --- period と書き出し --------------------------------------------------------


def test_a_weekly_period_is_refused(config: IntelligenceConfig) -> None:
    with pytest.raises(MonthlyRenderError, match="月次"):
        render_monthly_html(
            period="2026-W31", cases=[case()], config=config, narrative=narrative()
        )


def test_a_month_that_does_not_exist_is_refused(config: IntelligenceConfig) -> None:
    with pytest.raises(MonthlyRenderError):
        render_monthly_html(
            period="2026-13", cases=[case()], config=config, narrative=narrative()
        )


def test_the_output_file_name_follows_the_canonical_resolution(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    result = MonthlyRenderer(store).render(
        period=PERIOD,
        cases=[case(no=1), case(no=2, chapter=chapter_label(2, "B"))],
        config=config,
        narrative=narrative(),
        revision=REVISION,
        run_id=RUN_ID,
    )

    assert result.path == store.monthly_html_path(PERIOD)
    assert result.path.name == "monthly_belief_2026-07.html"
    assert result.path.read_text(encoding="utf-8") == result.markup
    assert (result.cases, result.chapters) == (2, 2)
    assert result.archived is None


def test_the_previous_version_is_archived_before_the_overwrite(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """設計判断B。⚠️ 退避が後だと、退避されるのが新しい内容になる。"""
    renderer = MonthlyRenderer(store)
    first = renderer.render(
        period=PERIOD,
        cases=[case(title="1回目")],
        config=config,
        narrative=narrative(),
        revision=REVISION,
        run_id=RUN_ID,
    )
    second = renderer.render(
        period=PERIOD,
        cases=[case(title="2回目")],
        config=config,
        narrative=narrative(),
        revision=REVISION,
        run_id="run-0002",
    )

    assert second.archived is not None
    assert second.archived.read_text(encoding="utf-8") == first.markup
    assert "2回目" in second.path.read_text(encoding="utf-8")


def test_rerunning_the_same_period_does_not_create_a_second_file(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    renderer = MonthlyRenderer(store)
    for run in ("run-0001", "run-0002"):
        renderer.render(
            period=PERIOD,
            cases=[case()],
            config=config,
            narrative=narrative(),
            revision=REVISION,
            run_id=run,
        )

    assert len(list(store.root.glob("*.html"))) == 1


# --- 再現性とゴールデンファイル ----------------------------------------------


def test_the_same_input_always_produces_the_same_output(
    config: IntelligenceConfig,
) -> None:
    """§14 再現性。render は AI を呼ばない（§1.1）ので出力は入力だけで決まる。"""
    cases = [case(no=1), case(no=2, chapter=chapter_label(2, "B"))]

    assert render(cases, config) == render(cases, config)


def golden_cases() -> list[dict[str, Any]]:
    return [
        case(
            no=1,
            chapter=chapter_label(1, "基幹業務の作り替え"),
            organizations=("大手不動産",),
            title="契約業務をAIエージェントで組み替えた",
            url="https://example.com/case/1",
            source="ITmedia（2026-07-08）",
            paragraphs=(
                "契約書のドラフト作成をAIエージェントへ移した。",
                # ⚠️ **キーとなる数値を1件だけ入れてある**（T-48 Step 2 の引用
                # ボックスがゴールデンを実際に通るように）。
                "ひな型のある書類から着手し、差分の確認は担当者が担う形にした。"
                "ドラフト作成の工数は月120時間ぶん減った。",
                "自社では書式の揃っている領域から試すのが早い。",
            ),
        ),
        case(
            no=2,
            chapter=chapter_label(1, "基幹業務の作り替え"),
            organizations=("地方銀行", "システム会社"),
            title="融資審査の一次確認を機械が担う体制へ",
            url="https://example.com/case/2",
            source="日経クロステック（2026-07-14）",
            paragraphs=(
                "融資審査の一次確認を自動化した。",
                "審査基準そのものを文書へ書き下し、判定の根拠を残す形にした。",
                "基準を言語化できているかが、置き換えの前提になる。",
            ),
        ),
        case(
            no=3,
            chapter=chapter_label(2, "規程と体制"),
            organizations=("製造大手",),
            title="AI利用規程を全社で刷新",
            url="https://example.com/case/3",
            source="プレスリリース（2026-07-21）",
            paragraphs=(
                "AI利用に関する社内規程を刷新した。",
                "持ち出してよい情報の線引きを部門ごとに定め、"
                "例外の申請経路も併せて用意した。",
                "規程は「禁止事項の列挙」より「判断の手順」の形が続く。",
            ),
        ),
    ]


def golden_narrative() -> MonthlyNarrative:
    return MonthlyNarrative(
        editorial_subtitle="『導入したか』ではなく『作り直したか』が問われ始めた月",
        editorial=(
            "今月の事例に共通していたのは、AIを既存の手順へ足すのではなく、"
            "手順そのものを組み替えている点だった。\n\n"
            "契約と審査という、判断基準を文書化しやすい業務が先に動いている。"
            "基準を言葉にできているかが、そのまま着手できるかの分かれ目になっている。\n\n"
            "一方で規程の整備も同時に進んでおり、"
            "「やってよいことを決める」作業が事例の前提として現れ始めた。"
        ),
        chapter_intros={
            chapter_label(1, "基幹業務の作り替え"): (
                "業務の一部を置き換えるのではなく、手順ごと組み替えた事例を集めた。"
            ),
            chapter_label(2, "規程と体制"): (
                "取り組みを支える側——規程と申請の経路をどう置いたかを見る。"
            ),
        },
        closing=(
            "今月は、判断基準を文書化できている業務から順に置き換えが進んだ。\n\n"
            "来月は、基準が暗黙のままの領域をどう扱うかが論点になりそうだ。"
        ),
    )


def test_the_rendered_html_matches_the_golden_file(
    config: IntelligenceConfig,
) -> None:
    """体裁の回帰を止める。

    ⚠️ **意図した体裁変更のときだけ** `UPDATE_GOLDEN=1 uv run pytest` で
    作り直し、生成物の差分をレビューすること。
    """
    markup = render_monthly_html(
        period=PERIOD,
        cases=golden_cases(),
        config=config,
        narrative=golden_narrative(),
    )

    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(markup, encoding="utf-8")

    assert markup == GOLDEN_PATH.read_text(encoding="utf-8")


def test_the_golden_file_itself_satisfies_the_mail_html_constraints() -> None:
    assert mail_html.forbidden_constructs(GOLDEN_PATH.read_text(encoding="utf-8")) == []
