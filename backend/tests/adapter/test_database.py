"""SQLite の置き場を用意する処理。

Docker 不要で動かすため、DB ファイルの親ディレクトリが無くても
初回のマイグレーション・起動が失敗しないことを保証する。
"""

from pathlib import Path

from adapter.database.database import prepare_sqlite_dir


def test_creates_missing_parent_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "nested" / "app.db"
    prepare_sqlite_dir(f"sqlite+aiosqlite:///{db_path}")
    assert db_path.parent.is_dir()


def test_is_idempotent(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'var' / 'app.db'}"
    prepare_sqlite_dir(dsn)
    prepare_sqlite_dir(dsn)
    assert (tmp_path / "var").is_dir()


def test_ignores_non_sqlite_dsn(tmp_path: Path) -> None:
    prepare_sqlite_dir("postgresql+asyncpg://u:p@localhost:5432/db")
    assert list(tmp_path.iterdir()) == []


def test_handles_bare_filename() -> None:
    """親ディレクトリが無い相対パスでも例外にならない。"""
    prepare_sqlite_dir("sqlite+aiosqlite:///app.db")
