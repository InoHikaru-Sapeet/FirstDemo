"""`raw_articles_{period}.json` のスキーマ（設計書 §2.3 ／ 仕様書 §13.2）。

crawl（PROMPT-1 / T-16）の出力で、filter（T-21）の入力。**この段階では取捨選択を
一切しない**のがこのスキーマの設計思想:

- スコアリング・除外判定・タグ確定はしない（次段の責務・§13.2）。だから点数や
  タグの欄を持たない
- **重複しうる記事も落とさない。** 同じ URL・似たタイトルの記事が並んでいても
  そのまま保持する（§13.2「重複しうる記事もこの段階では落とさず全て残す」）。
  統合判定は filter の責務（§11 / T-18）で、crawl 側で間引くと「同じ発表をどの
  媒体が報じたか」が失われ、代表記事の `ソース` 欄（`A / B(統合)`・§11.3）を
  組み立てられなくなる。**このモジュールに重複排除を足さないこと。**

`region_hint` / `primary_or_secondary` は crawl 段階の**当たり**で、config の
`enums.region` / `enums.info_type` とは別物（どちらも `不明` を持つ）。
確定値は filter が分類・タグ付与（T-19）で決める。
"""

from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter

from enterprise.entities.json_document import (
    dump_json_document,
    parse_json_document,
)

RAW_ARTICLES_LABEL = "raw_articles.json"

# 設計書 §2.3 の `pattern`。`YYYY-MM-DD` 以外の表記（`2026/07/27` 等）を弾く。
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def _ensure_calendar_date(value: str) -> str:
    """書式が合っていても実在しない日付（`2026-02-30` 等）は通さない。

    収集元は LLM の出力なので、桁数だけ合った日付が来ることがある。
    重複判定の参照範囲（`lookback_weeks`・T-18）が日付演算に依存するため、
    ここで弾いておかないと後段で例外になる。
    """
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"実在しない日付です: {value!r}") from exc
    return value


DateText = Annotated[
    str, Field(pattern=DATE_PATTERN), AfterValidator(_ensure_calendar_date)
]
"""`YYYY-MM-DD` の日付文字列。中間xlsx の日付列（T-07）と同じ表現。"""


class RegionHint(StrEnum):
    """地域の当たり（設計書 §2.3）。

    config の `enums.region`（日本 / 海外 / グローバル）に `不明` を足した4値。
    `不明` は crawl 限り。filter が確定値へ寄せる（T-19）。
    """

    JAPAN = "日本"
    OVERSEAS = "海外"
    GLOBAL = "グローバル"
    UNKNOWN = "不明"


class PrimaryOrSecondary(StrEnum):
    """一次情報か報道かの当たり（設計書 §2.3）。

    config の `enums.info_type`（5値）より粗い3値。信頼性の採点（T-19）と
    除外ルール1（真偽不明の噂・SNS単独。§5.4）の判断材料になる。
    """

    PRIMARY = "一次(公式)"
    REPORTED = "報道"
    UNKNOWN = "不明"


class RawArticle(BaseModel):
    """crawl が収集した記事1件（未加工）。

    フィールドの宣言順は設計書 §2.3 の `properties` 順。書き出した JSON の
    キー順が設計書と揃い、差分が読める形になる。
    """

    model_config = ConfigDict(extra="forbid")

    collected_at: DateText
    """収集日。"""

    published_at: DateText | None = None
    """公開日。分かる範囲で入れる（§13.2）。不明なら null または省略。"""

    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    """正規化前でよい（§13.2）。正規化は重複判定（T-18）が行う。"""

    source: str = Field(min_length=1)
    """媒体名。"""

    raw_summary: str = Field(min_length=1)
    """本文から2〜4文の客観要約（意見を混ぜない。§13.2）。"""

    region_hint: RegionHint
    primary_or_secondary: PrimaryOrSecondary

    @property
    def collected_on(self) -> date:
        """`collected_at` を日付として扱う（重複判定の参照範囲など）。"""
        return date.fromisoformat(self.collected_at)

    @property
    def published_on(self) -> date | None:
        """`published_at` を日付として扱う。不明なら None。"""
        return (
            None if self.published_at is None else date.fromisoformat(self.published_at)
        )


RAW_ARTICLES_ADAPTER: TypeAdapter[list[RawArticle]] = TypeAdapter(list[RawArticle])
"""配列としての読み書き用（設計書 §2.3 のトップレベルは array）。"""


def parse_raw_articles(text: str) -> list[RawArticle]:
    """`raw_articles_{period}.json` を読み込む。

    **重複は排除しない。** 同一 URL の記事が2件あればそのまま2件返す。

    Args:
        text: JSON テキスト（ArtifactStore 経由で UTF-8 読み込み）

    Returns:
        収集順のままの記事一覧

    Raises:
        DocumentParseError: JSON が壊れている、またはスキーマに合わない場合。
            どの要素のどのフィールドがなぜダメかを含む
    """
    return parse_json_document(RAW_ARTICLES_ADAPTER, text, label=RAW_ARTICLES_LABEL)


def dump_raw_articles(articles: Sequence[RawArticle]) -> str:
    """`raw_articles_{period}.json` として書き出す文字列。

    渡された順序をそのまま保つ（収集順が「同じ発表をどの媒体が先に報じたか」の
    手がかりになるため、並べ替えない）。
    """
    return dump_json_document(RAW_ARTICLES_ADAPTER, list(articles))
