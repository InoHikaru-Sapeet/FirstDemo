"""生成テキスト（narrative）の生成（2026-08-16 の決定3 ／ T-44）。

設計書 §7.3・§7.4 が「（生成テキスト）」としている項目を作る:

| 作るもの | 根拠 | 渡す先 |
|---|---|---|
| 今週のポイント（3〜4項目＝見出し＋詳細1段落） | §9.2-2 | `…point_of_week` |
| 記事ごとの示唆（「自社ではどう捉えるか」1段落） | §9.2-4 | `…insights` |
| 巻頭言（サブ見出し＋総論3段落） | §10.2-2 | `MonthlyNarrative.editorial*` |
| 章ごとの導入文 | §10.2-4 | `MonthlyNarrative.chapter_intros` |
| むすび（今月の総括＋来月の視点の2段落） | §10.2-5 | `MonthlyNarrative.closing` |
| **図解の内容**（記事／事例ごとに0〜1個） | T-49 | `…diagrams` / `…case_diagrams` |

⚠️ **これは filter ステップの内部**（`FilterWorker` から呼ぶ）。パイプラインは
crawl → filter → render の3段構成のままで、narrate の段は足していない
（§3.1 の3プロンプトとの1:1 対応・T-26 の状態機械 §8.4）。render 側は
`narrative_{period}.json` を読んで渡すだけで、**AI を呼ばない**（§1.1）。

---

**⚠️ 図解も「内容」だけをここで作る**（2026-08-18 の T-49）

図解は**この段の AI が構造化データとして申告**し（`Diagram`＝3タイプ固定。
`enterprise.entities.diagram`）、**描画は決定的 Python**（レンダラ）が行う。
render に AI は足していない。

⚠️ **往復は増やさない。** 図解は今週のポイント・示唆・章導入文と**同じ1往復**の
出力に相乗りさせる（CLI は1往復に数分かかる＝T-15 備考）。

⚠️ **`None`（図解なし）が正常な経路。** 3タイプのどれにも当てはまらない記事・
事例に無理やり図を作らせると、内容の薄い図が並ぶだけになる。出力スキーマは
`Diagram | None` で受け、`diagram_by_key()` が `None` を落とす。

---

**⚠️ 記事ごとに往復しない（週次の示唆は採用記事全件で1回）**

CLI は些細なプロンプトでも起動・初期化に約131秒かかる（T-15 備考の実測）。
示唆を1件ずつ聞くと、採用11件の週で **20分超がまるごと増える**。そこで:

- 週次 = **1往復**（今週のポイント ＋ 全記事ぶんの示唆）
- 月次 = **1往復**（巻頭言 ＋ 全章の導入文 ＋ むすび）

⚠️ **週次の往復が業界の数だけ増えることは無くなった**（2026-08-18 の T-52）。
T-46 Step 4 では週刊が業界ごとに1通だったので業界数ぶん往復していたが、業界版を
廃止して1本になったので **period につき1往復**に戻った（対象業界を増やしても
週次の実行時間は伸びない）。

どちらも「当週／当月の全体を見て書くもの」なので、まとめて渡すほうが内容の
面でも素直（§9.2-2 の「当週の総括」・§10.2-2 の「当月全事例を俯瞰する総論」）。

**取り違え防止は `Literal`。** 示唆の宛先（記事URL）と章導入文の宛先（章ラベル）は
**渡した値そのものしか出せない**形にしてある（T-21 の章テーマと同じ）。自由文字列に
すると、少し言い換えた URL・章名が返って**どのカードにも当たらない示唆**ができる。

---

**⚠️ 段落数・文の数は構造で固定する**（T-21 の解説3段落と同じ方式）

「3段落で書け」と文章で頼むと2段落・4段落が返りうる。そこで:

- 巻頭言3段落・むすび2段落 → **別フィールド**で受ける（`editorial_overview` /
  `editorial_analysis` / `editorial_takeaway`、`closing_summary` /
  `closing_outlook`）
- 今週のポイントは **3〜4項目**（§9.2-2）＝範囲なので別フィールドにできない。
  **要素数の下限・上限を持つ配列**で固定する。1要素は**見出し（1文）＋詳細
  （1段落）**の組（T-52 Step 1。閲覧ページの箇条書き＋クリック展開の材料）

連結（`\\n\\n` ／ 文の連結）は書き出し側が行う（`enterprise.entities.narrative`）。

⚠️ **巻頭言3段落の役割分担（俯瞰 → 共通する変化 → 持ち帰る視点）は本タスクの
判断**。仕様書 §10.2-2 は「3段落程度の総論」としか書いていない（§10.2-4 の
事例解説と違い、段落の役割は決まっていない）。段落数を構造で固定するには各段落に
名前が要るため置いた（→ サンプル HTML が入手できたら突き合わせる。T-38）。
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, create_model

from adapter.html.monthly_renderer import MonthlyNarrative
from adapter.html.weekly_renderer import WeeklyNarrative
from adapter.llm import AIClient
from adapter.llm.ai_client import AICallMeta
from application.usecases.monthly_cases import MonthlyCase
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.diagram import (
    COMPARE_MAX_POINTS,
    COMPARE_MIN_POINTS,
    FLOW_MAX_STEPS,
    FLOW_MIN_STEPS,
    METRICS_MAX_ITEMS,
    METRICS_MIN_ITEMS,
    Diagram,
)
from enterprise.entities.narrative import (
    MonthlyNarrativeDocument,
    WeeklyNarrativeDocument,
    case_diagram_key,
    diagram_by_key,
    text_by_key,
)
from enterprise.entities.period import Period

logger = logging.getLogger(__name__)

# プロンプトの版。**このモジュールが版と本文の正**で、
# `prompts/PROMPT-2-NARRATIVE-{WEEKLY,MONTHLY}.md` は `make prompts`
# （T-30 `adapter.cli.export_prompts`）が描画した読み物。
# ⚠️ **本文を変えたら版も上げ、`make prompts` で生成し直すこと**（§9.2 の再現性要件）。
WEEKLY_NARRATIVE_PROMPT_NAME = "PROMPT-2/weekly_narrative"
# 0.2.0: 読み手を1業界に固定した（業界ごとの生成。T-46 Step 4）。
# 0.3.0: 図解の申告を足した（T-49）。
# 0.4.0: 業界版を廃止し、今週のポイントを「見出し＋詳細1段落」にした（T-52 Step 1）。
WEEKLY_NARRATIVE_PROMPT_VERSION = "0.4.0"
MONTHLY_NARRATIVE_PROMPT_NAME = "PROMPT-2/monthly_narrative"
# 0.2.0: 図解の申告を足した（T-49）。
MONTHLY_NARRATIVE_PROMPT_VERSION = "0.2.0"

# 今週のポイントの項目数（仕様書 §9.2-2「当週の総括3〜4文」＝1文が1項目の見出し）。
POINT_OF_WEEK_MIN_SENTENCES = 3
POINT_OF_WEEK_MAX_SENTENCES = 4

# 参照する週次22列（列名の正は T-07。ここは「どの列を見るか」だけを持つ）。
COLUMN_TITLE = "タイトル"
COLUMN_SUMMARY = "一言要約"
COLUMN_CATEGORY = "情報カテゴリ"
COLUMN_TOTAL_SCORE = "合計スコア"
COLUMN_INDUSTRY = "業界"
COLUMN_SOURCE = "ソース"
COLUMN_URL = "URL"

_NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_STRICT_OUTPUT = ConfigDict(extra="forbid")

# --- 図解（T-49）--------------------------------------------------------------

DIAGRAM_FIELD_HINT = (
    "図解（0〜1個）。3タイプのどれにも当てはまらなければ null にする（無理に作らない）"
)
"""図解の欄の説明（JSON Schema に載る。プロンプト本文と重ねて伝える）。"""

DIAGRAM_PROMPT_LINES: tuple[str, ...] = (
    "■ 図解（任意・0〜1個）",
    "- 文章では伝わりにくい構造がある場合だけ、下の3タイプから1つ選んで書く。",
    f"  - flow: {FLOW_MIN_STEPS}〜{FLOW_MAX_STEPS}ステップの流れ"
    "（例: 受領 → AIが下書き → 担当者が確認 → 締結）。",
    f"  - compare: 2項目の対比（左右それぞれ見出し＋{COMPARE_MIN_POINTS}〜"
    f"{COMPARE_MAX_POINTS}点。例: 従来 / 導入後）。",
    f"  - metrics: 数値ハイライト{METRICS_MIN_ITEMS}〜{METRICS_MAX_ITEMS}個"
    "（値＋その値が何を指すか）。",
    "- **どれにも当てはまらなければ null にする。**"
    "当てはまらない図を無理に作らない（図解が無いのは正常）。",
    "- **本文に書かれている事実だけ**を使う。数字・固有名詞を創作しない。",
    "- 語は短く。マスに収まらない長さは受け付けられない。",
)
"""図解の書き方（週次・月次で同じ本文を使う。**写しを作らない**）。"""


class NarrativeError(Exception):
    """生成テキストを組み立てられない入力（宛先が1つも無い等）。

    ⚠️ **AI 呼び出しの失敗はこれに包まない。** `AIClientError` とその
    サブクラス（T-15）をそのまま呼び出し元へ通す（T-19・T-21 と同じ）。
    """


class NarrativeBuilder:
    """週次・月次の生成テキストを作る（週次は**業界ごと**に1往復、月次は1往復）。

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

    async def build_weekly(
        self, records: Sequence[Mapping[str, Any]], *, period: Period
    ) -> WeeklyNarrativeDocument:
        """今週のポイントと記事ごとの示唆を作る（**1往復**）。

        ⚠️ **業界ごとの生成は廃止した**（T-52 Step 1）。週刊は業界を問わない
        週次ダイジェスト1本になったので、読み手を業界で分ける理由が無い。
        T-46 Step 4 で業界数ぶんに増えていた往復は period につき1回へ戻った。

        ⚠️ **記事ごとの往復は相変わらず作らない**（全記事ぶんの示唆を1回で
        まとめて書かせる）。

        Args:
            records: 週次22列の行（採用済み・合計スコア降順。T-21 の出力）
            period: 対象週

        Returns:
            `narrative_{period}.json` に書ける形

        Raises:
            AIClientError: AI 呼び出しの失敗（握り潰さない）
        """
        # ⚠️ **示唆は当週シートの全行ぶん作る。** カードになるのは §9.3 の採用条件と
        # 上限（`max_industry_topics` / `max_common_topics`）を通ったものだけだが、
        # **その選別はレンダラ（T-24）の1箇所に置いたままにする**。ここで写しを
        # 持つと、上限を変えたときに「カードはあるのに示唆が無い」が黙って起きる。
        urls = _unique(str(record.get(COLUMN_URL) or "").strip() for record in records)
        if not urls:
            logger.warning(
                "生成テキストの対象になる記事がありません"
                "（period=%s・空の narrative を書きます）",
                period.text,
            )
            return WeeklyNarrativeDocument(period=period.text)

        logger.info("weekly narrative (period=%s, articles=%d)", period.text, len(urls))
        result = await self._client.complete(
            prompt=build_weekly_narrative_prompt(records, self._config),
            output_schema=build_weekly_narrative_schema(urls),
            prompt_version=WEEKLY_NARRATIVE_PROMPT_VERSION,
            timeout=self._timeout,
        )
        self._metas.append(result.meta)

        # スキーマは `build_weekly_narrative_schema()` が動的に組み立てたモデル
        # （型注釈は `BaseModel` までしか付かない）。
        draft: Any = result.value
        insights = text_by_key((item.url, item.insight) for item in draft.insights)
        _warn_about_missing("示唆", expected=urls, produced=insights)

        # ⚠️ **見出しと詳細は同じ要素から取る**（T-52 Step 1）。索引で対応づけると、
        # 片方が欠けたときに別の見出しへ詳細がずれて付く（内容が入れ替わっても
        # 誰も気づけない）。鍵は見出しの文そのもの。
        headings = [item.heading.strip() for item in draft.point_of_week]
        details = text_by_key(
            (item.heading, item.detail) for item in draft.point_of_week
        )

        # ⚠️ **図解は足りなくても警告しない**（`_warn_about_missing` を通さない）。
        # 「該当するタイプが無ければ作らない」が正しい振る舞いなので、欠けている
        # ことは異常ではない。代わりに**作られた件数**をログに出す。
        diagrams = diagram_by_key((item.url, item.diagram) for item in draft.insights)
        logger.info(
            "weekly diagrams declared (period=%s, articles=%d, diagrams=%d)",
            period.text,
            len(urls),
            len(diagrams),
        )

        return WeeklyNarrativeDocument(
            period=period.text,
            point_of_week_sentences=headings,
            point_of_week_details=details,
            insights=insights,
            diagrams=diagrams,
        )

    async def build_monthly(
        self,
        cases: Sequence[MonthlyCase],
        *,
        period: Period,
        industries: Mapping[int, Sequence[str]] | None = None,
    ) -> MonthlyNarrativeDocument:
        """巻頭言・章導入文・むすびを作る（**1往復**）。

        Args:
            cases: 当月の事例（`No` 昇順＝章グルーピング順。T-21 の出力）
            period: 対象月
            industries: 事例の `No` → **業界タグ**（T-52 Step 2）。⚠️ **AI には
                聞かない**——昇格元の週次22列 列19 の値をそのまま持ち込む
                （`enterprise.entities.narrative` のモジュール docstring）

        Returns:
            `narrative_{period}.json` に書ける形

        Raises:
            AIClientError: AI 呼び出しの失敗（握り潰さない）
        """
        case_industries = _case_industries(industries)
        chapters = _unique(case.chapter for case in cases)
        if not chapters:
            logger.warning(
                "生成テキストの対象になる事例がありません"
                "（period=%s・空の narrative を書きます）",
                period.text,
            )
            # ⚠️ 業界タグは事例が0件なら当然空（生成テキストと違い AI を待たない）。
            return MonthlyNarrativeDocument(period=period.text)

        result = await self._client.complete(
            prompt=build_monthly_narrative_prompt(cases, self._config, period=period),
            output_schema=build_monthly_narrative_schema(
                chapters, [case.no for case in cases]
            ),
            prompt_version=MONTHLY_NARRATIVE_PROMPT_VERSION,
            timeout=self._timeout,
        )
        self._metas.append(result.meta)

        draft: Any = result.value
        intros = text_by_key(
            (item.chapter, item.intro) for item in draft.chapter_intros
        )
        _warn_about_missing("章導入文", expected=chapters, produced=intros)

        # ⚠️ 図解は欠けていて当たり前（T-49）。件数だけログへ。
        diagrams = diagram_by_key(
            (case_diagram_key(item.no), item.diagram) for item in draft.case_diagrams
        )
        logger.info(
            "monthly diagrams declared (period=%s, cases=%d, diagrams=%d)",
            period.text,
            len(cases),
            len(diagrams),
        )

        return MonthlyNarrativeDocument(
            period=period.text,
            editorial_subtitle=draft.editorial_subtitle.strip(),
            editorial_paragraphs=[
                draft.editorial_overview.strip(),
                draft.editorial_analysis.strip(),
                draft.editorial_takeaway.strip(),
            ],
            chapter_intros=intros,
            closing_paragraphs=[
                draft.closing_summary.strip(),
                draft.closing_outlook.strip(),
            ],
            case_diagrams=diagrams,
            case_industries=case_industries,
        )


