"""Settings の既定値と派生プロパティ。

.env や環境変数に左右されないよう、テストでは両方を明示的に無効化する。
"""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from config import Settings

ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "AI_CLI_COMMAND",
    "AI_TIMEOUT_SECONDS",
    "AI_CRAWL_TIMEOUT_SECONDS",
    "AI_MAX_ATTEMPTS",
    "AI_RETRY_BACKOFF_SECONDS",
    "ARTIFACT_ROOT",
    "HISTORY_MAX_GENERATIONS",
    "SCRATCH_TTL_HOURS",
    "TIMEZONE",
    "DB_BACKEND",
    "SQLITE_PATH",
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASSWORD",
    "DB_NAME",
    "SERVICE_TOKEN_HASH",
)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return Settings(_env_file=None)


def test_ai_defaults(settings: Settings) -> None:
    assert settings.anthropic_api_key == ""
    assert settings.anthropic_model == "claude-opus-5"


def test_the_cli_is_the_current_ai_call_path(settings: Settings) -> None:
    """呼び出し経路は Claude Code CLI（TASKS.md §1.1・T-15）。"""
    assert settings.ai_cli_command == "claude"
    assert settings.ai_max_attempts == 3
    assert settings.ai_retry_backoff_seconds == 2.0


def test_the_ai_timeouts_account_for_the_cli_startup_overhead(
    settings: Settings,
) -> None:
    """⚠️ 些細なプロンプトでも実測 約131秒。既定を短くしないこと（T-15 備考）。

    分類・採点系は10分、crawl は30分。crawl の方を短くしない。
    """
    assert settings.ai_timeout_seconds == 600
    assert settings.ai_crawl_timeout_seconds == 1800
    assert settings.ai_crawl_timeout_seconds > settings.ai_timeout_seconds


def test_the_ai_timeouts_are_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "900")
    monkeypatch.setenv("AI_CRAWL_TIMEOUT_SECONDS", "3600")
    settings = Settings(_env_file=None)
    assert settings.ai_timeout_seconds == 900
    assert settings.ai_crawl_timeout_seconds == 3600


def test_artifact_defaults(settings: Settings) -> None:
    assert settings.artifact_root == Path("artifacts")
    assert settings.history_max_generations == 10
    assert settings.scratch_ttl_hours == 24


def test_timezone_default(settings: Settings) -> None:
    assert settings.timezone == "Asia/Tokyo"
    assert settings.tzinfo == ZoneInfo("Asia/Tokyo")


def test_scratch_and_history_are_under_artifact_root(settings: Settings) -> None:
    assert settings.scratch_root == Path("artifacts/scratch")
    assert settings.history_root == Path("artifacts/_history")


def test_artifact_root_accepts_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTIFACT_ROOT", "/var/lib/ai-intelligence")
    settings = Settings(_env_file=None)
    assert settings.artifact_root == Path("/var/lib/ai-intelligence")
    assert settings.scratch_root == Path("/var/lib/ai-intelligence/scratch")


def test_timezone_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIMEZONE", "UTC")
    assert Settings(_env_file=None).tzinfo == ZoneInfo("UTC")


def test_db_defaults_to_sqlite(settings: Settings) -> None:
    """手元は Docker 不要で動かしたいので、既定は SQLite。"""
    assert settings.db_backend == "sqlite"
    assert settings.sqlite_path == Path("var/ai_intelligence.db")
    assert settings.database_url == "sqlite+aiosqlite:///var/ai_intelligence.db"


def test_sqlite_path_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLITE_PATH", "/tmp/other.db")
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite+aiosqlite:////tmp/other.db"


def test_postgresql_backend_builds_asyncpg_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL へ戻す道を塞いでいないことの確認（TASKS.md §1 備考）。"""
    monkeypatch.setenv("DB_BACKEND", "postgresql")
    monkeypatch.setenv("DB_HOST", "db.example.com")
    monkeypatch.setenv("DB_USER", "postgres")
    monkeypatch.setenv("DB_PASSWORD", "password")
    monkeypatch.setenv("DB_NAME", "ai_intelligence")
    settings = Settings(_env_file=None)
    assert settings.database_url == (
        "postgresql+asyncpg://postgres:password@db.example.com:5432/ai_intelligence"
    )


def test_postgresql_password_is_url_encoded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_BACKEND", "postgresql")
    monkeypatch.setenv("DB_PASSWORD", "p@ss:word/1")
    assert "p%40ss%3Aword%2F1" in Settings(_env_file=None).database_url


def test_unknown_db_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_BACKEND", "mysql")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_the_service_token_path_is_disabled_by_default(settings: Settings) -> None:
    """⚠️ 既定で system 経路を有効にしない（T-41）。

    空文字は「未設定＝system 経路そのものが無い」を意味する。既定値を入れると
    「配布物に共通のトークンが埋まっている」状態になり、cron を騙れてしまう。
    """
    assert settings.service_token_hash == ""


def test_the_service_token_hash_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設定に入れるのは**ハッシュ**（生トークンではない）。"""
    monkeypatch.setenv("SERVICE_TOKEN_HASH", "a" * 64)
    assert Settings(_env_file=None).service_token_hash == "a" * 64
