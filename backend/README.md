# ai-intelligence-apps

社内標準の Python バックエンド（FastAPI + uv）。`python-backend-bootstrap` スキルで生成。

## 必要なもの

- [uv](https://docs.astral.sh/uv/)
- Python 3.13（`uv` が `.python-version` を見て用意する）

## セットアップ & 起動

```bash
cp .env.example .env
make sync          # 依存を同期
make migrate-all   # DB(SQLite) を作成してマイグレーション適用
make dev           # http://localhost:8000 で起動
curl http://localhost:8000/healthz   # {"status":"ok"}
curl http://localhost:8000/readyz    # {"status":"ready"}
```

**Docker は不要です。** 既定の DB は SQLite（`var/ai_intelligence.db`）で、`make up` を実行する必要はありません。

## よく使うコマンド

```bash
make lint          # ruff: 静的解析 + フォーマット検査
make format        # ruff: 自動整形 + import 整列
make type-check    # ty: 型チェック（ベストエフォート）
make test          # pytest
make test-ci       # カバレッジ付き pytest
```

`make help` の後ろに `migrate-*` / `up` / `down` / `db-init` が出る（`Makefile.db.mk`）。

## データベース

手元は **SQLite**（Docker 不要）。本番運用時、および全文検索・ベクトル検索
（[`../docs/future-roadmap.md`](../docs/future-roadmap.md) 構想1）を実装する際に
**PostgreSQL へ移行**する想定。

| | SQLite（既定） | PostgreSQL |
|---|---|---|
| 設定 | `DB_BACKEND=sqlite` | `DB_BACKEND=postgresql` |
| 保存先 | `SQLITE_PATH`（既定 `var/ai_intelligence.db`） | `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` |
| Docker | 不要 | 必要（`make up`） |

PostgreSQL へ切り替える手順:

```bash
make up                                   # postgres を起動（Docker 必要）
DB_BACKEND=postgresql make migrate-all
DB_BACKEND=postgresql make dev
```

> **移行の道を塞がないための制約**: モデル定義は SQLAlchemy に閉じ込め、
> DB 固有機能（`JSONB`・配列型・`tsvector`・pgvector 等）を使わない。
> JSON カラムは汎用 `JSON` 型を使う。

## レイヤ構成

```
src/
├── config.py                 # 設定（pydantic-settings）
├── run_local.py              # ローカル起動
├── common/                   # 横断的な部品（logger 等）
├── enterprise/               # ドメイン（エンティティ・値オブジェクト。外部に依存しない）
├── application/              # ユースケース（業務手順）
└── adapter/                  # 入出力（HTTP / DB など）
    └── http/fastapi/         # FastAPI のルーター
```

依存の向きは `adapter → application → enterprise`。enterprise は何にも依存しない。
