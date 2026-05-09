"""raw_chat_logs: session index for (wechat_id,talker,time_ms)

Revision ID: r6t7y8u9i0o1
Revises: q1w2e3r4t5y6
Create Date: 2026-05-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "r6t7y8u9i0o1"
down_revision: Union[str, Sequence[str], None] = "q1w2e3r4t5y6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(conn, table: str, index: str) -> bool:
    row = conn.execute(sa.text("SHOW INDEX FROM %s WHERE Key_name=:k" % table), {"k": index}).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    # 会话维度查询：WHERE wechat_id=? AND talker=? ORDER BY time_ms DESC LIMIT N
    # 备注：time_ms 为同步模块写入的“保存时间”，更适合排序与追赶；旧逻辑仍会回退 timestamp
    if not _has_index(conn, "raw_chat_logs", "ix_raw_chat_session_time"):
        op.create_index(
            "ix_raw_chat_session_time",
            "raw_chat_logs",
            ["wechat_id", "talker", "time_ms"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_index(conn, "raw_chat_logs", "ix_raw_chat_session_time"):
        op.drop_index("ix_raw_chat_session_time", table_name="raw_chat_logs")

