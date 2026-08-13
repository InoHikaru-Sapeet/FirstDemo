"""add users

ID/PW 認証を自前実装する方針（TASKS.md §1.1「認証」／T-08）に伴う利用者テーブル。

Revision ID: 7faada4f3755
Revises: 13248e4615f1
Create Date: 2026-08-13 13:44:43.841713
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7faada4f3755"
down_revision: str | None = "13248e4615f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        # bcrypt ハッシュ。平文は保存しない（T-08）。
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        # モデル側は UtcDateTime（TypeDecorator）だが、それは Python 側で UTC へ
        # 正規化するだけで **DDL は素の DateTime(timezone=True) と同一**。
        # 既存マイグレーション（13248e4615f1）と表記を揃える。
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("password_updated_at", sa.DateTime(timezone=True), nullable=False),
        # `system` はログインするユーザーではない（cron 用のサービストークン。T-41）。
        # パスワードで system 権限を取れる経路を DB 制約で塞ぐ。
        sa.CheckConstraint(
            "role IN ('admin', 'editor', 'viewer')",
            name="ck_users_role_is_assignable",
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    # SQLite は ALTER TABLE の制約が強いので batch モードを使う（T-03 と同じ）。
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_users_email"), ["email"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_email"))

    op.drop_table("users")
