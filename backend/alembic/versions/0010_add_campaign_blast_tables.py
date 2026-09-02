"""add campaign blast job / recipient / receipt tables

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
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
    if not _has_table(conn, "campaign_blast_jobs"):
        op.create_table(
            "campaign_blast_jobs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("sales_wechat_id", sa.String(length=100), nullable=False),
            sa.Column("unit_type", sa.String(length=50), nullable=False),
            sa.Column("campaign_id", sa.Integer(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="draft",
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_campaign_blast_jobs_user_campaign",
            "campaign_blast_jobs",
            ["user_id", "campaign_id", "status"],
        )
        op.create_index(
            op.f("ix_campaign_blast_jobs_user_id"),
            "campaign_blast_jobs",
            ["user_id"],
        )
        op.create_index(
            op.f("ix_campaign_blast_jobs_sales_wechat_id"),
            "campaign_blast_jobs",
            ["sales_wechat_id"],
        )
        op.create_index(
            op.f("ix_campaign_blast_jobs_campaign_id"),
            "campaign_blast_jobs",
            ["campaign_id"],
        )

    if not _has_table(conn, "campaign_blast_recipients"):
        op.create_table(
            "campaign_blast_recipients",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column(
                "raw_customer_id",
                sa.String(length=100, collation="utf8mb4_unicode_ci"),
                nullable=False,
            ),
            sa.Column("display_name", sa.String(length=200), nullable=True),
            sa.Column("remark", sa.String(length=500), nullable=True),
            sa.Column("unit_type", sa.String(length=50), nullable=True),
            sa.Column("script_text", sa.Text(), nullable=True),
            sa.Column("script_generated_at", sa.DateTime(), nullable=True),
            sa.Column("poster_id", sa.Integer(), nullable=True),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("outbound_action_id", sa.Integer(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["job_id"], ["campaign_blast_jobs.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["raw_customer_id"], ["raw_customers.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["outbound_action_id"],
                ["wechat_outbound_actions.id"],
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["poster_id"], ["campaign_posters.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_campaign_blast_recipients_job_status",
            "campaign_blast_recipients",
            ["job_id", "status"],
        )
        op.create_index(
            "uq_campaign_blast_recipients_job_customer",
            "campaign_blast_recipients",
            ["job_id", "raw_customer_id"],
            unique=True,
        )
        op.create_index(
            op.f("ix_campaign_blast_recipients_job_id"),
            "campaign_blast_recipients",
            ["job_id"],
        )
        op.create_index(
            op.f("ix_campaign_blast_recipients_raw_customer_id"),
            "campaign_blast_recipients",
            ["raw_customer_id"],
        )

    if not _has_table(conn, "campaign_blast_receipts"):
        op.create_table(
            "campaign_blast_receipts",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("campaign_id", sa.Integer(), nullable=False),
            sa.Column(
                "raw_customer_id",
                sa.String(length=100, collation="utf8mb4_unicode_ci"),
                nullable=False,
            ),
            sa.Column("sales_wechat_id", sa.String(length=100), nullable=True),
            sa.Column("job_id", sa.Integer(), nullable=True),
            sa.Column("recipient_id", sa.Integer(), nullable=True),
            sa.Column("outbound_action_id", sa.Integer(), nullable=True),
            sa.Column("poster_id", sa.Integer(), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["raw_customer_id"], ["raw_customers.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["job_id"], ["campaign_blast_jobs.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["recipient_id"],
                ["campaign_blast_recipients.id"],
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["outbound_action_id"],
                ["wechat_outbound_actions.id"],
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["poster_id"], ["campaign_posters.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "uq_campaign_blast_receipts_campaign_customer",
            "campaign_blast_receipts",
            ["campaign_id", "raw_customer_id"],
            unique=True,
        )
        op.create_index(
            op.f("ix_campaign_blast_receipts_campaign_id"),
            "campaign_blast_receipts",
            ["campaign_id"],
        )
        op.create_index(
            op.f("ix_campaign_blast_receipts_raw_customer_id"),
            "campaign_blast_receipts",
            ["raw_customer_id"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "campaign_blast_receipts"):
        op.drop_table("campaign_blast_receipts")
    if _has_table(conn, "campaign_blast_recipients"):
        op.drop_table("campaign_blast_recipients")
    if _has_table(conn, "campaign_blast_jobs"):
        op.drop_table("campaign_blast_jobs")
