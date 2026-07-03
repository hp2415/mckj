"""raw_orders: wechat_idx + staff_uuid for future customer matching

Revision ID: c6d7e8f9a0b1
Revises: b1c2d3e4f5a6
Create Date: 2026-07-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c6d7e8f9a0b1"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
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


def _has_index(conn, table: str, index_name: str) -> bool:
    row = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND INDEX_NAME = :i LIMIT 1"
        ),
        {"t": table, "i": index_name},
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "raw_orders", "wechat_idx"):
        op.add_column(
            "raw_orders",
            sa.Column("wechat_idx", sa.String(100), nullable=True),
        )
    if not _has_column(conn, "raw_orders", "staff_uuid"):
        op.add_column(
            "raw_orders",
            sa.Column("staff_uuid", sa.String(36), nullable=True),
        )
    if not _has_index(conn, "raw_orders", "ix_raw_orders_wechat_idx"):
        op.create_index("ix_raw_orders_wechat_idx", "raw_orders", ["wechat_idx"])
    if not _has_index(conn, "raw_orders", "ix_raw_orders_staff_uuid"):
        op.create_index("ix_raw_orders_staff_uuid", "raw_orders", ["staff_uuid"])


def downgrade() -> None:
    conn = op.get_bind()
    if _has_index(conn, "raw_orders", "ix_raw_orders_staff_uuid"):
        op.drop_index("ix_raw_orders_staff_uuid", table_name="raw_orders")
    if _has_index(conn, "raw_orders", "ix_raw_orders_wechat_idx"):
        op.drop_index("ix_raw_orders_wechat_idx", table_name="raw_orders")
    if _has_column(conn, "raw_orders", "staff_uuid"):
        op.drop_column("raw_orders", "staff_uuid")
    if _has_column(conn, "raw_orders", "wechat_idx"):
        op.drop_column("raw_orders", "wechat_idx")
