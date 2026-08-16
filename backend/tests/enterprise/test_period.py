"""period 値オブジェクト（T-21 で共通化）。

⚠️ **写しを作らないことがこのモジュールの目的**なので、テストも
「crawl / dedup / artifact_store が同じ定義を見ていること」を確かめる。
"""

from datetime import date

import pytest

from adapter.storage import artifact_store
from application.usecases import crawl
from enterprise.entities.period import (
    MONTHLY_PERIOD_RE,
    WEEKLY_PERIOD_RE,
    PeriodError,
    PeriodKind,
    monthly_period_of,
    monthly_periods_including,
    parse_period,
    preceding_weekly_periods,
    weekly_period_of,
)
from enterprise.services import dedup


def test_a_weekly_period_opens_into_an_iso_week() -> None:
    period = parse_period("2026-W31")

    assert period.kind is PeriodKind.WEEKLY
    assert period.is_weekly and not period.is_monthly
    assert (period.start, period.end) == (date(2026, 7, 27), date(2026, 8, 2))
    assert period.start.weekday() == 0  # 月曜始まり（設計書 §0・§14）
    assert str(period) == "2026-W31"


def test_a_monthly_period_opens_into_a_calendar_month() -> None:
    period = parse_period("2026-02")

    assert period.kind is PeriodKind.MONTHLY
    assert (period.start, period.end) == (date(2026, 2, 1), date(2026, 2, 28))


@pytest.mark.parametrize(
    "text",
    [
        "2026-13",  # 実在しない月
        "2026-00",
        "2025-W53",  # 2025 は53週を持たない（2026 は持つ）
        "2026W31",  # 表記違い
        "2026/07",
        "",
    ],
)
def test_an_impossible_period_is_rejected(text: str) -> None:
    """⚠️ 表記が合っているだけの period を先へ通さない（モデルが期間を補う）。"""
    with pytest.raises(PeriodError):
        parse_period(text)


def test_the_weekly_scope_excludes_the_target_week() -> None:
    """§14 冪等性：再実行で自分の出力と突き合わせない（T-18 モジュール docstring）。"""
    periods = preceding_weekly_periods("2026-W31", 3)

    assert periods == ["2026-W30", "2026-W29", "2026-W28"]
    assert "2026-W31" not in periods


def test_the_monthly_scope_includes_the_target_month() -> None:
    """§11.1 は月次だけ当月を含む（当月を外すのは呼び出し側＝T-21 の判断）。"""
    assert monthly_periods_including("2026-01", 3) == [
        "2026-01",
        "2025-12",
        "2025-11",
        "2025-10",
    ]


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 7, 27), "2026-W31"),  # 月曜
        (date(2026, 8, 2), "2026-W31"),  # 日曜
        (date(2026, 8, 3), "2026-W32"),
    ],
)
def test_weekly_period_of_uses_iso_weeks(day: date, expected: str) -> None:
    assert weekly_period_of(day) == expected


def test_monthly_period_of() -> None:
    assert monthly_period_of(date(2026, 7, 27)) == "2026-07"


@pytest.mark.parametrize(
    ("period", "count"),
    [("2026-07", 1), ("2026-W31", -1)],
)
def test_the_weekly_scope_rejects_a_bad_request(period: str, count: int) -> None:
    with pytest.raises(PeriodError):
        preceding_weekly_periods(period, count)


@pytest.mark.parametrize(
    ("period", "count"),
    [("2026-W31", 1), ("2026-07", -1)],
)
def test_the_monthly_scope_rejects_a_bad_request(period: str, count: int) -> None:
    with pytest.raises(PeriodError):
        monthly_periods_including(period, count)


# --- 写しが無いこと（T-16 / T-18 の申し送り）--------------------------------


def test_every_layer_shares_one_definition() -> None:
    """crawl・dedup・artifact_store が同じ表記の定義を見ていること。

    ⚠️ ここが落ちたら「同じ正規表現を別々に持ち始めた」合図。片方だけ直すと、
    パスは通るのに収集できない（またはその逆の）period が生まれる。
    """
    assert artifact_store.WEEKLY_PERIOD_RE is WEEKLY_PERIOD_RE
    assert artifact_store.MONTHLY_PERIOD_RE is MONTHLY_PERIOD_RE
    assert crawl.PeriodSpan is parse_period("2026-W31").__class__
    assert dedup.weekly_period_of is weekly_period_of
    assert dedup.monthly_period_of is monthly_period_of


def test_each_layer_keeps_its_own_exception_type() -> None:
    """`PeriodError` を層をまたいで漏らさない（工程で失敗を判別できる形を保つ）。"""
    with pytest.raises(crawl.CrawlError):
        crawl.period_span("2026-13")
    with pytest.raises(dedup.DedupError):
        dedup.weekly_periods_in_scope("2026-07", 8)
    with pytest.raises(artifact_store.ArtifactStoreError):
        artifact_store.validate_period("2026/07")
