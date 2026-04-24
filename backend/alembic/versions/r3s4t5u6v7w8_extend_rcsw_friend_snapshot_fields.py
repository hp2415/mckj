"""extend raw_customer_sales_wechats snapshot fields

Revision ID: r3s4t5u6v7w8
Revises: k7l8m9n0p1q2
Create Date: 2026-04-24

- Store per-(raw_customer_id, sales_wechat_id) friend snapshot fields (remark/label/etc),
  to prevent data loss when same customer appears under multiple sales accounts.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "r3s4t5u6v7w8"
down_revision: Union[str, Sequence[str], None] = "k7l8m9n0p1q2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("raw_customer_sales_wechats") as b:
        b.add_column(sa.Column("alias", sa.String(length=100), nullable=True))
        b.add_column(sa.Column("name", sa.String(length=100), nullable=True))
        b.add_column(sa.Column("remark", sa.String(length=100), nullable=True))
        b.add_column(sa.Column("phone", sa.String(length=100), nullable=True))
        b.add_column(sa.Column("label", sa.String(length=200), nullable=True))
        b.add_column(sa.Column("head_url", sa.String(length=500), nullable=True))
        b.add_column(sa.Column("description", sa.Text(), nullable=True))
        b.add_column(sa.Column("note_des", sa.Text(), nullable=True))
        b.add_column(sa.Column("gender", sa.String(length=10), nullable=True))
        b.add_column(sa.Column("region", sa.String(length=100), nullable=True))
        b.add_column(sa.Column("type", sa.Integer(), nullable=True))
        b.add_column(sa.Column("from_type", sa.String(length=50), nullable=True))

        b.add_column(sa.Column("create_time", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("last_chat_time", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("raw_customer_sales_wechats") as b:
        b.drop_column("last_chat_time")
        b.drop_column("create_time")
        b.drop_column("from_type")
        b.drop_column("type")
        b.drop_column("region")
        b.drop_column("gender")
        b.drop_column("note_des")
        b.drop_column("description")
        b.drop_column("head_url")
        b.drop_column("label")
        b.drop_column("phone")
        b.drop_column("remark")
        b.drop_column("name")
        b.drop_column("alias")

