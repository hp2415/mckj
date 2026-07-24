"""任务监测：汇总各销售微信号的任务完成情况。"""
from __future__ import annotations

import csv
import io
from datetime import date

from sqlalchemy import and_, case, func, select

from ai.task_allocation import PERIOD_DAILY, PERIOD_MONTHLY, period_bounds, today_shanghai
from models import ContactTask, SalesWechatAccount, TaskAllocationBatch, User, UserSalesWechat

# 导出日期区间上限（天），防止一次扫过大范围
EXPORT_MAX_RANGE_DAYS = 366

_BATCH_STATUS_QUERY_VALUES = frozenset({"active", "all", "draft", "published", "archived"})
_TASK_CATEGORY_QUERY_VALUES = frozenset({"all", "main", "icebreaker"})


def resolve_task_category(task_category: str) -> str:
    s = (task_category or "").strip().lower()
    if s in _TASK_CATEGORY_QUERY_VALUES:
        return s
    return "all"


def _task_category_clause(task_category: str):
    """按任务类别过滤 ContactTask；all 时返回 None。"""
    cat = resolve_task_category(task_category)
    if cat == "main":
        return ContactTask.task_kind != "icebreaker"
    if cat == "icebreaker":
        return ContactTask.task_kind == "icebreaker"
    return None


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
        "skip_rate": round(skipped / max(1, total), 4),
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
    total = totals["total"]
    denom = max(1, total - skipped)
    return {
        **totals,
        "sales_count": len(items),
        "completion_rate": round(totals["done"] / denom, 4),
        "skip_rate": round(skipped / max(1, total), 4),
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
            "staff_name": None,
            "label": _sales_label(
                sw,
                nickname=acc.nickname,
                alias_name=acc.alias_name,
                account_code=acc.account_code,
            ),
        }

    bind_res = await db.execute(
        select(UserSalesWechat.sales_wechat_id, User.real_name, UserSalesWechat.is_primary)
        .join(User, User.id == UserSalesWechat.user_id)
        .order_by(UserSalesWechat.is_primary.desc(), UserSalesWechat.id)
    )
    for sw, real_name, _ in bind_res.all():
        key = (sw or "").strip()
        name = (real_name or "").strip()
        if not key or not name:
            continue
        if key in out and out[key].get("staff_name"):
            continue
        if key in out:
            out[key]["staff_name"] = name
        else:
            out[key] = {
                "sales_wechat_id": key,
                "nickname": None,
                "staff_name": name,
                "label": key,
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
    db, batch_ids: list[int], *, task_category: str = "all"
) -> tuple[dict[int, dict[str, int]], dict[int, dict[str, int]]]:
    if not batch_ids:
        return {}, {}

    cat_clause = _task_category_clause(task_category)
    status_q = (
        select(ContactTask.batch_id, ContactTask.status, func.count(ContactTask.id))
        .where(ContactTask.batch_id.in_(batch_ids))
        .where(ContactTask.status != "reserve")
    )
    if cat_clause is not None:
        status_q = status_q.where(cat_clause)
    status_res = await db.execute(status_q.group_by(ContactTask.batch_id, ContactTask.status))
    status_map: dict[int, dict[str, int]] = {}
    for batch_id, status, cnt in status_res.all():
        bid = int(batch_id)
        status_map.setdefault(bid, {})[str(status or "pending")] = int(cnt or 0)

    breakdown_q = select(
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
    ).where(ContactTask.batch_id.in_(batch_ids)).where(ContactTask.status != "reserve")
    if cat_clause is not None:
        breakdown_q = breakdown_q.where(cat_clause)
    breakdown_res = await db.execute(breakdown_q.group_by(ContactTask.batch_id))
    breakdown_map: dict[int, dict[str, int]] = {}
    for batch_id, mw, mp, ice, pend in breakdown_res.all():
        breakdown_map[int(batch_id)] = {
            "main_wechat": int(mw or 0),
            "main_phone": int(mp or 0),
            "ice": int(ice or 0),
            "pending_active": int(pend or 0),
        }
    return status_map, breakdown_map


