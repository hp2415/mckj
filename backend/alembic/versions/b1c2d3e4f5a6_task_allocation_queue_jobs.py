"""task_allocation_queue_jobs for parallel scheduled allocation

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-06-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(conn, name: str) -> bool:
    row = conn.execute(sa.text("SHOW TABLES LIKE :t"), {"t": name}).fetchone()
    return bool(row)


def _has_index(conn, table: str, index: str) -> bool:
    row = conn.execute(sa.text("SHOW INDEX FROM %s WHERE Key_name=:k" % table), {"k": index}).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "task_allocation_queue_jobs"):
        return

    op.create_table(
        "task_allocation_queue_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sales_wechat_id", sa.String(length=100), nullable=False),
        sa.Column("period_type", sa.String(length=20), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("ref_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False, server_default="ai_auto"),
        sa.Column("auto_publish", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("batch_id", sa.String(length=32), nullable=True),
        sa.Column("batch_label", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result_batch_id", sa.Integer(), nullable=True),
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
    )

    if not _has_index(conn, "task_allocation_queue_jobs", "ix_taq_jobs_status_id"):
        op.create_index(
            "ix_taq_jobs_status_id",
            "task_allocation_queue_jobs",
            ["status", "id"],
            unique=False,
        )
    if not _has_index(conn, "task_allocation_queue_jobs", "ix_taq_jobs_batch"):
        op.create_index(
            "ix_taq_jobs_batch",
            "task_allocation_queue_jobs",
            ["batch_id"],
            unique=False,
        )
    if not _has_index(conn, "task_allocation_queue_jobs", "ix_taq_jobs_sales_period"):
        op.create_index(
            "ix_taq_jobs_sales_period",
            "task_allocation_queue_jobs",
            ["sales_wechat_id", "period_type", "period_start"],
            unique=False,
        )
    if not _has_index(conn, "task_allocation_queue_jobs", "ix_taq_jobs_dedupe_status"):
        op.create_index(
            "ix_taq_jobs_dedupe_status",
            "task_allocation_queue_jobs",
            ["dedupe_key", "status"],
            unique=False,
        )
    if not _has_index(conn, "task_allocation_queue_jobs", "ix_taq_jobs_locked_at"):
        op.create_index(
            "ix_taq_jobs_locked_at",
            "task_allocation_queue_jobs",
            ["locked_at"],
            unique=False,
        )
    if not _has_index(conn, "task_allocation_queue_jobs", "ix_task_allocation_queue_jobs_sales_wechat_id"):
        op.create_index(
            "ix_task_allocation_queue_jobs_sales_wechat_id",
            "task_allocation_queue_jobs",
            ["sales_wechat_id"],
            unique=False,
        )
    if not _has_index(conn, "task_allocation_queue_jobs", "ix_task_allocation_queue_jobs_dedupe_key"):
        op.create_index(
            "ix_task_allocation_queue_jobs_dedupe_key",
            "task_allocation_queue_jobs",
            ["dedupe_key"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "task_allocation_queue_jobs"):
        return
    op.drop_table("task_allocation_queue_jobs")
