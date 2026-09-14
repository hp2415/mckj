"""user delete: business_transfers CASCADE, chat_messages SET NULL

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-14

- business_transfers.from_user_id / to_user_id: ON DELETE CASCADE（删账号时带走移交记录）
- chat_messages.user_id: ON DELETE SET NULL（对话可保留，解除对 users 的 RESTRICT）
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(sa.text("SHOW TABLES LIKE :n"), {"n": name}).fetchone()
    return bool(row)


def _drop_fk_on_column(conn, table: str, column: str) -> None:
    row = conn.execute(
        sa.text(
            """
            SELECT CONSTRAINT_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :t
              AND COLUMN_NAME = :c
              AND REFERENCED_TABLE_NAME IS NOT NULL
            LIMIT 1
            """
        ),
        {"t": table, "c": column},
    ).fetchone()
    if row and row[0]:
        conn.execute(sa.text(f"ALTER TABLE `{table}` DROP FOREIGN KEY `{row[0]}`"))


def upgrade() -> None:
    conn = op.get_bind()

    if _table_exists(conn, "business_transfers"):
        _drop_fk_on_column(conn, "business_transfers", "from_user_id")
        _drop_fk_on_column(conn, "business_transfers", "to_user_id")
        op.create_foreign_key(
            "business_transfers_ibfk_1",
            "business_transfers",
            "users",
            ["from_user_id"],
            ["id"],
            onupdate="CASCADE",
            ondelete="CASCADE",
        )
        op.create_foreign_key(
            "business_transfers_ibfk_2",
            "business_transfers",
            "users",
            ["to_user_id"],
            ["id"],
            onupdate="CASCADE",
            ondelete="CASCADE",
        )

    if _table_exists(conn, "chat_messages"):
        _drop_fk_on_column(conn, "chat_messages", "user_id")
        op.create_foreign_key(
            "chat_messages_ibfk_user_id",
            "chat_messages",
            "users",
            ["user_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _table_exists(conn, "chat_messages"):
        _drop_fk_on_column(conn, "chat_messages", "user_id")
        op.create_foreign_key(
            "chat_messages_ibfk_user_id",
            "chat_messages",
            "users",
            ["user_id"],
            ["id"],
        )

    if _table_exists(conn, "business_transfers"):
        _drop_fk_on_column(conn, "business_transfers", "from_user_id")
        _drop_fk_on_column(conn, "business_transfers", "to_user_id")
        op.create_foreign_key(
            "business_transfers_ibfk_1",
            "business_transfers",
            "users",
            ["from_user_id"],
            ["id"],
            onupdate="CASCADE",
        )
        op.create_foreign_key(
            "business_transfers_ibfk_2",
            "business_transfers",
            "users",
            ["to_user_id"],
            ["id"],
            onupdate="CASCADE",
        )
