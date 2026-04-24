"""create sales_customer_profiles + scp_profile_tags

Revision ID: s0b1c2d3e4f5
Revises: s0a1b2c3d4e5
Create Date: 2026-04-24

- New per-sales profile table keyed by (raw_customer_id, sales_wechat_id)
- New many-to-many table for profile tags
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "s0b1c2d3e4f5"
down_revision: Union[str, Sequence[str], None] = "s0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sales_customer_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("raw_customer_id", sa.String(length=100), nullable=False),
        sa.Column("sales_wechat_id", sa.String(length=100), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("relation_type", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("title", sa.String(length=50), nullable=True),
        sa.Column("budget_amount", sa.Numeric(precision=12, scale=2), server_default="0", nullable=True),
        sa.Column("contact_date", sa.Date(), nullable=True),
        sa.Column("purchase_type", sa.String(length=100), nullable=True),
        sa.Column("wechat_remark", sa.String(length=200), nullable=True),
        sa.Column("ai_profile", sa.Text(), nullable=True),
        sa.Column("suggested_followup_date", sa.Date(), nullable=True),
        sa.Column("dify_conversation_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.ForeignKeyConstraint(["raw_customer_id"], ["raw_customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sales_wechat_id"], ["sales_wechat_accounts.sales_wechat_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("raw_customer_id", "sales_wechat_id", name="uq_scp_customer_sales_wechat"),
    )
    op.create_index(
        "ix_scp_user_sales",
        "sales_customer_profiles",
        ["user_id", "sales_wechat_id"],
        unique=False,
    )
    op.create_index(
        "ix_sales_customer_profiles_raw_customer_id",
        "sales_customer_profiles",
        ["raw_customer_id"],
        unique=False,
    )
    op.create_index(
        "ix_sales_customer_profiles_sales_wechat_id",
        "sales_customer_profiles",
        ["sales_wechat_id"],
        unique=False,
    )

    op.create_table(
        "scp_profile_tags",
        sa.Column("sales_customer_profile_id", sa.Integer(), nullable=False),
        sa.Column("profile_tag_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["sales_customer_profile_id"],
            ["sales_customer_profiles.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["profile_tag_id"],
            ["profile_tag_definitions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("sales_customer_profile_id", "profile_tag_id"),
    )
    op.create_index(
        "ix_scp_profile_tags_profile_tag_id",
        "scp_profile_tags",
        ["profile_tag_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scp_profile_tags_profile_tag_id", table_name="scp_profile_tags")
    op.drop_table("scp_profile_tags")

    op.drop_index("ix_sales_customer_profiles_sales_wechat_id", table_name="sales_customer_profiles")
    op.drop_index("ix_sales_customer_profiles_raw_customer_id", table_name="sales_customer_profiles")
    op.drop_index("ix_scp_user_sales", table_name="sales_customer_profiles")
    op.drop_table("sales_customer_profiles")

