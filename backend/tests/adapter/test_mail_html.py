"""メールHTML 基盤とカテゴリ色マップ（T-23 ／ 設計書 §7.1・§7.2）。

重点:

- **§7.1 の禁止事項が出力に混ざらない**（`<style>` / 外部CSS / flex / grid / JS）。
  `forbidden_constructs()` がその受け入れ条件で、**誤検知しない**ことも固定する
- **外部由来テキストのエスケープ**（記事タイトル・要約は crawl が拾ってきた文字列）
- **`href` のスキーム検査**（`javascript:` をリンクにしない）
- カテゴリ色マップ：**実測3色は逐語**、補完4色は要ブランド確認として別定数
"""

import re

import pytest

from adapter.html import mail_html
from adapter.html.category_colors import (
    CATEGORY_COLORS,
    CONFIRMED_CATEGORY_COLORS,
    FALLBACK_CATEGORY_COLOR,
    SUPPLEMENTED_CATEGORY_COLORS,
    color_of,
    is_brand_confirmed,
    missing_category_ids,
    unconfirmed_category_ids,
)
from adapter.html.mail_html import (
    MailHtmlError,
    assert_mail_safe,
    block,
    cell,
    document,
    element,
    escape,
    escape_style,
    forbidden_constructs,
    link,
    paragraphs,
    row,
    safe_url,
    split_paragraphs,
    styles,
    table,
)
from enterprise.entities.config import INFORMATION_CATEGORY_IDS

HEX_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")


# --- §7.1 の共通制約 ---------------------------------------------------------


def test_the_font_family_is_the_one_written_in_the_design_doc() -> None:
    """§7.1 のフォント指定（逐語）。写しを作らないための唯一の定義。"""
    assert mail_html.FONT_FAMILY == (
        "'Hiragino Kaku Gothic ProN','ヒラギノ角ゴ ProN','Meiryo',Arial,sans-serif"
    )


def test_a_document_declares_utf8_and_japanese() -> None:
    """§7.1「`<meta charset>` 明記」／§14「入出力は UTF-8」。"""
    markup = document(title="週刊", body="", background="#f3f4f6")

    assert markup.startswith("<!DOCTYPE html>")
    assert '<meta charset="utf-8">' in markup
    assert '<html lang="ja">' in markup


def test_a_document_carries_the_font_and_background_inline() -> None:
    markup = document(title="週刊", body="", background="#f3f4f6")

    assert "background-color:#f3f4f6" in markup
    assert mail_html.FONT_FAMILY in markup


def test_a_generated_document_contains_no_forbidden_construct() -> None:
    """T-23 完了条件の lint。部品だけで組んだ文書は制約を満たす。"""
    body = block(
        table([row([cell(escape("見出し"), style=styles("padding:8px"))])]),
        style=styles("background-color:#ffffff"),
    )
    markup = document(title="週刊", body=body, background="#f3f4f6")

    assert forbidden_constructs(markup) == []
    assert assert_mail_safe(markup) is markup


@pytest.mark.parametrize(
    ("name", "markup"),
    [
        ("<style> タグ", "<style>p{color:red}</style>"),
        ("<script> タグ", "<script>alert(1)</script>"),
        ("外部CSS（<link>）", '<link rel="stylesheet" href="a.css">'),
        ("外部CSS（@import）", '<td style="@import url(a.css)">x</td>'),
        ("flex レイアウト", '<td style="display:flex">x</td>'),
        ("flex レイアウト", '<td style="display: inline-flex">x</td>'),
        ("grid レイアウト", '<td style="display:grid">x</td>'),
        ("grid レイアウト", '<td style="display: inline-grid">x</td>'),
        ("javascript: の URL", '<a href="javascript:alert(1)">x</a>'),
        ("イベントハンドラ属性", '<td onclick="f()">x</td>'),
    ],
)
def test_each_forbidden_construct_is_detected(name: str, markup: str) -> None:
    assert name in forbidden_constructs(markup)


def test_assert_mail_safe_refuses_to_pass_a_forbidden_document_through() -> None:
    """壊れたメールHTML を配信するより、書き出す前に落とす。"""
    with pytest.raises(MailHtmlError, match="§7.1"):
        assert_mail_safe('<td style="display:flex">x</td>')


