"""週刊メルマガ レンダラ（T-24 ／ 設計書 §7.3 ／ 仕様書 §9）。

重点（§7.3 のマッピング表の各行）:

- **採用条件**（`採用区分 ≠ 不採用` かつ `合計スコア ≥ min_total_score_to_publish`）
- **業界振り分け**（`業界` に `target_industry` を含む → 業界関連／それ以外 → 共通）
- **上限件数**（`max_industry_topics` / `max_common_topics`）と**合計スコア降順**
- カード5要素（カテゴリラベルの色／タイトル `<a>`／一言要約／示唆ボックス／出典行）
- **§7.1 の禁止事項が出力に混ざらない**（T-23 の lint を通す）
- **生成テキスト**（今週のポイント・示唆）は渡された分だけ出す。
  `point_of_week_required=true` で空なら落とす
- ゴールデンファイル比較（体裁の回帰）
"""

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from adapter.html import mail_html
from adapter.html.category_colors import CATEGORY_COLORS
from adapter.html.weekly_renderer import (
    BRAND_TITLE,
    COMMON_SECTION_HEADING,
    FOOTER_NOTE,
    INDUSTRY_SECTION_FORMAT,
    NOT_ADOPTED,
    POINT_OF_WEEK_HEADING,
    REFERENCED_COLUMNS,
    WeeklyNarrative,
    WeeklyRenderer,
    WeeklyRenderError,
    industries_of,
    is_adopted,
    render_weekly_html,
    select_articles,
)
from adapter.storage.artifact_store import ArtifactStore
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.report_columns import (
    MULTI_VALUE_SEPARATOR,
    WEEKLY_ARTICLE_COLUMNS,
    WEEKLY_ARTICLE_COLUMNS_BY_NAME,
)

INITIAL_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)
GOLDEN_PATH = (
    Path(__file__).parent
    / "golden"
    / "weekly_ai_intelligence_newsletter_不動産_2026-W31.html"
)

PERIOD = "2026-W31"
REVISION = 3
RUN_ID = "run-0001"


@pytest.fixture
def config() -> IntelligenceConfig:
    """仕様書 §5.2 の確定 config（テストごとに使い捨て）。"""
    raw = json.loads(INITIAL_CONFIG_PATH.read_text(encoding="utf-8"))
    return IntelligenceConfig.model_validate(copy.deepcopy(raw))


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path)


def article(
    *,
    title: str = "大手不動産がAIエージェントで契約業務を自動化",
    url: str = "https://example.com/news/1",
    source: str = "ITmedia",
    summary: str = "AIエージェントが契約書のドラフトを作る。担当者は確認に専念する。",
    category: str = "ai_agent_automation",
    total: int = 88,
    adoption_class: str = "次回定例で提案",
    industries: tuple[str, ...] = ("不動産",),
    collected_at: str = "2026-07-28",
) -> dict[str, Any]:
    """当週シートの1行（列名 → 値。T-22 のリーダが返す形）。"""
    values: dict[str, Any] = {
        "収集日": collected_at,
        "情報カテゴリ": category,
        "タイトル": title,
        "一言要約": summary,
        "合計スコア": total,
        "レポート採用区分": adoption_class,
        "実務活用可能性": "高",
        "顧客関連度": "高",
        "信頼性": "一次情報",
        "地域": ["国内"],
        "情報種別": "ニュース",
        "業務領域": ["営業"],
        "業界": list(industries),
        "AIテーマ": ["AIエージェント"],
        "ソース": source,
        "URL": url,
    }
    for column in WEEKLY_ARTICLE_COLUMNS:
        if column.axis_id is not None:
            values.setdefault(column.name, 10)
    missing = [c.name for c in WEEKLY_ARTICLE_COLUMNS if c.name not in values]
    assert not missing, f"テスト側の行が22列を満たしていません: {missing}"
    return values


def render(
    articles: list[dict[str, Any]],
    config: IntelligenceConfig,
    narrative: WeeklyNarrative | None = None,
) -> str:
    return render_weekly_html(
        period=PERIOD,
        articles=articles,
        config=config,
        narrative=narrative or WeeklyNarrative(point_of_week="今週の総括。"),
    )


# --- 列定義との接続 -----------------------------------------------------------


def test_every_referenced_column_exists_in_the_shared_definition() -> None:
    """列名を変えられたら黙って空欄のカードを出すのではなく気づく（T-07）。"""
    assert all(name in WEEKLY_ARTICLE_COLUMNS_BY_NAME for name in REFERENCED_COLUMNS)


