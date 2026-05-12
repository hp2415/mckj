"""prompt_scenarios.router_hints_json: 场景路由命中规则

Revision ID: z9a8b7c6d5e4
Revises: y2u3i4o5p6a7
Create Date: 2026-05-12

为 `prompt_scenarios` 增加 `router_hints_json` JSON 字段，承载场景路由器的
命中规则（keywords/anti_keywords/examples/anti_examples/ui_categories/
requires_customer/priority）。允许 NULL，老数据零迁移压力。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "z9a8b7c6d5e4"
down_revision: Union[str, Sequence[str], None] = "y2u3i4o5p6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "prompt_scenarios",
        sa.Column("router_hints_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("prompt_scenarios", "router_hints_json")
