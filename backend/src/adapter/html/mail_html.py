"""メールHTML の共通部品（設計書 §7.1 ／ 仕様書 §9.1・§10.1・§13.4 ／ T-23）。

週刊（T-24）・月刊（T-25）のどちらのレンダラも、HTML の文字列を直接組み立てず
このモジュールの関数だけを通す。**メールクライアントで壊れない書き方を1箇所に
閉じ込める**のが目的で、守るべき制約は §7.1 の3行:

- **table レイアウト＋inline style のみ**。`<style>` タグ・外部CSS・flex・grid・
  JS は**禁止**
- 文字コードは UTF-8。`<meta charset="utf-8">` を明記（§14）
- フォントは `FONT_FAMILY`（§7.1 の逐語）

---

**⚠️ エスケープはこの層の責務**

カードに載る記事タイトル・一言要約・ソース名・解説は、**crawl が外部サイトから
拾ってきたテキスト**（T-16）で、途中の工程は誰も HTML として無害化していない。
`<` を含むタイトル1件で以降のレイアウトが崩れ、`"` を含む値1件で属性が閉じる。
したがって:

- **文字列を HTML へ入れる経路は `escape()` / `link()` / `paragraphs()` だけ**にし、
  f-string で `<td>{value}</td>` と書かない
- **href は `safe_url()` を通す。`http` / `https` 以外は リンクにしない**
  （§7.1 は「href は URL 列をそのまま使用」だが、`javascript:` をそのまま
  `href` へ置くのは混入経路そのもの。スキームだけは検査する）

`escape()` を通し忘れた経路は `forbidden_constructs()` では拾えない（`<b>` は
禁止構文ではない）ので、**テストで固定してある**のは「エスケープした結果が出る」
ことそのもの。

---

**style 属性の値だけ `'` をエスケープしない**

`FONT_FAMILY` は `'Hiragino Kaku Gothic ProN'` のように単引用符を含む。属性は
`"` で囲むので `'` を実体参照にする必要が無く、そのまま出す方が生成物が読める
（ゴールデンファイルの差分をレビューできる形に保つため）。`"` `<` `>` `&` は
style 値でもエスケープする。
"""

import html
import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# --- 共通定数（設計書 §7.1・逐語）-------------------------------------------

FONT_FAMILY = (
    "'Hiragino Kaku Gothic ProN','ヒラギノ角ゴ ProN','Meiryo',Arial,sans-serif"
)
"""§7.1 のフォント指定。**この文字列を各レンダラへ写さないこと。**"""

DOCTYPE = "<!DOCTYPE html>"
LANGUAGE = "ja"
CHARSET = "utf-8"

CHARSET_META = f'<meta charset="{CHARSET}">'
"""§7.1「`<meta charset>` 明記」（§14 の UTF-8 要件）。"""

VIEWPORT_META = '<meta name="viewport" content="width=device-width,initial-scale=1">'

# `href` に置いてよいスキーム。相対URL・スキーム無しも許す（`//` 始まりは除く）。
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# table を「レイアウト目的」と宣言する属性。読み上げソフトが表として読まないように
# するのと、メールクライアントの既定余白を消すため。
TABLE_LAYOUT_ATTRS: Mapping[str, str] = {
    "role": "presentation",
    "cellpadding": "0",
    "cellspacing": "0",
    "border": "0",
}

# 段落の区切り（月刊「解説」の `\n\n`。仕様書 §10.3）。空行が2つ以上でも1つ扱い。
_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")

_VOID_ELEMENTS = frozenset({"br", "hr", "img", "meta", "link"})


class MailHtmlError(Exception):
    """メールHTML の制約に反する生成物・使い方。"""


# --- エスケープ ---------------------------------------------------------------


