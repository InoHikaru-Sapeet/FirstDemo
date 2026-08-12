"""アプリ設定。環境変数 / .env から読み込む（pydantic-settings）。"""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "ai-intelligence-apps"
    log_level: str = "INFO"
    cors_allowed_origins: str = "*"

    # データベース（--with-db で生成した場合のみ参照される）
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "postgres"
    db_password: str = "password"
    db_name: str = "ai_intelligence"

    @property
    def database_url(self) -> str:
        """SQLAlchemy(asyncpg) 用の接続 URL。

        user / password は予約文字（@ : / %）を含みうるので URL エンコードする。
        """
        user = quote_plus(self.db_user)
        password = quote_plus(self.db_password)
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
