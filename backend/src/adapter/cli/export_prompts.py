"""パイプラインで実際に使われているプロンプトを `prompts/` へ書き出す。

    make prompts         # 生成して上書き
    make prompts-check   # コミット済みファイルが最新かを検査（CI 向け）

**正はコード**（`application.usecases.*` の `build_*_prompt()`）。`prompts/` の
Markdown は**その描画結果**であって、手で編集しない。`schemas/config.schema.json`
（`export_config_schema`）と同じ扱いで、外部（PM レビュー・運用ドキュメント）が
本文を読むためのリポジトリ資産としてコミットしておく。

⚠️ **「PM が読むファイル」と「実際に走るプロンプト」を乖離させないための仕組み**が
この CLI の目的（T-30）。乖離は次の2つで塞いでいる:

1. 本文はコードの `build_*_prompt()` を**そのまま呼んで**描画する（写しを書かない）
2. `--check` と `tests/adapter/test_export_prompts.py` が、コミット済みファイルと
   描画結果の**完全一致**を検査する（`make test` で落ちる）

⚠️ **`prompts/PROMPT-3-*.md` と `prompts/README.md` はこの CLI の管轄外**（手書き）。
PROMPT-3 は実行経路に無く（render は決定的 Python テンプレート＝T-24/T-25）、
描画元になるコードが存在しないため。

---

**描画に使う config と入力**

プロンプトの本文は**実行時の config から組み立てられる**（7カテゴリ・10必須タグ・
6軸の得点帯・13除外ルール・対象業界）。したがって描画には config が要る。
`--config` の既定は**仕様書 §5.2 の確定 config**
（`tests/enterprise/data/config_initial.json`）で、Makefile と drift 検査テストは
これで揃えてある。
運用中の値で読みたいときは `--config artifacts/config.json` を渡す。

記事・行・事例は**描画のためのサンプル**（下の `SAMPLE_*`）。実行時はその週・その月の
実データが入る。サンプルを変えると生成物が変わるので、変えたら `make prompts` を
流し直すこと。
"""

from argparse import ArgumentParser
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from adapter.llm.claude_cli_client import OUTPUT_INSTRUCTIONS
from application.usecases.classify_and_score import (
    PROMPT_NAME as CLASSIFY_PROMPT_NAME,
)
from application.usecases.classify_and_score import (
    PROMPT_VERSION as CLASSIFY_PROMPT_VERSION,
)
from application.usecases.classify_and_score import (
    build_classification_prompt,
)
from application.usecases.crawl import (
    PROMPT_NAME as CRAWL_PROMPT_NAME,
)
from application.usecases.crawl import (
    PROMPT_VERSION as CRAWL_PROMPT_VERSION,
)
from application.usecases.crawl import (
    build_crawl_prompt,
)
from application.usecases.monthly_cases import (
    CASE_PROMPT_NAME,
    CASE_PROMPT_VERSION,
    CHAPTER_PROMPT_NAME,
    CHAPTER_PROMPT_VERSION,
    CaseCandidate,
    MonthlyCase,
    build_case_prompt,
    build_chapter_prompt,
)
from application.usecases.narrative import (
    MONTHLY_NARRATIVE_PROMPT_NAME,
    MONTHLY_NARRATIVE_PROMPT_VERSION,
    WEEKLY_NARRATIVE_PROMPT_NAME,
    WEEKLY_NARRATIVE_PROMPT_VERSION,
    build_monthly_narrative_prompt,
    build_weekly_narrative_prompt,
)
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.json_document import parse_json_document
from enterprise.entities.period import parse_period
from enterprise.entities.raw_article import RawArticle

ENCODING = "utf-8"

CONFIG_ADAPTER: TypeAdapter[IntelligenceConfig] = TypeAdapter(IntelligenceConfig)

# backend/src/adapter/cli/export_prompts.py → リポジトリルート
BACKEND_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_ROOT.parent

