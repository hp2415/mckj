"""add marketing campaign / poster / send tables

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(conn, table: str) -> bool:
    row = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t LIMIT 1"
        ),
        {"t": table},
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "campaigns"):
        op.create_table(
            "campaigns",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("start_at", sa.DateTime(), nullable=False),
            sa.Column("end_at", sa.DateTime(), nullable=False),
            sa.Column("audience_unit_types", sa.JSON(), nullable=False),
            sa.Column("rules", sa.Text(), nullable=True),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="enabled",
            ),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_campaigns_status_window",
            "campaigns",
            ["status", "start_at", "end_at"],
        )

    if not _has_table(conn, "campaign_posters"):
        op.create_table(
            "campaign_posters",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("campaign_id", sa.Integer(), nullable=False),
            sa.Column("image_path", sa.String(length=500), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("send_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_campaign_posters_campaign",
            "campaign_posters",
            ["campaign_id", "is_active"],
        )

    if not _has_table(conn, "campaign_poster_sends"):
        op.create_table(
            "campaign_poster_sends",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("campaign_id", sa.Integer(), nullable=False),
            sa.Column("poster_id", sa.Integer(), nullable=False),
            sa.Column(
                "raw_customer_id",
                mysql.VARCHAR(length=100, collation="utf8mb4_unicode_ci"),
                nullable=False,
            ),
            sa.Column("sales_wechat_id", sa.String(length=100), nullable=True),
            sa.Column("actor_user_id", sa.Integer(), nullable=True),
            sa.Column("outbound_action_id", sa.Integer(), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["poster_id"], ["campaign_posters.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["raw_customer_id"], ["raw_customers.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(
                ["outbound_action_id"],
                ["wechat_outbound_actions.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_campaign_poster_sends_customer",
            "campaign_poster_sends",
            ["campaign_id", "raw_customer_id"],
        )
        op.create_index(
            "ix_campaign_poster_sends_poster_customer",
            "campaign_poster_sends",
            ["poster_id", "raw_customer_id"],
        )
        op.create_index(
            "ix_campaign_poster_sends_outbound_action_id",
            "campaign_poster_sends",
            ["outbound_action_id"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "campaign_poster_sends"):
        op.drop_table("campaign_poster_sends")
    if _has_table(conn, "campaign_posters"):
        op.drop_table("campaign_posters")
    if _has_table(conn, "campaigns"):
        op.drop_table("campaigns")
