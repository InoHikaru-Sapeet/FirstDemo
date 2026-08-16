# ai-intelligence-apps — 開発タスク
# DB を同梱した場合は Makefile.db.mk が読み込まれ、db-*/migrate-* が増える。

APP_MODULE := adapter.http.fastapi.main:app
PORT ?= 8000

.PHONY: help sync lint format type-check test test-ci dev
.PHONY: config-schema config-schema-check migrate-config
.PHONY: create-admin service-token
.PHONY: run-weekly run-monthly

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
	@echo "  migrate-config       - xlsx から config.json を初期投入する（既定は dry。ARGS='--apply' で書き込み）"
	@echo ""
	@echo "  create-admin   - 最初の admin を作る（対話。ARGS='--promote a@b.com' も可）"
	@echo "  service-token  - cron 用サービストークンを発行する"
	@echo ""
	@echo "  run-weekly     - 週刊を通しで実行する（PERIOD=2026-W33 で期間指定。既定は当週）"
	@echo "  run-monthly    - 月刊を通しで実行する（PERIOD=2026-07 で期間指定。既定は前月）"
	@echo "                   途中から再開: ARGS='--from filter' / ARGS='--from render'"
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

# === 初期投入（移行。T-14 / 設計書 §10）===
# docs/source/weekly_ai_intelligence_requirements.xlsx → artifacts/config.json。
# ⚠️ **既定は dry（検証と差分レポートだけ・書き込まない）。** 書き込むには
#    ARGS="--apply" を付ける。既存の config.json は上書きしない（§10.4）。
#    2回目以降の変更は管理画面（PUT /config）が正で、この CLI は初回だけ使う。

migrate-config:
	PYTHONPATH=src uv run python -m adapter.cli.migrate_config $(ARGS)

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

# === 通し実行（T-45。設計書 §8.2）===
# crawl → filter（narrative 含む）→ render を手元で通しに実行して HTML を目視確認する。
# ⚠️ **P7（T-26 Run Orchestrator）の代わりではない。** ジョブ管理・状態機械・
#    二重起動防止・実行の監査ログは T-26 の責務で、この CLI は持たない。
# ⚠️ **AI 呼び出しは1回数分かかる**（T-15 実測）。記事1件につき1往復するので、
#    crawl から通すと数十分規模になる。各ステップの開始・終了・所要時間は出る。
#    PERIOD 省略時は当週（weekly）／前月（monthly）を Asia/Tokyo 基準で解決する。

run-weekly:
	PYTHONPATH=src uv run python -m adapter.cli.run_pipeline weekly \
		$(if $(PERIOD),--period $(PERIOD)) $(ARGS)

run-monthly:
	PYTHONPATH=src uv run python -m adapter.cli.run_pipeline monthly \
		$(if $(PERIOD),--period $(PERIOD)) $(ARGS)

# === テスト ===

test:
	uv run pytest -vv

test-ci:
	uv run pytest --cov=src --cov-branch -vv

-include Makefile.db.mk
