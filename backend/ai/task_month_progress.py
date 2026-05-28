"""月进度：按 due_date 汇总本月全部联系任务（非 monthly 批次分配）。"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import func
from sqlalchemy.future import select

from models import ContactTask, RawCustomer, SalesCustomerProfile


async def query_month_progress_rows(
    db,
    *,
    sales_wechat_id: str,
    month_start: date,
    month_end: date,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 0,
) -> tuple[list[tuple[ContactTask, SalesCustomerProfile | None, RawCustomer | None]], int]:
    stmt = (
        select(ContactTask, SalesCustomerProfile, RawCustomer)
        .outerjoin(SalesCustomerProfile, SalesCustomerProfile.id == ContactTask.scp_id)
        .outerjoin(RawCustomer, RawCustomer.id == ContactTask.raw_customer_id)
        .where(ContactTask.sales_wechat_id == sales_wechat_id)
        .where(ContactTask.due_date >= month_start)
        .where(ContactTask.due_date <= month_end)
        .order_by(
            ContactTask.due_date.desc(),
            ContactTask.priority_rank.asc(),
            ContactTask.id.asc(),
        )
    )
    if status:
        stmt = stmt.where(ContactTask.status == status)

    if page_size and page_size > 0:
        count_stmt = (
            select(func.count(ContactTask.id))
            .where(ContactTask.sales_wechat_id == sales_wechat_id)
            .where(ContactTask.due_date >= month_start)
            .where(ContactTask.due_date <= month_end)
        )
        if status:
            count_stmt = count_stmt.where(ContactTask.status == status)
        total = int((await db.execute(count_stmt)).scalar() or 0)
        offset = max(0, (max(1, page) - 1) * page_size)
        rows = (await db.execute(stmt.offset(offset).limit(page_size))).all()
        return list(rows), total

    rows = (await db.execute(stmt)).all()
    items = list(rows)
    return items, len(items)


async def query_month_progress_stats(
    db,
    *,
    sales_wechat_id: str,
    month_start: date,
    month_end: date,
    status: Optional[str] = None,
) -> dict:
    """返回全月汇总统计（不受分页影响）。"""
    base = (
        select(ContactTask.status, func.count(ContactTask.id))
        .where(ContactTask.sales_wechat_id == sales_wechat_id)
        .where(ContactTask.due_date >= month_start)
        .where(ContactTask.due_date <= month_end)
        .group_by(ContactTask.status)
    )
    if status:
        base = base.where(ContactTask.status == status)
    rows = (await db.execute(base)).all()

    counts: dict[str, int] = {}
    for st, cnt in rows:
        key = str(st or "pending")
        counts[key] = int(cnt or 0)

    total = sum(counts.values())
    done = counts.get("done", 0)
    skipped = counts.get("skipped", 0)
    denom = max(1, total - skipped)
    return {
        "total": total,
        "done": done,
        "pending": counts.get("pending", 0),
        "in_progress": counts.get("in_progress", 0),
        "skipped": skipped,
        "overdue": counts.get("overdue", 0),
        "completion_rate": round(done / denom, 4),
    }


def stats_from_task_dicts(items: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for it in items:
        st = str(it.get("status") or "pending")
        counts[st] = counts.get(st, 0) + 1
    total = sum(counts.values())
    done = counts.get("done", 0)
    skipped = counts.get("skipped", 0)
    denom = max(1, total - skipped)
    return {
        "total": total,
        "done": done,
        "pending": counts.get("pending", 0),
        "in_progress": counts.get("in_progress", 0),
        "skipped": skipped,
        "overdue": counts.get("overdue", 0),
        "completion_rate": round(done / denom, 4),
    }
