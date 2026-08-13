"""`raw_articles.json` のスキーマ（設計書 §2.3 ／ 仕様書 §13.2）。

crawl 段階のスキーマなので、**取捨選択をしない**ことが要件そのもの。
特に「重複しうる記事も落とさない」（§13.2）は
`test_duplicate_articles_are_kept` 以下で明示的に固定している。
"""

import json
from typing import Any

import pytest

from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.json_document import DocumentParseError
from enterprise.entities.raw_article import (
    RAW_ARTICLES_ADAPTER,
    DateText,
    PrimaryOrSecondary,
    RawArticle,
    RegionHint,
    dump_raw_articles,
    parse_raw_articles,
)


def _article(**overrides: Any) -> dict[str, Any]:
    """§13.2 の出力例どおりの1件。テストごとに1箇所だけ崩す。"""
    return {
        "collected_at": "2026-07-27",
        "published_at": "2026-07-25",
        "title": "某社がAIエージェントを全社導入",
        "url": "https://example.com/agent",
        "source": "ITmedia",
        "raw_summary": "一文目。二文目。三文目。",
        "region_hint": "日本",
        "primary_or_secondary": "報道",
        **overrides,
    }


def _paths(exc_info: pytest.ExceptionInfo[DocumentParseError]) -> list[str]:
    return [issue.path for issue in exc_info.value.issues]


# --- 正常系 ------------------------------------------------------------------


def test_article_from_the_prompt_example_is_accepted() -> None:
    """§13.2 が PROMPT-1 に指示している出力がそのまま通ること。"""
    (article,) = parse_raw_articles(json.dumps([_article()], ensure_ascii=False))

    assert article.collected_at == "2026-07-27"
    assert article.published_at == "2026-07-25"
    assert article.title == "某社がAIエージェントを全社導入"
    assert article.url == "https://example.com/agent"
    assert article.source == "ITmedia"
    assert article.raw_summary == "一文目。二文目。三文目。"
    assert article.region_hint is RegionHint.JAPAN
    assert article.primary_or_secondary is PrimaryOrSecondary.REPORTED


def test_empty_array_is_valid() -> None:
    """収集0件でも壊れたファイル扱いにはしない（crawl の結果として起こりうる）。"""
    assert parse_raw_articles("[]") == []


def test_array_round_trips_and_keeps_order() -> None:
    """収集順は「同じ発表をどの媒体が先に報じたか」の手がかりなので保つ。"""
    payload = [
        _article(title="1件目", url="https://example.com/1"),
        _article(title="2件目", url="https://example.com/2"),
        _article(title="3件目", url="https://example.com/3"),
    ]

    articles = parse_raw_articles(json.dumps(payload, ensure_ascii=False))
    text = dump_raw_articles(articles)

    assert [a.title for a in articles] == ["1件目", "2件目", "3件目"]
    assert json.loads(text) == payload
    assert [a.title for a in parse_raw_articles(text)] == ["1件目", "2件目", "3件目"]


def test_dump_keeps_japanese_readable_and_ends_with_a_newline() -> None:
    """入出力は UTF-8（設計書 §14）。diff が読める形にしておく。"""
    text = dump_raw_articles(parse_raw_articles(json.dumps([_article()])))

    assert "日本" in text
    assert "\\u" not in text
    assert text.endswith("\n")


def test_dump_preserves_the_field_order_from_the_design() -> None:
    """設計書 §2.3 の `properties` 順。"""
    text = dump_raw_articles(parse_raw_articles(json.dumps([_article()])))

    assert list(json.loads(text)[0]) == [
        "collected_at",
        "published_at",
        "title",
        "url",
        "source",
        "raw_summary",
        "region_hint",
        "primary_or_secondary",
    ]


# --- 重複を落とさない（仕様書 §13.2・統合判定は filter の責務）---------------


def test_duplicate_urls_are_kept() -> None:
    """同一 URL が2件あってもそのまま2件。統合判定は T-18。"""
    payload = [
        _article(title="A社の発表", source="ITmedia"),
        _article(title="A社の発表", source="TechCrunch"),
    ]

    articles = parse_raw_articles(json.dumps(payload, ensure_ascii=False))

    assert len(articles) == 2
    assert {a.source for a in articles} == {"ITmedia", "TechCrunch"}


def test_completely_identical_articles_are_kept() -> None:
    """1文字も違わない2件でも間引かない。"""
    payload = [_article(), _article()]

    assert len(parse_raw_articles(json.dumps(payload, ensure_ascii=False))) == 2


