"""chat_messages: sales_wechat_id for per-sales AI thread

Revision ID: x8y9z0a1b2c3
Revises: m9n0p1q2r3s4
Create Date: 2026-05-01
"""

from alembic import op
import sqlalchemy as sa


revision = "x8y9z0a1b2c3"
down_revision = "m9n0p1q2r3s4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("sales_wechat_id", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "ix_chat_messages_user_customer_saleswx",
        "chat_messages",
        ["user_id", "raw_customer_id", "sales_wechat_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_user_customer_saleswx", table_name="chat_messages")
    op.drop_column("chat_messages", "sales_wechat_id")
