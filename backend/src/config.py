"""アプリ設定。環境変数 / .env から読み込む（pydantic-settings）。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "ai-intelligence-apps"
    log_level: str = "INFO"
    cors_allowed_origins: str = "*"

    # データベース。手元は SQLite（Docker 不要）、本番・検索機能の本格化時に
    # PostgreSQL へ切り替える想定（TASKS.md §1・T-39）。切替は db_backend だけで行う。
    db_backend: Literal["sqlite", "postgresql"] = "sqlite"

    # SQLite（db_backend="sqlite" のときに参照される）
    sqlite_path: Path = Path("var/ai_intelligence.db")

    # PostgreSQL（db_backend="postgresql" のときに参照される）
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "postgres"
    db_password: str = "password"
    db_name: str = "ai_intelligence"

    # AI（Claude API）。crawl / filter が利用する（設計書 §6・§9）。
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"

    # 成果物ストレージ。config.json / 中間xlsx / 生成HTML はファイルが正。
    # 正規名は上書きし、旧版は履歴へ退避する（設計書 §11 設計判断B）。
    # ドライラン結果は scratch へ隔離し TTL で掃除する（同 設計判断C）。
    artifact_root: Path = Path("artifacts")
    history_max_generations: int = 10
    scratch_ttl_hours: int = 24

    # タイムゾーン。入出力は Asia/Tokyo 基準、ISO週は月曜始まり（設計書 §0・§14）。
    timezone: str = "Asia/Tokyo"

    # 認証（TASKS.md §1.1「認証」「ログイン状態の保持」／T-40）。
    # ログイン保持は DB 永続セッション + HttpOnly Cookie。JWT は使わない。

    # ⚠️ 本番では必ず true（HTTPS 前提）。http の localhost で試すときだけ false。
    session_cookie_secure: bool = True
    # 絶対期限。ログインからこの日数で必ず切れる（アクセスしていても延長しない）。
    session_absolute_lifetime_days: int = 7
    # アイドル期限。最終アクセスからこの時間で切れる（アクセスのたびに延長する）。
    session_idle_timeout_hours: int = 8

    # 自己登録を許すメールドメイン（カンマ区切り）。2026-08-13 決定＝要確認事項 #6。
    # ⚠️ 空にすると**誰でも登録でき、viewer としてレポートを閲覧できる**。
    # 既定を無制限にしないこと。
    auth_allowed_email_domains: str = "sapeet.com"

    # ログインの総当たり対策。同一アカウントへの連続失敗がこの回数に達したら
    # 一定時間ロックする。ロック中でもエラー文言は変えない（存在を漏らさない）。
    login_max_failed_attempts: int = 5
    login_lockout_minutes: int = 15

    @property
    def database_url(self) -> str:
        """SQLAlchemy 用の非同期接続 URL。db_backend で切り替わる。

        PostgreSQL では user / password が予約文字（@ : / %）を含みうるので
        URL エンコードする。
        """
        if self.db_backend == "sqlite":
            return f"sqlite+aiosqlite:///{self.sqlite_path}"

        user = quote_plus(self.db_user)
        password = quote_plus(self.db_password)
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def allowed_email_domains(self) -> list[str]:
        """自己登録を許すメールドメイン。空リストなら無制限（既定ではない）。"""
        return [
            d.strip().lower()
            for d in self.auth_allowed_email_domains.split(",")
            if d.strip()
        ]

    @property
    def tzinfo(self) -> ZoneInfo:
        """アプリ共通のタイムゾーン。日時の生成・整形は必ずこれを経由する。"""
        return ZoneInfo(self.timezone)

    @property
    def scratch_root(self) -> Path:
        """ドライラン等の一時成果物を置く隔離パス（正規の成果物と混ぜない）。"""
        return self.artifact_root / "scratch"

    @property
    def history_root(self) -> Path:
        """上書き前の正規成果物を退避する世代スナップショットの置き場。"""
        return self.artifact_root / "_history"


@lru_cache
def get_settings() -> Settings:
    return Settings()