# --- レンダラへの受け渡し（T-24 / T-25 は無変更）------------------------------


def to_weekly_narrative(document: WeeklyNarrativeDocument) -> WeeklyNarrative:
    """`narrative_{period}.json` を T-24 の入力へ写す。

    ⚠️ **業界の引数は無くなった**（T-52 Step 1。週刊は業界版を廃止して1本）。

    ⚠️ **フィールドの対応は1:1**（項目を足したり畳んだりしない）。変換が要るのは
    次の2つの事情だけで、どちらもレンダラ側の都合ではない:

    1. レンダラの入力は adapter 層の**凍結データクラス**、ファイルのスキーマは
       enterprise 層の **Pydantic モデル**。enterprise が adapter を import する
       形にはできない（依存の向き）ので、写す関数がこちら側に要る。
    2. ファイルは文・段落を**要素の列**で持ち、レンダラは連結済みの文字列を取る
       （連結は `WeeklyNarrativeDocument.point_of_week` が行う）。
    """
    return WeeklyNarrative(
        point_of_week=document.point_of_week,
        # ⚠️ **箇条書きの材料も一緒に渡す**（T-52 Step 1）。HTML は連結した
        # `point_of_week` を描き、閲覧ページ（Step 2）は項目ごとに開く。
        # **写す場所をここ1つにする**ので、2つの見え方がずれない。
        points=tuple(
            (item.heading, item.detail) for item in document.point_of_week_items
        ),
        insights=dict(document.insights),
        diagrams=dict(document.diagrams),
    )


