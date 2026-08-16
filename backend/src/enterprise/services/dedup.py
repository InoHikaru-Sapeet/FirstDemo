"""重複検知・統合（設計書 §6.3 ／ 仕様書 §11 ／ T-18）。

フィルタ段の2番目。既出記事（過去週のシート＋除外ログ）と、**同じ実行内の別記事**を
突き合わせ、同一発表を指すものを**代表1件へ寄せる**。crawl は重複しうる記事を落とさ
ない（`raw_article.py`）ので、間引くのはここだけの責務。

---

**除外判定（T-17）と同じく、この層は config と記事データしか見ない。** 類似度の
計算・しきい値の比較・代表の決定はすべて決定的で、LLM に「これは同じ記事か」を
聞かない。同じ config・同じ入力なら何度実行しても同じ結果になる（§14 冪等性）。

---

**類似度の算出方法の選定（T-18 の「選定して備考に記録」）**

`difflib.SequenceMatcher(...).ratio()`（標準ライブラリ）を使う。

- **標準ライブラリで足りる**（依存を増やさない）。`python-Levenshtein` / `rapidfuzz`
  は速いが、この規模（週あたり数十件 × 履歴8週）で速度は問題にならない。
- `ratio()` は `2 * 一致文字数 / 両文字列長の和` で、**0.85 のような設定値と直感が
  合う**（既定 `title_similarity_threshold=0.85` は §5.2 の確定値で、これを変えずに
  そのまま使える尺度である必要がある）。
- 文字の**並び順**を見る（共通部分列ベース）ので、「同じ発表の見出しが媒体ごとに
  少し違う」形の差——語尾・媒体名の付加・一部の言い換え——に素直に効く。集合ベース
  （Jaccard 等）は語順を捨てるため、無関係な記事同士でも共通語が多いと上がりやすい。

⚠️ **決定性のために既定から2点変えている**（どちらもテストで固定）:

1. **引数の順序を正規化する**（`left > right` なら入れ替える）。`SequenceMatcher` は
   第2引数側を索引化するため、まれに `ratio(a, b) != ratio(b, a)` になる。比較の
   呼び順で結果が変わると「実行のたびに採否が変わる」ので、常に同じ向きで測る。
2. **`autojunk=False`**。既定の自動 junk 判定は長さ200以上の系列で「頻出要素」を
   無視するため、**長い見出しだけ別の尺度になる**。長さで挙動が変わらないようにする。

---

**⚠️ 参照範囲は「当該 period を含めない」**（§14 冪等性）

`{period}` の再実行はありうる（設計判断B）。もし履歴に自分自身の週シートを含める
と、**2回目の実行で全記事が「既出」になって全滅する**。したがって
`weekly_periods_in_scope()` は対象期間の**手前** `lookback_weeks` 週だけを返す。
同じ実行の中で先に採用した記事との突き合わせは `deduplicate()` が別に行う。
"""

import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from enterprise.entities.config import DedupThresholds
from enterprise.entities.period import (
    PeriodError,
    monthly_period_of,
    monthly_periods_including,
    preceding_weekly_periods,
    weekly_period_of,
)
from enterprise.entities.raw_article import RawArticle
from enterprise.entities.report_columns import (
    EXCLUSION_LOG_COLUMNS,
    SOURCE_MERGE_SEPARATOR,
    format_row,
)

# 統合された記事の媒体名に付ける印。仕様書 §11.3 の `A / B(統合)`（丸括弧は ASCII）。
MERGE_SUFFIX = "(統合)"

# 除外ログの `除外区分` / `除外理由`（仕様書 §11.3 の確定値）。
# `除外理由` は除外ルール12の `name` と同じ文字列
# （`test_the_duplicate_reason_matches_the_merge_rule_name` が対応を固定している）。
CATEGORY_MERGED = "統合"
REASON_DUPLICATE = "重複・転載記事"

