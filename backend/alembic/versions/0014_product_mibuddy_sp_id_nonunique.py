"""allow duplicate products.mibuddy_sp_id (same spec may bind many local rows)

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(conn, table: str, name: str) -> bool:
    row = conn.execute(
        sa.text("SHOW INDEX FROM `%s` WHERE Key_name = :n" % table), {"n": name}
    ).fetchone()
    return bool(row)


def _index_is_unique(conn, table: str, name: str) -> bool:
    row = conn.execute(
        sa.text(
            "SHOW INDEX FROM `%s` WHERE Key_name = :n AND Non_unique = 0" % table
        ),
        {"n": name},
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if _has_index(conn, "products", "ix_products_mibuddy_sp_id"):
        if _index_is_unique(conn, "products", "ix_products_mibuddy_sp_id"):
            op.drop_index("ix_products_mibuddy_sp_id", table_name="products")
            op.create_index(
                "ix_products_mibuddy_sp_id",
                "products",
                ["mibuddy_sp_id"],
                unique=False,
            )
    else:
        op.create_index(
            "ix_products_mibuddy_sp_id",
            "products",
            ["mibuddy_sp_id"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_index(conn, "products", "ix_products_mibuddy_sp_id"):
        op.drop_index("ix_products_mibuddy_sp_id", table_name="products")
    op.create_index(
        "ix_products_mibuddy_sp_id",
        "products",
        ["mibuddy_sp_id"],
        unique=True,
    )