def test_the_lint_does_not_fire_on_legitimate_inline_styles() -> None:
    """`style=` 属性・本文中の `javascript` 等で誤検知しない。

    誤検知するとレンダラが正しい出力を落とすようになるので、
    「見つける」ことと同じだけ大事。
    """
    markup = document(
        title="スタイルの話",
        body=block(
            paragraphs(
                "javascript: から始まる URL は使いません。"
                "display の話や script という語も本文には出ます。"
            ),
            style=styles("display:block", "padding:8px"),
        ),
        background="#ffffff",
    )

    assert forbidden_constructs(markup) == []


# --- エスケープ（外部由来テキスト）------------------------------------------


def test_external_text_is_escaped_before_it_reaches_the_markup() -> None:
    """記事タイトルは crawl が拾った外部テキスト。混入経路を塞ぐ。"""
    title = '<script>alert("x")</script> & "引用" '

    escaped = escape(title)

    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped
    assert "&quot;" in escaped
    assert "&amp;" in escaped


def test_a_card_title_with_markup_does_not_break_the_document() -> None:
    markup = document(
        title="週刊",
        body=block(cell(escape('<img src=x onerror="alert(1)">'))),
        background="#ffffff",
    )

    assert forbidden_constructs(markup) == []
    assert "&lt;img" in markup


def test_escape_turns_none_into_an_empty_string() -> None:
    assert escape(None) == ""


def test_style_values_keep_single_quotes_but_escape_the_rest() -> None:
    """フォント指定の `'` を実体参照にすると生成物が読めなくなる。"""
    assert escape_style("font-family:'Meiryo'") == "font-family:'Meiryo'"
    assert escape_style('a:"b"<c>&d') == "a:&quot;b&quot;&lt;c&gt;&amp;d"


# --- href のスキーム検査 ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/news/1",
        "http://example.com/news/1?a=1&b=2",
        "/relative/path",
    ],
)
def test_usable_urls_are_kept(url: str) -> None:
    assert safe_url(url) == url


def test_safe_url_does_not_escape_because_the_attribute_writer_does() -> None:
    """二重エスケープ（`&` → `&amp;amp;`）を防ぐ分担。"""
    assert safe_url("  https://example.com/a?x=1&y=2  ") == (
        "https://example.com/a?x=1&y=2"
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "ftp://example.com/a",
        "//example.com/a",
        "",
        "   ",
        None,
    ],
)
def test_unusable_urls_are_dropped(url: str | None) -> None:
    assert safe_url(url) is None


def test_a_link_with_an_unusable_url_keeps_the_text_but_loses_the_href() -> None:
    """記事そのものは落とさない（見出しは出す・リンクにはしない）。"""
    markup = link("契約業務をAIで自動化", "javascript:alert(1)")

    assert markup == "<span>契約業務をAIで自動化</span>"
    assert forbidden_constructs(markup) == []


def test_a_link_with_a_usable_url_points_at_it() -> None:
    markup = link("記事を読む", "https://example.com/news/1", style="color:#111827")

    assert 'href="https://example.com/news/1"' in markup
    assert 'target="_blank"' in markup
    assert 'style="color:#111827"' in markup


def test_a_url_with_an_ampersand_is_escaped_in_the_href() -> None:
    markup = link("記事", "https://example.com/a?x=1&y=2")

    assert 'href="https://example.com/a?x=1&amp;y=2"' in markup


# --- table 骨格 ---------------------------------------------------------------


def test_layout_tables_declare_themselves_as_presentation() -> None:
    markup = table([row([cell("x")])])

    assert 'role="presentation"' in markup
    assert 'cellpadding="0"' in markup
    assert 'cellspacing="0"' in markup
    assert 'border="0"' in markup


def test_a_block_is_a_single_cell_table() -> None:
    """箱は div ではなく table で作る（§7.1）。"""
    markup = block("中身", style="background-color:#ffffff")

    assert markup.startswith("<table")
    assert "<div" not in markup
    assert "中身" in markup


def test_a_void_element_refuses_content() -> None:
    with pytest.raises(MailHtmlError):
        element("br", "中身")


