"""周任务：由客户画像跟进日期/策略动态汇总，不经 LLM 任务分配。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import and_
from sqlalchemy.future import select

from ai.profile_followup_policy import (
    has_no_followup_profile_tag,
    should_suppress_profile_followup,
)
from ai.profile_staff_tag import has_staff_profile_tag
from ai.raw_profiling import (
    extract_followup_from_ai_profile,
    normalize_followup_channel,
    rcsw_active_for_profile_where,
)
from ai.task_allocation import PERIOD_WEEKLY, monday_week_bounds, today_shanghai
from ai.task_month_progress import stats_from_task_dicts
from crud import profile_tags_by_relation_ids
from models import (
    ContactTask,
    RawCustomer,
    RawCustomerSalesWechat,
    SalesCustomerProfile,
    TaskAllocationBatch,
)

WEEKLY_PROFILE_VIEW_MODE = "weekly_profile"
PROFILE_WEEKLY_BATCH_SOURCE = "profile_weekly"

_STATUS_RANK = {
    "done": 0,
    "skipped": 1,
    "overdue": 2,
    "in_progress": 3,
    "pending": 4,
    "reserve": 5,
}


def virtual_weekly_task_id(scp_id: int) -> int:
    return -abs(int(scp_id))


def is_virtual_weekly_task_id(task_id: int) -> bool:
    try:
        return int(task_id) < 0
    except (TypeError, ValueError):
        return False


def scp_id_from_virtual_weekly_task_id(task_id: int) -> int:
    return abs(int(task_id))


def profile_weekly_dedupe_key(week_start: date, scp_id: int) -> str:
    return f"profile_weekly:{week_start.isoformat()}:{int(scp_id)}"


def _display_name(rc: RawCustomer | None, scp: SalesCustomerProfile | None) -> str:
    for src in (rc, scp):
        if src is None:
            continue
        for key in ("customer_name", "wechat_remark", "unit_name"):
            val = str(getattr(src, key, None) or "").strip()
            if val:
                return val
    return "客户"


def _followup_meta(scp: SalesCustomerProfile) -> dict[str, str]:
    meta = extract_followup_from_ai_profile(scp.ai_profile)
    channel = normalize_followup_channel(meta.get("followup_channel"))
    strategy = str(meta.get("followup_strategy") or "").strip()
    if not strategy:
        strategy = (
            "电话深沟通，确认需求并推进合作"
            if channel == "phone"
            else "按建议日期微信轻触达，确认需求与采购计划"
        )
    return {
        "followup_channel": channel,
        "followup_strategy": strategy[:2000],
        "followup_reason": str(meta.get("followup_reason") or "").strip(),
    }


def _priority_score(scp: SalesCustomerProfile, followup_date: date, ref_date: date) -> float:
    try:
        intent = float(scp.intent_score) if scp.intent_score is not None else 0.0
    except (TypeError, ValueError):
        intent = 0.0
    overdue_boost = max(0, (ref_date - followup_date).days) * 4.0
    return round(intent + overdue_boost, 2)


def _task_dict_from_profile(
    *,
    scp: SalesCustomerProfile,
    rc: RawCustomer,
    followup_date: date,
    week_start: date,
    week_end: date,
    ref_date: date,
    rank: int,
    existing: ContactTask | None,
) -> dict[str, Any]:
    meta = _followup_meta(scp)
    name = _display_name(rc, scp)
    due = existing.due_date if existing else followup_date
    status = str(existing.status or "pending") if existing else "pending"
    task_id = int(existing.id) if existing else virtual_weekly_task_id(int(scp.id))
    batch_id = int(existing.batch_id) if existing else 0
    phone_raw = (rc.phone or "").strip() or None
    phone_norm = (rc.phone_normalized or "").strip() or None
    phone_display = phone_norm or phone_raw
    return {
        "id": task_id,
        "batch_id": batch_id,
        "raw_customer_id": scp.raw_customer_id,
        "sales_wechat_id": scp.sales_wechat_id,
        "period_type": PERIOD_WEEKLY,
        "due_date": due,
        "task_kind": str(existing.task_kind or "contact") if existing else "contact",
        "contact_channel": (
            str(existing.contact_channel or meta["followup_channel"])
            if existing
            else meta["followup_channel"]
        ),
        "priority_rank": int(existing.priority_rank) if existing else rank,
        "priority_score": (
            float(existing.priority_score)
            if existing and existing.priority_score is not None
            else _priority_score(scp, followup_date, ref_date)
        ),
        "title": (existing.title or f"周跟进 · {name}"[:200]) if existing else f"周跟进 · {name}"[:200],
        "instruction": (existing.instruction or meta["followup_strategy"]) if existing else meta["followup_strategy"],
        "status": status,
        "completed_at": existing.completed_at if existing else None,
        "completion_note": existing.completion_note if existing else None,
        "customer_name": (rc.customer_name or "").strip() or None,
        "unit_name": (rc.unit_name or "").strip() or None,
        "wechat_remark": (scp.wechat_remark or "").strip() or None,
        "phone": phone_display,
        "phone_raw": phone_raw,
        "phone_normalized": phone_norm,
        "ai_profile": scp.ai_profile,
        "suggested_followup_date": followup_date,
        "pool": None,
        "_profile_weekly": {
            "scp_id": int(scp.id),
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "virtual": existing is None,
        },
    }


async def _load_week_task_by_customer(
    db,
    *,
    sales_wechat_id: str,
    week_start: date,
    week_end: date,
) -> dict[str, ContactTask]:
    res = await db.execute(
        select(ContactTask)
        .where(ContactTask.sales_wechat_id == sales_wechat_id)
        .where(ContactTask.due_date >= week_start)
        .where(ContactTask.due_date <= week_end)
        .where(ContactTask.status != "reserve")
    )
    best: dict[str, ContactTask] = {}
    for task in res.scalars().all():
        rid = str(task.raw_customer_id or "").strip()
        if not rid:
            continue
        prev = best.get(rid)
        if prev is None:
            best[rid] = task
            continue
        prev_rank = _STATUS_RANK.get(str(prev.status or ""), 9)
        cur_rank = _STATUS_RANK.get(str(task.status or ""), 9)
        if cur_rank < prev_rank or (cur_rank == prev_rank and int(task.id or 0) > int(prev.id or 0)):
            best[rid] = task
    return best


async def _load_profile_candidates(
    db,
    *,
    sales_wechat_id: str,
    week_start: date,
    week_end: date,
) -> list[tuple[SalesCustomerProfile, RawCustomer, date]]:
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return []
    stmt = (
        select(SalesCustomerProfile, RawCustomer, RawCustomerSalesWechat)
        .join(RawCustomer, RawCustomer.id == SalesCustomerProfile.raw_customer_id)
        .join(
            RawCustomerSalesWechat,
            and_(
                RawCustomerSalesWechat.raw_customer_id == SalesCustomerProfile.raw_customer_id,
                RawCustomerSalesWechat.sales_wechat_id == SalesCustomerProfile.sales_wechat_id,
            ),
        )
        .where(SalesCustomerProfile.sales_wechat_id == sw)
        .where(SalesCustomerProfile.profile_status == 1)
        .where(SalesCustomerProfile.suggested_followup_date.isnot(None))
        .where(SalesCustomerProfile.suggested_followup_date >= week_start)
        .where(SalesCustomerProfile.suggested_followup_date <= week_end)
        .where(rcsw_active_for_profile_where())
    )
    rows = (await db.execute(stmt)).all()
    scp_ids = [int(scp.id) for scp, _, _ in rows if scp and scp.id]
    tag_map = await profile_tags_by_relation_ids(db, scp_ids)
    out: list[tuple[SalesCustomerProfile, RawCustomer, date]] = []
    for scp, rc, rcsw in rows:
        if not scp or not rc or not scp.id:
            continue
        tags = tag_map.get(scp.id, [])
        if has_staff_profile_tag(tags) or has_no_followup_profile_tag(tags):
            continue
        if should_suppress_profile_followup(
            tags=tags,
            ai_profile=str(scp.ai_profile or ""),
            raw_customer_id=str(scp.raw_customer_id or ""),
            rcsw=rcsw,
            raw=rc,
        ):
            continue
        followup_date = scp.suggested_followup_date
        if followup_date is None:
            continue
        out.append((scp, rc, followup_date))
    out.sort(
        key=lambda row: (
            row[2],
            -_priority_score(row[0], row[2], today_shanghai()),
            int(row[0].id or 0),
        )
    )
    return out


async def query_weekly_profile_items(
    db,
    *,
    sales_wechat_id: str,
    week_start: date,
    week_end: date,
    ref_date: date | None = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    ref = ref_date or today_shanghai()
    candidates = await _load_profile_candidates(
        db,
        sales_wechat_id=sales_wechat_id,
        week_start=week_start,
        week_end=week_end,
    )
    week_tasks = await _load_week_task_by_customer(
        db,
        sales_wechat_id=sales_wechat_id,
        week_start=week_start,
        week_end=week_end,
    )
    items: list[dict[str, Any]] = []
    for idx, (scp, rc, followup_date) in enumerate(candidates, start=1):
        rid = str(scp.raw_customer_id or "").strip()
        existing = week_tasks.get(rid)
        row = _task_dict_from_profile(
            scp=scp,
            rc=rc,
            followup_date=followup_date,
            week_start=week_start,
            week_end=week_end,
            ref_date=ref,
            rank=idx,
            existing=existing,
        )
        if status and str(row.get("status") or "") != status:
            continue
        items.append(row)
    total = len(items)
    if page_size and page_size > 0:
        offset = max(0, (max(1, page) - 1) * page_size)
        items = items[offset : offset + page_size]
    return items, total


async def query_weekly_profile_stats(
    db,
    *,
    sales_wechat_id: str,
    week_start: date,
    week_end: date,
    ref_date: date | None = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    items, _ = await query_weekly_profile_items(
        db,
        sales_wechat_id=sales_wechat_id,
        week_start=week_start,
        week_end=week_end,
        ref_date=ref_date,
        status=status,
        page=1,
        page_size=0,
    )
    return stats_from_task_dicts(items)


async def ensure_profile_weekly_batch(
    db,
    *,
    sales_wechat_id: str,
    week_start: date,
    week_end: date,
    user_id: int | None = None,
) -> TaskAllocationBatch:
    sw = (sales_wechat_id or "").strip()
    res = await db.execute(
        select(TaskAllocationBatch)
        .where(TaskAllocationBatch.sales_wechat_id == sw)
        .where(TaskAllocationBatch.period_type == PERIOD_WEEKLY)
        .where(TaskAllocationBatch.period_start == week_start)
        .where(TaskAllocationBatch.source == PROFILE_WEEKLY_BATCH_SOURCE)
        .order_by(TaskAllocationBatch.id.desc())
        .limit(1)
    )
    batch = res.scalars().first()
    if batch:
        return batch
    batch = TaskAllocationBatch(
        sales_wechat_id=sw,
        user_id=user_id,
        period_type=PERIOD_WEEKLY,
        period_start=week_start,
        period_end=week_end,
        source=PROFILE_WEEKLY_BATCH_SOURCE,
        status="published",
        task_count=0,
        input_snapshot_json={"view_mode": WEEKLY_PROFILE_VIEW_MODE},
        published_at=datetime.now(),
    )
    db.add(batch)
    await db.flush()
    return batch


async def materialize_weekly_profile_task(
    db,
    *,
    scp_id: int,
    sales_wechat_id: str,
    ref_date: date | None = None,
    user_id: int | None = None,
    status: str = "pending",
    due_date: date | None = None,
    pool_meta: dict[str, Any] | None = None,
) -> ContactTask | None:
    """将画像周任务物化为 contact_tasks 行（完成/认领/跳过时调用）。"""
    ref = ref_date or today_shanghai()
    week_start, week_end = monday_week_bounds(ref)
    sw = (sales_wechat_id or "").strip()
    sid = int(scp_id)
    res = await db.execute(
        select(SalesCustomerProfile, RawCustomer)
        .join(RawCustomer, RawCustomer.id == SalesCustomerProfile.raw_customer_id)
        .where(SalesCustomerProfile.id == sid)
        .where(SalesCustomerProfile.sales_wechat_id == sw)
        .limit(1)
    )
    row = res.first()
    if not row:
        return None
    scp, rc = row
    followup_date = scp.suggested_followup_date
    if followup_date is None or followup_date < week_start or followup_date > week_end:
        return None
    dedupe = profile_weekly_dedupe_key(week_start, sid)
    existing_res = await db.execute(select(ContactTask).where(ContactTask.dedupe_key == dedupe))
    existing = existing_res.scalars().first()
    if existing:
        if due_date is not None:
            existing.due_date = due_date
        if status:
            existing.status = status
        if pool_meta:
            af = dict(existing.alloc_feature_json or {})
            pool = dict(af.get("pool") or {})
            pool.update(pool_meta)
            af["pool"] = pool
            existing.alloc_feature_json = af
        return existing

    week_tasks = await _load_week_task_by_customer(
        db,
        sales_wechat_id=sw,
        week_start=week_start,
        week_end=week_end,
    )
    rid = str(scp.raw_customer_id or "").strip()
    if rid and rid in week_tasks:
        task = week_tasks[rid]
        if due_date is not None:
            task.due_date = due_date
        if status:
            task.status = status
        return task

    meta = _followup_meta(scp)
    name = _display_name(rc, scp)
    batch = await ensure_profile_weekly_batch(
        db,
        sales_wechat_id=sw,
        week_start=week_start,
        week_end=week_end,
        user_id=user_id,
    )
    eff_due = due_date or followup_date
    alloc_feature: dict[str, Any] = {
        "profile_weekly": {
            "scp_id": sid,
            "followup_date": followup_date.isoformat(),
            "channel": meta["followup_channel"],
            "has_strategy": bool(meta["followup_strategy"]),
        }
    }
    if pool_meta:
        alloc_feature["pool"] = pool_meta
    task = ContactTask(
        batch_id=batch.id,
        scp_id=sid,
        raw_customer_id=rid,
        sales_wechat_id=sw,
        period_type=PERIOD_WEEKLY,
        due_date=eff_due,
        task_kind="contact",
        contact_channel=meta["followup_channel"],
        priority_rank=0,
        priority_score=_priority_score(scp, followup_date, ref),
        title=f"周跟进 · {name}"[:200],
        instruction=meta["followup_strategy"],
        status=status,
        dedupe_key=dedupe,
        alloc_feature_json=alloc_feature,
    )
    db.add(task)
    await db.flush()
    batch.task_count = int(batch.task_count or 0) + 1
    return task


async def resolve_weekly_profile_task_for_action(
    db,
    task_id: int,
    *,
    sales_wechat_id: str,
    user_id: int | None = None,
) -> ContactTask | None:
    if not is_virtual_weekly_task_id(task_id):
        return None
    return await materialize_weekly_profile_task(
        db,
        scp_id=scp_id_from_virtual_weekly_task_id(task_id),
        sales_wechat_id=sales_wechat_id,
        user_id=user_id,
        status="pending",
    )


async def load_weekly_profile_reserve_candidates(
    db,
    *,
    sales_wechat_id: str,
    ref_date: date,
    exclude_raw_ids: set[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """日 reserve 不足时补充：本周画像到期且今日尚未主派的客户。"""
    week_start, week_end = monday_week_bounds(ref_date)
    cap = max(0, int(limit or 0))
    if cap <= 0:
        return []
    exclude = {str(x).strip() for x in (exclude_raw_ids or set()) if str(x).strip()}
    today_main_res = await db.execute(
        select(ContactTask.raw_customer_id)
        .where(ContactTask.sales_wechat_id == sales_wechat_id)
        .where(ContactTask.period_type == "daily")
        .where(ContactTask.due_date == ref_date)
        .where(ContactTask.status.in_(("pending", "done", "in_progress", "overdue")))
    )
    for rid, in today_main_res.all():
        if rid:
            exclude.add(str(rid).strip())
    candidates = await _load_profile_candidates(
        db,
        sales_wechat_id=sales_wechat_id,
        week_start=week_start,
        week_end=week_end,
    )
    week_tasks = await _load_week_task_by_customer(
        db,
        sales_wechat_id=sales_wechat_id,
        week_start=week_start,
        week_end=week_end,
    )
    out: list[dict[str, Any]] = []
    for idx, (scp, rc, followup_date) in enumerate(candidates, start=1):
        rid = str(scp.raw_customer_id or "").strip()
        if not rid or rid in exclude:
            continue
        existing = week_tasks.get(rid)
        if existing and str(existing.status or "") in ("done", "skipped", "pending", "in_progress", "overdue"):
            if str(existing.status or "") != "reserve":
                continue
        row = _task_dict_from_profile(
            scp=scp,
            rc=rc,
            followup_date=followup_date,
            week_start=week_start,
            week_end=week_end,
            ref_date=ref_date,
            rank=idx,
            existing=existing if existing and str(existing.status or "") == "reserve" else None,
        )
        row["status"] = "reserve"
        pool = {
            "tier": "profile_weekly",
            "claimed": False,
            "source": "profile_followup",
        }
        row["pool"] = pool
        out.append(row)
        if len(out) >= cap:
            break
    return out
