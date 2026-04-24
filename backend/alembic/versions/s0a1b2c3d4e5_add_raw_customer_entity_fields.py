"""add raw_customers entity fields (merge legacy customers)

Revision ID: s0a1b2c3d4e5
Revises: r3s4t5u6v7w8
Create Date: 2026-04-24

- Extend raw_customers with normalized/entity fields formerly stored in customers
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "s0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "r3s4t5u6v7w8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _colnames(conn, table: str) -> set[str]:
    rows = conn.execute(sa.text(f"SHOW COLUMNS FROM {table}")).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _colnames(conn, "raw_customers")

    with op.batch_alter_table("raw_customers") as b:
        if "phone_normalized" not in cols:
            b.add_column(sa.Column("phone_normalized", sa.String(length=100), nullable=True))
            b.create_index("ix_raw_customers_phone_normalized", ["phone_normalized"], unique=False)
        if "customer_name" not in cols:
            b.add_column(sa.Column("customer_name", sa.String(length=100), nullable=True))
        if "unit_name" not in cols:
            b.add_column(sa.Column("unit_name", sa.String(length=200), nullable=True))
        if "unit_type" not in cols:
            b.add_column(sa.Column("unit_type", sa.String(length=50), nullable=True))
        if "admin_division" not in cols:
            b.add_column(sa.Column("admin_division", sa.String(length=100), nullable=True))
        if "purchase_months" not in cols:
            b.add_column(sa.Column("purchase_months", sa.JSON(), nullable=True))
        if "profile_updated_at" not in cols:
            b.add_column(sa.Column("profile_updated_at", sa.DateTime(), nullable=True))
        if "entity_created_at" not in cols:
            b.add_column(sa.Column("entity_created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False))
        if "entity_updated_at" not in cols:
            b.add_column(sa.Column("entity_updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False))


def downgrade() -> None:
    conn = op.get_bind()
    cols = _colnames(conn, "raw_customers")
    with op.batch_alter_table("raw_customers") as b:
        if "entity_updated_at" in cols:
            b.drop_column("entity_updated_at")
        if "entity_created_at" in cols:
            b.drop_column("entity_created_at")
        if "profile_updated_at" in cols:
            b.drop_column("profile_updated_at")
        if "purchase_months" in cols:
            b.drop_column("purchase_months")
        if "admin_division" in cols:
            b.drop_column("admin_division")
        if "unit_type" in cols:
            b.drop_column("unit_type")
        if "unit_name" in cols:
            b.drop_column("unit_name")
        if "customer_name" in cols:
            b.drop_column("customer_name")
        if "phone_normalized" in cols:
            try:
                b.drop_index("ix_raw_customers_phone_normalized")
            except Exception:
                pass
            b.drop_column("phone_normalized")

