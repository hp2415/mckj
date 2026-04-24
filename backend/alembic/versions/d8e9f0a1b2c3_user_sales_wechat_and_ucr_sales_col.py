"""user_sales_wechats + user_customer_relations.sales_wechat_id

Revision ID: d8e9f0a1b2c3
Revises: c5e7f2a9b103
Create Date: 2026-04-24

- 新增 user_sales_wechats：用户与销售微信号绑定
- user_customer_relations 增加 sales_wechat_id 与 (customer_id, sales_wechat_id) 唯一约束
- 从 users.wechat_id 回填绑定表；回填关系表 sales_wechat_id
- 去除重复 (customer_id, sales_wechat_id) 行后建唯一索引
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, Sequence[str], None] = "c5e7f2a9b103"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, name: str) -> bool:
    res = conn.execute(sa.text("SHOW TABLES LIKE :n"), {"n": name})
    return res.fetchone() is not None


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, "user_sales_wechats"):
        op.create_table(
            "user_sales_wechats",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("sales_wechat_id", sa.String(100), nullable=False),
            sa.Column("label", sa.String(100), nullable=True),
            sa.Column("is_primary", sa.Boolean(), server_default="0", nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("verified_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("sales_wechat_id", name="uq_user_sales_wechats_sales_wechat_id"),
        )
        op.create_index("ix_user_sales_wechats_user_id", "user_sales_wechats", ["user_id"])

    # 回填绑定：每个已有 wechat_id 的用户一条主号绑定
    conn.execute(
        sa.text(
            """
            INSERT IGNORE INTO user_sales_wechats
              (user_id, sales_wechat_id, label, is_primary, created_at)
            SELECT id, wechat_id, '迁移自 users.wechat_id', 1, NOW()
            FROM users
            WHERE wechat_id IS NOT NULL AND TRIM(wechat_id) != ''
            """
        )
    )

    # 关系表增加列
    cols = [r[0] for r in conn.execute(sa.text("SHOW COLUMNS FROM user_customer_relations")).fetchall()]
    if "sales_wechat_id" not in cols:
        op.add_column(
            "user_customer_relations",
            sa.Column("sales_wechat_id", sa.String(100), nullable=True),
        )
        op.create_index("ix_user_customer_relations_sales_wechat_id", "user_customer_relations", ["sales_wechat_id"])

    conn.execute(
        sa.text(
            """
            UPDATE user_customer_relations ucr
            INNER JOIN users u ON u.id = ucr.user_id
            SET ucr.sales_wechat_id = u.wechat_id
            WHERE u.wechat_id IS NOT NULL AND TRIM(u.wechat_id) != ''
              AND (ucr.sales_wechat_id IS NULL OR ucr.sales_wechat_id = '')
            """
        )
    )

    # 去除违反唯一约束的重复行（保留最小 id）
    conn.execute(
        sa.text(
            """
            DELETE ucr1 FROM user_customer_relations ucr1
            INNER JOIN user_customer_relations ucr2
              ON ucr1.customer_id = ucr2.customer_id
              AND ucr1.sales_wechat_id <=> ucr2.sales_wechat_id
              AND ucr1.id > ucr2.id
            WHERE ucr1.sales_wechat_id IS NOT NULL
            """
        )
    )

    # 若仍存在 (customer_id, NULL) 重复，仅保留最小 id
    conn.execute(
        sa.text(
            """
            DELETE ucr1 FROM user_customer_relations ucr1
            INNER JOIN user_customer_relations ucr2
              ON ucr1.customer_id = ucr2.customer_id
              AND ucr1.sales_wechat_id IS NULL
              AND ucr2.sales_wechat_id IS NULL
              AND ucr1.id > ucr2.id
            """
        )
    )

    existing = conn.execute(
        sa.text(
            """
            SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'user_customer_relations'
              AND CONSTRAINT_NAME = 'uq_ucr_customer_sales_wechat'
            """
        )
    ).fetchone()
    if not existing:
        op.create_unique_constraint(
            "uq_ucr_customer_sales_wechat",
            "user_customer_relations",
            ["customer_id", "sales_wechat_id"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    existing = conn.execute(
        sa.text(
            """
            SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'user_customer_relations'
              AND CONSTRAINT_NAME = 'uq_ucr_customer_sales_wechat'
            """
        )
    ).fetchone()
    if existing:
        op.drop_constraint("uq_ucr_customer_sales_wechat", "user_customer_relations", type_="unique")

    cols = [r[0] for r in conn.execute(sa.text("SHOW COLUMNS FROM user_customer_relations")).fetchall()]
    if "sales_wechat_id" in cols:
        try:
            op.drop_index("ix_user_customer_relations_sales_wechat_id", "user_customer_relations")
        except Exception:
            pass
        op.drop_column("user_customer_relations", "sales_wechat_id")

    if _table_exists(conn, "user_sales_wechats"):
        op.drop_table("user_sales_wechats")
