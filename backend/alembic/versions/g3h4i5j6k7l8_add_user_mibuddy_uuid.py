"""users.mibuddy_uuid 米城主系统账号绑定

Revision ID: g3h4i5j6k7l8
Revises: f2g3h4i5j6k7
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g3h4i5j6k7l8"
down_revision: Union[str, Sequence[str], None] = "f2g3h4i5j6k7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = [r[0] for r in conn.execute(sa.text("SHOW COLUMNS FROM users")).fetchall()]
    if "mibuddy_uuid" not in cols:
        op.add_column(
            "users",
            sa.Column("mibuddy_uuid", sa.String(36), nullable=True),
        )
        op.create_index("ix_users_mibuddy_uuid", "users", ["mibuddy_uuid"], unique=True)


def downgrade() -> None:
    conn = op.get_bind()
    cols = [r[0] for r in conn.execute(sa.text("SHOW COLUMNS FROM users")).fetchall()]
    if "mibuddy_uuid" in cols:
        op.drop_index("ix_users_mibuddy_uuid", table_name="users")
        op.drop_column("users", "mibuddy_uuid")
