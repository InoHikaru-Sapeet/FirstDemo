"""Alembic 実行環境。接続 URL とメタデータは src/config・src/adapter から取る。"""

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# src/ を import パスに追加（alembic はリポジトリルートから起動される）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adapter.database.base import Base  # noqa: E402
import adapter.database.models  # noqa: E402,F401  # モデルを読み込み metadata に登録する
from config import get_settings  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# alembic.ini は ConfigParser 経由なので、URL 中の `%`（パスワードの URL エンコード等）は
# `%%` にエスケープしてから渡す（補間構文と誤認させない）。
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
