from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import OpContext, require_perm
from app.core import permissions as P
from app.core.dept_tree import descendant_dept_ids, member_user_ids_in_depts
from app.core.business_stats import aggregate_overview
from app.core.usage_stats import aggregate_summary, person_timeline, shanghai_day_start
from app.database import get_db
from app.models import OpDepartment, OpDepartmentMember

router = APIRouter(prefix="/api/op", tags=["op-usage"])


def _is_admin_viewer(ctx: OpContext) -> bool:
    return bool(ctx.is_desktop_admin or ctx.op_role == P.OP_BOSS)


async def _resolve_summary_user_ids(
    db: AsyncSession,
    ctx: OpContext,
    *,
    dept_id: int | None,
    unassigned: bool,
) -> tuple[frozenset[int], dict]:
    """在可见范围内按部门收窄；返回 (user_ids, scope)。"""
    visible = set(ctx.visible_user_ids)
    is_admin = _is_admin_viewer(ctx)
    scope: dict = {
        "role": "admin" if is_admin else ctx.op_role,
        "dept_id": None,
        "dept_name": None,
        "include_descendants": True,
        "unassigned": False,
    }

    if unassigned:
        if not is_admin:
            raise HTTPException(status_code=403, detail="仅管理员可按未分配筛选")
        if dept_id is not None:
            raise HTTPException(status_code=400, detail="dept_id 与 unassigned 不能同时使用")
        assigned = {
            int(r[0])
            for r in (
                await db.execute(
                    select(OpDepartmentMember.user_id).where(
                        OpDepartmentMember.user_id.in_(list(visible) or [-1])
                    )
                )
            ).all()
        }
        ids = frozenset(uid for uid in visible if uid not in assigned)
        scope["unassigned"] = True
        scope["dept_name"] = "未分配"
        scope["include_descendants"] = False
        return ids, scope

    if dept_id is not None:
        if not is_admin:
            # 经理：只能下钻到自己可见子树内的部门
            if not ctx.dept_id:
                raise HTTPException(status_code=403, detail="无部门范围")
            allowed = set(await descendant_dept_ids(db, int(ctx.dept_id)))
            if int(dept_id) not in allowed:
                raise HTTPException(status_code=403, detail="部门不在可见范围")
        dept = await db.get(OpDepartment, int(dept_id))
        if not dept:
            raise HTTPException(status_code=404, detail="部门不存在")
        d_ids = await descendant_dept_ids(db, int(dept_id))
        u_ids = await member_user_ids_in_depts(db, d_ids)
        ids = frozenset(set(u_ids) & visible)
        scope["dept_id"] = int(dept_id)
        scope["dept_name"] = dept.name
        scope["include_descendants"] = True
        return ids, scope

    # 经理默认以本部门为 scope 展示名 / 树根
    if not is_admin and ctx.dept_id:
        dept = await db.get(OpDepartment, int(ctx.dept_id))
        scope["dept_id"] = int(ctx.dept_id)
        scope["dept_name"] = dept.name if dept else None
    return frozenset(visible), scope


def _tree_root_for(
    ctx: OpContext, *, dept_id: int | None, unassigned: bool
) -> int | None:
    if unassigned:
        return None
    if dept_id is not None:
        return int(dept_id)
    if not _is_admin_viewer(ctx) and ctx.dept_id:
        return int(ctx.dept_id)
    return None


@router.get("/dashboard/overview")
async def dashboard_overview(
    days: int = Query(7, ge=1, le=90),
    dept_id: int | None = Query(None, description="按部门含下级筛选"),
    unassigned: bool = Query(False, description="仅未分配部门人员"),
    gmv_root_dept_id: int | None = Query(
        None, description="部门成单柱图对比根（默认销售部）"
    ),
    ctx: OpContext = Depends(require_perm(P.PERM_USAGE_DASHBOARD)),
    db: AsyncSession = Depends(get_db),
):
    """经营分析大屏：成单 / 好友 / 外呼 + 精简使用率。"""
    user_ids, scope = await _resolve_summary_user_ids(
        db, ctx, dept_id=dept_id, unassigned=unassigned
    )
    tree_root = _tree_root_for(ctx, dept_id=dept_id, unassigned=unassigned)
    data = await aggregate_overview(
        db,
        visible_user_ids=user_ids,
        days=days,
        scope=scope,
        root_dept_id=tree_root,
        gmv_root_dept_id=gmv_root_dept_id,
        unassigned_only=unassigned,
        is_admin=_is_admin_viewer(ctx),
    )
    return {"code": 200, "message": "ok", "data": data}


@router.get("/dashboard/summary")
async def dashboard_summary(
    days: int = Query(7, ge=1, le=90),
    dept_id: int | None = Query(None, description="按部门含下级筛选"),
    unassigned: bool = Query(False, description="仅未分配部门人员"),
    ctx: OpContext = Depends(require_perm(P.PERM_USAGE_DASHBOARD)),
    db: AsyncSession = Depends(get_db),
):
    user_ids, scope = await _resolve_summary_user_ids(
        db, ctx, dept_id=dept_id, unassigned=unassigned
    )
    # 管理员按筛选根；经理固定本部门为树根（前端不展示层级表，但子部门对比用）
    tree_root = _tree_root_for(ctx, dept_id=dept_id, unassigned=unassigned)

    data = await aggregate_summary(
        db,
        visible_user_ids=user_ids,
        days=days,
        scope=scope,
        root_dept_id=tree_root,
        unassigned_only=unassigned,
    )
    return {"code": 200, "message": "ok", "data": data}


@router.get("/people")
async def people_list(
    days: int = Query(7, ge=1, le=90),
    ctx: OpContext = Depends(require_perm(P.PERM_USAGE_PERSON_LIST)),
    db: AsyncSession = Depends(get_db),
):
    data = await aggregate_summary(
        db, visible_user_ids=ctx.visible_user_ids, days=days
    )
    return {
        "code": 200,
        "message": "ok",
        "data": {"days": days, "items": data.get("people") or []},
    }


def _parse_day(value: date | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    return datetime(value.year, value.month, value.day)


@router.get("/people/{user_id}")
async def people_detail(
    user_id: int,
    days: int = Query(7, ge=1, le=90),
    date_from: date | None = Query(None, description="动态起始日 YYYY-MM-DD"),
    date_to: date | None = Query(None, description="动态结束日 YYYY-MM-DD"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    ctx: OpContext = Depends(require_perm(P.PERM_USAGE_PERSON_DETAIL)),
    db: AsyncSession = Depends(get_db),
):
    ctx.ensure_visible(user_id)
    today = shanghai_day_start(0)
    # 默认当日；若只传一端则对齐为单日
    if date_from is None and date_to is None:
        d0 = today
        d1 = today
    else:
        d0 = _parse_day(date_from, today)
        d1 = _parse_day(date_to, d0)
        if d1 < d0:
            d0, d1 = d1, d0
        # 防止过大范围
        if (d1 - d0).days > 90:
            d0 = d1 - timedelta(days=90)

    timeline = await person_timeline(
        db,
        user_id=user_id,
        date_from=d0,
        date_to=d1,
        page=page,
        page_size=page_size,
    )
    summary = await aggregate_summary(
        db, visible_user_ids=frozenset({user_id}), days=days
    )
    person = (summary.get("people") or [{}])[0]
    return {
        "code": 200,
        "message": "ok",
        "data": {"person": person, "timeline": timeline},
    }
