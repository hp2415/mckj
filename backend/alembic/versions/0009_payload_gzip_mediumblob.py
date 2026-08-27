"""task_allocation_input_snapshots.payload_gzip BLOB -> MEDIUMBLOB

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(conn, name: str) -> bool:
    row = conn.execute(sa.text("SHOW TABLES LIKE :t"), {"t": name}).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "task_allocation_input_snapshots"):
        return
    op.execute(
        sa.text(
            "ALTER TABLE task_allocation_input_snapshots "
            "MODIFY payload_gzip MEDIUMBLOB NOT NULL"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "task_allocation_input_snapshots"):
        return
    op.execute(
        sa.text(
            "ALTER TABLE task_allocation_input_snapshots "
            "MODIFY payload_gzip BLOB NOT NULL"
        )
    )
