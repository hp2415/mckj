"""campaign blast jobs: custom kind / nullable campaign_id / media fields

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(conn, table: str, column: str) -> bool:
    row = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c LIMIT 1"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return bool(row)


def _has_index(conn, table: str, index: str) -> bool:
    row = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND INDEX_NAME = :i LIMIT 1"
        ),
        {"t": table, "i": index},
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    table = "campaign_blast_jobs"

    if not _has_column(conn, table, "job_kind"):
        op.add_column(
            table,
            sa.Column(
                "job_kind",
                sa.String(length=20),
                nullable=False,
                server_default="campaign",
            ),
        )
    if not _has_column(conn, table, "custom_brief"):
        op.add_column(table, sa.Column("custom_brief", sa.Text(), nullable=True))
    if not _has_column(conn, table, "media_mode"):
        op.add_column(
            table,
            sa.Column(
                "media_mode",
                sa.String(length=20),
                nullable=False,
                server_default="poster",
            ),
        )
    if not _has_column(conn, table, "custom_image_path"):
        op.add_column(
            table, sa.Column("custom_image_path", sa.String(length=500), nullable=True)
        )

    # campaign_id → nullable（自定义任务无活动）
    op.alter_column(
        table,
        "campaign_id",
        existing_type=sa.Integer(),
        nullable=True,
        existing_nullable=False,
    )

    if not _has_index(conn, table, "ix_campaign_blast_jobs_user_custom"):
        op.create_index(
            "ix_campaign_blast_jobs_user_custom",
            table,
            ["user_id", "sales_wechat_id", "unit_type", "job_kind", "status"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    table = "campaign_blast_jobs"

    if _has_index(conn, table, "ix_campaign_blast_jobs_user_custom"):
        op.drop_index("ix_campaign_blast_jobs_user_custom", table_name=table)

    # 降级前把自定义任务的空 campaign_id 清掉或填占位会失败；仅改回非空需先无 NULL
    op.execute(
        sa.text(
            "UPDATE campaign_blast_jobs SET campaign_id = 0 "
            "WHERE campaign_id IS NULL"
        )
    )
    op.alter_column(
        table,
        "campaign_id",
        existing_type=sa.Integer(),
        nullable=False,
        existing_nullable=True,
    )

    for col in ("custom_image_path", "media_mode", "custom_brief", "job_kind"):
        if _has_column(conn, table, col):
            op.drop_column(table, col)
