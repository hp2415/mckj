"""P0 attribution: prompt versions, outbound task link, input snapshots

Revision ID: 0001
Revises: f1a2b3c4d5e6
Create Date: 2026-07-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(conn, name: str) -> bool:
    row = conn.execute(sa.text("SHOW TABLES LIKE :t"), {"t": name}).fetchone()
    return bool(row)


def _has_column(conn, table: str, column: str) -> bool:
    row = conn.execute(sa.text(f"SHOW COLUMNS FROM `{table}` LIKE :c"), {"c": column}).fetchone()
    return bool(row)


def _has_index(conn, table: str, index: str) -> bool:
    row = conn.execute(sa.text(f"SHOW INDEX FROM `{table}` WHERE Key_name = :k"), {"k": index}).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_column(conn, "wechat_outbound_actions", "source_contact_task_id"):
        op.add_column(
            "wechat_outbound_actions",
            sa.Column("source_contact_task_id", sa.Integer(), nullable=True),
        )
    if not _has_index(conn, "wechat_outbound_actions", "ix_woa_contact_task"):
        op.create_index(
            "ix_woa_contact_task",
            "wechat_outbound_actions",
            ["source_contact_task_id"],
            unique=False,
        )
    try:
        op.create_foreign_key(
            "fk_woa_source_contact_task",
            "wechat_outbound_actions",
            "contact_tasks",
            ["source_contact_task_id"],
            ["id"],
            ondelete="SET NULL",
        )
    except Exception:
        pass

    if not _has_column(conn, "sales_customer_profiles", "profile_prompt_version_id"):
        op.add_column(
            "sales_customer_profiles",
            sa.Column("profile_prompt_version_id", sa.Integer(), nullable=True),
        )
    if not _has_index(conn, "sales_customer_profiles", "ix_scp_profile_prompt_version"):
        op.create_index(
            "ix_scp_profile_prompt_version",
            "sales_customer_profiles",
            ["profile_prompt_version_id"],
            unique=False,
        )

    if not _has_column(conn, "contact_tasks", "prompt_version_id"):
        op.add_column(
            "contact_tasks",
            sa.Column("prompt_version_id", sa.Integer(), nullable=True),
        )
    if not _has_index(conn, "contact_tasks", "ix_contact_tasks_pv"):
        op.create_index(
            "ix_contact_tasks_pv",
            "contact_tasks",
            ["prompt_version_id", "due_date"],
            unique=False,
        )

    if not _has_column(conn, "llm_usage_log", "prompt_version_id"):
        op.add_column(
            "llm_usage_log",
            sa.Column("prompt_version_id", sa.Integer(), nullable=True),
        )
    if not _has_index(conn, "llm_usage_log", "ix_llm_usage_prompt_version"):
        op.create_index(
            "ix_llm_usage_prompt_version",
            "llm_usage_log",
            ["prompt_version_id", "created_at"],
            unique=False,
        )

    if not _has_table(conn, "task_allocation_input_snapshots"):
        op.create_table(
            "task_allocation_input_snapshots",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("batch_id", sa.Integer(), nullable=False),
            sa.Column("sales_wechat_id", sa.String(length=100), nullable=False),
            sa.Column("ref_date", sa.Date(), nullable=False),
            sa.Column("payload_gzip", sa.LargeBinary(), nullable=False),
            sa.Column("payload_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("prompt_version_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(
                ["batch_id"],
                ["task_allocation_batches.id"],
                name="fk_tais_batch",
                ondelete="CASCADE",
            ),
        )
        op.create_index("ix_tais_batch", "task_allocation_input_snapshots", ["batch_id"])
        op.create_index(
            "ix_tais_sw_date",
            "task_allocation_input_snapshots",
            ["sales_wechat_id", "ref_date"],
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _has_table(conn, "task_allocation_input_snapshots"):
        op.drop_table("task_allocation_input_snapshots")

    if _has_index(conn, "llm_usage_log", "ix_llm_usage_prompt_version"):
        op.drop_index("ix_llm_usage_prompt_version", table_name="llm_usage_log")
    if _has_column(conn, "llm_usage_log", "prompt_version_id"):
        op.drop_column("llm_usage_log", "prompt_version_id")

    if _has_index(conn, "contact_tasks", "ix_contact_tasks_pv"):
        op.drop_index("ix_contact_tasks_pv", table_name="contact_tasks")
    if _has_column(conn, "contact_tasks", "prompt_version_id"):
        op.drop_column("contact_tasks", "prompt_version_id")

    if _has_index(conn, "sales_customer_profiles", "ix_scp_profile_prompt_version"):
        op.drop_index("ix_scp_profile_prompt_version", table_name="sales_customer_profiles")
    if _has_column(conn, "sales_customer_profiles", "profile_prompt_version_id"):
        op.drop_column("sales_customer_profiles", "profile_prompt_version_id")

    try:
        op.drop_constraint("fk_woa_source_contact_task", "wechat_outbound_actions", type_="foreignkey")
    except Exception:
        pass
    if _has_index(conn, "wechat_outbound_actions", "ix_woa_contact_task"):
        op.drop_index("ix_woa_contact_task", table_name="wechat_outbound_actions")
    if _has_column(conn, "wechat_outbound_actions", "source_contact_task_id"):
        op.drop_column("wechat_outbound_actions", "source_contact_task_id")
