"""add raw_chat_logs unique keys for wechat messages

Revision ID: u1v2w3x4y5z6
Revises: t2u3v4w5x6y7
Create Date: 2026-04-25

- Extend raw_chat_logs to support de-dup by (wechat_id, talker, msg_svr_id)
- Add time_ms/send_timestamp_ms for stable incremental cursors
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "u1v2w3x4y5z6"
down_revision: Union[str, Sequence[str], None] = "t2u3v4w5x6y7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _colnames(conn, table: str) -> set[str]:
    rows = conn.execute(sa.text(f"SHOW COLUMNS FROM {table}")).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _colnames(conn, "raw_chat_logs")

    with op.batch_alter_table("raw_chat_logs") as b:
        if "msg_svr_id" not in cols:
            b.add_column(sa.Column("msg_svr_id", sa.String(length=100), nullable=True))
        if "roomid" not in cols:
            b.add_column(sa.Column("roomid", sa.String(length=100), nullable=True))
        if "send_timestamp_ms" not in cols:
            b.add_column(sa.Column("send_timestamp_ms", sa.Numeric(20, 0), nullable=True))
        if "time_ms" not in cols:
            b.add_column(sa.Column("time_ms", sa.Numeric(20, 0), nullable=True))

        # Indexes / unique constraint (best-effort, avoid failing on reruns)
        try:
            b.create_index("ix_raw_chat_time_ms", ["time_ms"], unique=False)
        except Exception:
            pass
        try:
            b.create_index("ix_raw_chat_wechat_time", ["wechat_id", "time_ms"], unique=False)
        except Exception:
            pass
        try:
            b.create_unique_constraint(
                "uq_raw_chat_wechat_talker_msg", ["wechat_id", "talker", "msg_svr_id"]
            )
        except Exception:
            pass


def downgrade() -> None:
    conn = op.get_bind()
    cols = _colnames(conn, "raw_chat_logs")

    with op.batch_alter_table("raw_chat_logs") as b:
        try:
            b.drop_constraint("uq_raw_chat_wechat_talker_msg", type_="unique")
        except Exception:
            pass
        for ix in ("ix_raw_chat_wechat_time", "ix_raw_chat_time_ms"):
            try:
                b.drop_index(ix)
            except Exception:
                pass
        if "time_ms" in cols:
            b.drop_column("time_ms")
        if "send_timestamp_ms" in cols:
            b.drop_column("send_timestamp_ms")
        if "roomid" in cols:
            b.drop_column("roomid")
        if "msg_svr_id" in cols:
            b.drop_column("msg_svr_id")