def test_the_not_adopted_class_comes_from_the_enum_not_a_literal() -> None:
    assert NOT_ADOPTED == "不採用"


# --- 採用条件（§9.3）---------------------------------------------------------


def test_an_article_below_the_publish_threshold_is_not_adopted(
    config: IntelligenceConfig,
) -> None:
    threshold = config.tunable_thresholds.min_total_score_to_publish

    assert is_adopted(article(total=threshold), config)
    assert not is_adopted(article(total=threshold - 1), config)


def test_an_article_classified_as_not_adopted_is_dropped_even_with_a_high_score(
    config: IntelligenceConfig,
) -> None:
    assert not is_adopted(article(total=100, adoption_class=NOT_ADOPTED), config)


def test_an_article_without_a_total_score_is_not_adopted(
    config: IntelligenceConfig,
) -> None:
    """§12.1 は合計スコアを非空必須にしている（空なら検証を通っていない行）。"""
    record = article()
    record["合計スコア"] = None

    assert not is_adopted(record, config)


def test_only_adopted_articles_become_cards(config: IntelligenceConfig) -> None:
    articles = [
        article(title="採用", total=90),
        article(title="低スコア", total=10),
        article(title="不採用区分", total=95, adoption_class=NOT_ADOPTED),
    ]

    markup = render(articles, config)

    assert "採用" in markup
    assert "低スコア" not in markup
    assert "不採用区分" not in markup


# --- 業界振り分けと上限・並び順（§7.3・§9.3）--------------------------------


def test_articles_tagged_with_the_target_industry_go_to_the_industry_section(
    config: IntelligenceConfig,
) -> None:
    selection = select_articles(
        [
            article(title="不動産の記事", industries=("不動産", "業界横断")),
            article(title="横断の記事", industries=("業界横断",)),
            article(title="他業界の記事", industries=("金融",)),
        ],
        config,
    )

    assert [r["タイトル"] for r in selection.industry_topics] == ["不動産の記事"]
    assert [r["タイトル"] for r in selection.common_topics] == [
        "横断の記事",
        "他業界の記事",
    ]


def test_the_industry_column_is_read_whether_it_is_a_list_or_a_joined_string() -> None:
    joined = MULTI_VALUE_SEPARATOR.join(["不動産", "業界横断"])

    assert industries_of({"業界": ["不動産", "業界横断"]}) == ("不動産", "業界横断")
    assert industries_of({"業界": joined}) == ("不動産", "業界横断")
    assert industries_of({}) == ()


def test_each_section_is_capped_by_its_own_limit(config: IntelligenceConfig) -> None:
    weekly = config.tunable_thresholds.weekly
    articles = [
        article(title=f"業界{i}", url=f"https://example.com/i/{i}", total=90 - i)
        for i in range(weekly.max_industry_topics + 3)
    ] + [
        article(
            title=f"共通{i}",
            url=f"https://example.com/c/{i}",
            total=80 - i,
            industries=("業界横断",),
        )
        for i in range(weekly.max_common_topics + 3)
    ]

    selection = select_articles(articles, config)

    assert len(selection.industry_topics) == weekly.max_industry_topics
    assert len(selection.common_topics) == weekly.max_common_topics
    assert selection.adopted == len(articles)


def test_cards_are_ordered_by_total_score_descending(
    config: IntelligenceConfig,
) -> None:
    """§9.3「並び順は合計スコア降順」。シートの並びに依存しない。"""
    selection = select_articles(
        [
            article(title="中", total=70),
            article(title="高", total=95),
            article(title="低", total=61),
        ],
        config,
    )

    assert [r["タイトル"] for r in selection.industry_topics] == ["高", "中", "低"]


def test_articles_of_equal_score_keep_the_order_they_arrived_in(
    config: IntelligenceConfig,
) -> None:
    selection = select_articles(
        [article(title="先", total=80), article(title="後", total=80)], config
    )

    assert [r["タイトル"] for r in selection.industry_topics] == ["先", "後"]


def test_an_article_without_a_title_is_not_turned_into_a_card(
    config: IntelligenceConfig,
) -> None:
    """T-07 申し送り：§12.1 の非空必須にタイトルが無いのでここでガードする。"""
    selection = select_articles(
        [article(title="", total=90), article(title="あり", total=85)], config
    )

    assert [r["タイトル"] for r in selection.industry_topics] == ["あり"]
    assert selection.untitled == 1
    assert selection.adopted == 2


