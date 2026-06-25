"""phone_call_records table for MiBuddy history_call_record sync

Revision ID: y3z4a5b6c7d8
Revises: v2w3x4y5z6a7
Create Date: 2026-06-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "y3z4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "v2w3x4y5z6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(conn, name: str) -> bool:
    row = conn.execute(sa.text("SHOW TABLES LIKE :t"), {"t": name}).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "phone_call_records"):
        return

    op.create_table(
        "phone_call_records",
        sa.Column("call_id", sa.String(length=64), primary_key=True),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("dial_type", sa.Integer(), nullable=True),
        sa.Column("callee", sa.String(length=32), nullable=False),
        sa.Column(
            "user_wechat_account",
            sa.String(length=100, collation="utf8mb4_unicode_ci"),
            nullable=True,
        ),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("file_link", sa.String(length=512), nullable=True),
        sa.Column("status_text", sa.String(length=16), nullable=True),
        sa.Column("staff_name", sa.String(length=64), nullable=True),
        sa.Column("staff_uuid", sa.String(length=64), nullable=True),
        sa.Column("transcript_text", sa.Text(), nullable=True),
        sa.Column("transcript_json", sa.Text(), nullable=True),
        sa.Column("sentence_count", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column("imported_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_pcr_create_time", "phone_call_records", ["create_time"])
    op.create_index("ix_pcr_callee", "phone_call_records", ["callee"])
    op.create_index(
        "ix_pcr_user_wechat_account", "phone_call_records", ["user_wechat_account"]
    )
    op.create_index("ix_pcr_sales_callee", "phone_call_records", ["user_wechat_account", "callee"])
    op.create_index("ix_pcr_staff_uuid", "phone_call_records", ["staff_uuid"])


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "phone_call_records"):
        return
    op.drop_index("ix_pcr_staff_uuid", table_name="phone_call_records")
    op.drop_index("ix_pcr_sales_callee", table_name="phone_call_records")
    op.drop_index("ix_pcr_user_wechat_account", table_name="phone_call_records")
    op.drop_index("ix_pcr_callee", table_name="phone_call_records")
    op.drop_index("ix_pcr_create_time", table_name="phone_call_records")
    op.drop_table("phone_call_records")
