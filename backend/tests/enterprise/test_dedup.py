"""重複検知・統合（設計書 §6.3 ／ 仕様書 §11 ／ T-18）。

しきい値の境界（**0.84 / 0.85 / 0.86**）を両方向から固定する:

- 類似度をちょうど 0.84 / 0.85 / 0.86 に作った見出し対を、既定しきい値 0.85 に当てる
- 類似度ちょうど 0.85 の対を、しきい値 0.84 / 0.85 / 0.86 に当てる

類似度は `2 * 一致文字数 / 両文字列長の和` なので、共通部分と相違部分の長さを決めれば
**丸めの無い正確な値**を作れる（`_titles_with_similarity`）。たまたま通る文字列に
依存しないよう、比較用の文字は互いに重複しない CJK 文字から取る。
"""

from datetime import date

import pytest

from enterprise.entities.config import DedupThresholds, IntelligenceConfig, Severity
from enterprise.entities.raw_article import PrimaryOrSecondary, RawArticle, RegionHint
from enterprise.entities.report_columns import (
    EXCLUSION_LOG_COLUMNS,
    SOURCE_MERGE_SEPARATOR,
    header_row,
)
from enterprise.services.dedup import (
    CATEGORY_MERGED,
    MERGE_SUFFIX,
    REASON_DUPLICATE,
    DedupError,
    DedupHistory,
    DuplicateRecord,
    KnownArticle,
    KnownOrigin,
    MatchedBy,
    Representative,
    deduplicate,
    detect_duplicate,
    duplicate_log_entry,
    duplicate_log_row,
    merged_source_text,
    monthly_period_of,
    monthly_periods_in_scope,
    normalize_title,
    normalize_url,
    title_similarity,
    weekly_period_of,
    weekly_periods_in_scope,
)

CURRENT_PERIOD = "2026-W31"


def _thresholds(
    *,
    lookback_weeks: int = 8,
    title_similarity_threshold: float = 0.85,
    treat_same_url_as_duplicate: bool = True,
) -> DedupThresholds:
    return DedupThresholds(
        lookback_weeks=lookback_weeks,
        title_similarity_threshold=title_similarity_threshold,
        treat_same_url_as_duplicate=treat_same_url_as_duplicate,
    )


def _article(
    *,
    title: str = "OpenAI が企業向けエージェント基盤を発表",
    url: str = "https://example.com/news/agent-platform",
    source: str = "TechCrunch",
    collected_at: str = "2026-07-27",
) -> RawArticle:
    return RawArticle(
        collected_at=collected_at,
        title=title,
        url=url,
        source=source,
        raw_summary="OpenAI が企業向けのエージェント基盤を発表した。",
        region_hint=RegionHint.OVERSEAS,
        primary_or_secondary=PrimaryOrSecondary.REPORTED,
    )


def _known(
    *,
    title: str,
    url: str = "https://example.com/known",
    source: str = "VentureBeat",
    period: str = "2026-W30",
    origin: KnownOrigin = KnownOrigin.PUBLISHED,
) -> KnownArticle:
    return KnownArticle(
        title=title, url=url, source=source, period=period, origin=origin
    )


def _titles_with_similarity(matching: int, differing: int) -> tuple[str, str]:
    """類似度が正確に `matching / (matching + differing)` になる見出し対を作る。

    共通部分 `matching` 文字＋互いに素な相違部分 `differing` 文字ずつ。長さが同じ
    なので `2 * matching / (2 * (matching + differing))` に一致する。
    """
    pool = [chr(0x4E00 + index) for index in range(matching + 2 * differing)]
    common = "".join(pool[:matching])
    left = common + "".join(pool[matching : matching + differing])
    right = common + "".join(pool[matching + differing :])
    return left, right


# --- URL 正規化（仕様書 §11.2）----------------------------------------------


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        # クエリは丸ごと落とす（トラッキングパラメータの列挙に頼らない）
        (
            "https://example.com/news/a?utm_source=twitter&utm_medium=social",
            "https://example.com/news/a",
        ),
        ("https://example.com/news/a?id=123", "https://example.com/news/a"),
        # フラグメントも落とす
        ("https://example.com/news/a#section2", "https://example.com/news/a"),
        # 末尾スラッシュを統一する
        ("https://example.com/news/a/", "https://example.com/news/a"),
        ("https://example.com/news/a//", "https://example.com/news/a"),
        ("https://example.com/", "https://example.com"),
        # スキーム・ホストは大小を区別しない
        ("HTTPS://Example.COM/news/A", "https://example.com/news/A"),
        # 前後の空白
        ("  https://example.com/news/a  ", "https://example.com/news/a"),
    ],
)
def test_normalize_url(raw_url: str, expected: str) -> None:
    assert normalize_url(raw_url) == expected


