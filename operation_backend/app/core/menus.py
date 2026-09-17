"""运营侧边栏菜单：建表、种子、树构建。"""
from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions as P

MENU_DIRECTORY = "directory"
MENU_MENU = "menu"
MENU_BUTTON = "button"

MENU_TYPES = frozenset({MENU_DIRECTORY, MENU_MENU, MENU_BUTTON})

# 空表时灌入的默认菜单（与现有路由 / 权限对齐）
_DEFAULT_MENUS: list[dict[str, Any]] = [
    {
        "title": "运营",
        "menu_type": MENU_DIRECTORY,
        "icon": "",
        "sort_order": 10,
        "children": [
            {
                "title": "经营大屏",
                "menu_type": MENU_MENU,
                "path": "/dashboard",
                "component": "dashboard/DashboardView",
                "icon": "DataAnalysis",
                "perm_code": P.PERM_USAGE_DASHBOARD,
                "sort_order": 10,
            },
            {
                "title": "活动管理",
                "menu_type": MENU_MENU,
                "path": "/campaigns",
                "component": "CampaignsView",
                "icon": "Present",
                "perm_code": P.PERM_ACTIVITY_VIEW,
                "sort_order": 20,
                "children": [
                    {
                        "title": "活动编辑",
                        "menu_type": MENU_BUTTON,
                        "perm_code": P.PERM_ACTIVITY_EDIT,
                        "sort_order": 10,
                    },
                ],
            },
            {
                "title": "人员明细",
                "menu_type": MENU_MENU,
                "path": "/people",
                "component": "PeopleView",
                "icon": "User",
                "perm_code": P.PERM_USAGE_PERSON_LIST,
                "sort_order": 30,
            },
            {
                "title": "商品规格",
                "menu_type": MENU_MENU,
                "path": "/products",
                "component": "ProductsView",
                "icon": "Goods",
                "perm_code": P.PERM_PRODUCT_SPEC_VIEW,
                "sort_order": 40,
                "children": [
                    {
                        "title": "规格编辑",
                        "menu_type": MENU_BUTTON,
                        "perm_code": P.PERM_PRODUCT_SPEC_EDIT,
                        "sort_order": 10,
                    },
                ],
            },
        ],
    },
    {
        "title": "组织",
        "menu_type": MENU_DIRECTORY,
        "icon": "",
        "sort_order": 20,
        "children": [
            {
                "title": "账号花名册",
                "menu_type": MENU_MENU,
                "path": "/accounts",
                "component": "AccountsView",
                "icon": "Notebook",
                "perm_code": P.PERM_ORG_ROSTER,
                "sort_order": 10,
            },
            {
                "title": "邀请码",
                "menu_type": MENU_MENU,
                "path": "/invites",
                "component": "InvitesView",
                "icon": "Ticket",
                "perm_code": P.PERM_ORG_INVITE,
                "sort_order": 20,
            },
            {
                "title": "部门树",
                "menu_type": MENU_MENU,
                "path": "/org",
                "component": "OrgView",
                "icon": "OfficeBuilding",
                "perm_code": P.PERM_ORG_DEPT_MANAGE,
                "sort_order": 30,
            },
        ],
    },
    {
        "title": "系统",
        "menu_type": MENU_DIRECTORY,
        "icon": "",
        "sort_order": 30,
        "children": [
            {
                "title": "菜单管理",
                "menu_type": MENU_MENU,
                "path": "/menus",
                "component": "MenusView",
                "icon": "Menu",
                "perm_code": P.PERM_SYSTEM_MENU_MANAGE,
                "sort_order": 10,
                "children": [
                    {
                        "title": "新增",
                        "menu_type": MENU_BUTTON,
                        "perm_code": P.PERM_SYSTEM_MENU_MANAGE,
                        "sort_order": 10,
                    },
                    {
                        "title": "编辑",
                        "menu_type": MENU_BUTTON,
                        "perm_code": P.PERM_SYSTEM_MENU_MANAGE,
                        "sort_order": 20,
                    },
                    {
                        "title": "删除",
                        "menu_type": MENU_BUTTON,
                        "perm_code": P.PERM_SYSTEM_MENU_MANAGE,
                        "sort_order": 30,
                    },
                ],
            },
        ],
    },
]


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "parent_id": int(row.parent_id) if row.parent_id is not None else None,
        "menu_type": row.menu_type,
        "title": row.title,
        "path": row.path or "",
        "component": row.component or "",
        "icon": row.icon or "",
        "perm_code": row.perm_code or "",
        "sort_order": int(row.sort_order or 0),
        "is_enable": bool(row.is_enable),
        "is_hide": bool(row.is_hide),
        "link": row.link or "",
        "is_iframe": bool(row.is_iframe),
        "keep_alive": bool(row.keep_alive),
        "updated_at": row.updated_at.isoformat(sep=" ", timespec="seconds")
        if row.updated_at
        else None,
        "children": [],
    }


