"""P0.5: ai_optimization_proposals for optimizer parameter track

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(conn, name: str) -> bool:
    row = conn.execute(sa.text("SHOW TABLES LIKE :t"), {"t": name}).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "ai_optimization_proposals"):
        return

    op.create_table(
        "ai_optimization_proposals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("track", sa.String(length=20), nullable=False),
        sa.Column("scenario_key", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("trigger_metric_json", sa.JSON(), nullable=False),
        sa.Column("change_json", sa.JSON(), nullable=False),
        sa.Column("before_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("after_version_id", sa.Integer(), nullable=True),
        sa.Column("auto_applied", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("applied_by", sa.String(length=50), nullable=True),
        sa.Column("rollback_reason", sa.String(length=255), nullable=True),
        sa.Column("effect_json", sa.JSON(), nullable=True),
        sa.Column("observation_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_aop_status", "ai_optimization_proposals", ["status", "created_at"])
    op.create_index("ix_aop_track", "ai_optimization_proposals", ["track", "scenario_key"])


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "ai_optimization_proposals"):
        return
    op.drop_table("ai_optimization_proposals")
