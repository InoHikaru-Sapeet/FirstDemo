# prompts/ — パイプラインで使っているプロンプト

パイプライン（crawl → filter → render）が AI へ送っているプロンプトの**本文**を
読むための置き場です。仕様は仕様書 [`docs/spec.md`](../docs/spec.md) §13、
運用方針は設計書 [`docs/design.md`](../docs/design.md) §9（T-30）。

## ファイル一覧

| ファイル | ステージ | 状態 | `prompt_version` |
|---|---|---|---|
| [PROMPT-1.md](./PROMPT-1.md) | crawl（収集） | **使用中** | `0.2.0` |
| [PROMPT-2.md](./PROMPT-2.md) | filter（分類・10必須タグ・6軸採点） | **使用中** | `0.2.0` |
| [PROMPT-2-NARRATIVE-WEEKLY.md](./PROMPT-2-NARRATIVE-WEEKLY.md) | filter（週次の今週のポイント・示唆） | **使用中** | `0.2.0` |
| [PROMPT-2-NARRATIVE-MONTHLY.md](./PROMPT-2-NARRATIVE-MONTHLY.md) | filter（月次の巻頭言・章導入・むすび） | **使用中** | `0.1.0` |
| [PROMPT-2-MONTHLY-CASE.md](./PROMPT-2-MONTHLY-CASE.md) | filter（月次の事例1件の原稿） | **使用中** | `0.1.0` |
| [PROMPT-2-MONTHLY-CHAPTERS.md](./PROMPT-2-MONTHLY-CHAPTERS.md) | filter（月次の章の束ね直し） | **使用中** | `0.1.0` |
| [COMMON-OUTPUT-INSTRUCTIONS.md](./COMMON-OUTPUT-INSTRUCTIONS.md) | 全ステージ（末尾に付く共通部分） | **使用中** | —（クライアント層） |
| [PROMPT-3-WEEKLY.md](./PROMPT-3-WEEKLY.md) | render（週刊 HTML） | 🚫 **未使用**（運用ドキュメント） | `0.1.0` |
| [PROMPT-3-MONTHLY.md](./PROMPT-3-MONTHLY.md) | render（月刊 HTML） | 🚫 **未使用**（運用ドキュメント） | `0.1.0` |

**PROMPT-3 系は実行経路にありません。** render は決定的 Python テンプレート
（`backend/src/adapter/html/weekly_renderer.py` / `monthly_renderer.py`）が担当します
（TASKS.md §1.1「PROMPT-3 の扱い」）。将来 LLM 生成へ切り替える場合の仕様として
残してあります。

**仕様書 §13 の3プロンプト構成と、ここのファイル数が食い違って見える理由**：
filter（PROMPT-2）の中で AI に頼んでいる仕事が「分類・採点」「生成テキスト」
「月次の事例・章立て」に分かれているためです。パイプラインの段は crawl → filter →
render の3段のままです（設計書 §8.4 の状態機械）。

## 版管理ルール

1. **本文を変えたら `prompt_version`（semver）を上げる。**
   実行時に使った `prompt_version` は `AICallMeta` として返り、監査ログ／validation
   メタに載ります（設計書 §9.2 の再現性要件）。版を上げずに本文を変えると、
   「どの版で作られた号か」が過去分と区別できなくなります。
   - 語句の言い換え・並べ替えなど**出力の質に影響しうる変更**は minor（`0.1.0` → `0.2.0`）
   - 誤字修正など**意味の変わらない変更**は patch（`0.2.0` → `0.2.1`）
2. **プロンプトの改訂は PR レビュー必須。** 直接 main へ push しない。
   PR には「なぜ変えるか」と、可能なら**変更前後の出力の比較**を添える。
3. **テンプレート変数の増減は設計書 §9.1 の表と同時に更新する**（片方だけ直さない）。
4. **確定値（7カテゴリ / 10必須タグ / 6軸100点 / 13除外ルール / enum / 配色 / xlsx列）を
   プロンプトに直書きしない。** これらは config から差し込みます（仕様書 §13.3
   「実行時点の値をそのまま使用し主観で上書きしない」）。

## ⚠️ このディレクトリのファイルを直接編集しないこと（PROMPT-3 を除く）

`PROMPT-3-*.md` と本 README 以外は**生成物**です。本文は実行時と同じコード
（`backend/src/application/usecases/*.py` の `build_*_prompt()`）を**そのまま呼んで**
描画しているので、ここを手で書き換えても実行されるプロンプトは変わりません。

```bash
cd backend
make prompts         # コードから生成し直す
make prompts-check   # コミット済みファイルが最新かを検査（CI 向け）
```

**手順（本文を変えるとき）**

1. `backend/src/application/usecases/…` のプロンプト組み立てを直す
2. 同じモジュールの `PROMPT_VERSION` を上げる
3. `make prompts` で `prompts/` を生成し直す（対象ファイルの `最終更新日` は
   `backend/src/adapter/cli/export_prompts.py` の `PROMPT_DOCS` で更新する）
4. `make lint && make test` を通し、**1〜3 を同じ PR に含める**

`make test` に、コミット済みの `prompts/*.md` と描画結果が**完全一致**することを
検査するテストが入っています（`backend/tests/adapter/test_export_prompts.py`）。
コードだけ直して生成し直し忘れると落ちます。**「PM が読むファイル」と「実際に走る
プロンプト」を乖離させない**のがこの仕組みの目的です。

## 読み方の注意

- 本文中の**カテゴリ・必須タグ・得点帯・除外ルール・対象業界・しきい値**は
  実行時の `config.json` から差し込まれます。各ファイルに載っているのは
  **仕様書 §5.2 の確定 config（初期値）で描画したもの**で、運用中に管理画面から
  変更された値は反映されていません。運用中の値で読みたいときは:

  ```bash
  cd backend
  PYTHONPATH=src uv run python -m adapter.cli.export_prompts \
      --config artifacts/config.json --output-dir /tmp/prompts-now
  ```

  ⚠️ この結果を `prompts/` へ**コミットしないこと**（`make prompts-check` が落ちます）。
- **記事・行・事例はサンプル**です（実行時はその週・その月の実データ）。
  サンプルは `backend/src/adapter/cli/export_prompts.py` の `SAMPLE_*` にあります。
- **出力形式の指示（JSON だけを出す／JSON Schema）は各本文に含まれません。**
  AI クライアント層が末尾に付けます → [COMMON-OUTPUT-INSTRUCTIONS.md](./COMMON-OUTPUT-INSTRUCTIONS.md)。
