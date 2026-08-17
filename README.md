# ai-intelligence-apps

週刊メルマガ（Weekly AI Intelligence by Sapeet）／ 月刊ビリーフ（月刊ビリーフ by Sapeet）を支えるアプリケーション。

- 仕様書: [`docs/spec.md`](./docs/spec.md)
- 設計書: [`docs/design.md`](./docs/design.md)

## 構成

```
ai-intelligence-apps/
├── backend/    # FastAPI + uv（Config Service / パイプライン / スケジューラ）
├── frontend/   # Vite + React（管理画面 / レポート閲覧UI）
├── prompts/    # パイプラインが AI へ送っているプロンプト本文（読み物・生成物）
└── docs/       # 仕様書・設計書
```

インフラ（ホスティング環境）は未セットアップです。社内の既存システムの配置状況を確認してから、
`terraform-infra-bootstrap` スキルで追加してください。

## セットアップ

### バックエンド

```bash
cd backend
uv sync --all-extras --dev
cp .env.example .env
make migrate-all    # DB(SQLite)を作成してマイグレーション適用
make migrate-config              # 判断基準の初期投入を検証（dry。書き込まない）
make migrate-config ARGS="--apply"   # artifacts/config.json を作成（初回だけ）
make dev            # http://localhost:8000  (/healthz, /readyz)
```

`make migrate-config` は [`docs/source/weekly_ai_intelligence_requirements.xlsx`](./docs/source/weekly_ai_intelligence_requirements.xlsx)（初期投入元）から
`config.json` を起こす。以後の正は `config.json` で、変更は管理画面（`PUT /config`）から行う。

### フロントエンド

```bash
cd frontend
pnpm install
pnpm dev            # http://localhost:5173
```

## パイプラインを実行する

crawl → filter → render を通しで実行します。**入口は2つありますが中身は同じ**
（どちらも Run Orchestrator を通るので、状態機械・二重起動防止・監査ログが効きます）。
詳細は [`backend/README.md`](./backend/README.md#パイプラインを実行するcrawl--filter--render)。

```bash
cd backend

# サーバ不要。手元で通しに動かして HTML を目視する
make run-weekly                      # 当週（Asia/Tokyo）
make run-weekly PERIOD=2026-W33
make run-monthly                     # 前月
make run-weekly PERIOD=2026-W33 ARGS="--from filter"   # 途中から再開

# cron が叩くのと同じ経路（サーバ起動が必要）
make dev
SERVICE_TOKEN=<生トークン> make run-weekly-api
```

> ⚠️ **週次フル実行は業界数に依存し、18業界で 90〜100分**（実測）。
> API は `202` を返してから裏で走ります。進み方は `GET /run/{job_id}`。

### TODO: 本番 cron 登録

**インフラ（ホスティング環境）が未確定のためコマンド例はまだ書けません。**
登録するスケジュールは確定済みで、**週刊 `0 8 * * MON` / 月刊 `0 9 1 * *`（ともに
`Asia/Tokyo`）**。確定後に決めることの一覧は
[`backend/README.md`](./backend/README.md#todo-本番-cron-登録) にあります。

> ⚠️ **`claude -p` はログイン済みの CLI がある機でしか動きません**（TASKS.md §1.1）。
> 無人のサーバへ載せる時点で Anthropic API 実装への差し替えが要ります。

## プロンプト

パイプライン（crawl → filter → render）が AI へ送っているプロンプトの本文は
[`prompts/`](./prompts/) にあります。**PM・編集担当はここを読めば、実際に走っている
文面がそのまま読めます**（一覧と読み方は [`prompts/README.md`](./prompts/README.md)）。

### 置き場

| 種類 | 置き場 | 正（source of truth） |
|---|---|---|
| 使用中のプロンプト（PROMPT-1 / PROMPT-2 系 / 共通の出力指示） | `prompts/*.md` | **コード**（`backend/src/application/usecases/*.py` の `build_*_prompt()`）。`prompts/*.md` は**生成物** |
| 未使用の PROMPT-3 系（render は決定的 Python） | `prompts/PROMPT-3-*.md` | そのファイル自身（手書き。実行経路に無い） |
| 版（`prompt_version`） | 各 usecase モジュールの `PROMPT_VERSION` | **コード**。実行時に `AICallMeta` として監査／validation メタへ載る |

```bash
cd backend
make prompts         # コードから prompts/*.md を生成し直す
make prompts-check   # コミット済み prompts/*.md が最新かを検査する
```

⚠️ **`prompts/*.md` を手で編集しても実行されるプロンプトは変わりません**
（PROMPT-3 系を除く）。

### 版管理ルール

1. **本文を変えたら `prompt_version`（semver）を上げる。** 版は監査ログ・validation
   メタに記録され、「どの版で作られた号か」の手がかりになります（設計書 §9.2）。
   意味が変わる修正は minor、誤字修正は patch。
2. **プロンプトの改訂は PR レビュー必須。** 直接 main へ push しない。PR には変更理由と、
   可能なら変更前後の出力の比較を添える。
3. 変更は **コード修正 → 版を上げる → `make prompts` → 同じ PR にまとめる**の順で行う。
   生成し忘れは `make test` が検出します（`prompts/*.md` と描画結果の完全一致を検査）。
4. テンプレート変数の増減は設計書 §9.1 の表と同時に更新する。
5. 確定値（7カテゴリ / 10必須タグ / 6軸100点 / 13除外ルール / enum）はプロンプトに
   直書きせず、config から差し込む。

## 品質チェック

```bash
# backend
cd backend && make lint && make test   # test に prompts/ の最新性検査を含む

# frontend
cd frontend && pnpm check && pnpm build && pnpm test
```

## 次のタスク

1. インフラ（ホスティング環境）の決定 — 社内IT担当に確認の上、必要なら `terraform-infra-bootstrap` を実行
2. ~~既存SSOとフロントエンド/バックエンドの認証連携方式の確認~~ → **2026-08-13 方針変更**。SSO 連携はやらず、**ID/PW 認証を自前実装**する（[`TASKS.md`](./TASKS.md) §1.1「備考：SSO 前提からの差分」／実装は T-08・T-40〜T-43）。SSO は将来の選択肢として [`docs/future-roadmap.md`](./docs/future-roadmap.md) 構想3 へ
3. ~~設計書 §15 の10項目・設計判断4項目をベースにした実装タスクへの分解~~ → [`TASKS.md`](./TASKS.md) に完了。将来構想は [`docs/future-roadmap.md`](./docs/future-roadmap.md)
4. カテゴリ色マップの残り4色（設計書§7.2）についてブランド確認