def to_monthly_narrative(document: MonthlyNarrativeDocument) -> MonthlyNarrative:
    """`narrative_{period}.json` を T-25 の入力へ写す（週次と同じ理由・同じ形）。

    ⚠️ **業界タグ（`case_industries`）は渡さない。** 月刊 HTML の体裁に業界タグを
    足すのは T-52 のスコープ外で、使うのは閲覧ページ（`GET /reports/{period}/cases`）
    だけ。レンダラが受け取らなければ、描き忘れも描きすぎも起きない。
    """
    return MonthlyNarrative(
        editorial_subtitle=document.editorial_subtitle,
        editorial=document.editorial,
        chapter_intros=dict(document.chapter_intros),
        closing=document.closing,
        case_diagrams=dict(document.case_diagrams),
    )


# --- 出力スキーマ（宛先は Literal で閉じる）-----------------------------------


def build_weekly_narrative_schema(urls: Sequence[str]) -> type[BaseModel]:
    """週次の出力スキーマ。

    ⚠️ **示唆の宛先（`url`）は `Literal`**（渡した記事以外に示唆を付けられない）。

    ⚠️ **示唆の件数は記事数を下限にする**（上限は課さない）。下限が無いと「1件だけ
    書いて終わり」が構造的に通ってしまう。上限まで固定すると、重複した宛先を1件
    返しただけで**やり直し（1回数分）**になるので、過不足は受け取ってから
    警告に出す（`_warn_about_missing`）。

    ⚠️ **今週のポイントは「見出し＋詳細」を1要素で受ける**（T-52 Step 1）。見出しの
    配列と詳細の配列を別々に返させると、**片方が1件少ないだけで全部がずれる**
    （しかも文章としては読めてしまうので気づけない）。組にしておけば、欠けるのは
    その項目の詳細だけで済む。

    Raises:
        NarrativeError: 宛先が1つも無い場合（`Literal` を作れない）
    """
    if not urls:
        raise NarrativeError("示唆の宛先になる記事がありません")

    point_model = create_model(
        "PointOfWeek",
        __config__=_STRICT_OUTPUT,
        heading=(
            _NonEmptyText,
            Field(description="今週のポイント1件の見出し（**1文**。句点で終える）"),
        ),
        detail=(
            _NonEmptyText,
            Field(
                description=(
                    "その見出しを補う詳細（1段落）。"
                    "見出しの言い換えではなく、何が起きたか・なぜ要点なのかを述べる"
                )
            ),
        ),
    )

    insight_model = create_model(
        "ArticleInsight",
        __config__=_STRICT_OUTPUT,
        url=(
            Literal[tuple(_unique(urls))],  # ty: ignore[invalid-type-form]
            Field(description="示唆の対象記事のURL（提示したものをそのまま使う）"),
        ),
        insight=(
            _NonEmptyText,
            Field(description="その記事を「自社ではどう捉えるか」の1段落"),
        ),
        # ⚠️ **図解は示唆と同じ要素に相乗りさせる**（往復を増やさないため。T-49）。
        # `None` を既定にしてあるので、**該当するタイプが無ければ書かなくてよい**。
        diagram=(Diagram | None, Field(default=None, description=DIAGRAM_FIELD_HINT)),
    )
    return create_model(
        "WeeklyNarrativeDraft",
        __config__=_STRICT_OUTPUT,
        point_of_week=(
            list[point_model],  # ty: ignore[invalid-type-form]
            Field(
                min_length=POINT_OF_WEEK_MIN_SENTENCES,
                max_length=POINT_OF_WEEK_MAX_SENTENCES,
                description=(
                    f"今週のポイント。{POINT_OF_WEEK_MIN_SENTENCES}〜"
                    f"{POINT_OF_WEEK_MAX_SENTENCES}件。"
                    "**1要素＝見出し1文＋詳細1段落**"
                ),
            ),
        ),
        insights=(
            list[insight_model],  # ty: ignore[invalid-type-form]
            Field(
                min_length=len(urls),
                description="提示した記事すべてに1件ずつの示唆",
            ),
        ),
    )


