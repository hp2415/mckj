"""wechat_voice_transcripts table for async speech-to-text

Revision ID: v2w3x4y5z6a7
Revises: h5i6j7k8l9m0
Create Date: 2026-06-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "v2w3x4y5z6a7"
down_revision: Union[str, Sequence[str], None] = "h5i6j7k8l9m0"
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
    if _has_table(conn, "wechat_voice_transcripts"):
        return

    op.create_table(
        "wechat_voice_transcripts",
        sa.Column("record_id", sa.String(length=64), primary_key=True),
        sa.Column("we_chat_id", sa.String(length=100, collation="utf8mb4_unicode_ci"), nullable=False),
        sa.Column("talker", sa.String(length=100, collation="utf8mb4_unicode_ci"), nullable=False),
        sa.Column("file_link", sa.String(length=512), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("transcript_text", sa.Text(), nullable=True),
        sa.Column("transcript_json", sa.Text(), nullable=True),
        sa.Column("sentence_count", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=True),
        sa.Column("batch_id", sa.String(length=32), nullable=True),
        sa.Column("batch_label", sa.String(length=120), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("poll_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("is_send", sa.Integer(), nullable=True),
        sa.Column("call_start_time", sa.DateTime(), nullable=True),
        sa.Column("duration_file", sa.Integer(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    if not _has_index(conn, "wechat_voice_transcripts", "ix_wvt_status_id"):
        op.create_index("ix_wvt_status_id", "wechat_voice_transcripts", ["status", "record_id"], unique=False)
    if not _has_index(conn, "wechat_voice_transcripts", "ix_wvt_sales_talker"):
        op.create_index("ix_wvt_sales_talker", "wechat_voice_transcripts", ["we_chat_id", "talker"], unique=False)
    if not _has_index(conn, "wechat_voice_transcripts", "ix_wvt_task_id"):
        op.create_index("ix_wvt_task_id", "wechat_voice_transcripts", ["task_id"], unique=False)
    if not _has_index(conn, "wechat_voice_transcripts", "ix_wvt_batch"):
        op.create_index("ix_wvt_batch", "wechat_voice_transcripts", ["batch_id"], unique=False)
    if not _has_index(conn, "wechat_voice_transcripts", "ix_wvt_call_start"):
        op.create_index("ix_wvt_call_start", "wechat_voice_transcripts", ["call_start_time"], unique=False)

    # record_id 与 raw_wechat_voice_calls 对齐，避免 JOIN 时 collation 1267
    op.execute(
        sa.text(
            "ALTER TABLE wechat_voice_transcripts "
            "MODIFY record_id VARCHAR(64) "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "wechat_voice_transcripts"):
        return
    op.drop_table("wechat_voice_transcripts")
