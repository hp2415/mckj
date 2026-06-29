"""任务监测：汇总各销售微信号的任务完成情况。"""
from __future__ import annotations

from datetime import date

from sqlalchemy import and_, case, func, select

from ai.task_allocation import PERIOD_DAILY, PERIOD_MONTHLY, period_bounds, today_shanghai
from models import ContactTask, SalesWechatAccount, TaskAllocationBatch

_BATCH_STATUS_QUERY_VALUES = frozenset({"active", "all", "draft", "published", "archived"})


def resolve_batch_statuses(batch_status: str) -> tuple[str, ...]:
    s = (batch_status or "").strip().lower()
    if s in ("", "active", "current"):
        return ("draft", "published")
    if s == "all":
        return ("draft", "published", "archived")
    if s in ("draft", "published", "archived"):
        return (s,)
    return ("draft", "published")


def is_current_period(period_type: str, period_start: date) -> bool:
    cur_start, _ = period_bounds(period_type, today_shanghai())
    return period_start == cur_start


def stats_from_counts(counts: dict[str, int]) -> dict:
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


def merge_summary(items: list[dict]) -> dict:
    totals = {
        "total": 0,
        "done": 0,
        "pending": 0,
        "in_progress": 0,
        "skipped": 0,
        "overdue": 0,
    }
    for it in items:
        st = it.get("stats") or {}
        for k in totals:
            totals[k] += int(st.get(k) or 0)
    skipped = totals["skipped"]
    denom = max(1, totals["total"] - skipped)
    return {
        **totals,
        "sales_count": len(items),
        "completion_rate": round(totals["done"] / denom, 4),
    }


def _sales_label(
    sales_wechat_id: str,
    *,
    nickname: str | None = None,
    alias_name: str | None = None,
    account_code: str | None = None,
) -> str:
    sw = (sales_wechat_id or "").strip()
    display = (
        (nickname or "").strip()
        or (alias_name or "").strip()
        or (account_code or "").strip()
    )
    if display and display != sw:
        return f"{display}（{sw}）"
    return display or sw


async def _load_sales_labels(db) -> dict[str, dict]:
    res = await db.execute(
        select(SalesWechatAccount)
        .where(SalesWechatAccount.sales_wechat_id.isnot(None))
        .order_by(SalesWechatAccount.nickname, SalesWechatAccount.sales_wechat_id)
    )
    out: dict[str, dict] = {}
    for acc in res.scalars().all():
        sw = (acc.sales_wechat_id or "").strip()
        if not sw:
            continue
        out[sw] = {
            "sales_wechat_id": sw,
            "nickname": (acc.nickname or "").strip() or None,
            "label": _sales_label(
                sw,
                nickname=acc.nickname,
                alias_name=acc.alias_name,
                account_code=acc.account_code,
            ),
        }
    return out


async def _pick_batches_for_period(
    db,
    *,
    period_type: str,
    period_start: date,
    batch_status: str,
    is_current: bool,
) -> dict[str, TaskAllocationBatch]:
    statuses = resolve_batch_statuses(batch_status)
    gen_map: dict[str, TaskAllocationBatch] = {}
    if is_current:
        res = await db.execute(
            select(TaskAllocationBatch)
            .where(TaskAllocationBatch.period_type == period_type)
            .where(TaskAllocationBatch.period_start == period_start)
            .where(TaskAllocationBatch.status == "generating")
        )
        for b in res.scalars().all():
            sw = (b.sales_wechat_id or "").strip()
            if sw:
                gen_map[sw] = b

    res = await db.execute(
        select(TaskAllocationBatch)
        .where(TaskAllocationBatch.period_type == period_type)
        .where(TaskAllocationBatch.period_start == period_start)
        .where(TaskAllocationBatch.status.in_(statuses))
        .order_by(TaskAllocationBatch.sales_wechat_id, TaskAllocationBatch.id.desc())
    )
    picked: dict[str, TaskAllocationBatch] = {}
    for b in res.scalars().all():
        sw = (b.sales_wechat_id or "").strip()
        if not sw:
            continue
        if sw in gen_map:
            picked[sw] = gen_map[sw]
        elif sw not in picked:
            picked[sw] = b

    for sw, b in gen_map.items():
        picked.setdefault(sw, b)
    return picked


