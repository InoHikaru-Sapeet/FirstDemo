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
- **テスト**: Vitest + Testing Library（`@testing-library/user-event` は未導入。`fireEvent` を使う）
- **認証**: ID（メール）/ パスワードを自前実装。**SSO はやらない**（`../TASKS.md` §1.1「備考：SSO 前提からの差分」で方針変更済み）
  - セッションは **HttpOnly Cookie（`sid`）**。**トークンを JS 側で保持しない**（`localStorage` に置かない）
  - ログイン済みかどうかは `GET /auth/me` の成否だけで判断する（`hooks/useCurrentUser.ts`）
  - **401 は未ログイン → ログイン画面へ誘導、403 は権限なし → 画面内で処理**。この2つを混同しない
  - dev は **Vite proxy（`/api` → `:8000`）で同一オリジンにする**のが前提。直接 `:8000` を叩くと `SameSite=Lax` の Cookie が送られない
  - **ログイン失敗の文言はサーバーが返したものをそのまま出す**。「このメールは未登録です」等に言い換えない（アカウント列挙の防止）

## ディレクトリ構成

```
src/
├── components/
│   ├── common/     # 再利用可能な汎用コンポーネント
│   ├── pages/      # ページ単位のコンポーネント
│   └── ui/         # shadcn/ui の生成物（手で書かず CLI で追加する）
├── hooks/          # カスタムフック
├── api/            # APIクライアント・クエリキー規約
├── utils/          # ユーティリティ関数
├── types/          # 型定義
├── test/           # テスト用のヘルパ（fetch のスタブ・Provider 付き render）
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