def test_a_section_with_no_article_is_omitted(config: IntelligenceConfig) -> None:
    markup = render([article(industries=("不動産",))], config)

    assert INDUSTRY_SECTION_FORMAT.format(industry="不動産") in markup
    assert COMMON_SECTION_HEADING not in markup


# --- ヘッダ・フッタ（§9.2-1・§9.2-5）----------------------------------------


def test_the_header_carries_the_brand_industry_and_week(
    config: IntelligenceConfig,
) -> None:
    markup = render([article()], config)

    assert BRAND_TITLE in markup
    assert "不動産 版" in markup
    assert "対象週：2026-W31" in markup
    assert "linear-gradient(135deg,#4f46e5,#7c3aed)" in markup


def test_the_industry_name_comes_from_config_not_a_literal(
    config: IntelligenceConfig,
) -> None:
    config.tunable_thresholds.weekly.target_industry = "金融"

    markup = render([article(title="地銀のAI活用", industries=("金融",))], config)

    assert "金融 版" in markup
    assert "金融関連トピック" in markup
    assert "不動産" not in markup


def test_the_footer_states_it_is_not_advice(config: IntelligenceConfig) -> None:
    markup = render([article()], config)

    assert mail_html.escape(FOOTER_NOTE) in markup
    assert "助言ではありません" in markup


def test_the_outer_frame_is_centered_at_680px(config: IntelligenceConfig) -> None:
    markup = render([article()], config)

    assert "max-width:680px" in markup
    assert "margin:0 auto" in markup
    assert "background-color:#f3f4f6" in markup


# --- カード5要素（§9.2-4）----------------------------------------------------


def test_a_card_shows_the_category_label_in_its_mapped_color(
    config: IntelligenceConfig,
) -> None:
    markup = render([article(category="ai_governance_risk")], config)

    assert "AIガバナンス・法規制・リスク" in markup  # ラベルは config が正
    assert f"color:{CATEGORY_COLORS['ai_governance_risk']}" in markup


def test_a_card_links_the_title_to_the_url_column(config: IntelligenceConfig) -> None:
    markup = render([article(url="https://example.com/news/42")], config)

    assert 'href="https://example.com/news/42"' in markup


def test_a_card_shows_the_one_line_summary(config: IntelligenceConfig) -> None:
    markup = render([article(summary="要約テキスト。")], config)

    assert "要約テキスト。" in markup


def test_a_card_ends_with_the_source_line(config: IntelligenceConfig) -> None:
    markup = render([article(source="日経クロステック")], config)

    assert "出典：日経クロステック ／ " in markup
    assert "記事を読む" in markup


def test_an_unknown_category_still_renders_a_card(config: IntelligenceConfig) -> None:
    """色が引けないだけで記事を落とさない（T-23）。"""
    record = article(title="未知カテゴリ")
    record["情報カテゴリ"] = "does_not_exist"

    markup = render([record], config)

    assert "未知カテゴリ" in markup


# --- 生成テキスト（§7.3 の（生成テキスト）行）--------------------------------


def test_the_point_of_the_week_is_rendered_when_it_is_given(
    config: IntelligenceConfig,
) -> None:
    markup = render(
        [article()],
        config,
        WeeklyNarrative(point_of_week="第1段落。\n\n第2段落。"),
    )

    assert POINT_OF_WEEK_HEADING in markup
    assert "<p" in markup
    assert "第1段落。" in markup
    assert "第2段落。" in markup


def test_rendering_fails_when_the_required_point_of_the_week_is_missing(
    config: IntelligenceConfig,
) -> None:
    """§9.2-2「`point_of_week_required=true` の場合は必須」。黙って省かない。"""
    assert config.tunable_thresholds.weekly.point_of_week_required is True

    with pytest.raises(WeeklyRenderError, match="今週のポイント"):
        render_weekly_html(
            period=PERIOD,
            articles=[article()],
            config=config,
            narrative=WeeklyNarrative(),
        )


def test_the_point_of_the_week_may_be_omitted_when_config_does_not_require_it(
    config: IntelligenceConfig,
) -> None:
    config.tunable_thresholds.weekly.point_of_week_required = False

    markup = render_weekly_html(
        period=PERIOD, articles=[article()], config=config, narrative=None
    )

    assert POINT_OF_WEEK_HEADING not in markup