def build_monthly_narrative_schema(
    chapters: Sequence[str], case_numbers: Sequence[int] = ()
) -> type[BaseModel]:
    """月次の出力スキーマ（巻頭言3段落・むすび2段落は**別フィールド**）。

    ⚠️ **章導入文の宛先（`chapter`）は `Literal`**（渡した章ラベルそのもの）。
    T-25 は列2 の値で導入文を引くので、言い換えられると当たらない。

    ⚠️ **図解の宛先（`no`）も `Literal`**（月次8列の列1「No」。T-49）。事例ごとに
    1要素を返させ、**図解が無ければ `diagram` を `null`** にする形にしてある
    （要素ごと省かせると「書き忘れ」と「該当なし」の区別が付かない）。

    Args:
        chapters: 章ラベル（`第N章 …`）
        case_numbers: 事例の `No`。**空なら図解の欄そのものを作らない**
            （プロンプトだけを組み立てるときの経路）

    Raises:
        NarrativeError: 章が1つも無い場合
    """
    if not chapters:
        raise NarrativeError("章導入文の宛先になる章がありません")

    intro_model = create_model(
        "ChapterIntro",
        __config__=_STRICT_OUTPUT,
        chapter=(
            Literal[tuple(_unique(chapters))],  # ty: ignore[invalid-type-form]
            Field(description="章ラベル（提示したものをそのまま使う）"),
        ),
        intro=(
            _NonEmptyText,
            Field(description="その章に何を集めたのかを述べる導入文（1段落）"),
        ),
    )
    numbers = _unique_numbers(case_numbers)
    case_diagram_fields: dict[str, Any] = {}
    if numbers:
        case_diagram_model = create_model(
            "CaseDiagram",
            __config__=_STRICT_OUTPUT,
            no=(
                Literal[tuple(numbers)],  # ty: ignore[invalid-type-form]
                Field(description="事例の No（提示したものをそのまま使う）"),
            ),
            diagram=(
                Diagram | None,
                Field(default=None, description=DIAGRAM_FIELD_HINT),
            ),
        )
        case_diagram_fields["case_diagrams"] = (
            list[case_diagram_model],  # ty: ignore[invalid-type-form]
            Field(
                min_length=len(numbers),
                description=(
                    "提示した事例すべてに1件ずつ（図解が無ければ diagram は null）"
                ),
            ),
        )

    return create_model(
        "MonthlyNarrativeDraft",
        __config__=_STRICT_OUTPUT,
        editorial_subtitle=(
            _NonEmptyText,
            Field(description="当月を一言で表す命題（見出しの下に置くサブ見出し）"),
        ),
        editorial_overview=(
            _NonEmptyText,
            Field(description="巻頭言①：当月の事例全体を俯瞰した総論"),
        ),
        editorial_analysis=(
            _NonEmptyText,
            Field(description="巻頭言②：事例に共通する変化・論点"),
        ),
        editorial_takeaway=(
            _NonEmptyText,
            Field(description="巻頭言③：読者が持つべき視点"),
        ),
        chapter_intros=(
            list[intro_model],  # ty: ignore[invalid-type-form]
            Field(min_length=len(chapters), description="提示した章すべてに1件ずつ"),
        ),
        closing_summary=(
            _NonEmptyText,
            Field(description="むすび①：今月の総括"),
        ),
        closing_outlook=(
            _NonEmptyText,
            Field(description="むすび②：来月への視点"),
        ),
        # ⚠️ 図解の欄は**最後**（本文の欄より先に置くと、図から書き始めさせる形に
        # なる）。事例が渡されていなければ欄ごと作らない。
        **case_diagram_fields,
    )


