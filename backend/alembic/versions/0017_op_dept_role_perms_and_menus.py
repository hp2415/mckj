"""operation backend: dept role perms + menus

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0017"
down_revision: Union[str, Sequence[str], None] = "0016"
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

    # 部门 × 角色档权限（此前仅 operation_backend 启动时 ensure）
    if not _has_table(conn, "op_dept_role_perms"):
        op.create_table(
            "op_dept_role_perms",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("department_id", sa.Integer(), nullable=False),
            sa.Column("op_role", sa.String(length=20), nullable=False),
            sa.Column("perm_code", sa.String(length=64), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["department_id"], ["op_departments.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "department_id",
                "op_role",
                "perm_code",
                name="uq_op_dept_role_perms_dept_role_code",
            ),
        )
        op.create_index(
            "ix_op_dept_role_perms_department_id",
            "op_dept_role_perms",
            ["department_id"],
        )

    # 侧栏菜单树
    if not _has_table(conn, "op_menus"):
        op.create_table(
            "op_menus",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("parent_id", sa.Integer(), nullable=True),
            sa.Column("menu_type", sa.String(length=20), nullable=False),
            sa.Column("title", sa.String(length=100), nullable=False),
            sa.Column("path", sa.String(length=200), nullable=True),
            sa.Column("component", sa.String(length=200), nullable=True),
            sa.Column("icon", sa.String(length=80), nullable=True),
            sa.Column("perm_code", sa.String(length=64), nullable=True),
            sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
            sa.Column("is_enable", sa.Boolean(), server_default=sa.text("1"), nullable=False),
            sa.Column("is_hide", sa.Boolean(), server_default=sa.text("0"), nullable=False),
            sa.Column("link", sa.String(length=500), nullable=True),
            sa.Column("is_iframe", sa.Boolean(), server_default=sa.text("0"), nullable=False),
            sa.Column("keep_alive", sa.Boolean(), server_default=sa.text("1"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["parent_id"], ["op_menus.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_op_menus_parent_id", "op_menus", ["parent_id"])

    # 空表时灌入默认菜单（与 operation_backend seed 对齐；已有数据则跳过）
    if _has_table(conn, "op_menus"):
        cnt = conn.execute(sa.text("SELECT COUNT(*) FROM op_menus")).scalar()
        if int(cnt or 0) == 0:
            rows = [
                # id, parent_id, menu_type, title, path, component, icon, perm_code, sort_order
                (1, None, "directory", "运营", None, None, None, None, 10),
                (2, 1, "menu", "经营大屏", "/dashboard", "dashboard/DashboardView", "DataAnalysis", "usage.dashboard.view", 10),
                (3, 1, "menu", "活动管理", "/campaigns", "CampaignsView", "Present", "activity.campaign.view", 20),
                (4, 3, "button", "活动编辑", None, None, None, "activity.campaign.edit", 10),
                (5, 1, "menu", "人员明细", "/people", "PeopleView", "User", "usage.person.list", 30),
                (6, None, "directory", "组织", None, None, None, None, 20),
                (7, 6, "menu", "账号花名册", "/accounts", "AccountsView", "Notebook", "org.roster.view", 10),
                (8, 6, "menu", "邀请码", "/invites", "InvitesView", "Ticket", "org.invite.manage", 20),
                (9, 6, "menu", "部门树", "/org", "OrgView", "OfficeBuilding", "org.dept.manage", 30),
                (10, None, "directory", "系统", None, None, None, None, 30),
                (11, 10, "menu", "菜单管理", "/menus", "MenusView", "Menu", "system.menu.manage", 10),
                (12, 11, "button", "新增", None, None, None, "system.menu.manage", 10),
                (13, 11, "button", "编辑", None, None, None, "system.menu.manage", 20),
                (14, 11, "button", "删除", None, None, None, "system.menu.manage", 30),
            ]
            for (
                mid,
                parent_id,
                menu_type,
                title,
                path,
                component,
                icon,
                perm_code,
                sort_order,
            ) in rows:
                conn.execute(
                    sa.text(
                        "INSERT INTO op_menus "
                        "(id, parent_id, menu_type, title, path, component, icon, perm_code, "
                        "sort_order, is_enable, is_hide, link, is_iframe, keep_alive, "
                        "created_at, updated_at) VALUES "
                        "(:id, :parent_id, :menu_type, :title, :path, :component, :icon, "
                        ":perm_code, :sort_order, 1, 0, NULL, 0, 1, NOW(), NOW())"
                    ),
                    {
                        "id": mid,
                        "parent_id": parent_id,
                        "menu_type": menu_type,
                        "title": title,
                        "path": path,
                        "component": component,
                        "icon": icon,
                        "perm_code": perm_code,
                        "sort_order": sort_order,
                    },
                )


def downgrade() -> None:
    conn = op.get_bind()
    for table in ("op_menus", "op_dept_role_perms"):
        if _has_table(conn, table):
            op.drop_table(table)
