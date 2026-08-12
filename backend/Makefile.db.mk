# DB / マイグレーション（--with-db で生成）。base の Makefile から -include される。

PG := docker compose exec -T postgresql psql -U postgres

.PHONY: help-db up down db-init migrate-all migrate-up migrate-down migrate-status migrate-create

help-db:
	@echo ""
	@echo "  up             - postgres を起動 (docker compose up -d)"
	@echo "  down           - postgres を停止"
	@echo "  db-init        - DB を作り直して head まで migrate"
	@echo "  migrate-all    - head まで適用"
	@echo "  migrate-up     - 次の1つを適用"
	@echo "  migrate-down   - 1つ戻す"
	@echo "  migrate-status - 履歴を表示"
	@echo "  migrate-create - 自動生成マイグレーション作成 (ARG='msg')"
	@echo ""

up:
	docker compose up -d

down:
	docker compose down

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

db-init:
	$(PG) -c "DROP DATABASE IF EXISTS ai_intelligence;"
	$(PG) -c "CREATE DATABASE ai_intelligence;"
	uv run alembic upgrade head
