"""任务调度中枢：从 reserve 池按优先级 + 跨周期(日→周)顺序取可认领任务。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import case, func, select, update

from models import ContactTask, RawCustomer, SalesCustomerProfile, TaskAllocationBatch

_PERIOD_RANK = case(
    (ContactTask.period_type == "daily", 0),
    (ContactTask.period_type == "weekly", 1),
    else_=2,
)

_PERIOD_DAILY = "daily"


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


async def find_active_daily_batch(
    db,
    *,
    sales_wechat_id: str,
    ref_date: date,
) -> TaskAllocationBatch | None:
    """今日日任务批次（published 优先，其次 draft；不含 generating）。"""
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return None
    res = await db.execute(
        select(TaskAllocationBatch)
        .where(TaskAllocationBatch.sales_wechat_id == sw)
        .where(TaskAllocationBatch.period_type == _PERIOD_DAILY)
        .where(TaskAllocationBatch.period_start == ref_date)
        .where(TaskAllocationBatch.status.in_(("published", "draft")))
        .order_by(
            case(
                (TaskAllocationBatch.status == "published", 0),
                else_=1,
            ),
            TaskAllocationBatch.id.desc(),
        )
        .limit(1)
    )
    return res.scalars().first()


async def ensure_claim_daily_batch(
    db,
    *,
    sales_wechat_id: str,
    ref_date: date,
    user_id: int | None = None,
) -> TaskAllocationBatch | None:
    """认领后挂到今日日批次，使桌面端「日任务」列表可见。"""
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return None
    existing = await find_active_daily_batch(db, sales_wechat_id=sw, ref_date=ref_date)
    if existing:
        return existing
    batch = TaskAllocationBatch(
        sales_wechat_id=sw,
        user_id=user_id,
        period_type=_PERIOD_DAILY,
        period_start=ref_date,
        period_end=ref_date,
        source="claim_inject",
        status="published",
        task_count=0,
        input_snapshot_json={"source": "claim_inject"},
        published_at=datetime.now(),
    )
    db.add(batch)
    await db.flush()
    return batch


async def attach_claimed_task_to_daily(
    db,
    task: ContactTask,
    *,
    ref_date: date,
    user_id: int | None = None,
) -> ContactTask:
    """将已认领任务归入今日日批次（跨周期认领后才能出现在日任务列表）。"""
    if task is None:
        return task
    daily = await ensure_claim_daily_batch(
        db,
        sales_wechat_id=str(task.sales_wechat_id or ""),
        ref_date=ref_date,
        user_id=user_id,
    )
    if not daily:
        return task
    prev_period = str(task.period_type or "") or None
    prev_batch = int(task.batch_id) if task.batch_id else None
    if int(task.batch_id or 0) == int(daily.id) and str(task.period_type or "") == _PERIOD_DAILY:
        return task
    task.batch_id = daily.id
    task.period_type = _PERIOD_DAILY
    task.due_date = ref_date
    af = dict(task.alloc_feature_json or {})
    pool = dict(af.get("pool") or {})
    if prev_period and prev_period != _PERIOD_DAILY:
        pool.setdefault("source_batch_period", prev_period)
    if prev_batch and prev_batch != int(daily.id):
        pool.setdefault("source_batch_id", prev_batch)
    pool["moved_to_daily"] = True
    af["pool"] = pool
    task.alloc_feature_json = af
    daily.task_count = int(daily.task_count or 0) + 1
    return task


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
    # 周/月储备认领后须挂到今日日批次，否则日任务总览按 batch 过滤时看不到
    if str(task.period_type or "") != _PERIOD_DAILY:
        task = await attach_claimed_task_to_daily(
            db, task, ref_date=ref_date, user_id=user_id
        )
    return task
