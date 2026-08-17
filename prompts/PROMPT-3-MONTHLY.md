# PROMPT-3-MONTHLY — 月刊ビリーフ HTML 生成（**未使用**）

> 🚫 **現時点では運用ドキュメントです。実行経路にありません。**
> 月刊 HTML の生成（render）は**決定的 Python テンプレート**（T-25
> `backend/src/adapter/html/monthly_renderer.py`）が担当しており、このプロンプトは
> AI へ送られていません。将来 render を LLM 生成へ切り替える場合の仕様として、
> 仕様書 §13.4.2 の本文をそのまま残してあります（TASKS.md §1.1「PROMPT-3 の扱い」）。

| 項目 | 値 |
|---|---|
| `prompt_version` | `0.1.0`（仕様書 §13.4.2 のまま。実装されていないので上がっていません） |
| 実行時の識別子 | —（呼び出し無し） |
| 用途（ステージ） | render（月刊）— **未接続** |
| 対応する仕様・設計 | 仕様書 §13.4.2 ／ 設計書 §9.1・§7.1・§7.4 |
| 実際に走っているもの | `backend/src/adapter/html/monthly_renderer.py`（決定的 Python） |
| 最終更新日 | 2026-08-17（切り出し。本文は仕様書のまま） |

## 変数と注入元（設計書 §9.1）

| 変数 | 注入元 |
|---|---|
| `{{MONTH}}` | ジョブの period（仕様書 §13.1）。例 `2026-07` |
| `{{MONTH_JP}}` | period の和文表記（例「2026年7月」） |
| `{{month_range}}` | period を開いた実日付の範囲 |
| `{{monthly.target_case_count}}` | `config.tunable_thresholds.monthly.target_case_count` |
| `{{monthly.chapter_count_hint}}` | `config.tunable_thresholds.monthly.chapter_count_hint` |

## 実装との差分（切り替えるときに効いてくる点）

- **巻頭言・章の導入文・むすびは、実装では別プロンプトで作っています**
  （[PROMPT-2-NARRATIVE-MONTHLY.md](./PROMPT-2-NARRATIVE-MONTHLY.md)）。
  事例の本文（企業・組織／見出し／解説3段落）も別で
  （[PROMPT-2-MONTHLY-CASE.md](./PROMPT-2-MONTHLY-CASE.md)）、
  章の束ね直しはさらに別です（[PROMPT-2-MONTHLY-CHAPTERS.md](./PROMPT-2-MONTHLY-CHAPTERS.md)）。
  render は xlsx と `narrative_{period}.json` を組み合わせるだけの決定的処理です。
- `第N章` の採番・`No` の通し番号・`出典` の体裁はアプリ側が決めています
  （AI に書かせていません）。

## 本文（仕様書 §13.4.2 のまま・未使用）

````text
あなたは月刊特集の編集者です。monthly_ai_leading_cases.xlsx の {{MONTH}} シートを入力に、
月刊ビリーフHTMLを生成してください。体裁は monthly_belief_2026-07.html に厳密準拠。

【厳守事項（メールHTML）】
- table レイアウト＋inline style のみ。<style>/flex/grid/JS 禁止。width:680px; max-width:100%; 角丸8px; 中央。
- フォント: ヒラギノ/メイリオ系。配色: ネイビー#1F4E78 / アクセント#4FA8DB・#9FD4F2 / 本文#2C3E50 / 囲み#F7FAFC＋罫#DCE7F0。

【構成（上から）】
1. ヘッダ: 「MONTHLY REPORT ON LEADING AI CASES」/「月刊ビリーフ by Sapeet」/「{{MONTH_JP}}号」バッジ/「対象期間：{{month_range}}」/説明1文。
2. 巻頭言(EDITORIAL): 「巻頭言 ― 今月の総論」＋当月を一言で表すサブ見出し＋#F7FAFCカードに総論3段落（全事例を俯瞰）。
3. 目次(CONTENTS): ネイビーカード。章一覧（「第N章」バッジ＋章タイトル＋件数右寄せ）＋「全{{N}}事例・{{M}}章」。
4. 本編: 章ごとに [章ヘッダ（第N章バッジ＋章タイトル＋導入文, 下端2px#4FA8DB罫）] → [事例カード群]。
   事例カード = 「CASE NN ／ {{企業・組織}}」ラベル / {{タイトル}}を<a>リンク見出し(ネイビー) /
   {{解説}}を\n\n段落で<p>分割（3〜4段落, 最終段は示唆トーン）/ 出典行「出典：{{出典}}」(上罫・グレー小)。
5. むすび(CLOSING): 「むすび ― 来月への視点」＋#F7FAFCカードに2段落（今月総括＋来月視点）。
6. フッタ: ネイビー。「月刊ビリーフ by Sapeet ／ {{MONTH_JP}}号」/「収録事例 {{N}} 件」「トピック {{M}} 章」バッジ/対象期間・発行日。

【並び順】xlsx の No 昇順＝章グルーピング順。件数目安は target_case_count({{monthly.target_case_count}})・chapter_count_hint({{monthly.chapter_count_hint}})。
【リンク】各事例の<a> href は URL 列。
HTML以外は出力しない。
````
