"""add raw_customers profile_status

Revision ID: 8c4a2b1f0e3d
Revises: 516f31198606
Create Date: 2026-04-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "8c4a2b1f0e3d"
down_revision: Union[str, Sequence[str], None] = "516f31198606"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Check if column exists first to avoid error since it was added manually in some environments
    conn = op.get_bind()
    res = conn.execute(sa.text("SHOW COLUMNS FROM raw_customers LIKE 'profile_status'"))
    if not res.fetchone():
        op.add_column(
            "raw_customers",
            sa.Column("profile_status", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    op.drop_column("raw_customers", "profile_status")