async def _aggregate_batch_task_metrics(
    db, batch_ids: list[int]
) -> tuple[dict[int, dict[str, int]], dict[int, dict[str, int]]]:
    if not batch_ids:
        return {}, {}

    status_res = await db.execute(
        select(ContactTask.batch_id, ContactTask.status, func.count(ContactTask.id))
        .where(ContactTask.batch_id.in_(batch_ids))
        .group_by(ContactTask.batch_id, ContactTask.status)
    )
    status_map: dict[int, dict[str, int]] = {}
    for batch_id, status, cnt in status_res.all():
        bid = int(batch_id)
        status_map.setdefault(bid, {})[str(status or "pending")] = int(cnt or 0)

    breakdown_res = await db.execute(
        select(
            ContactTask.batch_id,
            func.sum(
                case(
                    (
                        and_(
                            ContactTask.task_kind != "icebreaker",
                            ContactTask.contact_channel != "phone",
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (
                        and_(
                            ContactTask.task_kind != "icebreaker",
                            ContactTask.contact_channel == "phone",
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            func.sum(case((ContactTask.task_kind == "icebreaker", 1), else_=0)),
            func.sum(
                case((ContactTask.status.in_(("pending", "in_progress")), 1), else_=0)
            ),
        )
        .where(ContactTask.batch_id.in_(batch_ids))
        .group_by(ContactTask.batch_id)
    )
    breakdown_map: dict[int, dict[str, int]] = {}
    for batch_id, mw, mp, ice, pend in breakdown_res.all():
        breakdown_map[int(batch_id)] = {
            "main_wechat": int(mw or 0),
            "main_phone": int(mp or 0),
            "ice": int(ice or 0),
            "pending_active": int(pend or 0),
        }
    return status_map, breakdown_map


async def _aggregate_monthly_by_sales(
    db, *, month_start: date, month_end: date
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    status_res = await db.execute(
        select(ContactTask.sales_wechat_id, ContactTask.status, func.count(ContactTask.id))
        .where(ContactTask.period_type == PERIOD_DAILY)
        .where(ContactTask.due_date >= month_start)
        .where(ContactTask.due_date <= month_end)
        .group_by(ContactTask.sales_wechat_id, ContactTask.status)
    )
    status_map: dict[str, dict[str, int]] = {}
    for sw, status, cnt in status_res.all():
        key = (sw or "").strip()
        if not key:
            continue
        status_map.setdefault(key, {})[str(status or "pending")] = int(cnt or 0)

    breakdown_res = await db.execute(
        select(
            ContactTask.sales_wechat_id,
            func.sum(
                case(
                    (
                        and_(
                            ContactTask.task_kind != "icebreaker",
                            ContactTask.contact_channel != "phone",
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (
                        and_(
                            ContactTask.task_kind != "icebreaker",
                            ContactTask.contact_channel == "phone",
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            func.sum(case((ContactTask.task_kind == "icebreaker", 1), else_=0)),
            func.sum(
                case((ContactTask.status.in_(("pending", "in_progress")), 1), else_=0)
            ),
        )
        .where(ContactTask.period_type == PERIOD_DAILY)
        .where(ContactTask.due_date >= month_start)
        .where(ContactTask.due_date <= month_end)
        .group_by(ContactTask.sales_wechat_id)
    )
    breakdown_map: dict[str, dict[str, int]] = {}
    for sw, mw, mp, ice, pend in breakdown_res.all():
        key = (sw or "").strip()
        if not key:
            continue
        breakdown_map[key] = {
            "main_wechat": int(mw or 0),
            "main_phone": int(mp or 0),
            "ice": int(ice or 0),
            "pending_active": int(pend or 0),
        }
    return status_map, breakdown_map


async def query_task_monitor(
    db,
    *,
    period: str,
    ref_date: date,
    batch_status: str = "active",
    ref_date_explicit: bool = False,
) -> dict:
    period = (period or PERIOD_DAILY).strip() or PERIOD_DAILY
    if batch_status not in _BATCH_STATUS_QUERY_VALUES:
        batch_status = "active"

    p_start, p_end = period_bounds(period, ref_date)
    is_historical = bool(ref_date_explicit) and not is_current_period(period, p_start)
    is_current = is_current_period(period, p_start)
    labels = await _load_sales_labels(db)
    items: list[dict] = []

    if period == PERIOD_MONTHLY:
        status_map, breakdown_map = await _aggregate_monthly_by_sales(
            db, month_start=p_start, month_end=p_end
        )
        sales_ids = set(status_map.keys()) | set(breakdown_map.keys())
        for sw in sorted(sales_ids):
            counts = status_map.get(sw, {})
            stats = stats_from_counts(counts)
            if stats["total"] <= 0:
                continue
            br = breakdown_map.get(sw, {})
            meta = labels.get(sw, {"sales_wechat_id": sw, "nickname": None, "label": sw})
            items.append(
                {
                    **meta,
                    "batch_id": None,
                    "batch_status": None,
                    "view_mode": "month_progress",
                    "stats": stats,
                    "main_wechat": br.get("main_wechat", 0),
                    "main_phone": br.get("main_phone", 0),
                    "ice": br.get("ice", 0),
                    "pending_active": br.get("pending_active", 0),
                }
            )
    else:
        batches = await _pick_batches_for_period(
            db,
            period_type=period,
            period_start=p_start,
            batch_status=batch_status,
            is_current=is_current,
        )
        batch_ids = [b.id for b in batches.values() if b.id]
        status_map, breakdown_map = await _aggregate_batch_task_metrics(db, batch_ids)

        for sw in sorted(batches.keys()):
            batch = batches[sw]
            if batch.status == "generating":
                meta = labels.get(sw, {"sales_wechat_id": sw, "nickname": None, "label": sw})
                items.append(
                    {
                        **meta,
                        "batch_id": batch.id,
                        "batch_status": batch.status,
                        "view_mode": "generating",
                        "stats": {
                            "total": 0,
                            "done": 0,
                            "pending": 0,
                            "in_progress": 0,
                            "skipped": 0,
                            "overdue": 0,
                            "completion_rate": 0,
                        },
                        "main_wechat": 0,
                        "main_phone": 0,
                        "ice": 0,
                        "pending_active": 0,
                    }
                )
                continue

            counts = status_map.get(int(batch.id), {})
            stats = stats_from_counts(counts)
            if stats["total"] <= 0:
                continue
            br = breakdown_map.get(int(batch.id), {})
            view_mode = "historical" if is_historical else "current"
            meta = labels.get(sw, {"sales_wechat_id": sw, "nickname": None, "label": sw})
            items.append(
                {
                    **meta,
                    "batch_id": batch.id,
                    "batch_status": batch.status,
                    "view_mode": view_mode,
                    "stats": stats,
                    "main_wechat": br.get("main_wechat", 0),
                    "main_phone": br.get("main_phone", 0),
                    "ice": br.get("ice", 0),
                    "pending_active": br.get("pending_active", 0),
                }
            )

    items.sort(
        key=lambda x: (
            -(x.get("stats") or {}).get("pending", 0)
            - (x.get("stats") or {}).get("overdue", 0),
            (x.get("stats") or {}).get("completion_rate", 0),
            x.get("label") or x.get("sales_wechat_id") or "",
        )
    )

    return {
        "period_type": period,
        "period_start": p_start.isoformat(),
        "period_end": p_end.isoformat(),
        "ref_date": ref_date.isoformat(),
        "is_historical": is_historical,
        "batch_status_filter": batch_status,
        "summary": merge_summary(items),
        "items": items,
    }
