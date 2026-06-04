"""raw_wechat_voice_calls: 微信语音/视频通话增量同步表

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-06-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, Sequence[str], None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "raw_wechat_voice_calls",
        sa.Column("record_id", sa.String(length=64), nullable=False),
        sa.Column("user_name", sa.String(length=128), nullable=True),
        sa.Column("user_phone", sa.String(length=32), nullable=True),
        sa.Column("user_we_chat_nick_name", sa.String(length=128), nullable=True),
        sa.Column("user_we_chat_alias", sa.String(length=64), nullable=True),
        sa.Column("user_we_chat_head_img", sa.String(length=512), nullable=True),
        sa.Column("user_we_chat_phone", sa.String(length=32), nullable=True),
        sa.Column("talker_head_img", sa.String(length=512), nullable=True),
        sa.Column("talker_nick_name", sa.String(length=128), nullable=True),
        sa.Column("talker_alias", sa.String(length=64), nullable=True),
        sa.Column("call_type", sa.Integer(), nullable=True),
        sa.Column("is_send", sa.Integer(), nullable=True),
        sa.Column("call_status", sa.Integer(), nullable=True),
        sa.Column("oss_file_name", sa.String(length=512), nullable=True),
        sa.Column("duration", sa.String(length=32), nullable=True),
        sa.Column("start_time", sa.DateTime(), nullable=False),
        sa.Column("end_time", sa.DateTime(), nullable=True),
        sa.Column("we_chat_id", sa.String(length=100), nullable=False),
        sa.Column("talker", sa.String(length=100), nullable=False),
        sa.Column("is_room", sa.Integer(), nullable=True),
        sa.Column("remark", sa.String(length=512), nullable=True),
        sa.Column("duration_file", sa.Integer(), nullable=True),
        sa.Column("cursor_next_id", sa.Numeric(20, 0), nullable=True),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column("imported_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("record_id"),
    )
    op.create_index("ix_raw_wvc_we_chat_id", "raw_wechat_voice_calls", ["we_chat_id"])
    op.create_index("ix_raw_wvc_talker", "raw_wechat_voice_calls", ["talker"])
    op.create_index("ix_raw_wvc_start_time", "raw_wechat_voice_calls", ["start_time"])
    op.create_index(
        "ix_raw_wvc_sales_talker_start",
        "raw_wechat_voice_calls",
        ["we_chat_id", "talker", "start_time"],
    )
    op.create_index(
        "ix_raw_wvc_cursor_next_id",
        "raw_wechat_voice_calls",
        ["cursor_next_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_raw_wvc_cursor_next_id", table_name="raw_wechat_voice_calls")
    op.drop_index("ix_raw_wvc_sales_talker_start", table_name="raw_wechat_voice_calls")
    op.drop_index("ix_raw_wvc_start_time", table_name="raw_wechat_voice_calls")
    op.drop_index("ix_raw_wvc_talker", table_name="raw_wechat_voice_calls")
    op.drop_index("ix_raw_wvc_we_chat_id", table_name="raw_wechat_voice_calls")
    op.drop_table("raw_wechat_voice_calls")
