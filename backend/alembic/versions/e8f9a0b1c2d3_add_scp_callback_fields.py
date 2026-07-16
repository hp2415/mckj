"""add callback_at / callback_done_at to sales_customer_profiles

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-07-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, Sequence[str], None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    for col_name, col in (
        ("callback_at", sa.Column("callback_at", sa.DateTime(), nullable=True)),
        ("callback_done_at", sa.Column("callback_done_at", sa.DateTime(), nullable=True)),
    ):
        res = conn.execute(
            sa.text(
                f"SHOW COLUMNS FROM sales_customer_profiles LIKE '{col_name}'"
            )
        )
        if not res.fetchone():
            op.add_column("sales_customer_profiles", col)
    # 轮询「今日待回访」按 callback_at 过滤
    try:
        op.create_index(
            "ix_scp_callback_at",
            "sales_customer_profiles",
            ["callback_at"],
            unique=False,
        )
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_index("ix_scp_callback_at", table_name="sales_customer_profiles")
    except Exception:
        pass
    op.drop_column("sales_customer_profiles", "callback_done_at")
    op.drop_column("sales_customer_profiles", "callback_at")