# period の表記・実日付への展開・週/月の割り出しは `enterprise.entities.period` が
# 唯一の定義（T-21 で3箇所にあった写しを寄せた）。`weekly_period_of` /
# `monthly_period_of` は履歴を組み立てる側（T-21 / T-22）がここから使う流れなので
# 再輸出する（`__all__` に載せているので import は未使用ではない）。
__all__ = [
    "CATEGORY_MERGED",
    "MERGE_SUFFIX",
    "REASON_DUPLICATE",
    "DedupError",
    "DedupHistory",
    "DedupResult",
    "DuplicateRecord",
    "DuplicateVerdict",
    "KnownArticle",
    "KnownOrigin",
    "MatchedBy",
    "Representative",
    "deduplicate",
    "detect_duplicate",
    "duplicate_log_entry",
    "duplicate_log_row",
    "merged_source_text",
    "monthly_period_of",
    "monthly_periods_in_scope",
    "normalize_title",
    "normalize_url",
    "title_similarity",
    "weekly_period_of",
    "weekly_periods_in_scope",
]


class DedupError(Exception):
    """重複判定に使えない入力（period 表記の誤りなど）。"""


# --- 正規化（仕様書 §11.2）--------------------------------------------------


def normalize_url(url: str) -> str:
    """URL を比較用に正規化する（クエリ・トラッキング除去、末尾スラッシュ統一）。

    やること:

    - **クエリ文字列とフラグメントを丸ごと落とす。** §11.2 の「クエリ・トラッキング
      除去」をそのまま採る。トラッキング用のキー名（`utm_*` / `gclid` / …）を列挙
      して消す方式は、**列挙から漏れたキーが残ると同じ記事が別物になる**うえ、
      一覧の保守が要る
    - スキームとホストを小文字化（URL 標準上、大小を区別しない部分）
    - パス末尾の `/` を落とす（`.../news/` と `.../news` を同一視）

    やらないこと（§11.2 に無いので推測で足さない）:

    - `http` と `https` の同一視、`www.` の有無の同一視、パーセントエンコーディングの
      復元。いずれも「別 URL を同じとみなす」方向の操作で、間違えると**別の記事が
      消える**

    ⚠️ **クエリを落とすため、クエリで記事を識別するサイト**（`?id=123` 形式）では
    別記事が同じ URL に潰れうる。その場合は同一発表とみなされ、片方が除外ログの
    `統合` へ回る。実データで問題になったら、**config にクエリキーの許可リストを
    足す**のが筋（ここへハードコードしない）。URL 一致そのものは
    `treat_same_url_as_duplicate=false` で止められる（§5.2 の可変項目）。

    Args:
        url: 収集したままの URL

    Returns:
        正規化した URL。比較にしか使わない（除外ログには収集したままを書く）
    """
    parts = urlsplit(url.strip())
    normalized = urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, "", "")
    )
    return normalized.rstrip("/")


def normalize_title(title: str) -> str:
    """タイトルを比較用に正規化する（記号・空白除去、全半角統一）。

    1. **NFKC 正規化**で全角/半角を寄せる（`ＡＩ`→`AI`、`ｶﾞ`→`ガ`、`㈱`→`(株)`）
    2. **記号・区切り・空白をすべて落とす**（Unicode 一般カテゴリが `P*`（句読点）/
       `S*`（記号）/ `Z*`（空白）のもの、および制御文字）。残すのは文字・数字・
       結合記号だけ
    3. **大文字小文字を同一視**（`casefold`）

    3 は §11.2 に明記が無い判断。同じ見出しの `OpenAI` / `OPENAI` を別物と数えると
    類似度が下がるだけで、**別記事を同じとみなす方向には働かない**ため入れた。

    べき等（正規化済みの文字列を渡しても結果は変わらない）。

    Args:
        title: 収集したままのタイトル

    Returns:
        正規化したタイトル。記号だけの見出しなら空文字になりうる
    """
    folded = unicodedata.normalize("NFKC", title).casefold()
    return "".join(
        char
        for char in folded
        if unicodedata.category(char)[0] not in ("P", "S", "Z", "C")
    )


def title_similarity(left: str, right: str) -> float:
    """タイトル類似度（0.0〜1.0）。内部で正規化してから測る。

    算出方法とその選定理由、決定性のための2つの設定はモジュール docstring を参照。

    Args:
        left: 比較するタイトル（正規化前でも後でもよい。正規化はべき等）
        right: 同上

    Returns:
        類似度。**どちらかの正規化結果が空なら 0.0**（記号だけの見出し同士が
        「完全一致」になって別記事を巻き込むのを防ぐ）
    """
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    # 呼び順で結果が変わらないよう、常に同じ向きにそろえてから測る。
    if a > b:
        a, b = b, a
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


