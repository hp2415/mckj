"""add raw_json to raw_chat_logs

Revision ID: u7v8w9x0y1z2
Revises: u1v2w3x4y5z6
Create Date: 2026-04-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "u7v8w9x0y1z2"
down_revision: Union[str, Sequence[str], None] = "u1v2w3x4y5z6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _colnames(conn, table: str) -> set[str]:
    rows = conn.execute(sa.text(f"SHOW COLUMNS FROM {table}")).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _colnames(conn, "raw_chat_logs")
    with op.batch_alter_table("raw_chat_logs") as b:
        if "raw_json" not in cols:
            b.add_column(sa.Column("raw_json", sa.Text(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    cols = _colnames(conn, "raw_chat_logs")
    with op.batch_alter_table("raw_chat_logs") as b:
        if "raw_json" in cols:
            b.drop_column("raw_json")

