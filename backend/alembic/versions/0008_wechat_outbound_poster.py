"""wechat outbound actions: campaign poster send fields

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(conn, table: str, column: str) -> bool:
    row = conn.execute(
        sa.text("SHOW COLUMNS FROM `%s` LIKE :c" % table), {"c": column}
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "wechat_outbound_actions", "campaign_id"):
        op.add_column(
            "wechat_outbound_actions",
            sa.Column("campaign_id", sa.Integer(), nullable=True),
        )
        op.create_index(
            "ix_wechat_outbound_actions_campaign_id",
            "wechat_outbound_actions",
            ["campaign_id"],
        )
        op.create_foreign_key(
            "fk_wechat_outbound_actions_campaign_id",
            "wechat_outbound_actions",
            "campaigns",
            ["campaign_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _has_column(conn, "wechat_outbound_actions", "poster_id"):
        op.add_column(
            "wechat_outbound_actions",
            sa.Column("poster_id", sa.Integer(), nullable=True),
        )
        op.create_index(
            "ix_wechat_outbound_actions_poster_id",
            "wechat_outbound_actions",
            ["poster_id"],
        )
        op.create_foreign_key(
            "fk_wechat_outbound_actions_poster_id",
            "wechat_outbound_actions",
            "campaign_posters",
            ["poster_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, "wechat_outbound_actions", "poster_id"):
        op.drop_constraint(
            "fk_wechat_outbound_actions_poster_id",
            "wechat_outbound_actions",
            type_="foreignkey",
        )
        op.drop_index(
            "ix_wechat_outbound_actions_poster_id",
            table_name="wechat_outbound_actions",
        )
        op.drop_column("wechat_outbound_actions", "poster_id")
    if _has_column(conn, "wechat_outbound_actions", "campaign_id"):
        op.drop_constraint(
            "fk_wechat_outbound_actions_campaign_id",
            "wechat_outbound_actions",
            type_="foreignkey",
        )
        op.drop_index(
            "ix_wechat_outbound_actions_campaign_id",
            table_name="wechat_outbound_actions",
        )
        op.drop_column("wechat_outbound_actions", "campaign_id")