# --- 参照範囲（仕様書 §11.1）------------------------------------------------


def weekly_periods_in_scope(period: str, lookback_weeks: int) -> list[str]:
    """週次の参照範囲（§11.1「直近 `lookback_weeks` 週」）。

    ⚠️ **対象期間そのものは含まない。** 含めると再実行で自分の出力と突き合わせて
    しまい、2回目に全記事が「既出」になる（§14 冪等性。モジュール docstring）。

    Args:
        period: 対象週（`2026-W31`）
        lookback_weeks: 遡る週数（config の `dedup.lookback_weeks`。既定8）

    Returns:
        新しい週から順に並べた period の一覧（`lookback_weeks` 件）

    Raises:
        DedupError: period が週次の表記でない、または実在しない週の場合
    """
    try:
        return preceding_weekly_periods(period, lookback_weeks)
    except PeriodError as exc:
        # 層をまたいで例外型を漏らさない（呼び出し側は工程で失敗を判別する）。
        raise DedupError(str(exc)) from exc


def monthly_periods_in_scope(period: str, lookback_months: int) -> list[str]:
    """月次の参照範囲（§11.1「当月＋直近数ヶ月」）。

    ⚠️ **「直近数ヶ月」の月数は仕様書にも設計書にも数字が無い。** ここで決め打ち
    すると「config に無いしきい値」が生まれるため、`lookback_months` は必ず
    呼び出し側から受け取る。2026-08-16 の決定2 で config に
    `tunable_thresholds.dedup.monthly_lookback_months`（既定3）が入ったので、
    **T-21 はその値を渡す**（この層は渡された値に従うだけ、という関係は変えない）。

    §11.1 は月次だけ **当月を含む**（当月の cases と対応する週次記事を見る）。
    ただし当月の cases は当該実行の出力そのものなので、**再実行時に自分の出力を
    履歴へ入れないのは呼び出し側（T-21）の責任**（`DedupHistory` は渡された
    ものしか見ない）。

    Args:
        period: 対象月（`2026-07`）
        lookback_months: 当月より前に遡る月数

    Returns:
        当月から順に古い方へ並べた period の一覧（`lookback_months + 1` 件）

    Raises:
        DedupError: period が月次の表記でない場合
    """
    try:
        return monthly_periods_including(period, lookback_months)
    except PeriodError as exc:
        raise DedupError(str(exc)) from exc


# --- 履歴 -------------------------------------------------------------------


class KnownOrigin(StrEnum):
    """既出記事がどこから来たか（§11.1 の参照先）。"""

    PUBLISHED = "published"
    """本編に載った記事（週次シート／月次 cases）。"""

    EXCLUDED = "excluded"
    """除外ログに記録された記事。§11.1 が参照先に含めている。"""


