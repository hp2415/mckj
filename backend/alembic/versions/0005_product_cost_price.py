"""add products.cost_price for margin-based proposal pricing

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(conn, table: str, column: str) -> bool:
    row = conn.execute(
        sa.text("SHOW COLUMNS FROM `%s` LIKE :c" % table), {"c": column}
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "products", "cost_price"):
        op.add_column(
            "products",
            sa.Column("cost_price", sa.Numeric(10, 2), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, "products", "cost_price"):
        op.drop_column("products", "cost_price")