def test_normalize_url_is_idempotent() -> None:
    once = normalize_url("https://example.com/news/a/?utm_source=x#top")
    assert normalize_url(once) == once


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # §11.2 に無い同一視は**しない**。別 URL を同じとみなすと記事が消える。
        ("http://example.com/a", "https://example.com/a"),
        ("https://www.example.com/a", "https://example.com/a"),
        ("https://example.com/a", "https://example.com/A"),
    ],
)
def test_normalize_url_keeps_unspecified_differences(left: str, right: str) -> None:
    """スキーム・`www.`・パスの大小は寄せない（推測で足さない）。"""
    assert normalize_url(left) != normalize_url(right)


# --- タイトル正規化（仕様書 §11.2）------------------------------------------


@pytest.mark.parametrize(
    ("raw_title", "expected"),
    [
        # 全半角統一（NFKC）
        ("ＡＩエージェント", "aiエージェント"),
        ("ｶﾞｲﾄﾞﾗｲﾝ", "ガイドライン"),
        # 記号・空白の除去
        ("OpenAI、「GPT-5」を発表！", "openaigpt5を発表"),
        ("A I  エ ー ジ ェ ン ト", "aiエージェント"),
        ("【速報】AI規制法が成立", "速報ai規制法が成立"),
        # 大小文字の同一視
        ("OpenAI", "openai"),
        ("OPENAI", "openai"),
        # 数字は残す
        ("GPT-5 が2026年に登場", "gpt5が2026年に登場"),
        # 記号だけの見出しは空になる
        ("!!! --- ???", ""),
    ],
)
def test_normalize_title(raw_title: str, expected: str) -> None:
    assert normalize_title(raw_title) == expected


def test_normalize_title_is_idempotent() -> None:
    once = normalize_title("【速報】ＯpenAI、「GPT-5」を発表！")
    assert normalize_title(once) == once


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("AI エージェント 基盤", "ＡＩエージェント基盤"),
        ("OpenAI、「GPT-5」を発表", "OpenAI【GPT-5】を発表"),
        ("生成AI／導入事例", "生成AI・導入事例"),
    ],
)
def test_the_same_headline_in_different_notations_normalizes_alike(
    left: str, right: str
) -> None:
    """媒体ごとの表記ゆれ（全半角・括弧・区切り記号）が正規化で吸収される（§11.2）。"""
    assert normalize_title(left) == normalize_title(right)


# --- 類似度 -----------------------------------------------------------------


def test_identical_titles_are_perfectly_similar() -> None:
    assert title_similarity("AI規制法が成立", "ＡＩ規制法が成立！") == 1.0


@pytest.mark.parametrize(
    ("left", "right"),
    [("!!!", "AI規制法"), ("AI規制法", "???"), ("", "AI規制法"), ("###", "***")],
)
def test_titles_that_normalize_to_nothing_are_not_similar(
    left: str, right: str
) -> None:
    """記号だけの見出しは 0.0。空文字同士を「完全一致」にすると巻き込む。"""
    assert title_similarity(left, right) == 0.0


def test_similarity_does_not_depend_on_the_argument_order() -> None:
    """`ratio()` は呼び順で値が変わることがあるので、向きをそろえてから測る。

    この対は素の `SequenceMatcher` では 0.1667 と 0.3333 に割れる（Python 3.13 実測）。
    """
    left, right = "dacddc", "cbbcba"
    assert title_similarity(left, right) == title_similarity(right, left)


def test_similarity_does_not_change_with_length() -> None:
    """長い見出しだけ別尺度にならない（`autojunk=False`）。

    同じ文字が何度も出る220文字の対。既定の自動 junk 判定が効くと一致とみなす
    範囲が狭まり 20/22（≒0.909）になるが、切ってあるので 21/22 になる。
    """
    unique = [chr(0x4E00 + index) for index in range(200)]
    left = "".join(char + "あ" for char in unique[:110])
    right = "".join(char + "あ" for char in unique[:100] + unique[110:120])

    assert len(left) == len(right) == 220
    assert title_similarity(left, right) == pytest.approx(21 / 22)


