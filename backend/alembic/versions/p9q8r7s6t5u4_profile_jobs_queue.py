"""profile_jobs queue for parallel profiling

Revision ID: p9q8r7s6t5u4
Revises: x8y9z0a1b2c3
Create Date: 2026-05-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "p9q8r7s6t5u4"
down_revision: Union[str, Sequence[str], None] = "x8y9z0a1b2c3"
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
    if _has_table(conn, "profile_jobs"):
        return

    op.create_table(
        "profile_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("raw_customer_id", sa.String(length=100), nullable=False),
        sa.Column("sales_wechat_id", sa.String(length=100), nullable=False),
        sa.Column("dedupe_key", sa.String(length=260), nullable=False),
        sa.Column("batch_id", sa.String(length=32), nullable=True),
        sa.Column("batch_label", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("dedupe_key", name="uq_profile_jobs_dedupe_key"),
    )

    # Indexes (MySQL)
    if not _has_index(conn, "profile_jobs", "ix_profile_jobs_status_id"):
        op.create_index("ix_profile_jobs_status_id", "profile_jobs", ["status", "id"], unique=False)
    if not _has_index(conn, "profile_jobs", "ix_profile_jobs_batch"):
        op.create_index("ix_profile_jobs_batch", "profile_jobs", ["batch_id"], unique=False)
    if not _has_index(conn, "profile_jobs", "ix_profile_jobs_raw_sales"):
        op.create_index(
            "ix_profile_jobs_raw_sales",
            "profile_jobs",
            ["raw_customer_id", "sales_wechat_id"],
            unique=False,
        )
    if not _has_index(conn, "profile_jobs", "ix_profile_jobs_locked_at"):
        op.create_index("ix_profile_jobs_locked_at", "profile_jobs", ["locked_at"], unique=False)


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "profile_jobs"):
        return
    op.drop_table("profile_jobs")