def test_similar_titles_from_different_outlets_are_kept() -> None:
    """同じ発表の別媒体記事は全部残す。

    ここで間引くと、代表記事の `ソース` 欄に `A / B(統合)` を組み立てられない
    （仕様書 §11.3）。
    """
    payload = [
        _article(title="A社、AIエージェントを全社導入", url="https://a.example/1"),
        _article(title="A社がAIエージェントを全社導入へ", url="https://b.example/2"),
        _article(title="【速報】A社 AIエージェント全社導入", url="https://c.example/3"),
    ]

    assert len(parse_raw_articles(json.dumps(payload, ensure_ascii=False))) == 3


def test_the_module_exposes_no_deduplication_helper() -> None:
    """重複排除をこの層へ足していないこと（責務は T-18）。"""
    import enterprise.entities.raw_article as module

    suspicious = [
        name
        for name in dir(module)
        if any(word in name.lower() for word in ("dedup", "unique", "distinct"))
    ]

    assert suspicious == []


# --- published_at は nullable かつ省略可 -------------------------------------


def test_published_at_may_be_null() -> None:
    (article,) = parse_raw_articles(json.dumps([_article(published_at=None)]))

    assert article.published_at is None
    assert article.published_on is None


def test_published_at_may_be_omitted() -> None:
    """設計書 §2.3 の `required` に published_at は入っていない。"""
    payload = _article()
    del payload["published_at"]

    (article,) = parse_raw_articles(json.dumps([payload], ensure_ascii=False))

    assert article.published_at is None


def test_date_helpers_expose_real_dates_for_lookback_arithmetic() -> None:
    """重複判定の参照範囲（`lookback_weeks`・T-18）が日付演算するため。"""
    from datetime import date

    (article,) = parse_raw_articles(json.dumps([_article()], ensure_ascii=False))

    assert article.collected_on == date(2026, 7, 27)
    assert article.published_on == date(2026, 7, 25)


# --- 日付の形式と実在性 ------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["2026/07/27", "26-07-27", "2026-7-1", "20260727", "2026-07-27T00:00:00"]
)
def test_dates_outside_yyyy_mm_dd_are_rejected(value: str) -> None:
    """設計書 §2.3 の `pattern` どおり `YYYY-MM-DD` のみ。"""
    with pytest.raises(DocumentParseError) as exc_info:
        parse_raw_articles(json.dumps([_article(collected_at=value)]))

    assert _paths(exc_info) == ["0.collected_at"]


@pytest.mark.parametrize("value", ["2026-13-01", "2026-02-30", "2026-00-10"])
def test_impossible_dates_are_rejected(value: str) -> None:
    """桁数が合っていても実在しない日付は通さない（LLM 出力なので起こりうる）。"""
    with pytest.raises(DocumentParseError) as exc_info:
        parse_raw_articles(json.dumps([_article(collected_at=value)]))

    assert _paths(exc_info) == ["0.collected_at"]
    assert "実在しない日付" in exc_info.value.issues[0].reason


def test_invalid_published_at_is_also_rejected() -> None:
    """nullable であっても、値が入っているなら形式は守らせる。"""
    with pytest.raises(DocumentParseError) as exc_info:
        parse_raw_articles(json.dumps([_article(published_at="2026/07/25")]))

    assert _paths(exc_info) == ["0.published_at"]


# --- enum 値 -----------------------------------------------------------------


@pytest.mark.parametrize("value", ["日本", "海外", "グローバル", "不明"])
def test_all_four_region_hints_are_accepted(value: str) -> None:
    (article,) = parse_raw_articles(json.dumps([_article(region_hint=value)]))

    assert article.region_hint == value


@pytest.mark.parametrize("value", ["一次(公式)", "報道", "不明"])
def test_all_three_primary_or_secondary_values_are_accepted(value: str) -> None:
    payload = _article(primary_or_secondary=value)

    (article,) = parse_raw_articles(json.dumps([payload], ensure_ascii=False))

    assert article.primary_or_secondary == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("region_hint", "国内"),
        ("region_hint", "japan"),
        ("region_hint", ""),
        ("primary_or_secondary", "一次情報(公式発表)"),
        ("primary_or_secondary", "二次"),
    ],
)
def test_values_outside_the_enums_are_rejected(field: str, value: str) -> None:
    with pytest.raises(DocumentParseError) as exc_info:
        parse_raw_articles(json.dumps([_article(**{field: value})]))

    assert _paths(exc_info) == [f"0.{field}"]


def test_crawl_hints_are_coarser_than_the_config_enums(
    config: IntelligenceConfig,
) -> None:
    """crawl の当たりは config の確定 enum とは別物。

    `region_hint` は `enums.region` に `不明` を足した4値、
    `primary_or_secondary` は `enums.info_type`（5値）より粗い3値。
    確定値は filter の分類・タグ付与（T-19）が決める。
    """
    assert [r.value for r in RegionHint if r is not RegionHint.UNKNOWN] == (
        config.enums.region
    )
    assert RegionHint.UNKNOWN.value not in config.enums.region

    assert len(list(PrimaryOrSecondary)) == 3
    assert len(config.enums.info_type) == 5
    assert PrimaryOrSecondary.PRIMARY.value not in config.enums.info_type


