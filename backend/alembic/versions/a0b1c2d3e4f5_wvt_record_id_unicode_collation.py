"""wechat_voice_transcripts.record_id: align collation with raw_wechat_voice_calls

Revision ID: a0b1c2d3e4f5
Revises: z4a5b6c7d8e9
Create Date: 2026-06-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, Sequence[str], None] = "z4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNICODE_CI = "utf8mb4_unicode_ci"


def _has_table(conn, name: str) -> bool:
    row = conn.execute(sa.text("SHOW TABLES LIKE :t"), {"t": name}).fetchone()
    return bool(row)


def _column_collation(conn, table: str, column: str) -> str | None:
    row = conn.execute(
        sa.text(
            "SELECT COLLATION_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c "
            "LIMIT 1"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return row[0] if row else None


def _align_record_id_collation(conn, table: str) -> None:
    if not _has_table(conn, table):
        return
    if _column_collation(conn, table, "record_id") == _UNICODE_CI:
        return
    op.execute(
        sa.text(
            f"ALTER TABLE {table} "
            "MODIFY record_id VARCHAR(64) "
            f"CHARACTER SET utf8mb4 COLLATE {_UNICODE_CI} NOT NULL"
        )
    )


def upgrade() -> None:
    conn = op.get_bind()
    # JOIN 两侧 record_id 须同一 collation，否则 MySQL 1267
    _align_record_id_collation(conn, "raw_wechat_voice_calls")
    _align_record_id_collation(conn, "wechat_voice_transcripts")


def downgrade() -> None:
    pass
