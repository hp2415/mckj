"""销售联系任务 API（桌面端 + 管理端 JSON）。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case
from sqlalchemy.future import select

import schemas
from ai.task_allocation import (
    PERIOD_DAILY,
    PERIOD_MONTHLY,
    PERIOD_WEEKLY,
    period_bounds,
    today_shanghai,
)
from api.auth import get_current_user
from crud import get_user_bound_sales_wechat_ids
from database import get_db
from models import (
    ContactTask,
    RawCustomer,
    SalesCustomerProfile,
    TaskAllocationBatch,
    User,
)

router = APIRouter(prefix="/api/tasks", tags=["Tasks"])


async def _resolve_sales_wechat_id(
    db,
    user: User,
    sales_wechat_id: Optional[str],
) -> str:
    sw = (sales_wechat_id or "").strip()
    bound = await get_user_bound_sales_wechat_ids(db, user.id)
    if not bound:
        raise HTTPException(status_code=400, detail="当前账号未绑定销售微信号")
    if not sw:
        if len(bound) == 1:
            return bound[0]
        raise HTTPException(status_code=400, detail="请指定 sales_wechat_id")
    if sw not in bound:
        raise HTTPException(status_code=403, detail="无权访问该销售微信号下的任务")
    return sw


def _task_to_out(task: ContactTask, scp: SalesCustomerProfile | None, rc: RawCustomer | None) -> dict:
    return {
        "id": task.id,
        "batch_id": task.batch_id,
        "raw_customer_id": task.raw_customer_id,
        "sales_wechat_id": task.sales_wechat_id,
        "period_type": task.period_type,
        "due_date": task.due_date,
        "task_kind": task.task_kind,
        "priority_rank": task.priority_rank,
        "priority_score": float(task.priority_score) if task.priority_score is not None else None,
        "title": task.title,
        "instruction": task.instruction,
        "status": task.status,
        "completed_at": task.completed_at,
        "completion_note": task.completion_note,
        "customer_name": (rc.customer_name if rc else None) or None,
        "unit_name": (rc.unit_name if rc else None) or None,
        "wechat_remark": (scp.wechat_remark if scp else None) or None,
        "ai_profile": (scp.ai_profile if scp else None) or None,
        "suggested_followup_date": scp.suggested_followup_date if scp else None,
    }


async def _load_tasks_with_customer(
    db,
    *,
    sales_wechat_id: str,
    period_type: str,
    period_start: date,
    status: Optional[str] = None,
) -> tuple[TaskAllocationBatch | None, list[dict]]:
    # 桌面端选批策略：
    #   1) 优先返回当期 published 批次（管理员审核发布过的版本）；
    #   2) 若该周期还没有 published 批次（例如日任务草稿刚生成、管理员还没点"发布"），
    #      则回落到最新的 draft，让销售立刻看到任务，避免空白页。
    #   不返回 archived / canceled 批次。
    batch_res = await db.execute(
        select(TaskAllocationBatch)
        .where(TaskAllocationBatch.sales_wechat_id == sales_wechat_id)
        .where(TaskAllocationBatch.period_type == period_type)
        .where(TaskAllocationBatch.period_start == period_start)
        .where(TaskAllocationBatch.status.in_(("published", "draft")))
        .order_by(
            case((TaskAllocationBatch.status == "published", 0), else_=1),
            TaskAllocationBatch.id.desc(),
        )
        .limit(1)
    )
    batch = batch_res.scalars().first()
    if not batch:
        return None, []

    stmt = (
        select(ContactTask, SalesCustomerProfile, RawCustomer)
        .outerjoin(SalesCustomerProfile, SalesCustomerProfile.id == ContactTask.scp_id)
        .outerjoin(RawCustomer, RawCustomer.id == ContactTask.raw_customer_id)
        .where(ContactTask.batch_id == batch.id)
        .order_by(ContactTask.priority_rank.asc(), ContactTask.id.asc())
    )
    if status:
        stmt = stmt.where(ContactTask.status == status)
    rows = (await db.execute(stmt)).all()
    items = [_task_to_out(t, scp, rc) for t, scp, rc in rows]
    return batch, items


def _stats_from_items(items: list[dict]) -> schemas.TaskPeriodStatsOut:
    counts: dict[str, int] = {}
    for it in items:
        st = str(it.get("status") or "pending")
        counts[st] = counts.get(st, 0) + 1
    total = sum(counts.values())
    done = counts.get("done", 0)
    skipped = counts.get("skipped", 0)
    denom = max(1, total - skipped)
    return schemas.TaskPeriodStatsOut(
        total=total,
        done=done,
        pending=counts.get("pending", 0),
        in_progress=counts.get("in_progress", 0),
        skipped=skipped,
        overdue=counts.get("overdue", 0),
        completion_rate=round(done / denom, 4),
    )


@router.get("/overview")
async def task_overview(
    period: str = Query(PERIOD_DAILY, description="daily|weekly|monthly"),
    date_str: Optional[str] = Query(None, alias="date"),
    sales_wechat_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if period not in (PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY):
        return {"code": 400, "message": "period 须为 daily|weekly|monthly", "data": None}
    ref = today_shanghai()
    if date_str:
        try:
            ref = date.fromisoformat(date_str[:10])
        except ValueError:
            return {"code": 400, "message": "date 格式须为 YYYY-MM-DD", "data": None}
    sw = await _resolve_sales_wechat_id(db, current_user, sales_wechat_id)
    p_start, p_end = period_bounds(period, ref)
    batch, items = await _load_tasks_with_customer(
        db, sales_wechat_id=sw, period_type=period, period_start=p_start, status=status
    )
    stats = _stats_from_items(items)
    data = schemas.TaskOverviewOut(
        period_type=period,
        period_start=p_start,
        period_end=p_end,
        batch_id=batch.id if batch else None,
        batch_status=batch.status if batch else None,
        stats=stats,
        items=[schemas.ContactTaskOut(**it) for it in items],
    )
    return {"code": 200, "message": "ok", "data": data}


@router.get("/list")
async def task_list(
    period: str = Query(PERIOD_DAILY),
    date_str: Optional[str] = Query(None, alias="date"),
    sales_wechat_id: Optional[str] = Query(None),
    status: Optional[str] = Query("pending"),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    resp = await task_overview(
        period=period,
        date_str=date_str,
        sales_wechat_id=sales_wechat_id,
        status=status,
        db=db,
        current_user=current_user,
    )
    if resp.get("code") != 200:
        return resp
    data = resp["data"]
    return {"code": 200, "message": "ok", "data": data.items}


@router.get("/calendar")
async def task_calendar(
    month: str = Query(..., description="YYYY-MM"),
    sales_wechat_id: Optional[str] = Query(None),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        y, m = map(int, month.split("-")[:2])
        month_start = date(y, m, 1)
    except (ValueError, TypeError):
        return {"code": 400, "message": "month 格式须为 YYYY-MM", "data": None}
    if m == 12:
        month_end = date(y + 1, 1, 1)
    else:
        month_end = date(y, m + 1, 1)
    from datetime import timedelta

    month_end = month_end - timedelta(days=1)

    sw = await _resolve_sales_wechat_id(db, current_user, sales_wechat_id)
    res = await db.execute(
        select(ContactTask.due_date, ContactTask.status)
        .join(TaskAllocationBatch, TaskAllocationBatch.id == ContactTask.batch_id)
        .where(ContactTask.sales_wechat_id == sw)
        .where(TaskAllocationBatch.status == "published")
        .where(ContactTask.due_date >= month_start)
        .where(ContactTask.due_date <= month_end)
    )
    by_day: dict[date, dict[str, int]] = {}
    for due, st in res.all():
        d = due
        bucket = by_day.setdefault(d, {"total": 0, "done": 0, "pending": 0, "overdue": 0})
        bucket["total"] += 1
        s = str(st or "pending")
        if s == "done":
            bucket["done"] += 1
        elif s == "overdue":
            bucket["overdue"] += 1
        elif s in ("pending", "in_progress"):
            bucket["pending"] += 1

    days = []
    cur = month_start
    while cur <= month_end:
        b = by_day.get(cur, {"total": 0, "done": 0, "pending": 0, "overdue": 0})
        days.append(schemas.TaskCalendarDayOut(date=cur, **b))
        cur += timedelta(days=1)

    return {"code": 200, "message": "ok", "data": days}


@router.post("/{task_id}/complete")
async def complete_task(
    task_id: int,
    body: schemas.TaskCompleteIn | None = None,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(select(ContactTask).where(ContactTask.id == task_id))
    task = res.scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    await _resolve_sales_wechat_id(db, current_user, task.sales_wechat_id)
    task.status = "done"
    task.completed_at = datetime.now()
    task.completed_by_user_id = current_user.id
    if body and body.note:
        task.completion_note = body.note.strip()[:500]
    await db.commit()
    return {"code": 200, "message": "已完成", "data": {"id": task.id, "status": task.status}}


@router.post("/{task_id}/skip")
async def skip_task(
    task_id: int,
    body: schemas.TaskSkipIn | None = None,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(select(ContactTask).where(ContactTask.id == task_id))
    task = res.scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    await _resolve_sales_wechat_id(db, current_user, task.sales_wechat_id)
    task.status = "skipped"
    if body and body.note:
        task.completion_note = body.note.strip()[:500]
    await db.commit()
    return {"code": 200, "message": "已跳过", "data": {"id": task.id, "status": task.status}}


@router.post("/{task_id}/restore")
async def restore_task(
    task_id: int,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """将已完成 / 已跳过的任务恢复为待办。"""
    res = await db.execute(select(ContactTask).where(ContactTask.id == task_id))
    task = res.scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    await _resolve_sales_wechat_id(db, current_user, task.sales_wechat_id)
    task.status = "pending"
    task.completed_at = None
    task.completed_by_user_id = None
    task.completion_note = None
    await db.commit()
    return {"code": 200, "message": "已恢复待办", "data": {"id": task.id, "status": task.status}}