# `prompts/` はリポジトリルート（設計書 §9.2 ／ T-30 成果物）。backend 配下ではなく
# ルートに置くのは、PM・編集担当が読む運用ドキュメントでもあるため（docs/ と同じ）。
DEFAULT_OUTPUT_DIR = REPO_ROOT / "prompts"

# 描画に使う config の既定。仕様書 §5.2 の確定値（xlsx 実データから起こした初期値）。
# ⚠️ 実行時に読む config ではない（実行時は `artifacts/config.json`）。ここは
# 「どの config で描画したか」を固定して差分を読める形にするためのもの。
DEFAULT_CONFIG = BACKEND_ROOT / "tests" / "enterprise" / "data" / "config_initial.json"

EXIT_OK = 0
EXIT_STALE = 1

# 出力形式の指示（全プロンプトの末尾に付く共通部分）のファイル名。config を使わない
# ので、config 由来の注記を出さない唯一のファイル。
COMMON_STEM = "COMMON-OUTPUT-INSTRUCTIONS"

# --- 描画用サンプル（実行時は実データが入る）---------------------------------

SAMPLE_WEEKLY_PERIOD = "2026-W33"
SAMPLE_MONTHLY_PERIOD = "2026-07"
SAMPLE_COLLECTED_AT = date(2026, 8, 17)

SAMPLE_ARTICLE = RawArticle(
    collected_at="2026-08-17",
    published_at="2026-08-12",
    title="大手不動産会社がAIエージェントで契約書チェックを自動化",
    url="https://example.com/news/1",
    source="ITmedia",
    raw_summary=(
        "国内大手の不動産会社がAIエージェントを導入したと発表した。"
        "契約書のチェック業務の一部を自動化し、担当者の確認時間を短縮したという。"
    ),
    region_hint="日本",
    primary_or_secondary="報道",
)

SAMPLE_WEEKLY_RECORDS: tuple[dict[str, Any], ...] = (
    {
        "タイトル": SAMPLE_ARTICLE.title,
        "URL": SAMPLE_ARTICLE.url,
        "情報カテゴリ": "enterprise_ai_case",
        "業界": ["不動産"],
        "ソース": SAMPLE_ARTICLE.source,
        "合計スコア": 82,
        "一言要約": "大手不動産会社が契約書チェックにAIエージェントを導入した。"
        "確認時間の短縮が報告されている。",
    },
    {
        "タイトル": "主要AIベンダーが業務自動化向けの新モデルを公開",
        "URL": "https://example.com/news/2",
        "情報カテゴリ": "ai_major_company_model",
        "業界": ["業界横断"],
        "ソース": "TechCrunch",
        "合計スコア": 74,
        "一言要約": "主要ベンダーが業務自動化を想定した新モデルを公開した。"
        "長時間の処理を前提とした構成が特徴とされる。",
    },
)

SAMPLE_MONTHLY_CASES: tuple[MonthlyCase, ...] = (
    MonthlyCase(
        no=1,
        chapter="第1章 契約・審査業務の自動化",
        organizations=("大手不動産会社A",),
        title="契約書チェックをAIエージェントへ移した事例",
        url=SAMPLE_ARTICLE.url,
        source_text="ITmedia（2026-08-12）",
        month=SAMPLE_MONTHLY_PERIOD,
        paragraphs=(
            "大手不動産会社Aが、契約書チェック業務にAIエージェントを導入した。",
            "対象は定型条項の突き合わせで、担当者は差分の確認だけを行う運用に変えた。",
            "定型業務の切り出し方が成否を分ける、という読み取りができる。",
        ),
        article=SAMPLE_ARTICLE,
    ),
)

SAMPLE_CASE_CANDIDATE = CaseCandidate(
    article=SAMPLE_ARTICLE,
    total_score=82,
    summary="大手不動産会社が契約書チェックにAIエージェントを導入した。"
    "確認時間の短縮が報告されている。",
)