async def _aggregate_by_sales_due_range(
    db, *, date_from: date, date_to: date, task_category: str = "all"
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """按日任务 due_date 区间汇总各销售完成情况（与月进度口径一致）。"""
    cat_clause = _task_category_clause(task_category)
    status_q = (
        select(ContactTask.sales_wechat_id, ContactTask.status, func.count(ContactTask.id))
        .where(ContactTask.period_type == PERIOD_DAILY)
        .where(ContactTask.due_date >= date_from)
        .where(ContactTask.due_date <= date_to)
        .where(ContactTask.status != "reserve")
    )
    if cat_clause is not None:
        status_q = status_q.where(cat_clause)
    status_res = await db.execute(status_q.group_by(ContactTask.sales_wechat_id, ContactTask.status))
    status_map: dict[str, dict[str, int]] = {}
    for sw, status, cnt in status_res.all():
        key = (sw or "").strip()
        if not key:
            continue
        status_map.setdefault(key, {})[str(status or "pending")] = int(cnt or 0)

    breakdown_q = select(
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
    ).where(ContactTask.period_type == PERIOD_DAILY).where(
        ContactTask.due_date >= date_from
    ).where(ContactTask.due_date <= date_to).where(ContactTask.status != "reserve")
    if cat_clause is not None:
        breakdown_q = breakdown_q.where(cat_clause)
    breakdown_res = await db.execute(breakdown_q.group_by(ContactTask.sales_wechat_id))
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


def _items_from_due_range_maps(
    *,
    status_map: dict[str, dict[str, int]],
    breakdown_map: dict[str, dict[str, int]],
    labels: dict[str, dict],
    view_mode: str,
) -> list[dict]:
    items: list[dict] = []
    sales_ids = set(status_map.keys()) | set(breakdown_map.keys())
    for sw in sorted(sales_ids):
        counts = status_map.get(sw, {})
        stats = stats_from_counts(counts)
        if stats["total"] <= 0:
            continue
        br = breakdown_map.get(sw, {})
        meta = labels.get(
            sw,
            {"sales_wechat_id": sw, "nickname": None, "staff_name": None, "label": sw},
        )
        items.append(
            {
                **meta,
                "batch_id": None,
                "batch_status": None,
                "view_mode": view_mode,
                "stats": stats,
                "main_wechat": br.get("main_wechat", 0),
                "main_phone": br.get("main_phone", 0),
                "ice": br.get("ice", 0),
                "pending_active": br.get("pending_active", 0),
            }
        )
    return items


async def query_task_monitor(
    db,
    *,
    period: str,
    ref_date: date,
    batch_status: str = "active",
    task_category: str = "all",
    ref_date_explicit: bool = False,
) -> dict:
    period = (period or PERIOD_DAILY).strip() or PERIOD_DAILY
    if batch_status not in _BATCH_STATUS_QUERY_VALUES:
        batch_status = "active"
    task_category = resolve_task_category(task_category)

    p_start, p_end = period_bounds(period, ref_date)
    is_historical = bool(ref_date_explicit) and not is_current_period(period, p_start)
    is_current = is_current_period(period, p_start)
    labels = await _load_sales_labels(db)
    items: list[dict] = []

    if period == PERIOD_MONTHLY:
        status_map, breakdown_map = await _aggregate_by_sales_due_range(
            db, date_from=p_start, date_to=p_end, task_category=task_category
        )
        items = _items_from_due_range_maps(
            status_map=status_map,
            breakdown_map=breakdown_map,
            labels=labels,
            view_mode="month_progress",
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
        status_map, breakdown_map = await _aggregate_batch_task_metrics(
            db, batch_ids, task_category=task_category
        )

        for sw in sorted(batches.keys()):
            batch = batches[sw]
            if batch.status == "generating":
                meta = labels.get(
                    sw,
                    {"sales_wechat_id": sw, "nickname": None, "staff_name": None, "label": sw},
                )
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
                            "skip_rate": 0,
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
            meta = labels.get(
                sw,
                {"sales_wechat_id": sw, "nickname": None, "staff_name": None, "label": sw},
            )
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
        "task_category": task_category,
        "summary": merge_summary(items),
        "items": items,
    }


async def query_task_monitor_range(
    db,
    *,
    date_from: date,
    date_to: date,
    task_category: str = "all",
) -> dict:
    """按任意日期区间汇总各销售日任务完成情况（用于导出）。"""
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    task_category = resolve_task_category(task_category)
    labels = await _load_sales_labels(db)
    status_map, breakdown_map = await _aggregate_by_sales_due_range(
        db, date_from=date_from, date_to=date_to, task_category=task_category
    )
    items = _items_from_due_range_maps(
        status_map=status_map,
        breakdown_map=breakdown_map,
        labels=labels,
        view_mode="date_range",
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
        "period_type": "range",
        "period_start": date_from.isoformat(),
        "period_end": date_to.isoformat(),
        "ref_date": date_to.isoformat(),
        "is_historical": True,
        "batch_status_filter": None,
        "task_category": task_category,
        "summary": merge_summary(items),
        "items": items,
    }


_TASK_CATEGORY_CSV_LABELS = {
    "all": "全部",
    "main": "主线任务",
    "icebreaker": "激活任务",
}


def build_task_monitor_csv(data: dict) -> bytes:
    """生成带 UTF-8 BOM 的 CSV，便于 Excel 打开中文。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    date_from = data.get("period_start") or ""
    date_to = data.get("period_end") or ""
    cat = data.get("task_category") or "all"
    cat_label = _TASK_CATEGORY_CSV_LABELS.get(cat, cat)
    summary = data.get("summary") or {}

    writer.writerow(["任务监测导出"])
    writer.writerow(["区间起", date_from, "区间止", date_to, "任务类别", cat_label])
    writer.writerow(
        [
            "汇总-有任务销售",
            int(summary.get("sales_count") or 0),
            "汇总-任务总数",
            int(summary.get("total") or 0),
            "汇总-已完成",
            int(summary.get("done") or 0),
            "汇总-待办",
            int(summary.get("pending") or 0) + int(summary.get("in_progress") or 0),
            "汇总-逾期",
            int(summary.get("overdue") or 0),
            "汇总-完成率",
            f"{round(float(summary.get('completion_rate') or 0) * 100, 2)}%",
        ]
    )
    writer.writerow([])
    writer.writerow(
        [
            "员工",
            "销售微信ID",
            "昵称/标签",
            "任务总数",
            "已完成",
            "待办",
            "进行中",
            "跳过",
            "逾期",
            "微信主线",
            "电话主线",
            "破冰",
            "完成率",
            "跳过率",
            "区间起",
            "区间止",
        ]
    )
    for it in data.get("items") or []:
        st = it.get("stats") or {}
        rate = float(st.get("completion_rate") or 0)
        skip_rate = float(st.get("skip_rate") or 0)
        writer.writerow(
            [
                (it.get("staff_name") or "").strip(),
                (it.get("sales_wechat_id") or "").strip(),
                (it.get("nickname") or it.get("label") or "").strip(),
                int(st.get("total") or 0),
                int(st.get("done") or 0),
                int(st.get("pending") or 0),
                int(st.get("in_progress") or 0),
                int(st.get("skipped") or 0),
                int(st.get("overdue") or 0),
                int(it.get("main_wechat") or 0),
                int(it.get("main_phone") or 0),
                int(it.get("ice") or 0),
                f"{round(rate * 100, 2)}%",
                f"{round(skip_rate * 100, 2)}%",
                date_from,
                date_to,
            ]
        )
    return ("\ufeff" + buf.getvalue()).encode("utf-8")

