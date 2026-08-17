<!-- 生成物。手で編集しないこと（`make prompts` で生成し直す）。 -->
# PROMPT-2（月次 事例） — 事例1件の原稿

> ⚠️ **このファイルは生成物です。**
> 本文は実行時と同じコードを**そのまま呼んで**
> 描画したもので、ここを手で編集しても実行されるプロンプトは変わりません。
> 本文を変えるときは**コード側を直し、`prompt_version` を上げ、`make prompts` で
> 生成し直して同じ PR に含める**こと（[版管理ルール](./README.md)）。
> コミット済みファイルとの一致は `make prompts-check` ／ `make test` が検査します。

| 項目 | 値 |
|---|---|
| `prompt_version` | `0.1.0` |
| 実行時の識別子 | `PROMPT-2/monthly_case` |
| 用途（ステージ） | filter の内部（月次。事例1件につき1往復） |
| 対応する仕様・設計 | 仕様書 §8.2・§13.3 出力1 ／ 設計書 §2.2.3 |
| 本文を組み立てているコード | `backend/src/application/usecases/monthly_cases.py` の `build_case_prompt()` |
| 最終更新日 | 2026-08-16 |

## 変数と注入元（設計書 §9.1）

| 変数 | 注入元 |
|---|---|
| `{{ARTICLE}}` | 昇格の判定を通った記事1件（そのまま JSON で） |
| `{{SUMMARY}}` / `{{TOTAL_SCORE}}` | 分類・採点（PROMPT-2）の結果 |
| `{{monthly.chapter_count_hint}}` | `config.tunable_thresholds.monthly.chapter_count_hint` |

## 補足

- `出典` / `掲載月` / `URL` / `No` は**書かせません**（収集済みの事実か、アプリが決める通し番号）。
- 事例へ昇格させるかどうかの判定は決定的（情報カテゴリ `enterprise_ai_case` ＋ `min_score_for_case` ＋ `target_case_count`）で、AI には聞きません。

## 本文

> 本文のうち
> **カテゴリ・必須タグ・得点帯・除外ルール・対象業界・しきい値**は
> config から差し込まれます（そのプロンプトが使うぶんだけ）。下の描画に使ったのは
> **仕様書 §5.2 の確定 config**
> （`backend/tests/enterprise/data/config_initial.json`・revision 1）です。
> 運用中の値は管理画面で変更できるため、実行時の本文はその時点の config に従います。

> 実際に送られる本文は、この後ろに
> [`COMMON-OUTPUT-INSTRUCTIONS.md`](./COMMON-OUTPUT-INSTRUCTIONS.md)
> （出力形式の指示と
> JSON Schema）が付いた形になります（AI クライアント層が付与）。

````text
あなたは月刊AIレポート（月刊ビリーフ）の編集者です。次の記事を「先進企業のAI活用事例」として紹介する原稿を書いてください。

■ 厳守事項
- 記事に書かれている事実だけを使う。数字・固有名詞を創作しない。
- 分からないことは書かない（推測で補わない）。
- URL・出典・掲載月・通し番号は**書かない**（アプリ側が埋める）。

■ 対象記事
{
  "collected_at": "2026-08-17",
  "published_at": "2026-08-12",
  "title": "大手不動産会社がAIエージェントで契約書チェックを自動化",
  "url": "https://example.com/news/1",
  "source": "ITmedia",
  "raw_summary": "国内大手の不動産会社がAIエージェントを導入したと発表した。契約書のチェック業務の一部を自動化し、担当者の確認時間を短縮したという。",
  "region_hint": "日本",
  "primary_or_secondary": "報道"
}

■ 一言要約（分類時の要約。合計スコア 82 点）
大手不動産会社が契約書チェックにAIエージェントを導入した。確認時間の短縮が報告されている。

■ 書くもの
- organizations: 事例の主体となる企業・組織名（記事に出てくるもの）。
- case_title: 事例の見出し（何をした事例かが一読で分かる短い日本語）。
- chapter_theme: この事例が属するテーマ。月全体で5前後の章に束ねるので、**他の事例とも共有できる粒度**の名詞句にする。
- commentary_fact: ①事実（何が起きたか）。
- commentary_detail: ②詳細（どう取り組んだか・仕組み・数字）。
- commentary_implication: ③示唆（読者が自社へ持ち帰れること）。

解説の3段落はそれぞれ独立した段落として書く（アプリ側が段落として連結する。段落記号や見出しを文中に入れない）。
````