def escape(value: object) -> str:
    """テキストを HTML へ入れられる形にする（`None` は空文字）。

    `quote=True` なので属性値としても安全。**外部由来のテキストは必ずここを通す。**
    """
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def escape_style(value: str) -> str:
    """style 属性の値をエスケープする（`'` は残す。モジュール docstring）。"""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def safe_url(url: object) -> str | None:
    """`href` に置いてよい URL だけを返す。

    ⚠️ **エスケープはしない**（属性へ入れる `attributes()` が行う）。ここで
    エスケープすると `?a=1&b=2` の `&` が二重に実体参照化される。

    Args:
        url: 中間xlsx の URL 列の値

    Returns:
        前後の空白を落とした URL。空・`http`/`https` 以外のスキームなら `None`
    """
    text = "" if url is None else str(url).strip()
    if not text:
        return None
    scheme = urlsplit(text).scheme.lower()
    if scheme and scheme not in ALLOWED_URL_SCHEMES:
        logger.warning("href に置けないスキームの URL を落としました: %r", text)
        return None
    if not scheme and text.startswith("//"):
        # スキーム相対（`//example.com`）はメールクライアントで解決先が定まらない。
        logger.warning("スキーム相対の URL を落としました: %r", text)
        return None
    return text


# --- 要素の組み立て -----------------------------------------------------------


def styles(*declarations: str | None) -> str:
    """CSS 宣言を `;` で連結する（`None` と空文字は捨てる）。

    ⚠️ 関数名が `style` でないのは、各部品が `style=` という引数を持つため
    （同じ名前だと関数本体から呼べなくなる）。
    """
    return ";".join(item for item in declarations if item)


def attributes(attrs: Mapping[str, str | None] | None) -> str:
    """属性列を組み立てる（値が `None` の属性は出さない）。"""
    if not attrs:
        return ""
    return "".join(
        f' {name}="{escape(value)}"'
        for name, value in attrs.items()
        if value is not None
    )


def element(
    name: str,
    content: str = "",
    *,
    style: str | None = None,
    attrs: Mapping[str, str | None] | None = None,
) -> str:
    """1要素を組み立てる。

    ⚠️ `content` は**組み立て済みの HTML** として扱う（エスケープしない）。
    テキストを入れるときは呼び出し側で `escape()` を通すこと。

    Raises:
        MailHtmlError: 空要素（`<br>` 等）に内容を渡した場合
    """
    style_attr = f' style="{escape_style(style)}"' if style else ""
    if name in _VOID_ELEMENTS:
        if content:
            raise MailHtmlError(f"<{name}> は内容を持てません")
        return f"<{name}{attributes(attrs)}{style_attr}>"
    return f"<{name}{attributes(attrs)}{style_attr}>{content}</{name}>"


def table(
    rows: Iterable[str],
    *,
    style: str | None = None,
    width: str | None = "100%",
    attrs: Mapping[str, str | None] | None = None,
) -> str:
    """レイアウト用の table（§7.1「table レイアウト」）。

    行は改行で連ねる。`<tr>` の**間**の空白は表の描画に影響しない一方、生成物
    （ゴールデンファイル）の差分が行単位で読めるようになるため。
    """
    merged: dict[str, str | None] = {**TABLE_LAYOUT_ATTRS, "width": width}
    if attrs:
        merged.update(attrs)
    return element("table", "\n".join(rows), style=style, attrs=merged)


def row(
    cells: Iterable[str],
    *,
    style: str | None = None,
) -> str:
    """`<tr>`。"""
    return element("tr", "".join(cells), style=style)


def cell(
    content: str = "",
    *,
    style: str | None = None,
    attrs: Mapping[str, str | None] | None = None,
) -> str:
    """`<td>`。"""
    return element("td", content, style=style, attrs=attrs)


def spacer_row(height: str) -> str:
    """縦の余白だけを作る行。

    `margin` は `<td>` では効かないメールクライアントが多いので、余白は
    padding か「高さだけを持つ行」で作る。空の `<td>` は潰れることがあるため
    `&nbsp;` を1つ入れ、`font-size:0` で見えなくする（メールHTML の定石）。
    """
    return row(
        [
            cell(
                "&nbsp;",
                style=styles(
                    f"height:{height}", f"line-height:{height}", "font-size:0"
                ),
            )
        ]
    )


