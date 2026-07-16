"""add event_profile_deferred_at to sales_customer_profiles

Revision ID: f1a2b3c4d5e6
Revises: e8f9a0b1c2d3
Create Date: 2026-07-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    res = conn.execute(
        sa.text(
            "SHOW COLUMNS FROM sales_customer_profiles LIKE 'event_profile_deferred_at'"
        )
    )
    if not res.fetchone():
        op.add_column(
            "sales_customer_profiles",
            sa.Column("event_profile_deferred_at", sa.DateTime(), nullable=True),
        )
    try:
        op.create_index(
            "ix_scp_event_profile_deferred_at",
            "sales_customer_profiles",
            ["event_profile_deferred_at"],
            unique=False,
        )
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_index(
            "ix_scp_event_profile_deferred_at",
            table_name="sales_customer_profiles",
        )
    except Exception:
        pass
    op.drop_column("sales_customer_profiles", "event_profile_deferred_at")
