# ai-intelligence-apps — 開発タスク
# DB を同梱した場合は Makefile.db.mk が読み込まれ、db-*/migrate-* が増える。

APP_MODULE := adapter.http.fastapi.main:app
PORT ?= 8000

.PHONY: help sync lint format type-check test test-ci dev
.PHONY: config-schema config-schema-check
.PHONY: create-admin service-token

help:
	@echo ""
	@echo "  sync         - 依存を同期する (uv sync)"
	@echo "  lint         - ruff で静的解析 + フォーマット検査"
	@echo "  format       - ruff で自動整形 + import 整列"
	@echo "  type-check   - ty で型チェック（ベストエフォート）"
	@echo "  test         - pytest を実行する"
	@echo "  test-ci      - カバレッジ付きで pytest を実行する"
	@echo "  dev          - 開発サーバーを起動する (uvicorn --reload)"
	@echo ""
	@echo "  config-schema        - config.json の JSON Schema を生成する"
	@echo "  config-schema-check  - 生成済み JSON Schema が最新かを検査する"
	@echo ""
	@echo "  create-admin   - 最初の admin を作る（対話。ARGS='--promote a@b.com' も可）"
	@echo "  service-token  - cron 用サービストークンを発行する"
	@echo ""

# === 開発 ===

sync:
	uv sync --all-extras --dev

lint:
	uv run ruff check ./src ./tests
	uv run ruff format --check ./src ./tests

format:
	uv run ruff check --fix ./src ./tests
	uv run ruff format ./src ./tests

type-check:
	uv run ty check src

dev:
	uv run uvicorn --reload --app-dir src --host 0.0.0.0 --port $(PORT) $(APP_MODULE)

# === スキーマ生成 ===
# schemas/config.schema.json は Pydantic モデル（enterprise.entities.config）
# からの生成物。手で編集せず、モデルを変えたら生成し直してコミットする。

config-schema:
	PYTHONPATH=src uv run python -m adapter.cli.export_config_schema

config-schema-check:
	PYTHONPATH=src uv run python -m adapter.cli.export_config_schema --check

# === 初期セットアップ（認証。T-41）===
# 自己登録は必ず viewer なので、最初の admin だけは API では作れない。
# ⚠️ パスワードは対話プロンプトで入力する。引数・環境変数で渡す経路は無い
#    （ps / シェル履歴 / .env に平文が残るため）。

create-admin:
	PYTHONPATH=src uv run python -m adapter.cli.create_admin $(ARGS)

# cron（system ロール）用のトークンを発行する。.env に入れるのは**ハッシュ**で、
# 生トークンは cron 側にだけ渡す。
service-token:
	PYTHONPATH=src uv run python -m adapter.cli.create_service_token

# === テスト ===

test:
	uv run pytest -vv

test-ci:
	uv run pytest --cov=src --cov-branch -vv

-include Makefile.db.mk
