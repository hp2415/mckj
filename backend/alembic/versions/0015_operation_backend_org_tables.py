"""operation backend: departments, invites, activity events, audit

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0015"
down_revision: Union[str, Sequence[str], None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(conn, table: str) -> bool:
    row = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t LIMIT 1"
        ),
        {"t": table},
    ).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, "op_departments"):
        op.create_table(
            "op_departments",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("parent_id", sa.Integer(), nullable=True),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("kind", sa.String(length=20), nullable=True),
            sa.Column("leader_user_id", sa.Integer(), nullable=True),
            sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["parent_id"], ["op_departments.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["leader_user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_op_departments_parent_id", "op_departments", ["parent_id"])
        op.create_index("ix_op_departments_leader_user_id", "op_departments", ["leader_user_id"])

    if not _has_table(conn, "op_department_closure"):
        op.create_table(
            "op_department_closure",
            sa.Column("ancestor_id", sa.Integer(), nullable=False),
            sa.Column("descendant_id", sa.Integer(), nullable=False),
            sa.Column("depth", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["ancestor_id"], ["op_departments.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["descendant_id"], ["op_departments.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("ancestor_id", "descendant_id"),
        )
        op.create_index(
            "ix_op_dept_closure_descendant",
            "op_department_closure",
            ["descendant_id"],
        )

    if not _has_table(conn, "op_department_members"):
        op.create_table(
            "op_department_members",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("department_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("joined_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["department_id"], ["op_departments.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", name="uq_op_department_members_user_id"),
        )
        op.create_index(
            "ix_op_department_members_department_id",
            "op_department_members",
            ["department_id"],
        )

    if not _has_table(conn, "op_user_profiles"):
        op.create_table(
            "op_user_profiles",
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("op_role", sa.String(length=20), server_default="none", nullable=False),
            sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
            sa.Column("created_via", sa.String(length=30), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("user_id"),
        )

    if not _has_table(conn, "op_invite_codes"):
        op.create_table(
            "op_invite_codes",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("code", sa.String(length=16), nullable=False),
            sa.Column("created_by_user_id", sa.Integer(), nullable=True),
            sa.Column("department_id", sa.Integer(), nullable=False),
            sa.Column("grant_op_role", sa.String(length=20), server_default="none", nullable=False),
            sa.Column("max_uses", sa.Integer(), server_default="1", nullable=False),
            sa.Column("used_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("note", sa.String(length=200), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["department_id"], ["op_departments.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code", name="uq_op_invite_codes_code"),
        )
        op.create_index("ix_op_invite_codes_department_id", "op_invite_codes", ["department_id"])

    if not _has_table(conn, "op_invite_redemptions"):
        op.create_table(
            "op_invite_redemptions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("invite_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("redeemed_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["invite_id"], ["op_invite_codes.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", name="uq_op_invite_redemptions_user_id"),
        )
        op.create_index("ix_op_invite_redemptions_invite_id", "op_invite_redemptions", ["invite_id"])

    if not _has_table(conn, "user_activity_events"):
        op.create_table(
            "user_activity_events",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("event_type", sa.String(length=40), nullable=False),
            sa.Column("source", sa.String(length=20), nullable=False),
            sa.Column("sales_wechat_id", sa.String(length=100), nullable=True),
            sa.Column("raw_customer_id", sa.String(length=100), nullable=True),
            sa.Column("object_type", sa.String(length=40), nullable=True),
            sa.Column("object_id", sa.String(length=100), nullable=True),
            sa.Column("extra_json", sa.JSON(), nullable=True),
            sa.Column("client_session_id", sa.String(length=64), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_user_activity_events_user_occurred",
            "user_activity_events",
            ["user_id", "occurred_at"],
        )
        op.create_index(
            "ix_user_activity_events_type_occurred",
            "user_activity_events",
            ["event_type", "occurred_at"],
        )
        op.create_index(
            "ix_user_activity_events_occurred_at",
            "user_activity_events",
            ["occurred_at"],
        )

    if not _has_table(conn, "op_audit_logs"):
        op.create_table(
            "op_audit_logs",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
            sa.Column("actor_user_id", sa.Integer(), nullable=True),
            sa.Column("action", sa.String(length=60), nullable=False),
            sa.Column("target_type", sa.String(length=40), nullable=True),
            sa.Column("target_id", sa.String(length=64), nullable=True),
            sa.Column("detail_json", sa.JSON(), nullable=True),
            sa.Column("ip", sa.String(length=64), nullable=True),
            sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_op_audit_logs_occurred_at",
            "op_audit_logs",
            ["occurred_at"],
        )
        op.create_index(
            "ix_op_audit_logs_actor_occurred",
            "op_audit_logs",
            ["actor_user_id", "occurred_at"],
        )

    # Seed root org tree if empty
    if _has_table(conn, "op_departments"):
        cnt = conn.execute(sa.text("SELECT COUNT(*) FROM op_departments")).scalar()
        if int(cnt or 0) == 0:
            now = sa.text("NOW()")
            conn.execute(
                sa.text(
                    "INSERT INTO op_departments "
                    "(id, parent_id, name, kind, leader_user_id, sort_order, is_active, created_at, updated_at) "
                    "VALUES "
                    "(1, NULL, '公司', NULL, NULL, 0, 1, NOW(), NOW()), "
                    "(2, 1, '销售部', 'sales', NULL, 10, 1, NOW(), NOW()), "
                    "(3, 1, '财务部', 'finance', NULL, 20, 1, NOW(), NOW()), "
                    "(4, 1, '供应链', 'supply', NULL, 30, 1, NOW(), NOW()), "
                    "(5, 1, '人事部', 'hr', NULL, 40, 1, NOW(), NOW())"
                )
            )
            # Self + parent closure rows
            rows = [
                (1, 1, 0),
                (2, 2, 0),
                (3, 3, 0),
                (4, 4, 0),
                (5, 5, 0),
                (1, 2, 1),
                (1, 3, 1),
                (1, 4, 1),
                (1, 5, 1),
            ]
            for a, d, depth in rows:
                conn.execute(
                    sa.text(
                        "INSERT INTO op_department_closure (ancestor_id, descendant_id, depth) "
                        "VALUES (:a, :d, :depth)"
                    ),
                    {"a": a, "d": d, "depth": depth},
                )


def downgrade() -> None:
    conn = op.get_bind()
    for table in (
        "op_audit_logs",
        "user_activity_events",
        "op_invite_redemptions",
        "op_invite_codes",
        "op_user_profiles",
        "op_department_members",
        "op_department_closure",
        "op_departments",
    ):
        if _has_table(conn, table):
            op.drop_table(table)
