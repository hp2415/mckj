"""drop legacy customer/order/wechat tables

Revision ID: s0e1f2g3h4i5
Revises: s0d1e2f3g4h5
Create Date: 2026-04-24

- Drop legacy tables after new per-sales profile model is in place:
  customers, user_customer_relations, ucr_profile_tags, orders, wechat_histories, prompt_rules
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "s0e1f2g3h4i5"
down_revision: Union[str, Sequence[str], None] = "s0d1e2f3g4h5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, name: str) -> bool:
    res = conn.execute(sa.text("SHOW TABLES LIKE :n"), {"n": name})
    return res.fetchone() is not None


def upgrade() -> None:
    conn = op.get_bind()

    # Drop join table first
    if _table_exists(conn, "ucr_profile_tags"):
        op.drop_table("ucr_profile_tags")

    if _table_exists(conn, "user_customer_relations"):
        op.drop_table("user_customer_relations")

    if _table_exists(conn, "orders"):
        op.drop_table("orders")

    if _table_exists(conn, "wechat_histories"):
        op.drop_table("wechat_histories")

    if _table_exists(conn, "prompt_rules"):
        op.drop_table("prompt_rules")

    if _table_exists(conn, "customers"):
        op.drop_table("customers")


def downgrade() -> None:
    # No automatic recreation: legacy schema is intentionally removed.
    pass