SAMPLE_CHAPTER_THEMES: tuple[str, ...] = (
    "契約・審査業務の自動化",
    "問い合わせ対応の自動化",
    "設計・提案業務の支援",
)

SAMPLE_CHAPTER_COUNT_HINT = 5

# --- 生成物の見出し ----------------------------------------------------------

GENERATED_NOTICE = """> ⚠️ **このファイルは生成物です。**
> 本文は実行時と同じコードを**そのまま呼んで**
> 描画したもので、ここを手で編集しても実行されるプロンプトは変わりません。
> 本文を変えるときは**コード側を直し、`prompt_version` を上げ、`make prompts` で
> 生成し直して同じ PR に含める**こと（[版管理ルール](./README.md)）。
> コミット済みファイルとの一致は `make prompts-check` ／ `make test` が検査します。"""

CONFIG_NOTE = """> 本文のうち
> **カテゴリ・必須タグ・得点帯・除外ルール・対象業界・しきい値**は
> config から差し込まれます（そのプロンプトが使うぶんだけ）。下の描画に使ったのは
> **仕様書 §5.2 の確定 config**
> （`backend/tests/enterprise/data/config_initial.json`・revision {revision}）です。
> 運用中の値は管理画面で変更できるため、実行時の本文はその時点の config に従います。"""

OUTPUT_INSTRUCTIONS_NOTE = """> 実際に送られる本文は、この後ろに
> [`COMMON-OUTPUT-INSTRUCTIONS.md`](./COMMON-OUTPUT-INSTRUCTIONS.md)
> （出力形式の指示と
> JSON Schema）が付いた形になります（AI クライアント層が付与）。"""

COMMON_BODY_NOTE = """> これは**すべてのプロンプト本文の末尾に付く共通部分**です。
> 単体では送られません。
> `{JSON Schema（出力スキーマから生成）}` の位置に、その呼び出しの出力スキーマ
> （Pydantic モデルから生成した JSON Schema）が差し込まれます。"""

# 本文に ``` を含むプロンプト（出力形式の指示）があるので、囲みは4連バッククォート。
BODY_FENCE = "````"


@dataclass(frozen=True, slots=True)
class PromptDoc:
    """`prompts/*.md` 1ファイルぶんの定義。

    Attributes:
        stem: ファイル名（拡張子なし）
        title: 見出し
        prompt_name: 実行時の識別子（`PROMPT_NAME` 定数）
        version: `prompt_version`（**コードの定数から取る**。ここに写さない）
        updated: 最終更新日（**本文を変えた日**。版と一緒に更新する）
        stage: パイプライン上の段
        spec_refs: 仕様書・設計書の対応箇所
        source: 本文を組み立てているコード
        variables: 設計書 §9.1 の「変数 → 注入元」
        bodies: （見出し, config を受け取って本文を返す関数）。
            **1プロンプト＝1ファイル**
            を保つため、週次/月次のように分岐するものは同じファイルに並べる
        notes: 補足（描画サンプルの説明など）
    """

    stem: str
    title: str
    prompt_name: str
    version: str
    updated: str
    stage: str
    spec_refs: str
    source: str
    variables: tuple[tuple[str, str], ...]
    bodies: tuple[tuple[str, Callable[[IntelligenceConfig], str]], ...]
    notes: tuple[str, ...] = field(default=())


def _crawl_weekly(config: IntelligenceConfig) -> str:
    return build_crawl_prompt(
        SAMPLE_WEEKLY_PERIOD, config, collected_at=SAMPLE_COLLECTED_AT
    )


def _crawl_monthly(config: IntelligenceConfig) -> str:
    return build_crawl_prompt(
        SAMPLE_MONTHLY_PERIOD, config, collected_at=SAMPLE_COLLECTED_AT
    )


def _classification(config: IntelligenceConfig) -> str:
    return build_classification_prompt(SAMPLE_ARTICLE, config)


