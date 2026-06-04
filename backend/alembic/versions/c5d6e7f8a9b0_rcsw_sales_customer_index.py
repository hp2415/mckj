"""rcsw: composite index (sales_wechat_id, raw_customer_id) for chat join

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-06-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(conn, table: str, index: str) -> bool:
    row = conn.execute(
        sa.text("SHOW INDEX FROM %s WHERE Key_name=:k" % table), {"k": index}
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_index(conn, "raw_customer_sales_wechats", "ix_rcsw_sales_customer"):
        op.create_index(
            "ix_rcsw_sales_customer",
            "raw_customer_sales_wechats",
            ["sales_wechat_id", "raw_customer_id"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_index(conn, "raw_customer_sales_wechats", "ix_rcsw_sales_customer"):
        op.drop_index("ix_rcsw_sales_customer", table_name="raw_customer_sales_wechats")
