"""add chat_model to chat_messages

Revision ID: m9n0p1q2r3s4
Revises: v1w2x3y4z5a6
Create Date: 2026-04-25
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "m9n0p1q2r3s4"
down_revision = "v1w2x3y4z5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("chat_model", sa.String(length=80), nullable=True))
    op.create_index("ix_chat_messages_chat_model", "chat_messages", ["chat_model"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_chat_messages_chat_model", table_name="chat_messages")
    op.drop_column("chat_messages", "chat_model")

