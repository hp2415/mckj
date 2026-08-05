"""bind proposals to their chat bubble so previews survive window switching

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(conn, table: str, column: str) -> bool:
    row = conn.execute(
        sa.text("SHOW COLUMNS FROM `%s` LIKE :c" % table), {"c": column}
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "ai_proposals", "chat_message_id"):
        op.add_column("ai_proposals", sa.Column("chat_message_id", sa.Integer(), nullable=True))
        op.create_index(
            "ix_ai_proposals_chat_message_id", "ai_proposals", ["chat_message_id"]
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, "ai_proposals", "chat_message_id"):
        op.drop_index("ix_ai_proposals_chat_message_id", table_name="ai_proposals")
        op.drop_column("ai_proposals", "chat_message_id")