# --- 必須・非空・未知キー ----------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "collected_at",
        "title",
        "url",
        "source",
        "raw_summary",
        "region_hint",
        "primary_or_secondary",
    ],
)
def test_required_fields_cannot_be_omitted(field: str) -> None:
    """設計書 §2.3 の `required` 7項目。"""
    payload = _article()
    del payload[field]

    with pytest.raises(DocumentParseError) as exc_info:
        parse_raw_articles(json.dumps([payload], ensure_ascii=False))

    assert _paths(exc_info) == [f"0.{field}"]


@pytest.mark.parametrize("field", ["title", "url", "source", "raw_summary"])
def test_empty_strings_are_rejected(field: str) -> None:
    """設計書 §2.3 の `minLength: 1`。空欄を黙って通すと後段で気づけない。"""
    with pytest.raises(DocumentParseError) as exc_info:
        parse_raw_articles(json.dumps([_article(**{field: ""})], ensure_ascii=False))

    assert _paths(exc_info) == [f"0.{field}"]


def test_unknown_keys_are_rejected() -> None:
    """`additionalProperties: false`（設計書 §2.3）。

    crawl は LLM 出力なので、余計なキー（点数やタグ）を勝手に足してくることが
    ある。この段階で採点させない方針（§13.2）を型で強制する。
    """
    with pytest.raises(DocumentParseError) as exc_info:
        parse_raw_articles(json.dumps([_article(total_score=87)], ensure_ascii=False))

    assert _paths(exc_info) == ["0.total_score"]


# --- 壊れた JSON はパス付きで落ちる ------------------------------------------


def test_malformed_json_reports_where_it_broke() -> None:
    with pytest.raises(DocumentParseError) as exc_info:
        parse_raw_articles('[{"collected_at": "2026-07-27",}]')

    assert exc_info.value.label == "raw_articles.json"
    assert "line" in exc_info.value.issues[0].path


def test_non_array_payload_is_rejected_at_the_root() -> None:
    """設計書 §2.3 のトップレベルは array。"""
    with pytest.raises(DocumentParseError) as exc_info:
        parse_raw_articles(json.dumps(_article()))

    assert _paths(exc_info) == ["(root)"]


def test_the_failing_element_index_is_in_the_path() -> None:
    """何件目の記事が悪いのか分かること。"""
    payload = [_article(), _article(url=""), _article()]

    with pytest.raises(DocumentParseError) as exc_info:
        parse_raw_articles(json.dumps(payload, ensure_ascii=False))

    assert _paths(exc_info) == ["1.url"]


def test_every_problem_is_reported_not_just_the_first() -> None:
    """壊れた要素だけ読み飛ばす挙動は持たない。全件まとめて返す。"""
    payload = [_article(url=""), _article(region_hint="国内"), _article(title="")]

    with pytest.raises(DocumentParseError) as exc_info:
        parse_raw_articles(json.dumps(payload, ensure_ascii=False))

    assert _paths(exc_info) == ["0.url", "1.region_hint", "2.title"]


def test_error_message_names_the_file_and_the_paths() -> None:
    """ログだけを見て原因が追えること。"""
    with pytest.raises(DocumentParseError) as exc_info:
        parse_raw_articles(json.dumps([_article(url="")], ensure_ascii=False))

    message = str(exc_info.value)

    assert "raw_articles.json" in message
    assert "0.url" in message


# --- 生成 JSON Schema が設計書 §2.3 と揃っていること -------------------------


def test_json_schema_matches_the_design() -> None:
    schema = RAW_ARTICLES_ADAPTER.json_schema()
    item = schema["$defs"]["RawArticle"]

    assert schema["type"] == "array"
    assert item["additionalProperties"] is False
    assert item["required"] == [
        "collected_at",
        "title",
        "url",
        "source",
        "raw_summary",
        "region_hint",
        "primary_or_secondary",
    ]
    assert item["properties"]["collected_at"]["pattern"] == r"^\d{4}-\d{2}-\d{2}$"
    assert item["properties"]["title"]["minLength"] == 1
    assert schema["$defs"]["RegionHint"]["enum"] == [
        "日本",
        "海外",
        "グローバル",
        "不明",
    ]
    assert schema["$defs"]["PrimaryOrSecondary"]["enum"] == [
        "一次(公式)",
        "報道",
        "不明",
    ]


def test_date_text_type_is_reusable() -> None:
    """日付表現は中間xlsx の日付列（T-07）と同じ `YYYY-MM-DD` 文字列。"""
    assert DateText is not None
    assert RawArticle.model_fields["collected_at"].annotation is str