def build_menu_tree(rows: list[Any]) -> list[dict[str, Any]]:
    nodes = {int(r.id): _row_to_dict(r) for r in rows}
    roots: list[dict[str, Any]] = []
    for r in rows:
        node = nodes[int(r.id)]
        pid = int(r.parent_id) if r.parent_id is not None else None
        if pid is not None and pid in nodes:
            nodes[pid]["children"].append(node)
        else:
            roots.append(node)

    def _sort(items: list[dict[str, Any]]) -> None:
        items.sort(key=lambda x: (x["sort_order"], x["id"]))
        for it in items:
            _sort(it["children"])

    _sort(roots)
    return roots


def filter_nav_tree(
    tree: list[dict[str, Any]], permissions: set[str]
) -> list[dict[str, Any]]:
    """侧栏导航：仅启用、未隐藏；目录需有可见子项；按钮不展示。"""

    def _walk(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for it in items:
            if not it.get("is_enable") or it.get("is_hide"):
                continue
            mtype = it.get("menu_type")
            if mtype == MENU_BUTTON:
                continue
            children = _walk(it.get("children") or [])
            if mtype == MENU_DIRECTORY:
                if not children:
                    continue
                out.append({**it, "children": children})
                continue
            # menu
            code = (it.get("perm_code") or "").strip()
            if code and code not in permissions:
                continue
            # 侧栏不带按钮子节点
            out.append({**it, "children": []})
        return out

    return _walk(tree)


def build_perm_assign_tree(
    tree: list[dict[str, Any]],
    *,
    blocked_codes: set[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    """供部门权限勾选：启用菜单树（过滤超管码），返回 (tree, menu_perm_codes)。"""

    used_codes: set[str] = set()

    def _walk(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for it in items:
            if not it.get("is_enable"):
                continue
            code = (it.get("perm_code") or "").strip()
            if code and code in blocked_codes:
                continue
            children = _walk(it.get("children") or [])
            # 目录无权限码且无子项 → 跳过
            if it.get("menu_type") == MENU_DIRECTORY and not children and not code:
                continue
            # 无权限码的空叶（异常数据）跳过
            if not code and not children and it.get("menu_type") != MENU_DIRECTORY:
                continue
            if code:
                used_codes.add(code)
            node = {
                "key": f"m:{it['id']}",
                "id": it["id"],
                "title": it["title"],
                "menu_type": it["menu_type"],
                "perm_code": code or None,
                "path": it.get("path") or "",
                "children": children,
            }
            out.append(node)
        return out

    return _walk(tree), used_codes


async def ensure_menus_table(db: AsyncSession) -> None:
    await db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS op_menus (
              id INT NOT NULL AUTO_INCREMENT,
              parent_id INT NULL,
              menu_type VARCHAR(20) NOT NULL,
              title VARCHAR(100) NOT NULL,
              path VARCHAR(200) NULL,
              component VARCHAR(200) NULL,
              icon VARCHAR(80) NULL,
              perm_code VARCHAR(64) NULL,
              sort_order INT NOT NULL DEFAULT 0,
              is_enable TINYINT(1) NOT NULL DEFAULT 1,
              is_hide TINYINT(1) NOT NULL DEFAULT 0,
              link VARCHAR(500) NULL,
              is_iframe TINYINT(1) NOT NULL DEFAULT 0,
              keep_alive TINYINT(1) NOT NULL DEFAULT 1,
              created_at DATETIME NOT NULL,
              updated_at DATETIME NOT NULL,
              PRIMARY KEY (id),
              KEY ix_op_menus_parent_id (parent_id),
              CONSTRAINT fk_op_menus_parent
                FOREIGN KEY (parent_id) REFERENCES op_menus (id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
    )
    await db.commit()


async def seed_default_menus(db: AsyncSession) -> int:
    """表为空时灌入默认菜单树，返回写入条数。"""
    from app.models import OpMenu

    count = (await db.execute(select(OpMenu.id).limit(1))).scalar_one_or_none()
    if count is not None:
        return 0

    now = datetime.datetime.now()
    inserted = 0

    async def _insert(node: dict[str, Any], parent_id: int | None) -> None:
        nonlocal inserted
        row = OpMenu(
            parent_id=parent_id,
            menu_type=node["menu_type"],
            title=node["title"],
            path=node.get("path") or None,
            component=node.get("component") or None,
            icon=node.get("icon") or None,
            perm_code=node.get("perm_code") or None,
            sort_order=int(node.get("sort_order") or 0),
            is_enable=True,
            is_hide=False,
            link=node.get("link") or None,
            is_iframe=bool(node.get("is_iframe") or False),
            keep_alive=bool(node.get("keep_alive", True)),
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        await db.flush()
        inserted += 1
        for child in node.get("children") or []:
            await _insert(child, int(row.id))

    for root in _DEFAULT_MENUS:
        await _insert(root, None)
    await db.commit()
    return inserted


async def ensure_product_menu(db: AsyncSession) -> int:
    """现网已有菜单时幂等补种「商品规格」页，返回新增条数。"""
    from app.models import OpMenu

    existing = (
        await db.execute(select(OpMenu.id).where(OpMenu.path == "/products").limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        return 0

    ops_dir = (
        await db.execute(
            select(OpMenu)
            .where(OpMenu.menu_type == MENU_DIRECTORY, OpMenu.title == "运营")
            .order_by(OpMenu.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if ops_dir is None:
        return 0

    now = datetime.datetime.now()
    menu = OpMenu(
        parent_id=int(ops_dir.id),
        menu_type=MENU_MENU,
        title="商品规格",
        path="/products",
        component="ProductsView",
        icon="Goods",
        perm_code=P.PERM_PRODUCT_SPEC_VIEW,
        sort_order=40,
        is_enable=True,
        is_hide=False,
        link=None,
        is_iframe=False,
        keep_alive=True,
        created_at=now,
        updated_at=now,
    )
    db.add(menu)
    await db.flush()
    btn = OpMenu(
        parent_id=int(menu.id),
        menu_type=MENU_BUTTON,
        title="规格编辑",
        path=None,
        component=None,
        icon=None,
        perm_code=P.PERM_PRODUCT_SPEC_EDIT,
        sort_order=10,
        is_enable=True,
        is_hide=False,
        link=None,
        is_iframe=False,
        keep_alive=True,
        created_at=now,
        updated_at=now,
    )
    db.add(btn)
    await db.commit()
    return 2


async def load_all_menus(db: AsyncSession) -> list[Any]:
    from app.models import OpMenu

    return list(
        (
            await db.execute(
                select(OpMenu).order_by(OpMenu.sort_order, OpMenu.id)
            )
        )
        .scalars()
        .all()
    )