# --- プロンプト ---------------------------------------------------------------


def build_weekly_narrative_prompt(
    records: Sequence[Mapping[str, Any]],
    config: IntelligenceConfig,
) -> str:
    """週次の生成テキストのプロンプト（仕様書 §9.2-2・§9.2-4）。

    ⚠️ **読み手を業界で分けない**（T-52 Step 1）。週刊は業界を問わない週次
    ダイジェスト1本になったので、T-46 Step 4 で入れていた「読み手の立場＝1業界」の
    指定を外した。**対象業界（`tunable_thresholds.target_industries`）はここでは
    使わない**——使うと、業界版を廃止したはずの号に特定業界向けの文章が混ざる。

    ⚠️ **出力形式（JSON だけを出せ・JSON Schema）の指示は含めない。**
    `AIClient` の実装が付ける（他のプロンプトと同じ）。
    """
    labels: dict[str, str] = {
        str(category.id): category.label for category in config.information_categories
    }
    return "\n".join(
        [
            "あなたは週刊AIメールマガジン（Weekly AI Intelligence by Sapeet）の"
            "編集者です。今週号に載せる**読み手向けの文章**だけを書いてください。",
            "",
            "■ 厳守事項",
            "- 下に示した記事に書かれている事実だけを使う。"
            "数字・固有名詞を創作しない。",
            "- 記事の要約をなぞらない（要約は別の欄に既にある）。",
            "- 記事の取捨選択・点数・掲載順は**すでに決まっている**。触れないこと。",
            "- URL は提示したものをそのまま使う（短縮・言い換えをしない）。",
            "",
            "■ 読み手",
            "- 業界を問わない社内の非専門メンバー（特定業界向けの号ではない）。",
            "",
            "■ 今週のポイント",
            f"- {POINT_OF_WEEK_MIN_SENTENCES}〜{POINT_OF_WEEK_MAX_SENTENCES}件で"
            "当週全体を総括する。",
            "- heading: その要点を**1文**で言い切る見出し（句点で終える）。"
            "箇条書きの1行として単独で読める形にする。",
            "- detail: その見出しを補う**1段落**。見出しの言い換えではなく、"
            "何が起きたか・なぜ今週の要点なのかを述べる"
            "（読み手は見出しを見て、気になった項目だけを開く）。",
            "- 個々の記事の羅列ではなく、今週まとめて何が起きたのかを述べる。",
            "",
            "■ 記事ごとの示唆",
            "- 各記事について「**自社ではどう捉えるか**」を1段落で書く。",
            "- 事実の繰り返しではなく、読み手が自社に引き寄せて"
            "考えるための視点を書く。",
            "- 断定できないことは断定しない（「〜の可能性がある」等）。",
            "",
            *DIAGRAM_PROMPT_LINES,
            "- 週刊の図解は**閲覧ページで記事を開いたときだけ**出る"
            "（一覧は要旨だけの体裁を保つため）。",
            "",
            "■ 対象記事（掲載順）",
            *_weekly_article_lines(records, labels),
        ]
    )


