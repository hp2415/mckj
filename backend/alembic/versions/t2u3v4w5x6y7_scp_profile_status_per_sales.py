"""add per-sales profile_status to sales_customer_profiles

Revision ID: t2u3v4w5x6y7
Revises: s0e1f2g3h4i5
Create Date: 2026-04-24

- Add profile_status/profiled_at to SalesCustomerProfile (per raw_customer_id + sales_wechat_id)
- Backfill based on actual per-sales artifacts (ai_profile / tags), not raw_customers.profile_status
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "t2u3v4w5x6y7"
down_revision: Union[str, Sequence[str], None] = "s0e1f2g3h4i5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _colnames(conn, table: str) -> set[str]:
    rows = conn.execute(sa.text(f"SHOW COLUMNS FROM {table}")).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _colnames(conn, "sales_customer_profiles")

    with op.batch_alter_table("sales_customer_profiles") as b:
        if "profile_status" not in cols:
            b.add_column(
                sa.Column(
                    "profile_status",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
        if "profiled_at" not in cols:
            b.add_column(sa.Column("profiled_at", sa.DateTime(), nullable=True))

    # Backfill: mark as analyzed when per-sales artifacts exist (ai_profile text or tags)
    conn.execute(
        sa.text(
            """
            UPDATE sales_customer_profiles
            SET
              profile_status = 1,
              profiled_at = COALESCE(profiled_at, updated_at, created_at)
            WHERE
              (ai_profile IS NOT NULL AND TRIM(ai_profile) <> '')
              OR id IN (SELECT DISTINCT sales_customer_profile_id FROM scp_profile_tags)
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    cols = _colnames(conn, "sales_customer_profiles")
    with op.batch_alter_table("sales_customer_profiles") as b:
        if "profiled_at" in cols:
            b.drop_column("profiled_at")
        if "profile_status" in cols:
            b.drop_column("profile_status")