def _weekly_narrative(config: IntelligenceConfig) -> str:
    return build_weekly_narrative_prompt(
        SAMPLE_WEEKLY_RECORDS,
        config,
        industry=config.tunable_thresholds.weekly.industries[0],
    )


def _monthly_narrative(config: IntelligenceConfig) -> str:
    return build_monthly_narrative_prompt(
        SAMPLE_MONTHLY_CASES, config, period=parse_period(SAMPLE_MONTHLY_PERIOD)
    )


def _monthly_case(config: IntelligenceConfig) -> str:
    return build_case_prompt(SAMPLE_CASE_CANDIDATE, config)


def _monthly_chapters(_: IntelligenceConfig) -> str:
    return build_chapter_prompt(SAMPLE_CHAPTER_THEMES, SAMPLE_CHAPTER_COUNT_HINT)


def _output_instructions(_: IntelligenceConfig) -> str:
    return OUTPUT_INSTRUCTIONS.format(schema="{JSON Schema（出力スキーマから生成）}")


# ⚠️ **実行経路にあるプロンプトはここに全部載せる。** 載せ忘れたものは PM の目に
# 触れないまま走り続ける（それが T-30 で塞ぎたい乖離そのもの）。
PROMPT_DOCS: tuple[PromptDoc, ...] = (
    PromptDoc(
        stem="PROMPT-1",
        title="PROMPT-1 — クローリング収集（crawl）",
        prompt_name=CRAWL_PROMPT_NAME,
        version=CRAWL_PROMPT_VERSION,
        updated="2026-08-17",
        stage="crawl（パイプライン1段目）",
        spec_refs="仕様書 §13.2 ／ 設計書 §9.1・§8.2",
        source="`backend/src/application/usecases/crawl.py` の `build_crawl_prompt()`",
        variables=(
            ("`{{PERIOD}}`", "ジョブの period（仕様書 §13.1）。`2026-W33` / `2026-07`"),
            (
                "`{{PERIOD_RANGE}}`",
                "period を開いた実日付（`enterprise.entities.period`）",
            ),
            ("`{{COLLECTED_AT}}`", "実行日（`Settings.tzinfo` の今日）"),
            (
                "`{{information_categories}}`",
                "`config.information_categories`（7カテゴリの id・ラベル・説明）",
            ),
            (
                "`{{weekly.target_industries}}`",
                "`config.tunable_thresholds.weekly.target_industries`（T-46 Step 1）",
            ),
        ),
        bodies=(
            ("週次（`period = 2026-W33`）", _crawl_weekly),
            ("月次（`period = 2026-07`）", _crawl_monthly),
        ),
        notes=(
            "週次と月次で入れ替わるのは**「収集範囲の観点」の最終行（収集の重心）"
            "だけ**です。差分が読めるよう両方を載せています。",
            "**出力形式（JSON だけを出す指示と JSON Schema）はこの本文に含みません。**"
            "AI クライアント層が付けます（下の注記を参照）。",
        ),
    ),
    PromptDoc(
        stem="PROMPT-2",
        title="PROMPT-2 — 分類・10必須タグ・6軸採点（filter）",
        prompt_name=CLASSIFY_PROMPT_NAME,
        version=CLASSIFY_PROMPT_VERSION,
        updated="2026-08-17",
        stage="filter（記事1件につき1往復）",
        spec_refs="仕様書 §13.3 ／ 設計書 §9.1・§6.1-3/4・§6.4",
        source=(
            "`backend/src/application/usecases/classify_and_score.py` の "
            "`build_classification_prompt()`"
        ),
        variables=(
            ("`{{ARTICLE}}`", "`raw_articles_{period}.json` の1件（そのまま JSON で）"),
            (
                "`{{weekly.target_industries}}`",
                "`config.tunable_thresholds.weekly.target_industries`（顧客関連度の基準）",
            ),
            (
                "`{{information_categories}}`",
                "`config.information_categories`（id・ラベル・優先度・説明）",
            ),
            (
                "`{{required_tags}}`",
                "`config.required_tags` と候補値（`config.enums.*`）",
            ),
            (
                "`{{scoring_axes}}`",
                "`config.scoring_axes`（配点＝実行時の `weight`・評価観点・得点帯）",
            ),
            (
                "`{{exclusion_rules}}`",
                "`config.exclusion_rules` の `no` / `name` / `examples`"
                "（**`severity` と `enabled` は載せない**）",
            ),
        ),
        bodies=(("", _classification),),
        notes=(
            "**合計スコアと `adoption_class` は載っていません。**"
            "どちらもアプリ側が config から決定的に決めるため、出力スキーマにも"
            "フィールドがありません（TASKS.md §1.1「AI利用範囲」）。",
            "設計書 §9.1 の表にある `{{dedup.*}}` はこの本文に現れません。"
            "重複・統合判定は決定的 Python（T-18）が持ち、AI に聞かないためです。",
        ),
    ),
    PromptDoc(
        stem="PROMPT-2-NARRATIVE-WEEKLY",
        title="PROMPT-2（週次 narrative） — 今週のポイント・記事ごとの示唆",
        prompt_name=WEEKLY_NARRATIVE_PROMPT_NAME,
        version=WEEKLY_NARRATIVE_PROMPT_VERSION,
        updated="2026-08-17",
        stage="filter の内部（**対象業界ごとに1往復**）",
        spec_refs="仕様書 §9.2-2・§9.2-4 ／ 設計書 §7.3",
        source=(
            "`backend/src/application/usecases/narrative.py` の "
            "`build_weekly_narrative_prompt()`"
        ),
        variables=(
            (
                "`{{target_industry}}`",
                "`config.tunable_thresholds.weekly.target_industries` の**1つ**"
                "（週刊は業界ごとに1通。T-46 Step 4）",
            ),
            (
                "`{{POINT_OF_WEEK_SENTENCES}}`",
                "`narrative.POINT_OF_WEEK_MIN/MAX_SENTENCES`"
                "（仕様書 §9.2-2 の 3〜4文）",
            ),
            (
                "`{{ARTICLES}}`",
                "当週シートの採用行（22列のうちタイトル・URL・カテゴリ・業界・出典・"
                "合計スコア・一言要約）",
            ),
        ),
        bodies=(("", _weekly_narrative),),
        notes=(
            "掲載順・上限（`weekly.max_industry_topics` / `max_common_topics`）は"
            "レンダラ（T-24）が持ちます。ここでは**当週シートの全行**に示唆を書かせます。",
        ),
    ),
    PromptDoc(
        stem="PROMPT-2-NARRATIVE-MONTHLY",
        title="PROMPT-2（月次 narrative） — 巻頭言・章の導入文・むすび",
        prompt_name=MONTHLY_NARRATIVE_PROMPT_NAME,
        version=MONTHLY_NARRATIVE_PROMPT_VERSION,
        updated="2026-08-16",
        stage="filter の内部（当月ぶんで1往復）",
        spec_refs="仕様書 §10.2-2・§10.2-4・§10.2-5 ／ 設計書 §7.4",
        source=(
            "`backend/src/application/usecases/narrative.py` の "
            "`build_monthly_narrative_prompt()`"
        ),
        variables=(
            ("`{{MONTH}}`", "対象月（`period` を開いた年・月）"),
            (
                "`{{monthly.target_case_count}}`",
                "`config.tunable_thresholds.monthly.target_case_count`",
            ),
            (
                "`{{CASES}}`",
                "当月の事例（章ラベル・CASE番号・企業/組織・見出し・解説）",
            ),
        ),
        bodies=(("", _monthly_narrative),),
        notes=(
            "巻頭言3段落・むすび2段落は**出力スキーマの別フィールド**で受けます"
            "（段落数を文章で頼まない）。章の導入文の宛先（章ラベル）は `Literal` で"
            "閉じてあり、言い換えられません。",
        ),
    ),
    PromptDoc(
        stem="PROMPT-2-MONTHLY-CASE",
        title="PROMPT-2（月次 事例） — 事例1件の原稿",
        prompt_name=CASE_PROMPT_NAME,
        version=CASE_PROMPT_VERSION,
        updated="2026-08-16",
        stage="filter の内部（月次。事例1件につき1往復）",
        spec_refs="仕様書 §8.2・§13.3 出力1 ／ 設計書 §2.2.3",
        source=(
            "`backend/src/application/usecases/monthly_cases.py` の "
            "`build_case_prompt()`"
        ),
        variables=(
            ("`{{ARTICLE}}`", "昇格の判定を通った記事1件（そのまま JSON で）"),
            ("`{{SUMMARY}}` / `{{TOTAL_SCORE}}`", "分類・採点（PROMPT-2）の結果"),
            (
                "`{{monthly.chapter_count_hint}}`",
                "`config.tunable_thresholds.monthly.chapter_count_hint`",
            ),
        ),
        bodies=(("", _monthly_case),),
        notes=(
            "`出典` / `掲載月` / `URL` / `No` は**書かせません**"
            "（収集済みの事実か、アプリが決める通し番号）。",
            "事例へ昇格させるかどうかの判定は決定的（情報カテゴリ "
            "`enterprise_ai_case` ＋ `min_score_for_case` ＋ `target_case_count`）で、"
            "AI には聞きません。",
        ),
    ),
    PromptDoc(
        stem="PROMPT-2-MONTHLY-CHAPTERS",
        title="PROMPT-2（月次 章立て） — テーマを章へ束ね直す",
        prompt_name=CHAPTER_PROMPT_NAME,
        version=CHAPTER_PROMPT_VERSION,
        updated="2026-08-16",
        stage="filter の内部（月次。テーマ数が hint を超えたときだけ1往復）",
        spec_refs="仕様書 §13.3 出力1（章を5前後に束ねる）",
        source=(
            "`backend/src/application/usecases/monthly_cases.py` の "
            "`build_chapter_prompt()`"
        ),
        variables=(
            ("`{{THEMES}}`", "事例ごとに書かせた `chapter_theme` の重複除去済みの並び"),
            (
                "`{{monthly.chapter_count_hint}}`",
                "`config.tunable_thresholds.monthly.chapter_count_hint`",
            ),
        ),
        bodies=(("", _monthly_chapters),),
        notes=(
            "テーマ数が `chapter_count_hint` 以下のときは"
            "**この往復自体を行いません**。",
            "この本文は config の値を1つ（`chapter_count_hint`）しか使いません。"
            "下の描画はサンプルのテーマ3件・hint 5 のものです。",
        ),
    ),
    PromptDoc(
        stem=COMMON_STEM,
        title="共通 — 出力形式の指示（全プロンプトの末尾に付く）",
        prompt_name="AIClient/output_instructions",
        version="—（AI クライアント層の実装。プロンプトの版とは別）",
        updated="2026-08-14",
        stage="全ステージ（AI クライアント層が付与）",
        spec_refs="設計書 §9.1「指定の成果物以外を出力しない」",
        source=(
            "`backend/src/adapter/llm/claude_cli_client.py` の `OUTPUT_INSTRUCTIONS`"
        ),
        variables=(
            (
                "`{schema}`",
                "呼び出しごとの出力スキーマ（Pydantic モデルから生成した JSON Schema）",
            ),
        ),
        bodies=(("", _output_instructions),),
        notes=(
            "各プロンプト本文には出力形式の指示を**書きません**"
            "（二重指示になり、AI クライアントの実装を差し替えたときに"
            "片方だけ残るため）。"
            "だから PROMPT-1 / PROMPT-2 系の本文には「JSON だけを出す」指示が"
            "現れません。",
        ),
    ),
)


