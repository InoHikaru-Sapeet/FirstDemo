# ai-intelligence-apps

週刊メルマガ（Weekly AI Intelligence by Sapeet）／ 月刊ビリーフ（月刊ビリーフ by Sapeet）を支えるアプリケーション。

- 仕様書: [`docs/spec.md`](./docs/spec.md)
- 設計書: [`docs/design.md`](./docs/design.md)

## 構成

```
ai-intelligence-apps/
├── backend/    # FastAPI + uv（Config Service / パイプライン / スケジューラ）
├── frontend/   # Vite + React（管理画面 / レポート閲覧UI）
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

## 品質チェック

```bash
# backend
cd backend && make lint && make test

# frontend
cd frontend && pnpm check && pnpm build && pnpm test
```

## 次のタスク

1. インフラ（ホスティング環境）の決定 — 社内IT担当に確認の上、必要なら `terraform-infra-bootstrap` を実行
2. ~~既存SSOとフロントエンド/バックエンドの認証連携方式の確認~~ → **2026-08-13 方針変更**。SSO 連携はやらず、**ID/PW 認証を自前実装**する（[`TASKS.md`](./TASKS.md) §1.1「備考：SSO 前提からの差分」／実装は T-08・T-40〜T-43）。SSO は将来の選択肢として [`docs/future-roadmap.md`](./docs/future-roadmap.md) 構想3 へ
3. ~~設計書 §15 の10項目・設計判断4項目をベースにした実装タスクへの分解~~ → [`TASKS.md`](./TASKS.md) に完了。将来構想は [`docs/future-roadmap.md`](./docs/future-roadmap.md)
4. カテゴリ色マップの残り4色（設計書§7.2）についてブランド確認
