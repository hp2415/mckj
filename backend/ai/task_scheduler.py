"""任务调度中枢：从 reserve 池按优先级 + 跨周期(日→周)顺序取可认领任务。"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import case, func, select, update

from models import ContactTask, RawCustomer, SalesCustomerProfile, TaskAllocationBatch

_PERIOD_RANK = case(
    (ContactTask.period_type == "daily", 0),
    (ContactTask.period_type == "weekly", 1),
    else_=2,
)


def _pool_claimed_on_expr():
    return func.json_unquote(func.json_extract(ContactTask.alloc_feature_json, "$.pool.claimed_on"))


def pool_meta_from_alloc(alloc: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(alloc, dict):
        return {}
    pool = alloc.get("pool")
    return pool if isinstance(pool, dict) else {}


def is_claimed_from_reserve(alloc: dict[str, Any] | None) -> bool:
    pool = pool_meta_from_alloc(alloc)
    if pool.get("claimed") is True:
        return True
    return bool(str(pool.get("claimed_on") or "").strip())


async def list_claimable_tasks(
    db,
    *,
    sales_wechat_id: str,
    ref_date: date,
    limit: int = 20,
) -> list[ContactTask]:
    """日内 reserve 优先，其次本周 reserve；仅来自 published/draft 批次。"""
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return []
    cap = max(1, min(int(limit or 20), 100))
    stmt = (
        select(ContactTask)
        .join(TaskAllocationBatch, TaskAllocationBatch.id == ContactTask.batch_id)
        .where(ContactTask.sales_wechat_id == sw)
        .where(ContactTask.status == "reserve")
        .where(ContactTask.due_date >= ref_date)
        .where(TaskAllocationBatch.status.in_(("published", "draft")))
        .order_by(_PERIOD_RANK.asc(), ContactTask.priority_rank.asc(), ContactTask.id.asc())
        .limit(cap)
    )
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def load_claimable_tasks_with_customer(
    db,
    *,
    sales_wechat_id: str,
    ref_date: date,
    limit: int = 20,
) -> list[tuple[ContactTask, SalesCustomerProfile | None, RawCustomer | None]]:
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return []
    cap = max(1, min(int(limit or 20), 100))
    stmt = (
        select(ContactTask, SalesCustomerProfile, RawCustomer)
        .join(TaskAllocationBatch, TaskAllocationBatch.id == ContactTask.batch_id)
        .outerjoin(SalesCustomerProfile, SalesCustomerProfile.id == ContactTask.scp_id)
        .outerjoin(RawCustomer, RawCustomer.id == ContactTask.raw_customer_id)
        .where(ContactTask.sales_wechat_id == sw)
        .where(ContactTask.status == "reserve")
        .where(ContactTask.due_date >= ref_date)
        .where(TaskAllocationBatch.status.in_(("published", "draft")))
        .order_by(_PERIOD_RANK.asc(), ContactTask.priority_rank.asc(), ContactTask.id.asc())
        .limit(cap)
    )
    rows = list((await db.execute(stmt)).all())
    if len(rows) >= cap:
        return rows
    from ai.task_weekly_profile import load_weekly_profile_reserve_candidates

    exclude = {str(t.raw_customer_id or "").strip() for t, _, _ in rows if t and t.raw_customer_id}
    profile_rows = await load_weekly_profile_reserve_candidates(
        db,
        sales_wechat_id=sw,
        ref_date=ref_date,
        exclude_raw_ids=exclude,
        limit=cap - len(rows),
    )
    if not profile_rows:
        return rows
    scp_ids = [
        int((row.get("_profile_weekly") or {}).get("scp_id") or 0)
        for row in profile_rows
        if (row.get("_profile_weekly") or {}).get("scp_id")
    ]
    scp_map: dict[int, SalesCustomerProfile] = {}
    rc_map: dict[str, RawCustomer] = {}
    if scp_ids:
        scp_res = await db.execute(
            select(SalesCustomerProfile, RawCustomer)
            .join(RawCustomer, RawCustomer.id == SalesCustomerProfile.raw_customer_id)
            .where(SalesCustomerProfile.id.in_(scp_ids))
        )
        for scp, rc in scp_res.all():
            if scp and scp.id:
                scp_map[int(scp.id)] = scp
            if rc and rc.id:
                rc_map[str(rc.id)] = rc
    for row in profile_rows:
        meta = row.get("_profile_weekly") or {}
        sid = int(meta.get("scp_id") or 0)
        scp = scp_map.get(sid)
        rc = rc_map.get(str(row.get("raw_customer_id") or "")) if row.get("raw_customer_id") else None
        pseudo = ContactTask(
            id=int(row.get("id") or 0),
            batch_id=int(row.get("batch_id") or 0),
            scp_id=sid or None,
            raw_customer_id=str(row.get("raw_customer_id") or ""),
            sales_wechat_id=sw,
            period_type=str(row.get("period_type") or "weekly"),
            due_date=row.get("due_date") or ref_date,
            task_kind=str(row.get("task_kind") or "contact"),
            contact_channel=str(row.get("contact_channel") or "wechat"),
            priority_rank=int(row.get("priority_rank") or 0),
            title=row.get("title"),
            instruction=row.get("instruction"),
            status="reserve",
            alloc_feature_json={"pool": row.get("pool") or {}},
        )
        rows.append((pseudo, scp, rc))
    return rows


async def daily_claimed_count(
    db,
    *,
    sales_wechat_id: str,
    ref_date: date,
) -> int:
    """当日该销售已认领条数（alloc_feature_json.pool.claimed_on）。"""
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return 0
    ref_iso = ref_date.isoformat()
    res = await db.execute(
        select(func.count(ContactTask.id))
        .where(ContactTask.sales_wechat_id == sw)
        .where(ContactTask.status != "reserve")
        .where(_pool_claimed_on_expr() == ref_iso)
    )
    return int(res.scalar() or 0)


async def claim_reserve_task(
    db,
    *,
    task_id: int,
    sales_wechat_id: str,
    user_id: int,
    ref_date: date,
) -> ContactTask | None:
    """原子认领 reserve→pending；成功返回任务，已被抢占返回 None。"""
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return None
    upd = await db.execute(
        update(ContactTask)
        .where(ContactTask.id == int(task_id))
        .where(ContactTask.sales_wechat_id == sw)
        .where(ContactTask.status == "reserve")
        .values(status="pending", due_date=ref_date)
    )
    if not upd.rowcount:
        return None
    res = await db.execute(select(ContactTask).where(ContactTask.id == int(task_id)))
    task = res.scalars().first()
    if not task:
        return None
    af = dict(task.alloc_feature_json or {})
    pool = dict(af.get("pool") or {})
    pool.update(
        {
            "tier": "reserve",
            "claimed": True,
            "claimed_by": int(user_id),
            "claimed_on": ref_date.isoformat(),
        }
    )
    af["pool"] = pool
    task.alloc_feature_json = af
    return task