def spacer_cell(width: str) -> str:
    """横の余白だけを作るセル（バッジを並べるときの間隔）。"""
    return cell(
        "&nbsp;", style=styles(f"width:{width}", "font-size:0", "line-height:0")
    )


def block(
    content: str,
    *,
    style: str | None = None,
    cell_style: str | None = None,
    width: str | None = "100%",
) -> str:
    """1セルだけの table。「余白と背景を持つ箱」を table で作る常套手段。

    `<div>` でも表示はできるが、余白の解釈がメールクライアントごとに割れるため
    箱は table に寄せる（§7.1「table レイアウト」）。

    Args:
        content: 箱の中身（組み立て済み HTML）
        style: table 側の style（背景・罫・角丸）
        cell_style: セル側の style（**内側の余白はここ**。table の padding は
            メールクライアントによって効かない）
        width: table の width 属性
    """
    return table([row([cell(content, style=cell_style)])], style=style, width=width)


def link(
    text: object,
    url: object,
    *,
    style: str | None = None,
) -> str:
    """リンク。**URL が使えない場合はテキストだけを返す**（記事は落とさない）。"""
    href = safe_url(url)
    label = escape(text)
    if href is None:
        return element("span", label, style=style)
    return element("a", label, style=style, attrs={"href": href, "target": "_blank"})


def truncate_fullwidth(text: object, *, limit: int, ellipsis: str = "…") -> str:
    """テキストを「全角 `limit` 字ぶん」で切る（T-48）。

    ⚠️ **切る位置は文字数ではなく見た目の幅で決める**。日本語と英数字が混ざる
    テキストで字数を数えると、英数字ばかりの行が全角 `limit` 字の2倍近い長さに
    なり、カードの高さが揃わない。`east_asian_width` の `F`（全角）・`W`（広）・
    `A`（曖昧）を全角として数える（`A` を全角側へ寄せているのは、日本語環境で
    全角表示される記号（`§`・`…`）が半角として数えられると行が想定より長く
    なるため）。

    ⚠️ **`ellipsis` を足すぶんの幅は上限から引かない**（1文字ぶんの超過を許す）。
    差し引くと上限ぎりぎりのテキストが2文字短くなり、切れていないのに切れて
    見える。

    Args:
        text: 元のテキスト（`None` は空文字）
        limit: 上限（**全角字**。半角は0.5字ぶん）
        ellipsis: 切ったときに末尾へ足す文字

    Returns:
        改行と連続空白を1つの空白へ潰し、上限を超えたら末尾を `ellipsis` に
        した1行。空なら空文字

    Examples:
        >>> truncate_fullwidth("AIが契約書を作る。", limit=5)
        'AIが契約…'
        >>> truncate_fullwidth("短い。", limit=5)
        '短い。'
    """
    flat = " ".join(str(text).split()) if text is not None else ""
    if not flat:
        return ""

    budget = limit * 2
    used = 0
    kept: list[str] = []
    for char in flat:
        width = 2 if unicodedata.east_asian_width(char) in ("F", "W", "A") else 1
        if used + width > budget:
            return "".join(kept).rstrip() + ellipsis
        kept.append(char)
        used += width
    return flat


def split_paragraphs(text: object) -> list[str]:
    """`\\n\\n` 区切りのテキストを段落へ分ける（仕様書 §10.3）。

    中間xlsx のリーダ（T-22）は PARAGRAPHS 列を既に `list[str]` で返すので、
    ここへ来るのは1本の文字列で渡された場合の保険。
    """
    if text is None:
        return []
    if isinstance(text, str):
        parts = _PARAGRAPH_BREAK_RE.split(text)
        return [part.strip() for part in parts if part.strip()]
    if isinstance(text, Sequence):
        return [str(part).strip() for part in text if str(part).strip()]
    return [str(text).strip()]


