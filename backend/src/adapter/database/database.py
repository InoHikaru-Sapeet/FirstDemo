"""非同期データベース接続を管理する。

エンジンは遅延接続なので、import の時点では DB へ接続しに行かない
（テスト時に DB が無くても import できる）。
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import get_settings


class AsyncDatabaseManager:
    def __init__(self, dsn: str, echo: bool = False) -> None:
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
