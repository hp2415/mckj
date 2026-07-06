"""task allocation optimization: alloc_feature_json, abc_grade, intent_score

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-07-03
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7e8f9a0b1c2"
down_revision: Union[str, Sequence[str], None] = "c6d7e8f9a0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "contact_tasks",
        sa.Column("alloc_feature_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "sales_customer_profiles",
        sa.Column("abc_grade", sa.String(length=1), nullable=True),
    )
    op.add_column(
        "sales_customer_profiles",
        sa.Column("intent_score", sa.Numeric(precision=5, scale=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sales_customer_profiles", "intent_score")
    op.drop_column("sales_customer_profiles", "abc_grade")
    op.drop_column("contact_tasks", "alloc_feature_json")
