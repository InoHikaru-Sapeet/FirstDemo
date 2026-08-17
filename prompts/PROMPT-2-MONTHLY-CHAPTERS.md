<!-- 生成物。手で編集しないこと（`make prompts` で生成し直す）。 -->
# PROMPT-2（月次 章立て） — テーマを章へ束ね直す

> ⚠️ **このファイルは生成物です。**
> 本文は実行時と同じコードを**そのまま呼んで**
> 描画したもので、ここを手で編集しても実行されるプロンプトは変わりません。
> 本文を変えるときは**コード側を直し、`prompt_version` を上げ、`make prompts` で
> 生成し直して同じ PR に含める**こと（[版管理ルール](./README.md)）。
> コミット済みファイルとの一致は `make prompts-check` ／ `make test` が検査します。

| 項目 | 値 |
|---|---|
| `prompt_version` | `0.1.0` |
| 実行時の識別子 | `PROMPT-2/monthly_chapters` |
| 用途（ステージ） | filter の内部（月次。テーマ数が hint を超えたときだけ1往復） |
| 対応する仕様・設計 | 仕様書 §13.3 出力1（章を5前後に束ねる） |
| 本文を組み立てているコード | `backend/src/application/usecases/monthly_cases.py` の `build_chapter_prompt()` |
| 最終更新日 | 2026-08-16 |

## 変数と注入元（設計書 §9.1）

| 変数 | 注入元 |
|---|---|
| `{{THEMES}}` | 事例ごとに書かせた `chapter_theme` の重複除去済みの並び |
| `{{monthly.chapter_count_hint}}` | `config.tunable_thresholds.monthly.chapter_count_hint` |

## 補足

- テーマ数が `chapter_count_hint` 以下のときは**この往復自体を行いません**。
- この本文は config の値を1つ（`chapter_count_hint`）しか使いません。下の描画はサンプルのテーマ3件・hint 5 のものです。

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
あなたは月刊AIレポート（月刊ビリーフ）の編集者です。次のテーマを、意味の近いものどうしで**5前後の章**へ束ね直してください。

■ 厳守事項
- 与えたテーマ名は**そのままの文字列**で使う（言い換えない）。
- **すべてのテーマをどれかの章へ入れる**（重複させない）。
- 章タイトルは短い日本語の名詞句。`第N章` は付けない（アプリ側が付ける）。

■ テーマ
- 契約・審査業務の自動化
- 問い合わせ対応の自動化
- 設計・提案業務の支援
````
