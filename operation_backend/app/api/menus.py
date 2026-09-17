"""菜单管理 API。"""
from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import OpContext, get_current_op_user, require_perm
from app.core import menus as M
from app.core import permissions as P
from app.database import get_db
from app.models import OpMenu

router = APIRouter(prefix="/api/op/menus", tags=["op-menus"])


class MenuCreateIn(BaseModel):
    parent_id: int | None = None
    menu_type: str = Field(min_length=1, max_length=20)
    title: str = Field(min_length=1, max_length=100)
    path: str | None = Field(default=None, max_length=200)
    component: str | None = Field(default=None, max_length=200)
    icon: str | None = Field(default=None, max_length=80)
    perm_code: str | None = Field(default=None, max_length=64)
    sort_order: int = 0
    is_enable: bool = True
    is_hide: bool = False
    link: str | None = Field(default=None, max_length=500)
    is_iframe: bool = False
    keep_alive: bool = True


class MenuPatchIn(BaseModel):
    parent_id: int | None = None
    menu_type: str | None = Field(default=None, max_length=20)
    title: str | None = Field(default=None, min_length=1, max_length=100)
    path: str | None = None
    component: str | None = None
    icon: str | None = None
    perm_code: str | None = None
    sort_order: int | None = None
    is_enable: bool | None = None
    is_hide: bool | None = None
    link: str | None = None
    is_iframe: bool | None = None
    keep_alive: bool | None = None
    clear_parent: bool = False


def _validate_type(menu_type: str) -> str:
    t = (menu_type or "").strip().lower()
    if t not in M.MENU_TYPES:
        raise HTTPException(status_code=400, detail="菜单类型无效")
    return t


def _normalize_optional(val: str | None) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


async def _assert_parent(db: AsyncSession, parent_id: int | None) -> None:
    if parent_id is None:
        return
    parent = await db.get(OpMenu, int(parent_id))
    if not parent:
        raise HTTPException(status_code=400, detail="父菜单不存在")
    if parent.menu_type == M.MENU_BUTTON:
        raise HTTPException(status_code=400, detail="按钮下不可再挂子项")


async def _would_cycle(db: AsyncSession, menu_id: int, new_parent_id: int | None) -> bool:
    if new_parent_id is None:
        return False
    if int(new_parent_id) == int(menu_id):
        return True
    cur: int | None = int(new_parent_id)
    seen: set[int] = set()
    while cur and cur not in seen:
        if cur == int(menu_id):
            return True
        seen.add(cur)
        row = await db.get(OpMenu, cur)
        if not row or row.parent_id is None:
            break
        cur = int(row.parent_id)
    return False


@router.get("/nav")
async def menu_nav(
    ctx: OpContext = Depends(get_current_op_user),
    db: AsyncSession = Depends(get_db),
):
    """登录用户侧栏导航（按权限过滤）。"""
    rows = await M.load_all_menus(db)
    tree = M.filter_nav_tree(M.build_menu_tree(rows), ctx.permissions)
    return {"code": 200, "message": "ok", "data": tree}


@router.get("/tree")
async def menu_tree(
    ctx: OpContext = Depends(require_perm(P.PERM_SYSTEM_MENU_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    """管理端完整菜单树。"""
    rows = await M.load_all_menus(db)
    return {"code": 200, "message": "ok", "data": M.build_menu_tree(rows)}


@router.post("")
async def create_menu(
    body: MenuCreateIn,
    ctx: OpContext = Depends(require_perm(P.PERM_SYSTEM_MENU_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    menu_type = _validate_type(body.menu_type)
    await _assert_parent(db, body.parent_id)

    if menu_type == M.MENU_MENU and not (body.path or body.link):
        raise HTTPException(status_code=400, detail="菜单需填写路由或外链")
    if menu_type == M.MENU_BUTTON and not (body.perm_code or "").strip():
        raise HTTPException(status_code=400, detail="按钮需填写权限标识")

    now = datetime.datetime.now()
    row = OpMenu(
        parent_id=body.parent_id,
        menu_type=menu_type,
        title=body.title.strip(),
        path=_normalize_optional(body.path),
        component=_normalize_optional(body.component),
        icon=_normalize_optional(body.icon),
        perm_code=_normalize_optional(body.perm_code),
        sort_order=int(body.sort_order or 0),
        is_enable=bool(body.is_enable),
        is_hide=bool(body.is_hide),
        link=_normalize_optional(body.link),
        is_iframe=bool(body.is_iframe),
        keep_alive=bool(body.keep_alive),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"code": 200, "message": "ok", "data": M._row_to_dict(row)}


@router.put("/{menu_id}")
async def update_menu(
    menu_id: int,
    body: MenuPatchIn,
    ctx: OpContext = Depends(require_perm(P.PERM_SYSTEM_MENU_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(OpMenu, int(menu_id))
    if not row:
        raise HTTPException(status_code=404, detail="菜单不存在")

    data = body.model_dump(exclude_unset=True)
    clear_parent = bool(data.pop("clear_parent", False))

    if "menu_type" in data and data["menu_type"] is not None:
        row.menu_type = _validate_type(data["menu_type"])

    if clear_parent:
        row.parent_id = None
    elif "parent_id" in data:
        new_pid = data["parent_id"]
        await _assert_parent(db, new_pid)
        if await _would_cycle(db, int(menu_id), new_pid):
            raise HTTPException(status_code=400, detail="不能将菜单移动到自身子树下")
        row.parent_id = new_pid

    if "title" in data and data["title"] is not None:
        row.title = str(data["title"]).strip()

    for field in ("path", "component", "icon", "perm_code", "link"):
        if field in data:
            setattr(row, field, _normalize_optional(data[field]))

    if "sort_order" in data and data["sort_order"] is not None:
        row.sort_order = int(data["sort_order"])
    if "is_enable" in data and data["is_enable"] is not None:
        row.is_enable = bool(data["is_enable"])
    if "is_hide" in data and data["is_hide"] is not None:
        row.is_hide = bool(data["is_hide"])
    if "is_iframe" in data and data["is_iframe"] is not None:
        row.is_iframe = bool(data["is_iframe"])
    if "keep_alive" in data and data["keep_alive"] is not None:
        row.keep_alive = bool(data["keep_alive"])

    final_type = row.menu_type
    if final_type == M.MENU_MENU and not (row.path or row.link):
        raise HTTPException(status_code=400, detail="菜单需填写路由或外链")
    if final_type == M.MENU_BUTTON and not (row.perm_code or "").strip():
        raise HTTPException(status_code=400, detail="按钮需填写权限标识")

    row.updated_at = datetime.datetime.now()
    await db.commit()
    await db.refresh(row)
    return {"code": 200, "message": "ok", "data": M._row_to_dict(row)}


@router.delete("/{menu_id}")
async def delete_menu(
    menu_id: int,
    ctx: OpContext = Depends(require_perm(P.PERM_SYSTEM_MENU_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(OpMenu, int(menu_id))
    if not row:
        raise HTTPException(status_code=404, detail="菜单不存在")

    # 有子节点时一并删除（FK CASCADE）；先提示由前端确认
    child = (
        await db.execute(select(OpMenu.id).where(OpMenu.parent_id == int(menu_id)).limit(1))
    ).scalar_one_or_none()
    await db.delete(row)
    await db.commit()
    return {
        "code": 200,
        "message": "ok",
        "data": {"id": int(menu_id), "had_children": child is not None},
    }