def test_the_insight_box_is_rendered_for_the_article_it_belongs_to(
    config: IntelligenceConfig,
) -> None:
    """§9.2-4 示唆ボックス（背景 `#eef2ff`・左罫 `#6366f1`）。鍵は URL。"""
    markup = render(
        [
            article(title="示唆あり", url="https://example.com/a"),
            article(title="示唆なし", url="https://example.com/b"),
        ],
        config,
        WeeklyNarrative(
            point_of_week="総括。",
            insights={"https://example.com/a": "自社では取引書類から始められる。"},
        ),
    )

    assert "自社では取引書類から始められる。" in markup
    assert markup.count("background-color:#eef2ff") == 1
    assert "border-left:3px solid #6366f1" in markup


def test_a_card_without_an_insight_has_no_empty_box(
    config: IntelligenceConfig,
) -> None:
    markup = render([article()], config)

    assert "#eef2ff" not in markup


# --- §7.1 の制約とエスケープ --------------------------------------------------


def test_the_rendered_document_contains_no_forbidden_construct(
    config: IntelligenceConfig,
) -> None:
    markup = render(
        [article(), article(url="https://example.com/2", industries=("業界横断",))],
        config,
        WeeklyNarrative(
            point_of_week="総括。", insights={"https://example.com/1": "示唆。"}
        ),
    )

    assert mail_html.forbidden_constructs(markup) == []
    assert '<meta charset="utf-8">' in markup


def test_markup_inside_an_article_title_is_escaped(
    config: IntelligenceConfig,
) -> None:
    """タイトル・要約・ソースは crawl が拾ってきた外部テキスト（T-16）。"""
    markup = render(
        [
            article(
                title='<script>alert("x")</script>',
                summary="<b>強調</b> & その他",
                source='"媒体"',
            )
        ],
        config,
    )

    assert mail_html.forbidden_constructs(markup) == []
    assert "&lt;script&gt;" in markup
    assert "&lt;b&gt;強調&lt;/b&gt;" in markup
    assert "&amp;" in markup


def test_an_article_whose_url_is_not_http_keeps_its_card_without_a_link(
    config: IntelligenceConfig,
) -> None:
    markup = render([article(title="怪しいURL", url="javascript:alert(1)")], config)

    assert "怪しいURL" in markup
    assert mail_html.forbidden_constructs(markup) == []
    assert "javascript:" not in markup


# --- period と書き出し --------------------------------------------------------


def test_a_monthly_period_is_refused(config: IntelligenceConfig) -> None:
    with pytest.raises(WeeklyRenderError, match="週次"):
        render_weekly_html(
            period="2026-07",
            articles=[article()],
            config=config,
            narrative=WeeklyNarrative(point_of_week="総括。"),
        )


def test_a_week_that_does_not_exist_is_refused(config: IntelligenceConfig) -> None:
    """2025 は53週を持たない（表記の検査だけでは通ってしまう。T-21 の period）。"""
    with pytest.raises(WeeklyRenderError):
        render_weekly_html(
            period="2025-W53",
            articles=[article()],
            config=config,
            narrative=WeeklyNarrative(point_of_week="総括。"),
        )


