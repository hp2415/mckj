"""users.wechat_remark_for_prompt 注册时微信备注供提示词

Revision ID: e7f8a9b0c1d2
Revises: d8e9f0a1b2c3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = [r[0] for r in conn.execute(sa.text("SHOW COLUMNS FROM users")).fetchall()]
    if "wechat_remark_for_prompt" not in cols:
        op.add_column(
            "users",
            sa.Column("wechat_remark_for_prompt", sa.String(200), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    cols = [r[0] for r in conn.execute(sa.text("SHOW COLUMNS FROM users")).fetchall()]
    if "wechat_remark_for_prompt" in cols:
        op.drop_column("users", "wechat_remark_for_prompt")