def _weekly_article_lines(
    records: Sequence[Mapping[str, Any]], labels: Mapping[str, str]
) -> list[str]:
    """記事1件ぶんの提示（示唆の宛先になる URL を必ず添える）。"""
    lines: list[str] = []
    for index, record in enumerate(records, start=1):
        category = str(record.get(COLUMN_CATEGORY) or "")
        lines.extend(
            [
                f"{index}. {record.get(COLUMN_TITLE)}",
                f"   URL: {record.get(COLUMN_URL)}",
                f"   カテゴリ: {labels.get(category, category)}"
                f" ／ 業界: {_joined(record.get(COLUMN_INDUSTRY))}"
                f" ／ 出典: {record.get(COLUMN_SOURCE)}"
                f" ／ 合計スコア: {record.get(COLUMN_TOTAL_SCORE)}",
                f"   要約: {record.get(COLUMN_SUMMARY)}",
            ]
        )
    return lines


def build_monthly_narrative_prompt(
    cases: Sequence[MonthlyCase], config: IntelligenceConfig, *, period: Period
) -> str:
    """月次の生成テキストのプロンプト（仕様書 §10.2-2・§10.2-4・§10.2-5）。

    ⚠️ **全事例をまとめて渡す。** §10.2-2 が求めているのは「当月**全事例を俯瞰
    する**総論」で、事例ごとに書かせて後から束ねられる文章ではない。
    """
    monthly = config.tunable_thresholds.monthly
    return "\n".join(
        [
            "あなたは月刊AIレポート（月刊ビリーフ by Sapeet）の編集長です。"
            f"{period.start.year}年{period.start.month}月号の"
            "**巻頭言・章の導入文・むすび**を書いてください。",
            "",
            "■ 厳守事項",
            "- 下に示した事例に書かれている事実だけを使う。"
            "数字・固有名詞を創作しない。",
            "- 事例の解説をなぞらない（解説は本編に既にある）。",
            "- 章の構成・事例の順序は**すでに決まっている**。組み替えを提案しない。",
            "- 章ラベルは提示したものをそのまま使う（言い換えない）。",
            "",
            "■ 巻頭言（3段落）",
            "- editorial_subtitle: 当月を一言で表す命題"
            "（例「『導入したか』ではなく『作り直したか』が問われ始めた月」）。",
            "- editorial_overview: 当月の事例全体を俯瞰した総論。",
            "- editorial_analysis: 事例に共通する変化・論点。",
            "- editorial_takeaway: 読者が持つべき視点。",
            "",
            "■ 章の導入文",
            f"- 各章（全{len(_unique(case.chapter for case in cases))}章）について、"
            "その章に何を集めたのかが分かる導入文を1段落で書く。",
            "",
            "■ むすび（2段落）",
            "- closing_summary: 今月の総括。",
            "- closing_outlook: 来月への視点。",
            "",
            *DIAGRAM_PROMPT_LINES,
            "- 月刊の図解は**事例カードの中**に描かれる（解説の後・出典の前）。",
            "",
            f"■ 収録事例（全{len(cases)}件・目安 {monthly.target_case_count} 件）",
            *_monthly_case_lines(cases),
        ]
    )


