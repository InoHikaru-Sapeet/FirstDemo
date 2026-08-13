# DB / マイグレーション。base の Makefile から -include される。
#
# 既定の DB は SQLite（Docker 不要）。migrate-* はそのまま使える。
# up / down / db-init は PostgreSQL 専用で、手元の開発には不要。

PG := docker compose exec -T postgresql psql -U postgres

.PHONY: help-db up down db-init migrate-all migrate-up migrate-down migrate-status migrate-create

help-db:
	@echo ""
	@echo "  [SQLite / PostgreSQL 共通]"
	@echo "  migrate-all    - head まで適用"
	@echo "  migrate-up     - 次の1つを適用"
	@echo "  migrate-down   - 1つ戻す"
	@echo "  migrate-status - 履歴を表示"
	@echo "  migrate-create - 自動生成マイグレーション作成 (ARG='msg')"
	@echo ""
	@echo "  [PostgreSQL 専用 / Docker 必要 — 手元の開発には不要]"
	@echo "  up             - postgres を起動 (docker compose up -d)"
	@echo "  down           - postgres を停止"
	@echo "  db-init        - DB を作り直して head まで migrate"
	@echo ""

# === マイグレーション（SQLite / PostgreSQL 共通）===

migrate-all:
	uv run alembic upgrade head

migrate-up:
	uv run alembic upgrade +1

migrate-down:
	uv run alembic downgrade -1

migrate-status:
	uv run alembic history

migrate-create:
	uv run alembic revision --autogenerate -m "$(ARG)"

# === PostgreSQL 専用（Docker 必要）===
# 既定の SQLite では使わない。使う場合は DB_BACKEND=postgresql を併せて指定する。

up:
	docker compose up -d

down:
	docker compose down

db-init:
	$(PG) -c "DROP DATABASE IF EXISTS ai_intelligence;"
	$(PG) -c "CREATE DATABASE ai_intelligence;"
	DB_BACKEND=postgresql uv run alembic upgrade head
