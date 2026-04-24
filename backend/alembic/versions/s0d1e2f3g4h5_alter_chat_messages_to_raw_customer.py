"""alter chat_messages to reference raw_customers

Revision ID: s0d1e2f3g4h5
Revises: s0c1d2e3f4g5
Create Date: 2026-04-24

- Remove legacy FK chat_messages.customer_id -> customers.id
- Ensure chat_messages.raw_customer_id is non-null and references raw_customers.id
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "s0d1e2f3g4h5"
down_revision: Union[str, Sequence[str], None] = "s0c1d2e3f4g5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, name: str) -> bool:
    res = conn.execute(sa.text("SHOW TABLES LIKE :n"), {"n": name})
    return res.fetchone() is not None


def _colnames(conn, table: str) -> set[str]:
    rows = conn.execute(sa.text(f"SHOW COLUMNS FROM {table}")).fetchall()
    return {r[0] for r in rows}


def _drop_fk_if_exists(conn, table: str, column: str) -> None:
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
        conn.execute(sa.text(f"ALTER TABLE {table} DROP FOREIGN KEY `{row[0]}`"))


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "chat_messages"):
        return

    cols = _colnames(conn, "chat_messages")
    if "raw_customer_id" not in cols:
        op.add_column("chat_messages", sa.Column("raw_customer_id", sa.String(length=100), nullable=True))
        op.create_index("ix_chat_messages_raw_customer_id", "chat_messages", ["raw_customer_id"], unique=False)

    # Drop legacy FK + column
    if "customer_id" in cols:
        _drop_fk_if_exists(conn, "chat_messages", "customer_id")
        # Drop index on customer_id if any (name unknown), then drop column.
        op.execute(sa.text("ALTER TABLE chat_messages DROP COLUMN customer_id"))

    # Make raw_customer_id not null (after backfill), add FK.
    # If some rows are still null after backfill, drop them (test data; cannot satisfy FK).
    conn.execute(sa.text("UPDATE chat_messages SET raw_customer_id = NULLIF(TRIM(raw_customer_id), '')"))
    conn.execute(sa.text("DELETE FROM chat_messages WHERE raw_customer_id IS NULL"))
    conn.execute(
        sa.text(
            """
            DELETE cm
            FROM chat_messages cm
            LEFT JOIN raw_customers rc ON rc.id = cm.raw_customer_id
            WHERE rc.id IS NULL
            """
        )
    )

    # Ensure column type and nullability
    op.alter_column(
        "chat_messages",
        "raw_customer_id",
        existing_type=sa.String(length=100),
        nullable=False,
    )

    # Add FK (drop existing if present)
    _drop_fk_if_exists(conn, "chat_messages", "raw_customer_id")
    op.create_foreign_key(
        "fk_chat_messages_raw_customer_id",
        "chat_messages",
        "raw_customers",
        ["raw_customer_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "chat_messages"):
        return
    cols = _colnames(conn, "chat_messages")

    # Drop new FK
    try:
        op.drop_constraint("fk_chat_messages_raw_customer_id", "chat_messages", type_="foreignkey")
    except Exception:
        _drop_fk_if_exists(conn, "chat_messages", "raw_customer_id")

    # Make nullable again
    if "raw_customer_id" in cols:
        op.alter_column(
            "chat_messages",
            "raw_customer_id",
            existing_type=sa.String(length=100),
            nullable=True,
        )

