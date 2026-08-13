"""add sessions and login lockout

ログイン保持を DB 永続セッション + HttpOnly Cookie で行う方針（TASKS.md §1.1
「ログイン状態の保持」／T-40）に伴うテーブルと、総当たり対策の列。

Revision ID: 49982b99a593
Revises: 7faada4f3755
Create Date: 2026-08-13 15:18:25.090939
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "49982b99a593"
down_revision: str | None = "7faada4f3755"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        # ⚠️ Cookie の生トークンではなく、その SHA-256（16進64文字）。
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        # モデル側は UtcDateTime だが DDL は素の DateTime(timezone=True) と同一
        # （T-08 の備考と同じ理由で手書きに直している）。
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
    )
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sessions_user_id"), ["user_id"], unique=False
        )

    with op.batch_alter_table("users", schema=None) as batch_op:
        # ⚠️ NOT NULL 列を既存テーブルへ足すので server_default が要る。
        # autogenerate はこれを付けないため手で補った（既存行があると失敗する）。
        batch_op.add_column(
            sa.Column(
                "failed_login_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("locked_until")
        batch_op.drop_column("failed_login_attempts")

    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sessions_user_id"))

    op.drop_table("sessions")