@pytest.mark.parametrize(
    ("matching", "differing", "expected"),
    [(21, 4, 0.84), (17, 3, 0.85), (43, 7, 0.86)],
)
def test_the_test_fixtures_have_the_exact_similarity(
    matching: int, differing: int, expected: float
) -> None:
    """境界値テストの土台。作った対の類似度が狙いどおりであること。"""
    left, right = _titles_with_similarity(matching, differing)
    assert title_similarity(left, right) == pytest.approx(expected)


# --- しきい値の境界（0.84 / 0.85 / 0.86）------------------------------------


@pytest.mark.parametrize(
    ("similarity", "matching", "differing", "expected_duplicate"),
    [
        (0.84, 21, 4, False),
        (0.85, 17, 3, True),
        (0.86, 43, 7, True),
    ],
)
def test_the_default_threshold_takes_similarity_at_or_above_it(
    similarity: float, matching: int, differing: int, expected_duplicate: bool
) -> None:
    """既定しきい値 0.85 に対し、**0.85 ちょうどは重複**・0.84 は重複でない。"""
    left, right = _titles_with_similarity(matching, differing)
    history = DedupHistory([_known(title=right)])

    verdict = detect_duplicate(
        _article(title=left, url="https://example.com/other"),
        history,
        _thresholds(title_similarity_threshold=0.85),
    )

    assert verdict.is_duplicate is expected_duplicate
    if expected_duplicate:
        assert verdict.matched_by is MatchedBy.TITLE
        assert verdict.similarity == pytest.approx(similarity)


@pytest.mark.parametrize(
    ("threshold", "expected_duplicate"),
    [(0.84, True), (0.85, True), (0.86, False)],
)
def test_a_similarity_of_exactly_the_default_against_moved_thresholds(
    threshold: float, expected_duplicate: bool
) -> None:
    """類似度 0.85 ちょうどの対を、しきい値 0.84 / 0.85 / 0.86 に当てる。

    しきい値は admin が変えられる（§7.2）ので、比較が `≥` であることを
    しきい値側からも固定する。
    """
    left, right = _titles_with_similarity(17, 3)
    history = DedupHistory([_known(title=right)])

    verdict = detect_duplicate(
        _article(title=left, url="https://example.com/other"),
        history,
        _thresholds(title_similarity_threshold=threshold),
    )

    assert verdict.is_duplicate is expected_duplicate


# --- URL 一致（仕様書 §11.2-1）----------------------------------------------


def test_the_same_normalized_url_is_a_duplicate() -> None:
    history = DedupHistory(
        [_known(title="無関係な見出し", url="https://example.com/news/a")]
    )

    verdict = detect_duplicate(
        _article(title="別の見出し", url="https://example.com/news/a/?utm_source=x"),
        history,
        _thresholds(),
    )

    assert verdict.is_duplicate
    assert verdict.matched_by is MatchedBy.URL
    assert verdict.similarity is None


def test_url_matching_can_be_switched_off() -> None:
    """`treat_same_url_as_duplicate=false` なら URL 一致では重複にしない（§11.2）。"""
    history = DedupHistory(
        [_known(title="無関係な見出し", url="https://example.com/news/a")]
    )

    verdict = detect_duplicate(
        _article(title="別の見出し", url="https://example.com/news/a"),
        history,
        _thresholds(treat_same_url_as_duplicate=False),
    )

    assert not verdict.is_duplicate


def test_url_matching_comes_before_title_matching() -> None:
    """URL 一致が先（§11.2 の並び）。類似タイトルの別記事より URL の相手を取る。"""
    left, right = _titles_with_similarity(17, 3)
    history = DedupHistory(
        [
            _known(title=right, url="https://example.com/news/similar"),
            _known(title="まったく違う見出し", url="https://example.com/news/same"),
        ]
    )

    verdict = detect_duplicate(
        _article(title=left, url="https://example.com/news/same"),
        history,
        _thresholds(),
    )

    assert verdict.matched_by is MatchedBy.URL
    assert verdict.representative_index == 1


