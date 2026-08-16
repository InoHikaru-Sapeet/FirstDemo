"""対象期間 `period` の値オブジェクト（仕様書 §4・§8 ／ T-21）。

`period` は週次 `YYYY-Www`（ISO 週・月曜始まり）と月次 `YYYY-MM` の2表記だけで、
crawl（T-16）・重複判定（T-18）・成果物のパス解決（T-02）・フィルタ（T-21）が
それぞれ「表記が正しいか」「実在する期間か」「何月何日から何日までか」を必要とする。

**このモジュールがその唯一の定義。** T-16 / T-18 の備考が申し送っていた
「共通の period 値オブジェクトを作るのは T-21 の担当」の実体で、それまで
`adapter.storage.artifact_store` と `enterprise.services.dedup` に写しがあった
正規表現と、`application.usecases.crawl` にあった実日付への展開をここへ寄せた。

⚠️ **表記の検査だけで済ませない。** `2026-13`（存在しない月）や 53週を持たない年の
`-W53` は表記としては通ってしまう。そのままプロンプトへ載せるとモデルが適当な期間を
補って収集するので（T-16）、**実日付へ開けるかどうかまで**をここで確かめる。

各層の例外型（`CrawlError` / `DedupError` / `ArtifactStoreError`）はそのまま残して
あるので、呼び出し側は `PeriodError` を自分の層の例外へ包み直して使う
（層をまたいで例外型が漏れると、上位が「どの工程で失敗したか」を判別できなくなる）。
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

# 仕様書 §4・§8 の表記。⚠️ **写しを作らないこと**（3箇所に散っていたのを集めた）。
WEEKLY_PERIOD_RE = re.compile(r"^(\d{4})-W(\d{2})$")
MONTHLY_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")


class PeriodError(Exception):
    """period として扱えない文字列（表記違い・実在しない期間）。"""


class PeriodKind(StrEnum):
    """週次か月次か。値は crawl のプロンプト分岐（§13.2）でも使う。"""

    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass(frozen=True, slots=True)
class Period:
    """対象期間1つ（表記と実日付の両方を持つ）。

    Attributes:
        text: 表記（`2026-W31` / `2026-07`）。成果物のファイル名・シート名になる
        kind: 週次か月次か
        start: 期間の初日
        end: 期間の最終日（**両端を含む**）
    """

    text: str
    kind: PeriodKind
    start: date
    end: date

    @property
    def is_weekly(self) -> bool:
        return self.kind is PeriodKind.WEEKLY

    @property
    def is_monthly(self) -> bool:
        return self.kind is PeriodKind.MONTHLY

    def __str__(self) -> str:
        return self.text


def parse_period(period: str) -> Period:
    """`2026-W31` / `2026-07` を実日付まで開く。

    Args:
        period: 期間の表記

    Returns:
        表記と実日付を持つ `Period`

    Raises:
        PeriodError: どちらの表記でもない、または実在しない期間
    """
    if matched := WEEKLY_PERIOD_RE.match(period):
        year, week = int(matched.group(1)), int(matched.group(2))
        try:
            monday = date.fromisocalendar(year, week, 1)
        except ValueError as exc:  # 53週を持たない年の `-W53` など
            raise PeriodError(f"実在しない週です: {period!r}") from exc
        return Period(
            text=period,
            kind=PeriodKind.WEEKLY,
            start=monday,
            end=date.fromisocalendar(year, week, 7),
        )

    if matched := MONTHLY_PERIOD_RE.match(period):
        year, month = int(matched.group(1)), int(matched.group(2))
        if not 1 <= month <= 12:
            raise PeriodError(f"実在しない月です: {period!r}")
        last_day = calendar.monthrange(year, month)[1]
        return Period(
            text=period,
            kind=PeriodKind.MONTHLY,
            start=date(year, month, 1),
            end=date(year, month, last_day),
        )

    raise PeriodError(
        f"period は 'YYYY-Www'（週次）または 'YYYY-MM'（月次）が必要です: {period!r}"
    )


def weekly_period_of(day: date) -> str:
    """日付が属する週次 period（`YYYY-Www`）。ISO 週（月曜始まり）。

    除外ログの行は `収集日` しか持たない（6列。§2.2.2）ので、履歴へ積むときに
    どの週の情報かを決めるのに使う（T-18 申し送り①）。
    """
    iso = day.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def monthly_period_of(day: date) -> str:
    """日付が属する月次 period（`YYYY-MM`）。"""
    return f"{day.year:04d}-{day.month:02d}"


def preceding_weekly_periods(period: str, count: int) -> list[str]:
    """対象週の**手前** `count` 週（新しい週から順）。

    ⚠️ **対象期間そのものを含めない。** 含めると再実行で自分の出力と突き合わせて
    しまい、2回目に全記事が「既出」になる（§14 冪等性。T-18 モジュール docstring）。

    Raises:
        PeriodError: 週次の表記でない／実在しない週／`count` が負
    """
    if count < 0:
        raise PeriodError(f"遡る週数が負です: {count}")
    parsed = parse_period(period)
    if not parsed.is_weekly:
        raise PeriodError(f"週次 period は 'YYYY-Www' 形式が必要です: {period!r}")
    return [
        weekly_period_of(parsed.start - timedelta(weeks=back))
        for back in range(1, count + 1)
    ]


def monthly_periods_including(period: str, lookback: int) -> list[str]:
    """当月＋その前 `lookback` ヶ月（新しい月から順）。

    §11.1 は月次だけ**当月を含む**（当月の cases と対応する週次記事を見る）。

    Raises:
        PeriodError: 月次の表記でない／`lookback` が負
    """
    if lookback < 0:
        raise PeriodError(f"遡る月数が負です: {lookback}")
    parsed = parse_period(period)
    if not parsed.is_monthly:
        raise PeriodError(f"月次 period は 'YYYY-MM' 形式が必要です: {period!r}")

    year, month = parsed.start.year, parsed.start.month
    periods = []
    for back in range(lookback + 1):
        total = year * 12 + (month - 1) - back
        periods.append(f"{total // 12:04d}-{total % 12 + 1:02d}")
    return periods


__all__ = [
    "MONTHLY_PERIOD_RE",
    "WEEKLY_PERIOD_RE",
    "Period",
    "PeriodError",
    "PeriodKind",
    "monthly_period_of",
    "monthly_periods_including",
    "parse_period",
    "preceding_weekly_periods",
    "weekly_period_of",
]
