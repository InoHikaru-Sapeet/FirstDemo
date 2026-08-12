# CLAUDE.md

このファイルは Claude Code がプロジェクトを理解するためのガイドです。

## プロジェクト概要

ai-intelligence-apps (frontend) - 週刊メルマガ（Weekly AI Intelligence）／月刊ビリーフ（Monthly Belief）の管理コンソール。
config.json（判断基準）の編集・差分プレビュー・ドライラン再フィルタ、および生成済みレポートの一覧閲覧を提供する。
バックエンドは `../backend`（FastAPI）。設計書は `../docs/design.md`、仕様書は `../docs/spec.md` を参照。

## 技術スタック

- **Framework**: Vite + React (SPA)
- **Language**: TypeScript (strict mode)
- **Lint/Format**: Biome
- **Package Manager**: pnpm(cooldown: 2日 — `pnpm-workspace.yaml` の `minimumReleaseAge`)
- **UI**: Tailwind CSS + shadcn/ui（`pnpm dlx shadcn@latest add <component>` で追加）
- **データ取得**: TanStack Query
- **フォーム**: react-hook-form + zod（`config.json` 編集フォームのバリデーションに使用。合計100・降順整合など§7.4の制約はzodスキーマで表現する）
- **ルーティング**: react-router
- **API型生成**: openapi-typescript（バックエンドのOpenAPIスキーマから型生成。`pnpm openapi-types` のようなscriptを別途用意する）
- **テスト**: Vitest + Testing Library
- **認証**: 未導入。既存SSOとの連携方式は導入前に社内IT担当へ確認すること（設計書§1.3・§4）

## ディレクトリ構成

```
src/
├── components/
│   ├── common/     # 再利用可能な汎用コンポーネント
│   └── pages/      # ページ単位のコンポーネント
├── hooks/          # カスタムフック
├── api/            # APIクライアント
├── utils/          # ユーティリティ関数
├── types/          # 型定義
└── styles/         # グローバルスタイル
```

## よく使うコマンド

```bash
pnpm dev       # 開発サーバー起動
pnpm build     # ビルド(型チェック込み)
pnpm check     # Biome lint + format
pnpm test      # テスト実行（導入した場合のみ）
```

## コーディング規約

- TypeScript strict mode。`as T` キャストは極力避け、型ガード・ジェネリクスで型を通す
- コードスタイルは Biome(`biome.json`)が唯一の正。手動整形しない
- インポートは `@/` エイリアス(`src/` 起点)を使う
- コンポーネントは関数コンポーネントのみ。1ファイル1コンポーネントを基本とする
- 環境変数を追加したら `.env.example` にも必ず追記する

## 実装完了後の確認

実装が完了したら、以下がすべてパスすることを確認する:

```bash
pnpm check
pnpm build
pnpm test      # テスト実行（導入した場合のみ）