def test_the_first_matching_history_entry_becomes_the_representative() -> None:
    """タイトル一致は**先に当たった1件**を代表にする（設計書 §6.3）。"""
    left, right = _titles_with_similarity(17, 3)
    history = DedupHistory(
        [
            _known(title=right, url="https://example.com/1", source="A"),
            _known(title=left, url="https://example.com/2", source="B"),
        ]
    )

    verdict = detect_duplicate(
        _article(title=left, url="https://example.com/3"), history, _thresholds()
    )

    assert verdict.representative_index == 0
    assert verdict.representative is not None
    assert verdict.representative.source == "A"


def test_an_article_with_no_match_is_not_a_duplicate() -> None:
    history = DedupHistory([_known(title="AI規制法が成立", url="https://a.example/1")])

    verdict = detect_duplicate(
        _article(title="半導体大手が新工場", url="https://b.example/2"),
        history,
        _thresholds(),
    )

    assert not verdict.is_duplicate
    assert verdict.representative is None
    assert verdict.representative_index is None


def test_an_empty_history_matches_nothing() -> None:
    verdict = detect_duplicate(_article(), DedupHistory(), _thresholds())

    assert not verdict.is_duplicate
    assert len(DedupHistory()) == 0


# --- 参照範囲（仕様書 §11.1）------------------------------------------------


def test_weekly_scope_excludes_the_target_period() -> None:
    """⚠️ 対象週を含めない（含めると再実行で全記事が既出になる。§14）。"""
    periods = weekly_periods_in_scope("2026-W31", 8)

    assert CURRENT_PERIOD not in periods
    assert len(periods) == 8
    assert periods[0] == "2026-W30"
    assert periods[-1] == "2026-W23"


def test_weekly_scope_crosses_the_year_boundary() -> None:
    """ISO 週で遡るので年またぎ・53週の年も壊れない。"""
    assert weekly_periods_in_scope("2027-W02", 3) == [
        "2027-W01",
        "2026-W53",  # 2026年は53週まである
        "2026-W52",
    ]


def test_weekly_scope_of_zero_weeks_is_empty() -> None:
    assert weekly_periods_in_scope("2026-W31", 0) == []


@pytest.mark.parametrize("period", ["2026-07", "2026W31", "26-W31", "", "2026-W99"])
def test_weekly_scope_rejects_bad_periods(period: str) -> None:
    with pytest.raises(DedupError):
        weekly_periods_in_scope(period, 8)


def test_weekly_scope_rejects_negative_lookback() -> None:
    with pytest.raises(DedupError):
        weekly_periods_in_scope("2026-W31", -1)


def test_monthly_scope_includes_the_current_month() -> None:
    """月次だけは当月を含む（§11.1「当月＋直近数ヶ月」）。"""
    assert monthly_periods_in_scope("2026-01", 3) == [
        "2026-01",
        "2025-12",
        "2025-11",
        "2025-10",
    ]


@pytest.mark.parametrize("period", ["2026-W31", "2026-13", "2026-00", "202607"])
def test_monthly_scope_rejects_bad_periods(period: str) -> None:
    with pytest.raises(DedupError):
        monthly_periods_in_scope(period, 3)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 7, 27), "2026-W31"),  # 月曜
        (date(2026, 8, 2), "2026-W31"),  # 日曜（同じ ISO 週）
        (date(2027, 1, 1), "2026-W53"),  # ISO 週は年をまたぐ
    ],
)
def test_weekly_period_of(day: date, expected: str) -> None:
    assert weekly_period_of(day) == expected


def test_monthly_period_of() -> None:
    assert monthly_period_of(date(2026, 7, 27)) == "2026-07"


def test_history_can_be_narrowed_to_the_scope() -> None:
    """参照範囲外の週の記事とは突き合わせない。"""
    history = DedupHistory(
        [
            _known(title="範囲内", url="https://a.example/1", period="2026-W30"),
            _known(title="範囲外", url="https://a.example/2", period="2026-W10"),
            _known(
                title="除外ログ由来",
                url="https://a.example/3",
                period="2026-W29",
                origin=KnownOrigin.EXCLUDED,
            ),
        ]
    )

    narrowed = history.in_scope(weekly_periods_in_scope(CURRENT_PERIOD, 8))

    assert [entry.title for entry in narrowed.entries] == ["範囲内", "除外ログ由来"]
    assert narrowed.entries[1].origin is KnownOrigin.EXCLUDED
    assert narrowed.index_by_url("https://a.example/2") is None