def test_attributes_whose_value_is_none_are_omitted() -> None:
    assert element("td", "x", attrs={"width": None}) == "<td>x</td>"


def test_styles_drops_empty_declarations() -> None:
    assert styles("a:1", None, "", "b:2") == "a:1;b:2"


# --- 段落 --------------------------------------------------------------------


def test_paragraphs_split_on_blank_lines_and_escape_each_part() -> None:
    markup = paragraphs("一段落目 <b>\n\n二段落目")

    assert markup == "<p>一段落目 &lt;b&gt;</p><p>二段落目</p>"


def test_paragraphs_accept_the_list_the_xlsx_reader_returns() -> None:
    """T-22 のリーダは PARAGRAPHS 列を `list[str]` で返す。"""
    assert paragraphs(["①事実", "②詳細", "③示唆"]).count("<p") == 3


def test_the_last_paragraph_can_carry_its_own_style() -> None:
    """§10.3「最終段落は示唆トーン」。"""
    markup = paragraphs(["事実", "詳細", "示唆"], last_style="color:#1F4E78")

    assert markup.endswith('<p style="color:#1F4E78">示唆</p>')


def test_blank_paragraphs_are_dropped() -> None:
    assert split_paragraphs("a\n\n\n\n b \n\n") == ["a", "b"]
    assert split_paragraphs(None) == []


# --- カテゴリ色マップ（§7.2）-------------------------------------------------


def test_the_three_measured_colors_are_the_confirmed_values() -> None:
    """実サンプルHTML 実測の確定値（§7.2 の検証記録）。動かさない。"""
    assert dict(CONFIRMED_CATEGORY_COLORS) == {
        "ai_agent_automation": "#0891b2",
        "ai_major_company_model": "#7c3aed",
        "ai_governance_risk": "#dc2626",
    }


def test_the_four_supplemented_colors_are_kept_apart_as_unconfirmed() -> None:
    """要確認事項 #1：サンプル未収載なのでブランド確認が要る4色。"""
    assert dict(SUPPLEMENTED_CATEGORY_COLORS) == {
        "enterprise_ai_case": "#059669",
        "industry_ai_trend": "#d97706",
        "ai_training_org_change": "#db2777",
        "ai_implementation_ops": "#4f46e5",
    }
    assert unconfirmed_category_ids() == tuple(SUPPLEMENTED_CATEGORY_COLORS)
    assert all(not is_brand_confirmed(cid) for cid in unconfirmed_category_ids())
    assert all(is_brand_confirmed(cid) for cid in CONFIRMED_CATEGORY_COLORS)


def test_every_config_category_has_a_color() -> None:
    """config に7カテゴリあるのに色が6つ、という取りこぼしを拾う。"""
    assert missing_category_ids() == ()
    assert set(CATEGORY_COLORS) == set(INFORMATION_CATEGORY_IDS)


def test_the_seven_colors_are_distinct_and_well_formed() -> None:
    """同じ色が2カテゴリに付くと「色分け」（§9.2-4）の意味が無くなる。"""
    assert len(set(CATEGORY_COLORS.values())) == len(CATEGORY_COLORS)
    assert all(HEX_COLOR_RE.match(color) for color in CATEGORY_COLORS.values())
    assert HEX_COLOR_RE.match(FALLBACK_CATEGORY_COLOR)


def test_an_unknown_category_falls_back_instead_of_failing() -> None:
    """色が引けないだけで記事を1件落とす方が損（カードは出す）。"""
    assert color_of("ai_agent_automation") == "#0891b2"
    assert color_of("does_not_exist") == FALLBACK_CATEGORY_COLOR
    assert color_of(None) == FALLBACK_CATEGORY_COLOR
    assert FALLBACK_CATEGORY_COLOR not in set(CATEGORY_COLORS.values())


def test_the_color_map_cannot_be_mutated_by_a_caller() -> None:
    """差し替えは定義側（§7.2）で行う。実行中に書き換わらない。"""
    with pytest.raises(TypeError):
        CATEGORY_COLORS["ai_agent_automation"] = "#000000"  # ty: ignore[invalid-assignment]
