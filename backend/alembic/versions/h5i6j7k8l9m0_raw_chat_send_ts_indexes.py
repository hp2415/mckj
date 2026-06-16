"""raw_chat_logs send_timestamp_ms indexes + scp profiled_at index

Revision ID: h5i6j7k8l9m0
Revises: g3h4i5j6k7l8
Create Date: 2026-06-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h5i6j7k8l9m0"
down_revision: Union[str, Sequence[str], None] = "g3h4i5j6k7l8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(conn, table: str, index: str) -> bool:
    row = conn.execute(
        sa.text("SHOW INDEX FROM %s WHERE Key_name=:k" % table),
        {"k": index},
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_index(conn, "raw_chat_logs", "ix_raw_chat_send_ts"):
        op.create_index(
            "ix_raw_chat_send_ts",
            "raw_chat_logs",
            ["send_timestamp_ms"],
            unique=False,
        )
    if not _has_index(conn, "raw_chat_logs", "ix_raw_chat_wechat_send_ts"):
        op.create_index(
            "ix_raw_chat_wechat_send_ts",
            "raw_chat_logs",
            ["wechat_id", "send_timestamp_ms"],
            unique=False,
        )
    if not _has_index(conn, "raw_chat_logs", "ix_raw_chat_talker_send_ts"):
        op.create_index(
            "ix_raw_chat_talker_send_ts",
            "raw_chat_logs",
            ["talker", "send_timestamp_ms"],
            unique=False,
        )
    if not _has_index(conn, "sales_customer_profiles", "ix_scp_profiled_at"):
        op.create_index(
            "ix_scp_profiled_at",
            "sales_customer_profiles",
            ["profiled_at"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_index(conn, "sales_customer_profiles", "ix_scp_profiled_at"):
        op.drop_index("ix_scp_profiled_at", table_name="sales_customer_profiles")
    if _has_index(conn, "raw_chat_logs", "ix_raw_chat_talker_send_ts"):
        op.drop_index("ix_raw_chat_talker_send_ts", table_name="raw_chat_logs")
    if _has_index(conn, "raw_chat_logs", "ix_raw_chat_wechat_send_ts"):
        op.drop_index("ix_raw_chat_wechat_send_ts", table_name="raw_chat_logs")
    if _has_index(conn, "raw_chat_logs", "ix_raw_chat_send_ts"):
        op.drop_index("ix_raw_chat_send_ts", table_name="raw_chat_logs")
