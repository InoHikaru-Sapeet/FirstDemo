# PROMPT-3-WEEKLY — 週刊メルマガ HTML 生成（**未使用**）

> 🚫 **現時点では運用ドキュメントです。実行経路にありません。**
> 週刊 HTML の生成（render）は**決定的 Python テンプレート**（T-24
> `backend/src/adapter/html/weekly_renderer.py`）が担当しており、このプロンプトは
> AI へ送られていません。将来 render を LLM 生成へ切り替える場合の仕様として、
> 仕様書 §13.4.1 の本文をそのまま残してあります（TASKS.md §1.1「PROMPT-3 の扱い」）。

| 項目 | 値 |
|---|---|
| `prompt_version` | `0.1.0`（仕様書 §13.4.1 のまま。実装されていないので上がっていません） |
| 実行時の識別子 | —（呼び出し無し） |
| 用途（ステージ） | render（週刊）— **未接続** |
| 対応する仕様・設計 | 仕様書 §13.4.1 ／ 設計書 §9.1・§7.1・§7.3 |
| 実際に走っているもの | `backend/src/adapter/html/weekly_renderer.py`（決定的 Python） |
| 最終更新日 | 2026-08-17（切り出し。本文は仕様書のまま） |

## 変数と注入元（設計書 §9.1）

| 変数 | 注入元 |
|---|---|
| `{{ISO_WEEK}}` | ジョブの period（仕様書 §13.1）。例 `2026-W33` |
| `{{target_industry}}` | `config.tunable_thresholds.weekly.target_industries` の1つ |
| `{{weekly.max_industry_topics}}` | `config.tunable_thresholds.weekly.max_industry_topics` |
| `{{weekly.max_common_topics}}` | `config.tunable_thresholds.weekly.max_common_topics` |

## 実装との差分（切り替えるときに効いてくる点）

- **「今週のポイント」と各カードの「示唆」は、実装では別プロンプトで作っています**
  （[PROMPT-2-NARRATIVE-WEEKLY.md](./PROMPT-2-NARRATIVE-WEEKLY.md)）。
  文章生成は filter 段の AI 呼び出しに寄せ、render は xlsx と
  `narrative_{period}.json` を組み合わせるだけの決定的処理にしてあります。
- **業界ごとに1通**出します（`weekly_ai_intelligence_newsletter_{industry}_{period}.html`。
  T-46 Step 4）。本文の `{{target_industry}}` は「対象業界のうちの1つ」です。
- カテゴリ色マップは設計書 §7.2 が正で、実装は
  `backend/src/adapter/html/category_colors.py` が持っています。

## 本文（仕様書 §13.4.1 のまま・未使用）

````text
あなたはニュースレター編集者です。weekly_ai_intelligence_report.xlsx の {{ISO_WEEK}} シート（採用記事のみ）を入力に、
週刊メルマガHTMLを生成してください。体裁は weekly_ai_intelligence_newsletter_不動産_2026-W31.html に厳密準拠。

【厳守事項（メールHTML）】
- table レイアウト＋inline style のみ。<style>タグ/外部CSS/flex/grid/JS 禁止。max-width:680px 中央。
- フォント: 'Hiragino Kaku Gothic ProN','Meiryo',Arial,sans-serif。
- 配色: ヘッダ グラデーション linear-gradient(135deg,#4f46e5,#7c3aed)、アクセント#4f46e5、示唆ボックス背景#eef2ff/左罫#6366f1。

【構成（上から）】
1. ヘッダ: 「Weekly AI Intelligence by Sapeet」/「{{target_industry}} 版」/「対象週：{{ISO_WEEK}}」
2. 今週のポイント（白カード, 3〜4文の業界視点総括）
3. {{target_industry}}関連トピック: 業界に「業界」タグが一致する記事。「その他ピックアップ」の<ul>リンク列挙可。上限 {{weekly.max_industry_topics}} 件。
4. 業界共通トピック: 「業界横断」等の記事カード群。上限 {{weekly.max_common_topics}} 件。各カード=
   カテゴリラベル（色分け）/ タイトル<a>リンク / 一言要約 / 示唆ボックス（自社視点1段落）/ 出典行「出典：{{ソース}} ／ 記事を読む」
5. フッタ: 「編集部整理であり投資・法務助言ではない」旨の注記。

【並び順】合計スコア降順。【カテゴリ色マップ】ai_agent_automation=#0891b2, ai_major_company_model=#7c3aed, ai_governance_risk=#dc2626 …（サンプル準拠、他は設計書の色マップに従う）。
【リンク】各カードの<a> href は xlsx の URL 列をそのまま使用。
HTML以外は出力しない。
````