def paragraphs(
    text: object,
    *,
    style: str | None = None,
    first_style: str | None = None,
    last_style: str | None = None,
) -> str:
    """段落列を `<p>` へ（**テキストはここでエスケープする**）。

    Args:
        text: 段落の列、または `\\n\\n` 区切りの文字列
        style: 各段落の style
        first_style: 先頭段落だけ差し替える style（上余白を消す用途）
        last_style: 最終段落だけ差し替える style（示唆トーンの段落・§10.3）
    """
    parts = split_paragraphs(text)
    rendered: list[str] = []
    for index, part in enumerate(parts):
        applied = style
        if index == 0 and first_style is not None:
            applied = first_style
        if index == len(parts) - 1 and last_style is not None:
            applied = last_style
        rendered.append(element("p", escape(part), style=applied))
    return "".join(rendered)


def document(*, title: object, body: str, background: str) -> str:
    """HTML 文書1本。

    Args:
        title: `<title>`（エスケープする）
        body: `<body>` の中身（組み立て済み HTML）
        background: `<body>` の背景色（週刊 `#f3f4f6` ／ 月刊 `#EEF2F6`）

    Returns:
        UTF-8 で書き出す前提の HTML 文字列（§14）
    """
    body_style = styles(
        "margin:0",
        "padding:0",
        f"background-color:{background}",
        f"font-family:{FONT_FAMILY}",
        "-webkit-text-size-adjust:100%",
    )
    return "\n".join(
        [
            DOCTYPE,
            f'<html lang="{LANGUAGE}">',
            "<head>",
            CHARSET_META,
            VIEWPORT_META,
            element("title", escape(title)),
            "</head>",
            f'<body style="{escape_style(body_style)}">',
            body,
            "</body>",
            "</html>",
            "",
        ]
    )


# --- 生成結果の検査（§7.1 の禁止事項）----------------------------------------

# --- 出力の検査（T-23）--------------------------------------------------------
#
# ⚠️ **2026-08-18（T-52 Step 3）に2つへ割った。** もともと §7.1 の禁止事項として
# 1つのタプルに混ざっていたが、中身は**性質の違う2種類**だった:
#
# | 種類 | 何のための制約か | メール配信を廃止したら |
# |---|---|---|
# | **安全性** | crawl 由来の外部文字列を HTML へ入れる経路を塞ぐ（T-23） | **残る** |
# | **メール互換性** | メールクライアントが解釈できない構文を出さない（§7.1） | 凍結 |
#
# メール版 HTML の体裁制約は廃止した（§1「備考：成果物の再定義」）が、
# **安全性の検査は媒体に関係なく必要**——記事タイトル・要約・ソース名・図解の語は
# 外部サイトから拾ってきたテキストで（T-16）、途中の工程は誰も無害化していない。

UNSAFE_CONSTRUCTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("<script> タグ", re.compile(r"</?script\b", re.IGNORECASE)),
    (
        "javascript: の URL",
        re.compile(r"""(?:href|src)\s*=\s*["']?\s*javascript:""", re.IGNORECASE),
    ),
    # 属性の中だけを見る（`>` を跨がない）ので、本文テキストでは反応しない。
    ("イベントハンドラ属性", re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE)),
)
"""**媒体に関係なく出してはいけない構文**（T-23 の安全側の責務）。

⚠️ **レンダラは今もこれを通す**（`assert_safe_html()`）。エスケープの取りこぼしを
書き出す前に落とすための関門で、メール配信の有無とは関係しない。
"""

MAIL_ONLY_CONSTRUCTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("<style> タグ", re.compile(r"</?style\b", re.IGNORECASE)),
    ("外部CSS（<link>）", re.compile(r"<link\b", re.IGNORECASE)),
    ("外部CSS（@import）", re.compile(r"@import\b", re.IGNORECASE)),
    ("flex レイアウト", re.compile(r"display\s*:\s*(inline-)?flex", re.IGNORECASE)),
    ("grid レイアウト", re.compile(r"display\s*:\s*(inline-)?grid", re.IGNORECASE)),
)
"""**メール互換性のための制約**（§7.1）。⚠️ **2026-08-18 に凍結**（T-52 Step 3）。

メール配信は仕様書 §1.3 でスコープ外のままで、実際には配られていない。Web 版が
唯一の閲覧形式になった以上、この制約を守り続ける理由は無い
（§1「備考：成果物の再定義」）。

⚠️ **消さずに残してある。** 将来メール配信が復活したときに、**出力先ごとの
レンダラを1つ足して `assert_mail_safe()` を通す**だけで済むようにするため。
検査の中身（正規表現・誤検知しないこと）はテストで生きたまま保つ
（凍結＝呼ばれていない、であって壊れている、ではない）。
"""

