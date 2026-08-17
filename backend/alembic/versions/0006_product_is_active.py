"""add products.is_active for soft delist on 832 sync

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(conn, table: str, column: str) -> bool:
    row = conn.execute(
        sa.text("SHOW COLUMNS FROM `%s` LIKE :c" % table), {"c": column}
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "products", "is_active"):
        op.add_column(
            "products",
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
        op.create_index(
            "ix_products_supplier_active",
            "products",
            ["supplier_id", "is_active"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, "products", "is_active"):
        op.drop_index("ix_products_supplier_active", table_name="products")
        op.drop_column("products", "is_active")
