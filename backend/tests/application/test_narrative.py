"""生成テキストの生成（T-44 ／ 決定3 ／ 仕様書 §9.2-2・§9.2-4・§10.2-2/4/5）。

⚠️ **実際の `claude` は起動しない**（T-21 のテストと同じく `AIClient` を差し替える）。

重点:

- **往復は週次＝業界ごとに1回・月次＝1回**（記事ごと・章ごとには往復しない
  ＝T-15 のオーバーヘッド。業界ごとになるのは週刊が業界ごとに1通だから＝T-46）
- **宛先は `Literal` で閉じる**（示唆の URL・導入文の章ラベルを言い換えられない）
- **段落数・文の数は構造で固定**（別フィールド／要素数の下限・上限）
- **T-24 / T-25 のレンダラへ無変更で渡せる**（実際に HTML を組み立てて確かめる）
- **図解は示唆・章導入文と同じ往復に相乗りし、無いのが正常**（T-49）
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from adapter.html.monthly_renderer import render_monthly_html
from adapter.html.weekly_renderer import render_weekly_html
from adapter.llm.ai_client import (
    AICallMeta,
    AIResult,
    OutputSchema,
    resolve_output_adapter,
)
from application.usecases.monthly_cases import MonthlyCase
from application.usecases.narrative import (
    MONTHLY_NARRATIVE_PROMPT_VERSION,
    POINT_OF_WEEK_MAX_SENTENCES,
    POINT_OF_WEEK_MIN_SENTENCES,
    WEEKLY_NARRATIVE_PROMPT_VERSION,
    NarrativeBuilder,
    NarrativeError,
    build_monthly_narrative_prompt,
    build_monthly_narrative_schema,
    build_weekly_narrative_prompt,
    build_weekly_narrative_schema,
    to_monthly_narrative,
    to_weekly_narrative,
)
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.diagram import DIAGRAM_TYPES
from enterprise.entities.json_document import (
    DocumentParseError,
    parse_json_document,
)
from enterprise.entities.period import Period, parse_period
from enterprise.entities.raw_article import RawArticle

INITIAL_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)

WEEKLY_PERIOD = parse_period("2026-W31")
MONTHLY_PERIOD = parse_period("2026-07")

URL_A = "https://example.com/news/a"
URL_B = "https://example.com/news/b"

INDUSTRY = "不動産"

CHAPTER_1 = "第1章 業務への組み込み"
CHAPTER_2 = "第2章 モデルの動向"


@pytest.fixture(scope="session")
def initial_raw() -> dict[str, Any]:
    return json.loads(INITIAL_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def config(initial_raw: dict[str, Any]) -> IntelligenceConfig:
    return IntelligenceConfig.model_validate(copy.deepcopy(initial_raw))


# --- 入力（T-21 の出力）------------------------------------------------------


def record(
    *,
    title: str = "大手不動産がAIエージェントを導入",
    url: str = URL_A,
    industry: list[str] | None = None,
    total: int = 83,
) -> dict[str, Any]:
    """週次22列の行（このモジュールが見る列だけを持つ）。"""
    return {
        "タイトル": title,
        "URL": url,
        "一言要約": "大手不動産がAIエージェントを契約業務へ導入した。",
        "情報カテゴリ": "enterprise_ai_case",
        "合計スコア": total,
        "業界": industry if industry is not None else ["不動産"],
        "ソース": "ITmedia",
        "レポート採用区分": "参考情報",
    }


def case(*, no: int = 1, chapter: str = CHAPTER_1, url: str = URL_A) -> MonthlyCase:
    return MonthlyCase(
        no=no,
        chapter=chapter,
        organizations=("大手不動産",),
        title="契約業務のAIエージェント化",
        url=url,
        source_text="ITmedia（2026-07-27）",
        month=MONTHLY_PERIOD.text,
        paragraphs=("事実の段落。", "詳細の段落。", "示唆の段落。"),
        article=RawArticle(
            collected_at="2026-07-28",
            published_at="2026-07-27",
            title="大手不動産がAIエージェントを導入",
            url=url,
            source="ITmedia",
            raw_summary="国内大手がAIエージェントを導入した。",
            region_hint="日本",
            primary_or_secondary="報道",
        ),
    )


# --- AI 出力 -----------------------------------------------------------------

SENTENCES = [
    "今週はAIエージェントの実務投入が相次いだ。",
    "契約や審査など定型度の高い業務から広がっている。",
    "不動産では現場の運用設計が論点になる。",
]


def weekly_payload(
    *, urls: list[str] | None = None, sentences: list[str] | None = None
) -> dict[str, Any]:
    return {
        "point_of_week_sentences": sentences if sentences is not None else SENTENCES,
        "insights": [
            {"url": url, "insight": f"{url} を自社ではこう捉える。"}
            for url in (urls if urls is not None else [URL_A])
        ],
    }


def monthly_payload(
    *,
    chapters: list[str] | None = None,
    case_numbers: list[int] | None = None,
    diagrams: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """月次の AI 出力。

    ⚠️ **`case_diagrams` は事例ごとに1要素**（図解が無ければ `diagram` は `null`）。
    既定は「全事例が図解なし」＝T-49 の正常経路。
    """
    numbers = case_numbers if case_numbers is not None else [1]
    declared = diagrams or {}
    return {
        "editorial_subtitle": "『導入したか』ではなく『作り直したか』が問われた月",
        "editorial_overview": "俯瞰の段落。",
        "editorial_analysis": "共通する変化の段落。",
        "editorial_takeaway": "持ち帰る視点の段落。",
        "chapter_intros": [
            {"chapter": chapter, "intro": f"{chapter} には…を集めた。"}
            for chapter in (chapters if chapters is not None else [CHAPTER_1])
        ],
        "closing_summary": "今月の総括。",
        "closing_outlook": "来月への視点。",
        "case_diagrams": [{"no": no, "diagram": declared.get(no)} for no in numbers],
    }


class ScriptedAIClient:
    """`AIClient` のテストダブル（渡された出力スキーマで本物と同じく検証する）。"""

    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = list(payloads)
        self.prompts: list[str] = []
        self.versions: list[str | None] = []

    async def complete[T](
        self,
        *,
        prompt: str,
        output_schema: OutputSchema[T],
        prompt_version: str | None = None,
        timeout: float | None = None,
    ) -> AIResult[T]:
        self.prompts.append(prompt)
        self.versions.append(prompt_version)
        payload = self.payloads.pop(0)
        value = parse_json_document(
            resolve_output_adapter(output_schema),
            json.dumps(payload, ensure_ascii=False),
            label="AI 出力",
        )
        return AIResult(
            value=value,
            meta=AICallMeta(
                requested_model="claude-opus-5", prompt_version=prompt_version
            ),
        )

    @property
    def calls(self) -> int:
        return len(self.prompts)


def builder(config: IntelligenceConfig, client: ScriptedAIClient) -> NarrativeBuilder:
    return NarrativeBuilder(client=client, config=config)


# --- 週次 ---------------------------------------------------------------------


async def test_all_articles_are_covered_in_one_round_trip(
    config: IntelligenceConfig,
) -> None:
    """⚠️ 記事ごとに往復しない（1件ずつだと採用11件で20分超が増える。T-15 備考）。"""
    client = ScriptedAIClient(weekly_payload(urls=[URL_A, URL_B]))

    document = await builder(config, client).build_weekly(
        [record(url=URL_A), record(title="別の記事", url=URL_B)], period=WEEKLY_PERIOD
    )

    assert client.calls == 1  # 対象業界が1件の config
    assert set(document.for_industry(INDUSTRY).insights) == {URL_A, URL_B}
    assert client.versions == [WEEKLY_NARRATIVE_PROMPT_VERSION]


async def test_each_target_industry_gets_its_own_round_trip(
    config: IntelligenceConfig,
) -> None:
    """T-46 Step 4：週刊は業界ごとに1通なので、生成テキストも業界ごとに作る。

    ⚠️ 往復は業界の数だけ増える（1回数分）。**記事ごとの往復は増やさない**。
    """
    config.tunable_thresholds.weekly.target_industries = ["不動産", "金融"]
    client = ScriptedAIClient(weekly_payload(), weekly_payload())

    document = await builder(config, client).build_weekly(
        [record()], period=WEEKLY_PERIOD
    )

    assert client.calls == 2
    assert set(document.industries) == {"不動産", "金融"}
    # 読み手の立場は混ぜない（プロンプトはその業界版として書かせる）。
    assert "■ 対象業界（読み手の立場）: 不動産" in client.prompts[0]
    assert "■ 対象業界（読み手の立場）: 金融" in client.prompts[1]


async def test_the_point_of_week_is_assembled_from_the_sentences(
    config: IntelligenceConfig,
) -> None:
    client = ScriptedAIClient(weekly_payload())

    document = await builder(config, client).build_weekly(
        [record()], period=WEEKLY_PERIOD
    )

    generated = document.for_industry(INDUSTRY)
    assert generated.point_of_week_sentences == SENTENCES
    assert generated.point_of_week == "".join(SENTENCES)
    assert document.period == WEEKLY_PERIOD.text


async def test_the_insight_key_is_the_url_as_it_appears_in_the_sheet(
    config: IntelligenceConfig,
) -> None:
    """⚠️ 正規化しない。T-24 は当週シート列22 の値で示唆を引く。"""
    url = "https://Example.com/News/A?utm_source=x"
    client = ScriptedAIClient(weekly_payload(urls=[url]))

    document = await builder(config, client).build_weekly(
        [record(url=url)], period=WEEKLY_PERIOD
    )

    assert list(document.for_industry(INDUSTRY).insights) == [url]


async def test_no_adopted_articles_means_no_ai_call(
    config: IntelligenceConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """0件の実行でも narrative は書ける（空のまま返す）。AI は呼ばない。"""
    client = ScriptedAIClient()

    with caplog.at_level("WARNING"):
        document = await builder(config, client).build_weekly([], period=WEEKLY_PERIOD)

    assert client.calls == 0
    assert document.industries == {}
    assert document.for_industry(INDUSTRY).point_of_week is None
    assert any("対象になる記事がありません" in r.message for r in caplog.records)


async def test_a_missing_insight_is_warned_about_but_does_not_fail(
    config: IntelligenceConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """足りない示唆はレンダラがボックスごと出さない（号ごと落とすほうが損）。

    ⚠️ ただし**静かに減らさない**：警告に出す。
    """
    # 2件ぶん required（`min_length`）だが、同じ記事へ2件書いた出力。
    payload = weekly_payload(urls=[URL_A, URL_A])
    client = ScriptedAIClient(payload)

    with caplog.at_level("WARNING"):
        document = await builder(config, client).build_weekly(
            [record(url=URL_A), record(title="別の記事", url=URL_B)],
            period=WEEKLY_PERIOD,
        )

    assert set(document.for_industry(INDUSTRY).insights) == {URL_A}
    assert any("示唆が足りません" in r.message for r in caplog.records)


async def test_the_prompt_carries_the_target_industry_and_every_url(
    config: IntelligenceConfig,
) -> None:
    client = ScriptedAIClient(weekly_payload(urls=[URL_A, URL_B]))

    await builder(config, client).build_weekly(
        [record(url=URL_A), record(title="別の記事", url=URL_B)], period=WEEKLY_PERIOD
    )

    prompt = client.prompts[0]
    for industry in config.tunable_thresholds.weekly.industries:
        assert industry in prompt
    assert URL_A in prompt and URL_B in prompt
    assert "自社ではどう捉えるか" in prompt


# --- 週次の出力スキーマ（構造で閉じているもの）--------------------------------


def test_an_insight_for_an_unknown_article_cannot_be_produced() -> None:
    """⚠️ 宛先は `Literal`。言い換えた URL はスキーマで落ちる。"""
    schema = build_weekly_narrative_schema([URL_A])

    with pytest.raises(DocumentParseError):
        parse_json_document(
            resolve_output_adapter(schema),
            json.dumps(weekly_payload(urls=["https://example.com/other"])),
            label="AI 出力",
        )


@pytest.mark.parametrize("count", [POINT_OF_WEEK_MIN_SENTENCES - 1, 5])
def test_the_point_of_week_sentence_count_is_fixed_by_the_schema(count: int) -> None:
    """§9.2-2「3〜4文」を文章で頼まず、要素数で固定する。"""
    schema = build_weekly_narrative_schema([URL_A])

    with pytest.raises(DocumentParseError):
        parse_json_document(
            resolve_output_adapter(schema),
            json.dumps(weekly_payload(sentences=["文。"] * count)),
            label="AI 出力",
        )


def test_one_insight_per_article_is_required() -> None:
    """1件だけ書いて終わり、が構造的に通らないこと。"""
    schema = build_weekly_narrative_schema([URL_A, URL_B])

    with pytest.raises(DocumentParseError):
        parse_json_document(
            resolve_output_adapter(schema),
            json.dumps(weekly_payload(urls=[URL_A])),
            label="AI 出力",
        )


def test_the_schema_needs_at_least_one_destination() -> None:
    with pytest.raises(NarrativeError):
        build_weekly_narrative_schema([])
    with pytest.raises(NarrativeError):
        build_monthly_narrative_schema([])


# --- 月次 ---------------------------------------------------------------------


async def test_the_editorial_and_closing_come_back_as_fixed_paragraphs(
    config: IntelligenceConfig,
) -> None:
    """⚠️ 3段落・2段落は**別フィールド**で受ける（T-21 の解説3段落と同じ方式）。"""
    client = ScriptedAIClient(monthly_payload())

    document = await builder(config, client).build_monthly(
        [case()], period=MONTHLY_PERIOD
    )

    assert client.calls == 1
    assert client.versions == [MONTHLY_NARRATIVE_PROMPT_VERSION]
    assert document.editorial_paragraphs == [
        "俯瞰の段落。",
        "共通する変化の段落。",
        "持ち帰る視点の段落。",
    ]
    assert document.closing_paragraphs == ["今月の総括。", "来月への視点。"]
    assert document.editorial_subtitle is not None


async def test_every_chapter_gets_an_intro_keyed_by_its_label(
    config: IntelligenceConfig,
) -> None:
    """鍵は月次8列の列2 の値そのもの（T-25 の `intro_for()` が引く形）。"""
    client = ScriptedAIClient(
        monthly_payload(chapters=[CHAPTER_1, CHAPTER_2], case_numbers=[1, 2])
    )

    document = await builder(config, client).build_monthly(
        [case(no=1), case(no=2, chapter=CHAPTER_2, url=URL_B)], period=MONTHLY_PERIOD
    )

    assert list(document.chapter_intros) == [CHAPTER_1, CHAPTER_2]


async def test_the_monthly_prompt_carries_every_case_and_chapter(
    config: IntelligenceConfig,
) -> None:
    """§10.2-2 は「当月**全事例を俯瞰する**総論」なので全件を渡す。"""
    client = ScriptedAIClient(
        monthly_payload(chapters=[CHAPTER_1, CHAPTER_2], case_numbers=[1, 2])
    )

    await builder(config, client).build_monthly(
        [case(no=1), case(no=2, chapter=CHAPTER_2, url=URL_B)], period=MONTHLY_PERIOD
    )

    prompt = client.prompts[0]
    assert CHAPTER_1 in prompt and CHAPTER_2 in prompt
    assert "CASE 1" in prompt and "CASE 2" in prompt
    assert "2026年7月号" in prompt


async def test_no_cases_means_no_ai_call(config: IntelligenceConfig) -> None:
    client = ScriptedAIClient()

    document = await builder(config, client).build_monthly([], period=MONTHLY_PERIOD)

    assert client.calls == 0
    assert document.editorial is None
    assert document.closing is None


def test_an_intro_for_an_unknown_chapter_cannot_be_produced() -> None:
    """⚠️ 章ラベルも `Literal`（言い換えると T-25 の導入文が当たらない）。"""
    schema = build_monthly_narrative_schema([CHAPTER_1])

    with pytest.raises(DocumentParseError):
        parse_json_document(
            resolve_output_adapter(schema),
            json.dumps(monthly_payload(chapters=["第9章 なにか"])),
            label="AI 出力",
        )


# --- レンダラへの受け渡し（T-24 / T-25 は無変更）------------------------------


async def test_the_weekly_document_feeds_the_renderer_as_is(
    config: IntelligenceConfig,
) -> None:
    """生成 → ファイルの形 → レンダラ、が変換1回で繋がること。"""
    client = ScriptedAIClient(weekly_payload())
    document = await builder(config, client).build_weekly(
        [record()], period=WEEKLY_PERIOD
    )

    markup = render_weekly_html(
        period=WEEKLY_PERIOD.text,
        articles=[record()],
        config=config,
        narrative=to_weekly_narrative(document, INDUSTRY),
        industry=INDUSTRY,
    )

    assert "今週のポイント" in markup
    assert SENTENCES[0] in markup
    assert f"{URL_A} を自社ではこう捉える。" in markup


async def test_the_monthly_document_feeds_the_renderer_as_is(
    config: IntelligenceConfig,
) -> None:
    client = ScriptedAIClient(monthly_payload())
    document = await builder(config, client).build_monthly(
        [case()], period=MONTHLY_PERIOD
    )

    markup = render_monthly_html(
        period=MONTHLY_PERIOD.text,
        cases=[case().to_row()],
        config=config,
        narrative=to_monthly_narrative(document),
    )

    # 巻頭言の3段落が `<p>` 3つに割れる（連結 → 分割が同じ区切りで往復する）。
    for paragraph in document.editorial_paragraphs:
        assert f">{paragraph}</p>" in markup
    assert "今月の総括。" in markup
    assert f"{CHAPTER_1} には…を集めた。" in markup


async def test_the_ai_calls_are_reported_for_the_audit_log(
    config: IntelligenceConfig,
) -> None:
    """§9.2：`prompt_version` を監査へ渡せる形で持つこと。"""
    client = ScriptedAIClient(weekly_payload())
    subject = builder(config, client)

    await subject.build_weekly([record()], period=WEEKLY_PERIOD)

    assert [meta.prompt_version for meta in subject.ai_calls] == [
        WEEKLY_NARRATIVE_PROMPT_VERSION
    ]


def test_the_sentence_bounds_match_the_spec() -> None:
    """§9.2-2 の「3〜4文」をコード側の定数として固定する。"""
    assert (POINT_OF_WEEK_MIN_SENTENCES, POINT_OF_WEEK_MAX_SENTENCES) == (3, 4)


def test_the_period_type_is_what_the_worker_passes() -> None:
    """入口は `Period`（表記の解釈をこの層で二重に持たない）。"""
    assert isinstance(WEEKLY_PERIOD, Period)


# --- 図解の申告（T-49）--------------------------------------------------------

FLOW_PAYLOAD: dict[str, Any] = {
    "type": "flow",
    "title": "契約業務の自動化",
    "steps": ["契約書を受領", "AIが下書き", "担当者が確認"],
}


async def test_a_weekly_diagram_rides_along_with_the_insight(
    config: IntelligenceConfig,
) -> None:
    """⚠️ 図解のために往復を増やさない（示唆と同じ1往復に相乗り）。"""
    payload = weekly_payload()
    payload["insights"][0]["diagram"] = FLOW_PAYLOAD
    client = ScriptedAIClient(payload)

    document = await builder(config, client).build_weekly(
        [record()], period=WEEKLY_PERIOD
    )

    assert client.calls == 1
    assert document.for_industry(INDUSTRY).diagrams[URL_A].type == "flow"


async def test_no_weekly_diagram_is_a_normal_result(
    config: IntelligenceConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """⚠️ **図解なしは異常ではない**（示唆と違って警告も出さない）。"""
    client = ScriptedAIClient(weekly_payload())

    with caplog.at_level("WARNING"):
        document = await builder(config, client).build_weekly(
            [record()], period=WEEKLY_PERIOD
        )

    assert document.for_industry(INDUSTRY).diagrams == {}
    assert not [r for r in caplog.records if "図解" in r.message]


async def test_a_weekly_diagram_outside_the_schema_never_reaches_the_narrative(
    config: IntelligenceConfig,
) -> None:
    """スキーマ外の図解は**出力の読み込みで**落ちる（レンダラまで届かない）。"""
    payload = weekly_payload()
    payload["insights"][0]["diagram"] = {"type": "timeline", "title": "年表"}
    client = ScriptedAIClient(payload)

    with pytest.raises(DocumentParseError):
        await builder(config, client).build_weekly([record()], period=WEEKLY_PERIOD)


async def test_a_monthly_diagram_is_keyed_by_the_case_number(
    config: IntelligenceConfig,
) -> None:
    client = ScriptedAIClient(
        monthly_payload(
            chapters=[CHAPTER_1, CHAPTER_2],
            case_numbers=[1, 2],
            diagrams={2: FLOW_PAYLOAD},
        )
    )

    document = await builder(config, client).build_monthly(
        [case(no=1), case(no=2, chapter=CHAPTER_2, url=URL_B)], period=MONTHLY_PERIOD
    )

    # ⚠️ 図解を申告しなかった事例（No=1）は鍵ごと入らない。
    assert list(document.case_diagrams) == ["2"]
    assert document.case_diagrams["2"].title == "契約業務の自動化"


async def test_no_monthly_diagram_is_a_normal_result(
    config: IntelligenceConfig,
) -> None:
    client = ScriptedAIClient(monthly_payload())

    document = await builder(config, client).build_monthly(
        [case()], period=MONTHLY_PERIOD
    )

    assert document.case_diagrams == {}


async def test_a_diagram_for_an_unknown_case_is_rejected(
    config: IntelligenceConfig,
) -> None:
    """宛先は `Literal`（提示していない `No` へは図解を付けられない）。"""
    client = ScriptedAIClient(
        monthly_payload(case_numbers=[99], diagrams={99: FLOW_PAYLOAD})
    )

    with pytest.raises(DocumentParseError):
        await builder(config, client).build_monthly([case()], period=MONTHLY_PERIOD)


def test_both_prompts_explain_the_three_diagram_types(
    config: IntelligenceConfig,
) -> None:
    """プロンプトが案内する種類とスキーマの種類が同じであること。"""
    weekly_prompt = build_weekly_narrative_prompt([record()], config)
    monthly_prompt = build_monthly_narrative_prompt(
        [case()], config, period=MONTHLY_PERIOD
    )

    for prompt in (weekly_prompt, monthly_prompt):
        for name in DIAGRAM_TYPES:
            assert name in prompt
        assert "null にする" in prompt


def test_the_monthly_schema_without_cases_has_no_diagram_field() -> None:
    """事例を渡さずにスキーマだけ組むとき（プロンプト検査など）は欄を作らない。"""
    assert (
        "case_diagrams" not in build_monthly_narrative_schema([CHAPTER_1]).model_fields
    )
    assert (
        "case_diagrams" in build_monthly_narrative_schema([CHAPTER_1], [1]).model_fields
    )