FORBIDDEN_CONSTRUCTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    *UNSAFE_CONSTRUCTS,
    *MAIL_ONLY_CONSTRUCTS,
)
"""§7.1 の禁止事項ぜんぶ（安全性 ＋ メール互換性）。

**メール版レンダラの受け入れ条件**（凍結中＝現在どのレンダラも通していない）。
"""


def unsafe_constructs(markup: str) -> list[str]:
    """**媒体に関係なく**出してはいけない構文が含まれていれば、その名前を返す。"""
    return [name for name, pattern in UNSAFE_CONSTRUCTS if pattern.search(markup)]


def forbidden_constructs(markup: str) -> list[str]:
    """生成物に §7.1 の禁止構文（安全性 ＋ メール互換性）が含まれていれば名前を返す。

    ⚠️ **メール互換性の側は凍結中**（`MAIL_ONLY_CONSTRUCTS`）。現在のレンダラは
    `unsafe_constructs()` だけを通す。

    Returns:
        見つかった禁止構文の名前（空なら問題なし）
    """
    return [name for name, pattern in FORBIDDEN_CONSTRUCTS if pattern.search(markup)]


def assert_safe_html(markup: str) -> str:
    """安全性の検査だけを通して、そのまま返す（T-52 Step 3）。

    レンダラの最後に通す（**生成物を書き出す前に落とす**）。エスケープの
    取りこぼしが配信物・閲覧物へ回るより、ジョブを失敗させて気づく方がよい。

    Raises:
        MailHtmlError: `<script>` / `javascript:` の URL / イベントハンドラ属性が
            含まれる場合
    """
    if found := unsafe_constructs(markup):
        raise MailHtmlError("出してはいけない構文が含まれています: " + "、".join(found))
    return markup


def assert_mail_safe(markup: str) -> str:
    """§7.1 の禁止構文（安全性 ＋ **メール互換性**）が無いことを確かめる。

    ⚠️ **2026-08-18 の T-52 Step 3 で凍結した**。メール版 HTML の体裁制約を
    廃止したので、**現在のレンダラはこれを呼ばない**（呼ぶのは
    `assert_safe_html()`）。将来メール配信が復活したときに、出力先ごとの
    レンダラから通す口として残してある（§1「備考：成果物の再定義」）。

    Raises:
        MailHtmlError: 禁止構文が含まれる場合
    """
    if found := forbidden_constructs(markup):
        raise MailHtmlError(
            "メールHTML の制約（設計書 §7.1）に反する構文が含まれています: "
            + "、".join(found)
        )
    return markup


__all__ = [
    "ALLOWED_URL_SCHEMES",
    "CHARSET",
    "CHARSET_META",
    "DOCTYPE",
    "FONT_FAMILY",
    "FORBIDDEN_CONSTRUCTS",
    "LANGUAGE",
    "MAIL_ONLY_CONSTRUCTS",
    "TABLE_LAYOUT_ATTRS",
    "VIEWPORT_META",
    "UNSAFE_CONSTRUCTS",
    "MailHtmlError",
    "assert_mail_safe",
    "assert_safe_html",
    "attributes",
    "block",
    "cell",
    "document",
    "element",
    "escape",
    "escape_style",
    "forbidden_constructs",
    "link",
    "paragraphs",
    "row",
    "safe_url",
    "spacer_cell",
    "spacer_row",
    "split_paragraphs",
    "styles",
    "table",
    "truncate_fullwidth",
    "unsafe_constructs",
]