def test_the_exclusion_log_is_part_of_the_history(
    config: IntelligenceConfig,
) -> None:
    """除外ログの既出記事とも突き合わせる（§11.1）。"""
    history = DedupHistory(
        [
            _known(
                title="AI規制法が成立",
                url="https://a.example/1",
                origin=KnownOrigin.EXCLUDED,
            )
        ]
    )

    verdict = detect_duplicate(
        _article(title="AI規制法が成立", url="https://b.example/2"),
        history,
        config.tunable_thresholds.dedup,
    )

    assert verdict.is_duplicate
    assert verdict.representative is not None
    assert verdict.representative.origin is KnownOrigin.EXCLUDED


# --- 統合（仕様書 §11.3）----------------------------------------------------


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (["TechCrunch"], "TechCrunch"),
        (["TechCrunch", "ITmedia"], "TechCrunch / ITmedia(統合)"),
        (
            ["TechCrunch", "ITmedia", "Ledge.ai"],
            "TechCrunch / ITmedia(統合) / Ledge.ai(統合)",
        ),
        # 同じ媒体名は重ねない（`A / A(統合)` にしない）
        (["TechCrunch", "TechCrunch"], "TechCrunch"),
    ],
)
def test_merged_source_text(sources: list[str], expected: str) -> None:
    assert merged_source_text(sources) == expected


def test_merged_source_text_uses_the_column_separator() -> None:
    """区切りは T-07 の定義（`SOURCE_MERGE_SEPARATOR`）を使う。"""
    assert SOURCE_MERGE_SEPARATOR in merged_source_text(["A", "B"])
    assert merged_source_text(["A", "B"]).endswith(MERGE_SUFFIX)


def test_merged_source_text_needs_a_representative() -> None:
    with pytest.raises(DedupError):
        merged_source_text([])


def test_a_representative_starts_with_its_own_source() -> None:
    representative = Representative(article=_article(source="TechCrunch"))

    assert representative.source_text == "TechCrunch"
    assert representative.merged_count == 0


def test_the_same_announcement_from_another_outlet_is_merged() -> None:
    """同一発表の別媒体は代表1件へ統合し、`ソース` 欄へ併記する（§11.3）。"""
    title = "OpenAI が企業向けエージェント基盤を発表"
    articles = [
        _article(title=title, url="https://techcrunch.example/a", source="TechCrunch"),
        _article(title=title, url="https://itmedia.example/b", source="ITmedia"),
        _article(title=title, url="https://ledge.example/c", source="Ledge.ai"),
    ]

    result = deduplicate(articles, DedupHistory(), _thresholds(), period=CURRENT_PERIOD)

    assert len(result.representatives) == 1
    representative = result.representatives[0]
    assert representative.article.source == "TechCrunch"
    assert representative.merged_count == 2
    assert representative.source_text == "TechCrunch / ITmedia(統合) / Ledge.ai(統合)"
    assert [record.article.source for record in result.duplicates] == [
        "ITmedia",
        "Ledge.ai",
    ]


def test_articles_that_differ_are_all_kept() -> None:
    articles = [
        _article(title="AI規制法が成立", url="https://a.example/1"),
        _article(title="半導体大手が新工場を建設", url="https://b.example/2"),
        _article(title="国内銀行が生成AIを全店導入", url="https://c.example/3"),
    ]

    result = deduplicate(articles, DedupHistory(), _thresholds(), period=CURRENT_PERIOD)

    assert len(result.representatives) == 3
    assert result.duplicates == []


def test_input_order_is_preserved() -> None:
    """代表の並びは入力順（並べ替えは合計スコア降順で T-21 が行う）。"""
    articles = [
        _article(title="三つ目の話題", url="https://a.example/3"),
        _article(title="一つ目の話題", url="https://a.example/1"),
        _article(title="二つ目の話題", url="https://a.example/2"),
    ]

    result = deduplicate(articles, DedupHistory(), _thresholds(), period=CURRENT_PERIOD)

    assert [rep.article.title for rep in result.representatives] == [
        "三つ目の話題",
        "一つ目の話題",
        "二つ目の話題",
    ]


