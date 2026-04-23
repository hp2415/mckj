"""add suggested_followup_date to user_customer_relations

Revision ID: a1b2c3d4e5f6
Revises: 4cb12b38ff84
Create Date: 2026-04-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "4cb12b38ff84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    res = conn.execute(
        sa.text("SHOW COLUMNS FROM user_customer_relations LIKE 'suggested_followup_date'")
    )
    if not res.fetchone():
        op.add_column(
            "user_customer_relations",
            sa.Column("suggested_followup_date", sa.Date(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("user_customer_relations", "suggested_followup_date")
