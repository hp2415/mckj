"""raw_wechat_voice_calls: align we_chat_id/talker collation with contact_tasks

Revision ID: f2g3h4i5j6k7
Revises: e1f2g3h4i5j6
Create Date: 2026-06-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "f2g3h4i5j6k7"
down_revision: Union[str, Sequence[str], None] = "e1f2g3h4i5j6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNICODE_CI = "utf8mb4_unicode_ci"
_WVC_STRING = mysql.VARCHAR(collation=_UNICODE_CI, length=100)


def upgrade() -> None:
    # contact_tasks / raw_customer_sales_wechats 使用 utf8mb4_unicode_ci；
    # raw_wechat_voice_calls 建表时未显式指定，MySQL8 默认为 utf8mb4_0900_ai_ci，JOIN 会报 1267。
    op.alter_column(
        "raw_wechat_voice_calls",
        "we_chat_id",
        existing_type=sa.String(length=100),
        type_=_WVC_STRING,
        existing_nullable=False,
    )
    op.alter_column(
        "raw_wechat_voice_calls",
        "talker",
        existing_type=sa.String(length=100),
        type_=_WVC_STRING,
        existing_nullable=False,
    )


def downgrade() -> None:
    _default = mysql.VARCHAR(collation="utf8mb4_0900_ai_ci", length=100)
    op.alter_column(
        "raw_wechat_voice_calls",
        "talker",
        existing_type=_WVC_STRING,
        type_=_default,
        existing_nullable=False,
    )
    op.alter_column(
        "raw_wechat_voice_calls",
        "we_chat_id",
        existing_type=_WVC_STRING,
        type_=_default,
        existing_nullable=False,
    )
