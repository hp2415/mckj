"""wechat_outbound_actions: desktop RPA outbound audit

Revision ID: w5x6y7z8a9b0
Revises: x8y9z0a1b2c3
Create Date: 2026-05-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "w5x6y7z8a9b0"
down_revision: Union[str, Sequence[str], None] = "x8y9z0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wechat_outbound_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        # MySQL 外键要求字符集/排序规则完全一致。
        # raw_customers.id 在部分环境为 utf8mb4_unicode_ci，因此此处显式对齐，避免 3780 不兼容报错。
        sa.Column(
            "raw_customer_id",
            sa.String(length=100, collation="utf8mb4_unicode_ci"),
            nullable=False,
        ),
        sa.Column("sales_wechat_id", sa.String(length=100), nullable=False),
        sa.Column("source_chat_message_id", sa.Integer(), nullable=True),
        sa.Column("receiver", sa.String(length=500), nullable=True),
        sa.Column("receiver_source", sa.String(length=30), nullable=True),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=True),
        sa.Column("edited_text", sa.Text(), nullable=False),
        sa.Column("claimed_local_sales_wechat_id", sa.String(length=100), nullable=True),
        sa.Column("auto_detected_wxid", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("block_reason", sa.String(length=50), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["raw_customer_id"], ["raw_customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_chat_message_id"], ["chat_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_wechat_outbound_actor_created",
        "wechat_outbound_actions",
        ["actor_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_wechat_outbound_customer",
        "wechat_outbound_actions",
        ["raw_customer_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_wechat_outbound_customer", table_name="wechat_outbound_actions")
    op.drop_index("ix_wechat_outbound_actor_created", table_name="wechat_outbound_actions")
    op.drop_table("wechat_outbound_actions")