def load_config(path: Path) -> IntelligenceConfig:
    """描画に使う config を読む。

    Raises:
        DocumentParseError: JSON として読めない／スキーマに合わない場合
    """
    return parse_json_document(
        CONFIG_ADAPTER, path.read_text(encoding=ENCODING), label=str(path)
    )


def render_document(doc: PromptDoc, config: IntelligenceConfig) -> str:
    """1ファイルぶんの Markdown を組み立てる。"""
    lines = [
        "<!-- 生成物。手で編集しないこと（`make prompts` で生成し直す）。 -->",
        f"# {doc.title}",
        "",
        GENERATED_NOTICE,
        "",
        "| 項目 | 値 |",
        "|---|---|",
        f"| `prompt_version` | `{doc.version}` |",
        f"| 実行時の識別子 | `{doc.prompt_name}` |",
        f"| 用途（ステージ） | {doc.stage} |",
        f"| 対応する仕様・設計 | {doc.spec_refs} |",
        f"| 本文を組み立てているコード | {doc.source} |",
        f"| 最終更新日 | {doc.updated} |",
        "",
        "## 変数と注入元（設計書 §9.1）",
        "",
        "| 変数 | 注入元 |",
        "|---|---|",
        *(f"| {name} | {source} |" for name, source in doc.variables),
        "",
    ]
    if doc.notes:
        lines.extend(["## 補足", ""])
        lines.extend(f"- {note}" for note in doc.notes)
        lines.append("")

    lines.extend(["## 本文", ""])
    if doc.stem == COMMON_STEM:
        lines.append(COMMON_BODY_NOTE)
    else:
        lines.append(
            CONFIG_NOTE.format(revision=config.meta.revision)
            + "\n\n"
            + OUTPUT_INSTRUCTIONS_NOTE
        )
    lines.append("")

    for heading, render in doc.bodies:
        if heading:
            lines.extend([f"### {heading}", ""])
        lines.extend([f"{BODY_FENCE}text", render(config), BODY_FENCE, ""])
    return "\n".join(lines)


