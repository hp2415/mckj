"""raw_customer_sales_wechats mapping table

Revision ID: k7l8m9n0p1q2
Revises: h2i3j4k5l6m7
Create Date: 2026-04-24

- Preserve (raw_customer_id, sales_wechat_id) ownership to avoid overwriting when same friend id appears under multiple sales.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "k7l8m9n0p1q2"
down_revision: Union[str, Sequence[str], None] = "h2i3j4k5l6m7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "raw_customer_sales_wechats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("raw_customer_id", sa.String(length=100), nullable=False),
        sa.Column("sales_wechat_id", sa.String(length=100), nullable=False),
        sa.Column("add_time", sa.DateTime(), nullable=True),
        sa.Column("update_time", sa.DateTime(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["raw_customer_id"],
            ["raw_customers.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("raw_customer_id", "sales_wechat_id", name="uq_rcsw_customer_sales"),
    )
    op.create_index(
        "ix_rcsw_sales_wechat_id",
        "raw_customer_sales_wechats",
        ["sales_wechat_id"],
        unique=False,
    )
    op.create_index(
        "ix_raw_customer_sales_wechats_raw_customer_id",
        "raw_customer_sales_wechats",
        ["raw_customer_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_raw_customer_sales_wechats_raw_customer_id", table_name="raw_customer_sales_wechats")
    op.drop_index("ix_rcsw_sales_wechat_id", table_name="raw_customer_sales_wechats")
    op.drop_table("raw_customer_sales_wechats")

