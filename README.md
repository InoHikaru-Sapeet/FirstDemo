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
make up             # PostgreSQLをローカル起動（Docker必要）
make migrate-all    # 初回マイグレーション適用
make dev            # http://localhost:8000  (/healthz, /readyz)
```

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
2. 既存SSOとフロントエンド/バックエンドの認証連携方式の確認
3. 設計書 §15 の10項目・設計判断4項目をベースにした実装タスクへの分解（Claude Codeで `docs/design.md` を読み込んで依頼する）
4. カテゴリ色マップの残り4色（設計書§7.2）についてブランド確認