class KnownArticle(BaseModel):
    """既出記事1件（履歴の要素）。

    xlsx の読み戻し（T-22 のリーダ）と突き合わせやすいよう、除外ログ6列にも週次
    22列にも共通で存在する項目だけを持つ。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    url: str
    source: str = ""
    period: str
    """どの週／月のものか。参照範囲の絞り込み（§11.1）に使う。"""

    origin: KnownOrigin = KnownOrigin.PUBLISHED


class DedupHistory:
    """突き合わせ先の既出記事の集まり。

    **順序に意味がある。** 設計書 §6.3 は「最初に類似が見つかった1件」を代表に
    するので、渡した順がそのまま優先順になる。呼び出し側は安定した順序
    （例: 新しい週から順）で渡すこと。
    """

    def __init__(self, entries: Iterable[KnownArticle] = ()) -> None:
        self._entries: list[KnownArticle] = []
        self._url_index: dict[str, int] = {}
        for entry in entries:
            self.append(entry)

    @property
    def entries(self) -> tuple[KnownArticle, ...]:
        return tuple(self._entries)

    def append(self, entry: KnownArticle) -> int:
        """1件足して、その位置（索引）を返す。

        URL 索引は**先勝ち**（同じ正規化 URL が複数あれば最初のものが代表）。
        """
        index = len(self._entries)
        self._entries.append(entry)
        normalized = normalize_url(entry.url)
        if normalized and normalized not in self._url_index:
            self._url_index[normalized] = index
        return index

    def index_by_url(self, url: str) -> int | None:
        """正規化 URL が一致する既出記事の位置。無ければ `None`。"""
        normalized = normalize_url(url)
        return self._url_index.get(normalized) if normalized else None

    def in_scope(self, periods: Sequence[str]) -> "DedupHistory":
        """参照範囲の period だけに絞った履歴を返す（順序は保つ）。

        Args:
            periods: `weekly_periods_in_scope()` 等が返す period の一覧
        """
        allowed = set(periods)
        return DedupHistory(entry for entry in self._entries if entry.period in allowed)

    def __len__(self) -> int:
        return len(self._entries)


# --- 判定 -------------------------------------------------------------------


class MatchedBy(StrEnum):
    """何で重複と判定したか（§11.2 の1と2）。"""

    URL = "url"
    TITLE = "title"


@dataclass(frozen=True, slots=True)
class DuplicateVerdict:
    """重複判定の結果（設計書 §6.3 の戻り値）。"""

    is_duplicate: bool
    representative: KnownArticle | None = None
    """統合先の代表記事。"""

    representative_index: int | None = None
    """履歴の中での代表の位置（同じ実行内で採用した記事へ辿るのに使う）。"""

    matched_by: MatchedBy | None = None
    similarity: float | None = None
    """タイトル一致のときの類似度。URL 一致のときは `None`（測っていない）。"""


def detect_duplicate(
    article: RawArticle, history: DedupHistory, dedup: DedupThresholds
) -> DuplicateVerdict:
    """既出記事と突き合わせる（設計書 §6.3・仕様書 §11.2）。

    1. `treat_same_url_as_duplicate=true` なら、**正規化 URL 一致**を重複とする
    2. 次に、正規化タイトルの類似度 ≥ `title_similarity_threshold` を重複候補とする。
       **先に当たった1件**を代表にする（履歴の順序＝優先順）

    Args:
        article: 判定対象（今回収集した記事）
        history: 参照範囲に絞った既出記事（§11.1）
        dedup: config の `tunable_thresholds.dedup`

    Returns:
        重複なら代表つきの判定、そうでなければ `is_duplicate=False`
    """
    if dedup.treat_same_url_as_duplicate:
        index = history.index_by_url(article.url)
        if index is not None:
            return DuplicateVerdict(
                is_duplicate=True,
                representative=history.entries[index],
                representative_index=index,
                matched_by=MatchedBy.URL,
            )

    for index, known in enumerate(history.entries):
        similarity = title_similarity(article.title, known.title)
        if similarity >= dedup.title_similarity_threshold:
            return DuplicateVerdict(
                is_duplicate=True,
                representative=known,
                representative_index=index,
                matched_by=MatchedBy.TITLE,
                similarity=similarity,
            )

    return DuplicateVerdict(is_duplicate=False)


# --- 統合 -------------------------------------------------------------------


def merged_source_text(sources: Sequence[str]) -> str:
    """代表の `ソース` 欄を組み立てる（仕様書 §11.3 `A / B(統合)`）。

    先頭が代表の媒体、2件目以降が統合された媒体で `(統合)` を付ける。区切りは
    T-07 の `SOURCE_MERGE_SEPARATOR`。

    **同じ媒体名は重ねない**（`A / A(統合)` にしない）。同一媒体の再掲を統合した
    場合に媒体名が2つあるように読めてしまうため。

    Args:
        sources: 代表の媒体名で始まる媒体名の列

    Returns:
        `ソース` 欄の文字列

    Raises:
        DedupError: 空の列を渡した場合
    """
    if not sources:
        raise DedupError("代表の媒体名がありません")

    parts = [sources[0]]
    seen = {sources[0]}
    for source in sources[1:]:
        if source in seen:
            continue
        seen.add(source)
        parts.append(f"{source}{MERGE_SUFFIX}")
    return SOURCE_MERGE_SEPARATOR.join(parts)


@dataclass(slots=True)
class Representative:
    """統合先として残る記事1件。"""

    article: RawArticle
    sources: list[str] = field(default_factory=list)
    """代表の媒体名で始まる媒体名の列（統合されるたびに増える）。"""

    merged_count: int = 0
    """統合した件数（0 なら単独）。"""

    def __post_init__(self) -> None:
        if not self.sources:
            self.sources = [self.article.source]

    def merge(self, other: RawArticle) -> None:
        """別媒体の同一発表を吸収する（§11.3）。"""
        self.sources.append(other.source)
        self.merged_count += 1

    @property
    def source_text(self) -> str:
        """xlsx の `ソース` 欄へ書く文字列。"""
        return merged_source_text(self.sources)


@dataclass(frozen=True, slots=True)
class DuplicateRecord:
    """重複として本編から外した記事1件（除外ログ行の元）。"""

    article: RawArticle
    verdict: DuplicateVerdict


@dataclass(frozen=True, slots=True)
class DedupResult:
    """`deduplicate()` の結果。"""

    representatives: list[Representative]
    """本編へ残る記事（入力順）。"""

    duplicates: list[DuplicateRecord]
    """除外ログへ回す記事（入力順）。"""


def deduplicate(
    articles: Sequence[RawArticle],
    history: DedupHistory,
    dedup: DedupThresholds,
    *,
    period: str,
) -> DedupResult:
    """今回の記事群を重複判定し、代表へ統合する（設計書 §6.1 の 2）。

    **既出記事との突き合わせと、同じ実行内の記事同士の突き合わせを両方行う。**
    crawl は同一発表の別媒体記事を落とさない（§13.2）ので、後者が無いと同じ発表が
    本編に2件並ぶ。採用した記事はその場で履歴へ積み、後続の記事から見えるようにする。

    代表が**今回の記事**なら `ソース` 欄へ媒体名を足す（`A / B(統合)`）。代表が
    **過去週の既出記事**の場合は、過去のシートを書き換えない——重複した記事を
    除外ログへ記録するところまでが §11.3 の要求で、過去の成果物の更新は
    冪等性（§14）の扱いが別途要るため。どちらだったかは
    `DuplicateRecord.verdict.representative.period` で分かる。

    Args:
        articles: 今回収集した記事（入力順を保つ）
        history: 参照範囲に絞った既出記事（**対象 period を含めない**）
        dedup: config の `tunable_thresholds.dedup`
        period: 対象期間。今回採用した記事を履歴へ積むときの印

    Returns:
        代表の一覧と、除外ログへ回す重複の一覧
    """
    working = DedupHistory(history.entries)
    representatives: list[Representative] = []
    duplicates: list[DuplicateRecord] = []
    representative_at: dict[int, Representative] = {}

    for article in articles:
        verdict = detect_duplicate(article, working, dedup)
        if verdict.is_duplicate:
            assert verdict.representative_index is not None  # is_duplicate が保証
            if target := representative_at.get(verdict.representative_index):
                target.merge(article)
            duplicates.append(DuplicateRecord(article=article, verdict=verdict))
            continue

        representative = Representative(article=article)
        representatives.append(representative)
        index = working.append(
            KnownArticle(
                title=article.title,
                url=article.url,
                source=article.source,
                period=period,
                origin=KnownOrigin.PUBLISHED,
            )
        )
        representative_at[index] = representative

    return DedupResult(representatives=representatives, duplicates=duplicates)


# --- 除外ログ（6列）---------------------------------------------------------


def duplicate_log_entry(record: DuplicateRecord) -> dict[str, Any]:
    """重複記事の除外ログ1行（仕様書 §11.3）。

    `除外区分=統合` / `除外理由=重複・転載記事` は §11.3 の確定値。URL は収集した
    ままを書く（正規化は比較のための内部処理）。
    """
    article = record.article
    return {
        "収集日": article.collected_at,
        "タイトル": article.title,
        "URL": article.url,
        "ソース": article.source,
        "除外区分": CATEGORY_MERGED,
        "除外理由": REASON_DUPLICATE,
    }


def duplicate_log_row(record: DuplicateRecord) -> list[str | int | None]:
    """重複記事の除外ログ1行を xlsx の列順（6列）で組み立てる。"""
    return format_row(EXCLUSION_LOG_COLUMNS, duplicate_log_entry(record))
