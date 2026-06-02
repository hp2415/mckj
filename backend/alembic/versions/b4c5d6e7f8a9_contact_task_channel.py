"""contact_tasks: add contact_channel (wechat | phone)

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-06-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, Sequence[str], None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "contact_tasks",
        sa.Column("contact_channel", sa.String(length=20), server_default="wechat", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("contact_tasks", "contact_channel")
