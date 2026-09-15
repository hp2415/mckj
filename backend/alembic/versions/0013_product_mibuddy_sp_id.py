"""add products.mibuddy_sp_id for main-system spec id cost sync

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(conn, table: str, column: str) -> bool:
    row = conn.execute(
        sa.text("SHOW COLUMNS FROM `%s` LIKE :c" % table), {"c": column}
    ).fetchone()
    return bool(row)


def _has_index(conn, table: str, name: str) -> bool:
    row = conn.execute(
        sa.text("SHOW INDEX FROM `%s` WHERE Key_name = :n" % table), {"n": name}
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "products", "mibuddy_sp_id"):
        op.add_column(
            "products",
            sa.Column("mibuddy_sp_id", sa.String(64), nullable=True),
        )
    if not _has_index(conn, "products", "ix_products_mibuddy_sp_id"):
        op.create_index(
            "ix_products_mibuddy_sp_id",
            "products",
            ["mibuddy_sp_id"],
            unique=True,
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_index(conn, "products", "ix_products_mibuddy_sp_id"):
        op.drop_index("ix_products_mibuddy_sp_id", table_name="products")
    if _has_column(conn, "products", "mibuddy_sp_id"):
        op.drop_column("products", "mibuddy_sp_id")
