"""销售微信主数据表 sales_wechat_accounts（accounts.xlsx / 云客接口）

Revision ID: f0a1b2c3d4e5
Revises: e7f8a9b0c1d2
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sales_wechat_accounts",
        sa.Column("sales_wechat_id", sa.String(100), primary_key=True, nullable=False),
        sa.Column("nickname", sa.String(200), nullable=True),
        sa.Column("alias_name", sa.String(200), nullable=True),
        sa.Column("account_code", sa.String(100), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("source", sa.String(30), nullable=False, server_default="xlsx"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("sales_wechat_accounts")
