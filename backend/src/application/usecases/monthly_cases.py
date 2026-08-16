"""月次：事例(case)への昇格と章の束ね（仕様書 §8.2・§13.3 の出力1 ／ T-21）。

月刊ビリーフの中間xlsx（8列）は、週次と同じ分類・採点を通った採用記事のうち
**「企業・組織の具体的活用事例」を事例へ昇格**させ、章（トピック）に束ねたもの。
この モジュールはその昇格と章立て、各事例の「解説」（3段落）を担う。

---

**昇格の判定は決定的**（AI に「これは活用事例か」を聞かない）

「企業・組織の具体的活用事例」は config の情報カテゴリ **`enterprise_ai_case`**
（ラベル「企業AI活用事例」／説明「国内外企業によるAI導入・活用・業務改善・社内展開の
具体事例」）そのもの。カテゴリは T-19 が config の候補から選んで付けているので、
ここで改めて AI に聞くと **config と無関係な2つ目の定義**が生まれる（§5.1「config が
唯一の判断基準」に反する）。したがって昇格の条件は次の3つだけで、すべて config 由来:

1. 情報カテゴリが `enterprise_ai_case`
2. 合計スコア ≥ `tunable_thresholds.monthly.min_score_for_case`
3. 合計スコア降順の上位 `tunable_thresholds.monthly.target_case_count` 件

---

**AI に頼むのは「書けないもの」だけ**

| AI が作る | アプリが決める |
|---|---|
| 企業・組織（本文からの抽出） | `No`（章順の通し番号） |
| 事例見出し | 章番号と `第N章 <章タイトル>` の体裁 |
| 章のテーマ名とその束ね方 | 昇格するかどうか・何件までか |
| 解説の3段落（①事実 ②詳細 ③示唆） | 段落の連結（`\\n\\n`）・`出典`・`掲載月`・`URL` |

⚠️ **`出典` は AI に書かせない。** `媒体（日付）` は収集済みの事実（`source` /
`published_at`）で、聞けば創作の余地が生まれるだけ。

⚠️ **解説は3つの別フィールドで受け取る。** 「`\\n\\n` 区切りで3段落」と文章で頼むと
2段落・4段落が返りうるので、**段落数を構造で固定**し、連結はアプリが行う
（区切りは T-07 の `PARAGRAPH_SEPARATOR`）。

---

**章の束ね方**（AI の出力が欠けても事例を落とさない形）

事例ごとに `chapter_theme`（自由記述のテーマ名）を書かせ、テーマが
`chapter_count_hint` より多いときだけ **「テーマを章へまとめ直す」1往復**を足す。
返るのは「章タイトル → そこへ入れるテーマ名」の対応表で、**割り当てから漏れた
テーマは自分自身を章名として末尾に残す**。事例そのものの取りこぼしが起きない
（分割の網羅性を AI の出力に依存させない）ようにするための形。
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, create_model

from adapter.llm import AIClient
from adapter.llm.ai_client import AICallMeta
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.raw_article import RawArticle
from enterprise.entities.report_columns import (
    MONTHLY_CASE_COLUMNS,
    format_row,
)

logger = logging.getLogger(__name__)

# プロンプトの版（T-30 で `prompts/` へ切り出す前提の暫定置き場）。
# ⚠️ **本文を変えたら版も上げること**（§9.2 の再現性要件）。
CASE_PROMPT_NAME = "PROMPT-2/monthly_case"
CASE_PROMPT_VERSION = "0.1.0"
CHAPTER_PROMPT_NAME = "PROMPT-2/monthly_chapters"
CHAPTER_PROMPT_VERSION = "0.1.0"

# 事例へ昇格させる情報カテゴリ（§13.3 出力1「企業・組織の具体的活用事例」）。
# ⚠️ 文字列ではなく **config の7カテゴリのID** を指している。カテゴリの
# ラベルや説明が変わっても、事例の定義は config 側で1つに保たれる。
CASE_CATEGORY_ID = "enterprise_ai_case"

# 参照する週次22列（列名の正は T-07。ここは「どの列を見るか」だけを持つ）。
COLUMN_CATEGORY = "情報カテゴリ"
COLUMN_TOTAL_SCORE = "合計スコア"

# 章見出しの体裁（仕様書 §8.2「`第N章 <章タイトル>`」）。
CHAPTER_LABEL_FORMAT = "第{number}章 {title}"

# `出典` の体裁（仕様書 §8.2「`媒体（日付）／ プレスリリース` 形式」）。
SOURCE_WITH_DATE_FORMAT = "{source}（{published_at}）"

_NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_STRICT_OUTPUT = ConfigDict(extra="forbid")


class MonthlyCaseError(Exception):
    """月次の事例組み立てに使えない入力。"""


class CaseDraft(BaseModel):
    """1事例ぶんの AI 出力（§8.2 の列のうち、記事から書き起こす必要があるもの）。

    ⚠️ **`出典` / `掲載月` / `URL` / `No` のフィールドは無い。** どれも収集済みの
    事実か、アプリが決める通し番号（モジュール docstring の表）。
    """

    model_config = _STRICT_OUTPUT

    organizations: list[_NonEmptyText] = Field(
        min_length=1, description="主体となる企業・組織名（複数可）"
    )
    case_title: _NonEmptyText = Field(description="事例見出し")
    chapter_theme: _NonEmptyText = Field(
        description="この事例が属するテーマ（章の候補。短い日本語の名詞句）"
    )
    commentary_fact: _NonEmptyText = Field(description="解説①事実：何が起きたか")
    commentary_detail: _NonEmptyText = Field(
        description="解説②詳細：どう取り組んだか・数字や仕組み"
    )
    commentary_implication: _NonEmptyText = Field(
        description="解説③示唆：読者が持ち帰れること"
    )


@dataclass(frozen=True, slots=True)
class MonthlyCase:
    """月次8列の1行になる事例（`No` と章は束ね終わった後に確定する）。

    Attributes:
        no: 通し番号（1〜）。**章順に並べた後の位置**（§8.2「昇順＝章グルーピング順」）
        chapter: `第N章 <章タイトル>`
        organizations: 企業・組織（複数可）
        title: 事例見出し
        url: 記事URL（収集したまま）
        source_text: `出典`（`媒体（日付）`）
        month: `掲載月`（`YYYY-MM`）
        paragraphs: 解説の3段落（①事実 ②詳細 ③示唆）
        article: 元の記事（監査・突き合わせ用）
    """

    no: int
    chapter: str
    organizations: tuple[str, ...]
    title: str
    url: str
    source_text: str
    month: str
    paragraphs: tuple[str, str, str]
    article: RawArticle

    def to_row(self) -> dict[str, Any]:
        """月次8列の行（列名 → 値）。列順を知らずに済むよう名前で組み立てる。"""
        return {
            "No": self.no,
            "トピック(章)": self.chapter,
            "企業・組織": list(self.organizations),
            "タイトル": self.title,
            "URL": self.url,
            "出典": self.source_text,
            "掲載月": self.month,
            # PARAGRAPHS 列。連結（`\n\n`）は T-07 の `format_cell` が行う。
            "解説": list(self.paragraphs),
        }


@dataclass(frozen=True, slots=True)
class CaseCandidate:
    """昇格の判定を通った記事1件（AI へ渡す前の状態）。

    Attributes:
        article: 元の記事
        total_score: 合計スコア（降順に並べる鍵）
        summary: 一言要約（AI への手がかり）
    """

    article: RawArticle
    total_score: int
    summary: str


@dataclass(frozen=True, slots=True)
class CaseSelection:
    """昇格の判定の結果と**その内訳**（T-46 Step 2 の診断ログ）。

    初運用（2026-07）で事例が0件になったとき、「カテゴリ該当が0件なのか、
    `min_score_for_case` で落ちたのか、`target_case_count` の絞りなのか」を
    後から診断できなかった。**3条件それぞれの通過件数を数えておく**のが
    この型の目的で、条件そのものは `select_cases()` の1箇所にしかない
    （内訳を別関数で数え直すと、条件の写しが2つになる）。

    Attributes:
        indexes: 昇格させる記事の位置（合計スコア降順・`target_case_count` 件まで）
        category_matched: 情報カテゴリが `enterprise_ai_case` の件数
        above_min_score: うち合計スコアが `min_score_for_case` 以上の件数
        dropped_by_target_count: `target_case_count` の絞りで落ちた件数
    """

    indexes: tuple[int, ...]
    category_matched: int
    above_min_score: int

    @property
    def dropped_by_target_count(self) -> int:
        return self.above_min_score - len(self.indexes)


def select_cases(
    records: Sequence[Mapping[str, Any]],
    articles: Sequence[RawArticle],
    config: IntelligenceConfig,
) -> CaseSelection:
    """事例へ昇格させる記事の位置と内訳を決める（決定的。モジュール docstring）。

    Args:
        records: 週次22列の行（採用済み・合計スコア降順で渡すこと）
        articles: `records` と同じ並びの元記事
        config: 実行時 config

    Returns:
        昇格させる位置と、3条件それぞれの通過件数

    Raises:
        MonthlyCaseError: `records` と `articles` の件数が食い違う場合
    """
    if len(records) != len(articles):
        raise MonthlyCaseError(
            f"行と記事の件数が違います（行 {len(records)} / 記事 {len(articles)}）"
        )

    monthly = config.tunable_thresholds.monthly
    category_matched = 0
    scored: list[tuple[int, int]] = []
    for index, record in enumerate(records):
        if record.get(COLUMN_CATEGORY) != CASE_CATEGORY_ID:
            continue
        category_matched += 1
        total = record.get(COLUMN_TOTAL_SCORE)
        if not isinstance(total, int) or isinstance(total, bool):
            continue
        if total < monthly.min_score_for_case:
            continue
        scored.append((index, total))

    # 合計スコア降順。同点は元の並び（＝採用側の降順）を保つ＝安定ソート。
    scored.sort(key=lambda item: -item[1])
    return CaseSelection(
        indexes=tuple(index for index, _ in scored[: monthly.target_case_count]),
        category_matched=category_matched,
        above_min_score=len(scored),
    )


def select_case_candidates(
    records: Sequence[Mapping[str, Any]],
    articles: Sequence[RawArticle],
    config: IntelligenceConfig,
) -> list[int]:
    """`select_cases()` の位置だけを取る口（内訳が要らない呼び出し向け）。"""
    return list(select_cases(records, articles, config).indexes)


class MonthlyCaseBuilder:
    """事例の本文を AI に書かせ、章へ束ねて月次8列の行にする。

    `config` は実行開始時に固定参照しているものを渡すこと（§6.3）。
    """

    def __init__(
        self,
        *,
        client: AIClient,
        config: IntelligenceConfig,
        timeout: float | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._timeout = timeout
        self._metas: list[AICallMeta] = []

    @property
    def ai_calls(self) -> tuple[AICallMeta, ...]:
        """行った AI 呼び出しのメタ（監査・validation メタ用）。"""
        return tuple(self._metas)

    async def build(
        self, candidates: Sequence[CaseCandidate], *, period: str
    ) -> list[MonthlyCase]:
        """候補を事例へ組み立てる（1件1往復 ＋ 章の束ね直しで最大1往復）。

        Args:
            candidates: 昇格の判定を通った記事（合計スコア降順）
            period: 対象月（`YYYY-MM`）。`掲載月` に入る

        Returns:
            `No` 昇順・章ごとに連続した事例の一覧

        Raises:
            AIClientError: AI 呼び出しの失敗（握り潰さない）
        """
        if not candidates:
            return []

        drafts = [await self._draft(candidate) for candidate in candidates]
        chapters = await self._group_into_chapters(
            [draft.chapter_theme for draft in drafts]
        )
        return self._assemble(candidates, drafts, chapters, period=period)

    async def _draft(self, candidate: CaseCandidate) -> CaseDraft:
        """事例1件の本文を書かせる。"""
        result = await self._client.complete(
            prompt=build_case_prompt(candidate, self._config),
            output_schema=CaseDraft,
            prompt_version=CASE_PROMPT_VERSION,
            timeout=self._timeout,
        )
        self._metas.append(result.meta)
        return result.value

    async def _group_into_chapters(self, themes: Sequence[str]) -> list[str]:
        """テーマの並びを章タイトルの並びへ写す（テーマと同じ長さ）。

        章数が `chapter_count_hint` 以下ならまとめ直す必要が無いので **AI を
        呼ばない**（1回あたり数分。無駄打ちしない）。
        """
        hint = self._config.tunable_thresholds.monthly.chapter_count_hint
        distinct = list(dict.fromkeys(themes))
        if hint <= 0 or len(distinct) <= hint:
            return list(themes)

        result = await self._client.complete(
            prompt=build_chapter_prompt(distinct, hint),
            output_schema=build_chapter_schema(distinct),
            prompt_version=CHAPTER_PROMPT_VERSION,
            timeout=self._timeout,
        )
        self._metas.append(result.meta)

        # テーマ → 章タイトル。⚠️ **先に割り当てた章が勝つ**（同じテーマが複数の章に
        # 現れても、事例が2つの章に分かれない）。
        mapping: dict[str, str] = {}
        # スキーマは `build_chapter_schema()` が動的に組み立てたモデル（型注釈は
        # `BaseModel` までしか付かない）。
        for chapter in result.value.chapters:  # ty: ignore[unresolved-attribute]
            for theme in chapter.themes:
                mapping.setdefault(theme, chapter.title)

        if missing := [theme for theme in distinct if theme not in mapping]:
            # ⚠️ **落とさない**（漏れたテーマは自分自身を章名にする）。章立ての
            # 網羅性を AI の出力に依存させると、事例そのものが消える形の失敗に
            # なるため。件数はログに出す（静かに劣化させない）。
            logger.warning(
                "章の割り当てから漏れたテーマをそのまま章にしました: %s", missing
            )
        return [mapping.get(theme, theme) for theme in themes]

    def _assemble(
        self,
        candidates: Sequence[CaseCandidate],
        drafts: Sequence[CaseDraft],
        chapter_titles: Sequence[str],
        *,
        period: str,
    ) -> list[MonthlyCase]:
        """章ごとに並べ直し、`第N章` と `No` を打つ（§8.2）。

        章の順序は**最初にその章が現れた事例の位置**（＝合計スコアの高い事例が
        属する章が先）。同じ章の事例は連続配置（§8.2 の要求）。
        """
        order: list[str] = list(dict.fromkeys(chapter_titles))
        numbered = {
            title: CHAPTER_LABEL_FORMAT.format(number=index + 1, title=title)
            for index, title in enumerate(order)
        }

        cases: list[MonthlyCase] = []
        no = 0
        for title in order:
            for candidate, draft, chapter in zip(
                candidates, drafts, chapter_titles, strict=True
            ):
                if chapter != title:
                    continue
                no += 1
                cases.append(
                    MonthlyCase(
                        no=no,
                        chapter=numbered[title],
                        organizations=tuple(dict.fromkeys(draft.organizations)),
                        title=draft.case_title,
                        url=candidate.article.url,
                        source_text=source_text_of(candidate.article),
                        month=period,
                        paragraphs=(
                            draft.commentary_fact,
                            draft.commentary_detail,
                            draft.commentary_implication,
                        ),
                        article=candidate.article,
                    )
                )
        return cases


def source_text_of(article: RawArticle) -> str:
    """`出典` 欄（仕様書 §8.2「`媒体（日付）／ プレスリリース` 形式」）。

    ⚠️ **AI に書かせない**（媒体名も日付も収集済みの事実）。公開日が分からない
    記事は媒体名だけにする（**収集日で代用しない**。収集日は「いつ拾ったか」で
    あって発表日ではない）。
    """
    if article.published_at is None:
        return article.source
    return SOURCE_WITH_DATE_FORMAT.format(
        source=article.source, published_at=article.published_at
    )


def case_row(case: MonthlyCase) -> list[str | int | None]:
    """月次8列の1行を xlsx の列順で組み立てる（T-22 のライタへ渡す形）。"""
    return format_row(MONTHLY_CASE_COLUMNS, case.to_row())


def build_chapter_schema(themes: Sequence[str]) -> type[BaseModel]:
    """章の束ね直しの出力スキーマ。

    ⚠️ **テーマ名は `Literal`**（渡したテーマ以外を構造的に出せない）。自由文字列に
    すると、少し言い換えたテーマ名が返って**どの事例にも対応しない章**ができる。
    """
    if not themes:
        raise MonthlyCaseError("束ねるテーマがありません")

    chapter_model = create_model(
        "ChapterGroup",
        __config__=_STRICT_OUTPUT,
        title=(_NonEmptyText, Field(description="章タイトル（`第N章` は付けない）")),
        themes=(
            list[Literal[tuple(dict.fromkeys(themes))]],  # ty: ignore[invalid-type-form]
            Field(min_length=1, description="この章へ入れるテーマ名"),
        ),
    )
    return create_model(
        "ChapterGrouping",
        __config__=_STRICT_OUTPUT,
        chapters=(
            list[chapter_model],  # ty: ignore[invalid-type-form]
            Field(min_length=1),
        ),
    )


def build_case_prompt(candidate: CaseCandidate, config: IntelligenceConfig) -> str:
    """事例1件の本文を書かせるプロンプト（仕様書 §8.2・§10.2）。

    ⚠️ **出力形式（JSON だけを出せ・JSON Schema）の指示は含めない。**
    `AIClient` の実装が付ける（他のプロンプトと同じ）。
    """
    monthly = config.tunable_thresholds.monthly
    article = candidate.article
    return "\n".join(
        [
            "あなたは月刊AIレポート（月刊ビリーフ）の編集者です。"
            "次の記事を「先進企業のAI活用事例」として紹介する原稿を書いてください。",
            "",
            "■ 厳守事項",
            "- 記事に書かれている事実だけを使う。数字・固有名詞を創作しない。",
            "- 分からないことは書かない（推測で補わない）。",
            "- URL・出典・掲載月・通し番号は**書かない**（アプリ側が埋める）。",
            "",
            "■ 対象記事",
            article.model_dump_json(indent=2),
            "",
            f"■ 一言要約（分類時の要約。合計スコア {candidate.total_score} 点）",
            candidate.summary,
            "",
            "■ 書くもの",
            "- organizations: 事例の主体となる企業・組織名（記事に出てくるもの）。",
            "- case_title: 事例の見出し（何をした事例かが一読で分かる短い日本語）。",
            "- chapter_theme: この事例が属するテーマ。"
            f"月全体で{monthly.chapter_count_hint}前後の章に束ねるので、"
            "**他の事例とも共有できる粒度**の名詞句にする。",
            "- commentary_fact: ①事実（何が起きたか）。",
            "- commentary_detail: ②詳細（どう取り組んだか・仕組み・数字）。",
            "- commentary_implication: ③示唆（読者が自社へ持ち帰れること）。",
            "",
            "解説の3段落はそれぞれ独立した段落として書く"
            "（アプリ側が段落として連結する。段落記号や見出しを文中に入れない）。",
        ]
    )


def build_chapter_prompt(themes: Sequence[str], chapter_count_hint: int) -> str:
    """テーマを章へ束ね直すプロンプト（仕様書 §13.3 出力1「章を5前後に束ねる」）。"""
    return "\n".join(
        [
            "あなたは月刊AIレポート（月刊ビリーフ）の編集者です。"
            f"次のテーマを、意味の近いものどうしで**{chapter_count_hint}前後の章**へ"
            "束ね直してください。",
            "",
            "■ 厳守事項",
            "- 与えたテーマ名は**そのままの文字列**で使う（言い換えない）。",
            "- **すべてのテーマをどれかの章へ入れる**（重複させない）。",
            "- 章タイトルは短い日本語の名詞句。"
            "`第N章` は付けない（アプリ側が付ける）。",
            "",
            "■ テーマ",
            *(f"- {theme}" for theme in themes),
        ]
    )


__all__ = [
    "CASE_CATEGORY_ID",
    "CASE_PROMPT_NAME",
    "CASE_PROMPT_VERSION",
    "CHAPTER_LABEL_FORMAT",
    "CHAPTER_PROMPT_NAME",
    "CHAPTER_PROMPT_VERSION",
    "COLUMN_CATEGORY",
    "COLUMN_TOTAL_SCORE",
    "CaseCandidate",
    "CaseDraft",
    "CaseSelection",
    "MonthlyCase",
    "MonthlyCaseBuilder",
    "MonthlyCaseError",
    "build_case_prompt",
    "build_chapter_prompt",
    "build_chapter_schema",
    "case_row",
    "select_case_candidates",
    "select_cases",
    "source_text_of",
]
