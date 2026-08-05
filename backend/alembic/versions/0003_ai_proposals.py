"""P0 proposal artifacts and persistent async jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(conn, name: str) -> bool:
    row = conn.execute(sa.text("SHOW TABLES LIKE :t"), {"t": name}).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "ai_proposals"):
        op.create_table(
            "ai_proposals",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("raw_customer_id", sa.String(length=100), nullable=True),
            sa.Column("sales_wechat_id", sa.String(length=100), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
            sa.Column("current_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("query", sa.Text(), nullable=False),
            sa.Column("constraint_json", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("followup_note_written", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("locked_by", sa.String(length=80), nullable=True),
            sa.Column("locked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
                server_onupdate=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["raw_customer_id"], ["raw_customers.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_ai_proposals_user_id", "ai_proposals", ["user_id"])
        op.create_index("ix_ai_proposals_raw_customer_id", "ai_proposals", ["raw_customer_id"])
        op.create_index("ix_ai_proposals_sales_wechat_id", "ai_proposals", ["sales_wechat_id"])
        op.create_index("ix_ai_proposals_user_status", "ai_proposals", ["user_id", "status"])
        op.create_index("ix_ai_proposals_status_id", "ai_proposals", ["status", "id"])

    if not _has_table(conn, "ai_proposal_versions"):
        op.create_table(
            "ai_proposal_versions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("proposal_id", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("spec_json", sa.JSON(), nullable=False),
            sa.Column("file_path", sa.String(length=500), nullable=False),
            sa.Column("source", sa.String(length=20), nullable=False, server_default="generate"),
            sa.Column("user_feedback", sa.Text(), nullable=True),
            sa.Column("prompt_version_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["proposal_id"], ["ai_proposals.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("proposal_id", "version", name="uq_ai_proposal_version"),
        )
        op.create_index("ix_ai_proposal_versions_proposal_id", "ai_proposal_versions", ["proposal_id"])
        op.create_index("ix_ai_proposal_versions_prompt_version_id", "ai_proposal_versions", ["prompt_version_id"])
        op.create_index(
            "ix_ai_proposal_versions_proposal",
            "ai_proposal_versions",
            ["proposal_id", "version"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "ai_proposal_versions"):
        op.drop_table("ai_proposal_versions")
    if _has_table(conn, "ai_proposals"):
        op.drop_table("ai_proposals")
