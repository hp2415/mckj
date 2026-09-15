from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import OpContext, require_perm
from app.core import permissions as P
from app.core.usage_stats import aggregate_summary, person_timeline, shanghai_day_start
from app.database import get_db

router = APIRouter(prefix="/api/op", tags=["op-usage"])


@router.get("/dashboard/summary")
async def dashboard_summary(
    days: int = Query(7, ge=1, le=90),
    ctx: OpContext = Depends(require_perm(P.PERM_USAGE_DASHBOARD)),
    db: AsyncSession = Depends(get_db),
):
    data = await aggregate_summary(
        db, visible_user_ids=ctx.visible_user_ids, days=days
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