def test_a_duplicate_of_a_past_week_does_not_touch_the_past_sheet() -> None:
    """代表が過去週の記事なら、今回の代表一覧には現れない（過去の xlsx は触らない）。

    §11.3 が求めるのは「本編に載せず除外ログへ記録する」ところまで。
    """
    title = "AI規制法が成立"
    history = DedupHistory(
        [_known(title=title, url="https://old.example/1", period="2026-W29")]
    )

    result = deduplicate(
        [_article(title=title, url="https://new.example/2", source="ITmedia")],
        history,
        _thresholds(),
        period=CURRENT_PERIOD,
    )

    assert result.representatives == []
    assert len(result.duplicates) == 1
    representative = result.duplicates[0].verdict.representative
    assert representative is not None
    assert representative.period == "2026-W29"


def test_the_history_passed_in_is_not_modified() -> None:
    """今回採用した記事を呼び出し側の履歴へ書き戻さない（実行の独立性）。"""
    history = DedupHistory([_known(title="既出", url="https://old.example/1")])

    deduplicate(
        [_article(title="新しい話題", url="https://new.example/2")],
        history,
        _thresholds(),
        period=CURRENT_PERIOD,
    )

    assert len(history) == 1


def test_running_twice_over_the_same_input_gives_the_same_result() -> None:
    """同じ入力・同じ config なら同じ結果（§14 冪等性）。"""
    title = "OpenAI が企業向けエージェント基盤を発表"
    articles = [
        _article(title=title, url="https://a.example/1", source="TechCrunch"),
        _article(title=title, url="https://b.example/2", source="ITmedia"),
        _article(title="別の話題", url="https://c.example/3", source="Ledge.ai"),
    ]

    first = deduplicate(articles, DedupHistory(), _thresholds(), period=CURRENT_PERIOD)
    second = deduplicate(articles, DedupHistory(), _thresholds(), period=CURRENT_PERIOD)

    assert [rep.source_text for rep in first.representatives] == [
        rep.source_text for rep in second.representatives
    ]
    assert [record.article.url for record in first.duplicates] == [
        record.article.url for record in second.duplicates
    ]


def test_the_config_thresholds_drive_the_result(config: IntelligenceConfig) -> None:
    """しきい値は config から来る（テスト側で数値を決めない）。"""
    left, right = _titles_with_similarity(17, 3)
    articles = [
        _article(title=left, url="https://a.example/1"),
        _article(title=right, url="https://b.example/2"),
    ]

    assert config.tunable_thresholds.dedup.title_similarity_threshold == 0.85
    result = deduplicate(
        articles,
        DedupHistory(),
        config.tunable_thresholds.dedup,
        period=CURRENT_PERIOD,
    )

    assert len(result.representatives) == 1


# --- 除外ログ（6列）---------------------------------------------------------


def test_the_duplicate_log_row_follows_the_column_definition() -> None:
    """`除外区分=統合` / `除外理由=重複・転載記事`（§11.3）。"""
    article = _article(url="https://example.com/news/a?utm_source=x")
    record = DuplicateRecord(
        article=article,
        verdict=detect_duplicate(
            article,
            DedupHistory([_known(title=article.title)]),
            _thresholds(),
        ),
    )

    entry = duplicate_log_entry(record)
    row = duplicate_log_row(record)

    assert list(entry) == header_row(EXCLUSION_LOG_COLUMNS)
    assert row == [
        article.collected_at,
        article.title,
        article.url,  # 収集したままの URL（正規化しない）
        article.source,
        CATEGORY_MERGED,
        REASON_DUPLICATE,
    ]
    assert len(row) == 6


def test_the_duplicate_reason_matches_the_merge_rule_name(
    config: IntelligenceConfig,
) -> None:
    """`除外理由` が除外ルール（severity=merge）の名前と一致していること。

    §11.3 の確定値と §5.2 のルール名が同じ文字列であることを固定しておくと、
    どちらかが動いたときに気づける（T-17 の `merge` 分岐と地続きの語彙）。
    """
    merge_rules = [
        rule for rule in config.exclusion_rules if rule.severity is Severity.MERGE
    ]

    assert [rule.name for rule in merge_rules] == [REASON_DUPLICATE]


def test_dedup_thresholds_come_from_the_config(config: IntelligenceConfig) -> None:
    """既定値（8週 / 0.85 / URL一致=真）は §5.2 の確定値。"""
    thresholds = config.tunable_thresholds.dedup

    assert isinstance(thresholds, DedupThresholds)
    assert thresholds.lookback_weeks == 8
    assert thresholds.title_similarity_threshold == 0.85
    assert thresholds.treat_same_url_as_duplicate is True
