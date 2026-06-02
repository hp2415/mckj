"""task allocation batches and contact tasks

Revision ID: a3b4c5d6e7f8
Revises: y2u3i4o5p6a7
Create Date: 2026-05-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "z9a8b7c6d5e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_allocation_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        # sales_wechat_accounts.sales_wechat_id 为 utf8mb4_unicode_ci，外键列须显式对齐
        sa.Column("sales_wechat_id", sa.String(length=100, collation="utf8mb4_unicode_ci"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("period_type", sa.String(length=20), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=30), server_default="ai_auto", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("task_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["sales_wechat_id"], ["sales_wechat_accounts.sales_wechat_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tab_sales_period", "task_allocation_batches", ["sales_wechat_id", "period_type", "period_start"])
    op.create_index("ix_tab_status", "task_allocation_batches", ["status"])
    op.create_index(op.f("ix_task_allocation_batches_sales_wechat_id"), "task_allocation_batches", ["sales_wechat_id"])
    op.create_index(op.f("ix_task_allocation_batches_user_id"), "task_allocation_batches", ["user_id"])

    op.create_table(
        "contact_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("scp_id", sa.Integer(), nullable=True),
        sa.Column("raw_customer_id", sa.String(length=100, collation="utf8mb4_unicode_ci"), nullable=False),
        # sales_wechat_accounts.sales_wechat_id 为 utf8mb4_unicode_ci，外键列须显式对齐
        sa.Column("sales_wechat_id", sa.String(length=100, collation="utf8mb4_unicode_ci"), nullable=False),
        sa.Column("period_type", sa.String(length=20), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("task_kind", sa.String(length=30), server_default="contact", nullable=False),
        sa.Column("priority_rank", sa.Integer(), server_default="0", nullable=False),
        sa.Column("priority_score", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("instruction", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("completed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("completion_note", sa.String(length=500), nullable=True),
        sa.Column("dedupe_key", sa.String(length=320), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["task_allocation_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["completed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scp_id"], ["sales_customer_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_contact_tasks_dedupe_key"),
    )
    op.create_index("ix_contact_tasks_batch_rank", "contact_tasks", ["batch_id", "priority_rank"])
    op.create_index("ix_contact_tasks_sales_due", "contact_tasks", ["sales_wechat_id", "due_date", "status"])
    op.create_index("ix_contact_tasks_dedupe", "contact_tasks", ["dedupe_key"])
    op.create_index(op.f("ix_contact_tasks_batch_id"), "contact_tasks", ["batch_id"])
    op.create_index(op.f("ix_contact_tasks_raw_customer_id"), "contact_tasks", ["raw_customer_id"])
    op.create_index(op.f("ix_contact_tasks_sales_wechat_id"), "contact_tasks", ["sales_wechat_id"])
    op.create_index(op.f("ix_contact_tasks_scp_id"), "contact_tasks", ["scp_id"])


def downgrade() -> None:
    op.drop_table("contact_tasks")
    op.drop_table("task_allocation_batches")