def render_all(config: IntelligenceConfig) -> dict[str, str]:
    """ファイル名 → 中身。"""
    return {f"{doc.stem}.md": render_document(doc, config) for doc in PROMPT_DOCS}


def write_prompts(
    config: IntelligenceConfig, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> list[Path]:
    """`prompts/` へ書き出す。

    Returns:
        書き出したパス（ファイル名順）
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in render_all(config).items():
        path = output_dir / name
        path.write_text(text, encoding=ENCODING)
        written.append(path)
    return written


def stale_files(
    config: IntelligenceConfig, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> list[Path]:
    """コミット済みファイルのうち、描画結果と食い違うもの（無いものを含む）。"""
    stale: list[Path] = []
    for name, text in render_all(config).items():
        path = output_dir / name
        if not path.is_file() or path.read_text(encoding=ENCODING) != text:
            stale.append(path)
    return stale


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"描画に使う config.json（既定: {DEFAULT_CONFIG}）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="書き出さず、コミット済みファイルが最新かだけを検査する",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    config = load_config(args.config)

    if args.check:
        if stale := stale_files(config, args.output_dir):
            print(
                "stale:\n"
                + "\n".join(f"  {path}" for path in stale)
                + "\n\nプロンプトを変えたら `make prompts` で生成し直し、"
                "`prompt_version` を上げてコミットしてください。"
            )
            return EXIT_STALE
        print(f"up to date: {args.output_dir}")
        return EXIT_OK

    for path in write_prompts(config, args.output_dir):
        print(f"wrote: {path}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_OUTPUT_DIR",
    "EXIT_OK",
    "EXIT_STALE",
    "PROMPT_DOCS",
    "PromptDoc",
    "load_config",
    "main",
    "render_all",
    "render_document",
    "stale_files",
    "write_prompts",
]
