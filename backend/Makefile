# ai-intelligence-apps — 開発タスク
# DB を同梱した場合は Makefile.db.mk が読み込まれ、db-*/migrate-* が増える。

APP_MODULE := adapter.http.fastapi.main:app
PORT ?= 8000

.PHONY: help sync lint format type-check test test-ci dev

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

# === テスト ===

test:
	uv run pytest -vv

test-ci:
	uv run pytest --cov=src --cov-branch -vv

-include Makefile.db.mk