def _monthly_case_lines(cases: Sequence[MonthlyCase]) -> list[str]:
    """事例1件ぶんの提示（章ラベルは導入文の宛先なので必ず添える）。"""
    lines: list[str] = []
    for case in cases:
        lines.extend(
            [
                f"[{case.chapter}] CASE {case.no} ／ {'・'.join(case.organizations)}",
                f"   {case.title}",
                *(f"   {paragraph}" for paragraph in case.paragraphs),
            ]
        )
    return lines


# --- 内部ヘルパ ---------------------------------------------------------------


def _case_industries(
    industries: Mapping[int, Sequence[str]] | None,
) -> dict[str, list[str]]:
    """事例の `No` → 業界タグ（鍵は図解と同じ文字列。T-52 Step 2）。

    ⚠️ **空のタグは鍵ごと落とす**（「業界タグなし」と「空の配列」を分けない）。
    鍵の作り方は `case_diagram_key()` の1箇所だけを通す。
    """
    if not industries:
        return {}
    cleaned: dict[str, list[str]] = {}
    for no, tags in industries.items():
        values = _unique(tags)
        if values:
            cleaned[case_diagram_key(no)] = values
    return cleaned


def _unique(values: Any) -> list[str]:
    """順序を保った重複除去（空文字は落とす）。"""
    seen: dict[str, None] = {}
    for value in values:
        text = str(value).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def _unique_numbers(values: Sequence[int]) -> list[int]:
    """順序を保った重複除去（`No` は整数なので文字列版と分けてある）。"""
    seen: dict[int, None] = {}
    for value in values:
        seen.setdefault(int(value), None)
    return list(seen)


