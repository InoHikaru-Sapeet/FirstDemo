# ai-intelligence-apps

社内標準の Python バックエンド（FastAPI + uv）。`python-backend-bootstrap` スキルで生成。

## 必要なもの

- [uv](https://docs.astral.sh/uv/)
- Python 3.13（`uv` が `.python-version` を見て用意する）

## セットアップ & 起動

```bash
cp .env.example .env
make sync          # 依存を同期
make dev           # http://localhost:8000 で起動
curl http://localhost:8000/healthz   # {"status":"ok"}
```

## よく使うコマンド

```bash
make lint          # ruff: 静的解析 + フォーマット検査
make format        # ruff: 自動整形 + import 整列
make type-check    # ty: 型チェック（ベストエフォート）
make test          # pytest
make test-ci       # カバレッジ付き pytest
```

DB を同梱した場合は `make help` の後ろに `db-*` / `migrate-*` が出る（`Makefile.db.mk`）。

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
