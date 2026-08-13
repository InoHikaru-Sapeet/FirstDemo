"""非同期データベース接続を管理する。

エンジンは遅延接続なので、import の時点では DB へ接続しに行かない
（テスト時に DB が無くても import できる）。

接続先は `Settings.db_backend` で切り替わる（既定は SQLite / Docker 不要）。
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import get_settings

SQLITE_URL_PREFIX = "sqlite+aiosqlite:///"


def prepare_sqlite_dir(dsn: str) -> None:
    """SQLite の場合だけ、DB ファイルの置き場を先に作る。

    SQLite は存在しないディレクトリにファイルを作れないため、初回起動や
    マイグレーションが「ディレクトリが無い」だけで失敗するのを防ぐ。
    SQLite 以外の DSN では何もしない。

    Args:
        dsn: SQLAlchemy の接続 URL
    """
    if not dsn.startswith(SQLITE_URL_PREFIX):
        return
    path = Path(dsn.removeprefix(SQLITE_URL_PREFIX))
    if path.parent != Path():
        path.parent.mkdir(parents=True, exist_ok=True)


class AsyncDatabaseManager:
    def __init__(self, dsn: str, echo: bool = False) -> None:
        prepare_sqlite_dir(dsn)
        self.engine = create_async_engine(dsn, echo=echo, pool_pre_ping=True)
        self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """セッションを払い出す。

        Yields:
            DB セッション
        """
        async with self._sessionmaker() as session:
            yield session


db_manager = AsyncDatabaseManager(get_settings().database_url)