def _joined(value: Any) -> str:
    """multi 値（`list`）を読める形へ（xlsx の区切りには依存しない）。"""
    if isinstance(value, (list, tuple)):
        return "・".join(str(item) for item in value)
    return str(value or "")


def _warn_about_missing(
    label: str, *, expected: Sequence[str], produced: Mapping[str, str]
) -> None:
    """宛先の取りこぼしを警告に出す（**落とさない**）。

    足りない示唆・導入文はレンダラがその箱だけ出さない（T-24 / T-25）。生成が
    1件欠けたからといって号全体を落とすほうが損なので、**静かに減らさない**ことを
    warning で担保する。
    """
    if missing := [key for key in expected if key not in produced]:
        logger.warning("%sが足りません（%d件）: %s", label, len(missing), missing)
    if extra := [key for key in produced if key not in expected]:  # pragma: no cover
        # `Literal` で閉じているので構造的には起きない（起きたら実装の合図）。
        logger.warning("%sの宛先が対象外です（%d件）: %s", label, len(extra), extra)


__all__ = [
    "DIAGRAM_FIELD_HINT",
    "DIAGRAM_PROMPT_LINES",
    "MONTHLY_NARRATIVE_PROMPT_NAME",
    "MONTHLY_NARRATIVE_PROMPT_VERSION",
    "POINT_OF_WEEK_MAX_SENTENCES",
    "POINT_OF_WEEK_MIN_SENTENCES",
    "WEEKLY_NARRATIVE_PROMPT_NAME",
    "WEEKLY_NARRATIVE_PROMPT_VERSION",
    "NarrativeBuilder",
    "NarrativeError",
    "build_monthly_narrative_prompt",
    "build_monthly_narrative_schema",
    "build_weekly_narrative_prompt",
    "build_weekly_narrative_schema",
    "to_monthly_narrative",
    "to_weekly_narrative",
]