def test_the_output_file_name_follows_the_canonical_resolution(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """T-02 の `weekly_html_path()` が正規名の唯一の解決元。"""
    result = WeeklyRenderer(store).render(
        period=PERIOD,
        articles=[article()],
        config=config,
        narrative=WeeklyNarrative(point_of_week="総括。"),
        revision=REVISION,
        run_id=RUN_ID,
    )

    assert result.path == store.weekly_html_path("不動産", PERIOD)
    assert result.path.name == (
        "weekly_ai_intelligence_newsletter_不動産_2026-W31.html"
    )
    assert result.path.read_text(encoding="utf-8") == result.markup
    assert result.cards == 1
    assert result.archived is None


def test_the_previous_version_is_archived_before_the_overwrite(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """設計判断B。⚠️ 退避が後だと、退避されるのが新しい内容になる。"""
    renderer = WeeklyRenderer(store)
    first = renderer.render(
        period=PERIOD,
        articles=[article(title="1回目")],
        config=config,
        narrative=WeeklyNarrative(point_of_week="総括。"),
        revision=REVISION,
        run_id=RUN_ID,
    )
    second = renderer.render(
        period=PERIOD,
        articles=[article(title="2回目")],
        config=config,
        narrative=WeeklyNarrative(point_of_week="総括。"),
        revision=REVISION,
        run_id="run-0002",
    )

    assert second.archived is not None
    assert second.archived.read_text(encoding="utf-8") == first.markup
    assert "2回目" in second.path.read_text(encoding="utf-8")


def test_rerunning_the_same_period_does_not_create_a_second_file(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """§14 冪等性：正規名は upsert（T-22 と同じ）。"""
    renderer = WeeklyRenderer(store)
    for run in ("run-0001", "run-0002"):
        renderer.render(
            period=PERIOD,
            articles=[article()],
            config=config,
            narrative=WeeklyNarrative(point_of_week="総括。"),
            revision=REVISION,
            run_id=run,
        )

    assert len(list(store.root.glob("*.html"))) == 1


def test_the_file_is_written_as_utf8(
    store: ArtifactStore, config: IntelligenceConfig
) -> None:
    """§14「入出力はすべて UTF-8」。"""
    result = WeeklyRenderer(store).render(
        period=PERIOD,
        articles=[article()],
        config=config,
        narrative=WeeklyNarrative(point_of_week="総括。"),
        revision=REVISION,
        run_id=RUN_ID,
    )

    assert "不動産".encode() in result.path.read_bytes()


# --- 再現性とゴールデンファイル ----------------------------------------------


def test_the_same_input_always_produces_the_same_output(
    config: IntelligenceConfig,
) -> None:
    """§14 再現性。render は AI を呼ばない（§1.1）ので出力は入力だけで決まる。"""
    articles = [article(), article(url="https://example.com/2")]
    narrative = WeeklyNarrative(point_of_week="総括。")

    assert render(articles, config, narrative) == render(articles, config, narrative)


def golden_articles() -> list[dict[str, Any]]:
    """ゴールデンファイル用の当週シート（体裁の各要素が1度は出る形）。"""
    return [
        article(
            title="大手不動産がAIエージェントで契約業務を自動化",
            url="https://example.com/news/1",
            source="ITmedia",
            summary="AIエージェントが契約書のドラフトを作る。担当者は確認に専念する。",
            category="ai_agent_automation",
            total=88,
            industries=("不動産",),
        ),
        article(
            title="賃貸仲介の問い合わせ対応をAIが一次受け",
            url="https://example.com/news/2",
            source="日経クロステック",
            summary="夜間の問い合わせをAIが受ける。翌朝の折り返し件数が減った。",
            category="enterprise_ai_case",
            total=81,
            industries=("不動産", "業界横断"),
        ),
        article(
            title="主要AIベンダが長文脈モデルを更新",
            url="https://example.com/news/3",
            source="TechCrunch",
            summary="扱える文脈長が伸びた。長い資料を分割せずに渡せる。",
            category="ai_major_company_model",
            total=76,
            industries=("業界横断",),
        ),
        article(
            title="AI利用に関する社内規程の整備が進む",
            url="https://example.com/news/4",
            source="日経新聞",
            summary="規程を整える企業が増えた。持ち出し可能な情報の線引きが論点。",
            category="ai_governance_risk",
            total=70,
            industries=("業界横断",),
        ),
        article(
            title="スコアが基準に届かなかった記事",
            url="https://example.com/news/5",
            source="ブログ",
            total=42,
            industries=("業界横断",),
        ),
    ]


def golden_narrative() -> WeeklyNarrative:
    return WeeklyNarrative(
        point_of_week=(
            "今週は不動産業務そのものへAIが入り込む動きが目立った。"
            "契約と問い合わせという定型度の高い業務が先に置き換わっている。"
            "モデル側の更新も長文脈が中心で、社内資料をそのまま扱う方向と噛み合う。"
        ),
        insights={
            "https://example.com/news/1": (
                "自社では契約書のひな型が揃っている領域から試すのが早い。"
            ),
            "https://example.com/news/3": (
                "分割前提で組んだ社内の前処理を見直す余地がある。"
            ),
        },
    )


def test_the_rendered_html_matches_the_golden_file(
    config: IntelligenceConfig,
) -> None:
    """体裁の回帰を止める。

    ⚠️ **意図した体裁変更のときだけ** `UPDATE_GOLDEN=1 uv run pytest` で
    作り直し、生成物の差分をレビューすること。
    """
    markup = render_weekly_html(
        period=PERIOD,
        articles=golden_articles(),
        config=config,
        narrative=golden_narrative(),
    )

    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(markup, encoding="utf-8")

    assert markup == GOLDEN_PATH.read_text(encoding="utf-8")


def test_the_golden_file_itself_satisfies_the_mail_html_constraints() -> None:
    """ゴールデンを更新したときに §7.1 を割らないための歯止め。"""
    assert mail_html.forbidden_constructs(GOLDEN_PATH.read_text(encoding="utf-8")) == []
