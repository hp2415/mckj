"""profile_tag_definitions + ucr_profile_tags (客户动态标签)

Revision ID: h2i3j4k5l6m7
Revises: g1h2i3j4k5l6
Create Date: 2026-04-24

- 管理平台可维护画像动态标签（名称、特征、策略）
- user_customer_relations 与标签多对多：ucr_profile_tags
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h2i3j4k5l6m7"
down_revision: Union[str, Sequence[str], None] = "g1h2i3j4k5l6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "profile_tag_definitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("feature_note", sa.Text(), nullable=True),
        sa.Column("strategy_note", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "ucr_profile_tags",
        sa.Column("user_customer_relation_id", sa.Integer(), nullable=False),
        sa.Column("profile_tag_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_tag_id"],
            ["profile_tag_definitions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_customer_relation_id"],
            ["user_customer_relations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_customer_relation_id", "profile_tag_id"),
    )
    op.create_index(
        "ix_ucr_profile_tags_profile_tag_id",
        "ucr_profile_tags",
        ["profile_tag_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ucr_profile_tags_profile_tag_id", table_name="ucr_profile_tags")
    op.drop_table("ucr_profile_tags")
    op.drop_table("profile_tag_definitions")
