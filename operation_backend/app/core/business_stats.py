"""经营大屏聚合（成单 / 好友 / 外呼 + 精简使用率）。上海自然日。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dept_tree import descendant_dept_ids
from app.core.usage_stats import (
    ONLINE_WITHIN_SECONDS,
    is_user_online,
    shanghai_day_start,
    window_since,
)
from app.models import (
    CampaignBlastJob,
    CampaignBlastRecipient,
    ChatMessage,
    ContactTask,
    OpDepartment,
    OpDepartmentMember,
    PhoneCallRecord,
    RawCustomerSalesWechat,
    RawOrder,
    User,
    UserActivityEvent,
    UserSalesWechat,
    WechatOutboundAction,
)

# status_name 含这些关键词的订单不计入成单
_CANCEL_KEYWORDS = ("取消", "退款", "关闭", "作废")


def _money(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _pct_change(cur: float, prev: float) -> float | None:
    """较上期涨跌百分比；上期为 0 时：当前>0 视为 +100，否则 0。"""
    if prev == 0:
        if cur == 0:
            return 0.0
        return 100.0
    return round((cur - prev) / prev * 100.0, 1)


def _valid_order_filter():
    """有效订单：非取消/退款/关闭/作废；空 status 仍计入。"""
    parts = [RawOrder.status_name.like(f"%{kw}%") for kw in _CANCEL_KEYWORDS]
    return or_(RawOrder.status_name.is_(None), RawOrder.status_name == "", not_(or_(*parts)))


async def _staff_maps(
    db: AsyncSession, visible_user_ids: frozenset[int]
) -> tuple[dict[str, int], dict[int, int | None], dict[int, str]]:
    """返回 (mibuddy_uuid→user_id, user_id→dept_id, user_id→name)。"""
    ids = list(visible_user_ids) or [-1]
    users = (
        await db.execute(
            select(User.id, User.mibuddy_uuid, User.real_name, User.username).where(
                User.id.in_(ids)
            )
        )
    ).all()
    uuid_to_uid: dict[str, int] = {}
    uid_to_name: dict[int, str] = {}
    for uid, mu, real_name, username in users:
        uid = int(uid)
        uid_to_name[uid] = (real_name or username or f"user#{uid}").strip()
        key = (mu or "").strip()
        if key:
            uuid_to_uid[key] = uid

    member_dept = {
        int(m.user_id): int(m.department_id)
        for m in (
            await db.execute(
                select(OpDepartmentMember).where(OpDepartmentMember.user_id.in_(ids))
            )
        )
        .scalars()
        .all()
    }
    return uuid_to_uid, member_dept, uid_to_name


async def _sales_wechat_to_user(
    db: AsyncSession, visible_user_ids: frozenset[int]
) -> dict[str, int]:
    rows = (
        await db.execute(
            select(UserSalesWechat.sales_wechat_id, UserSalesWechat.user_id).where(
                UserSalesWechat.user_id.in_(list(visible_user_ids) or [-1])
            )
        )
    ).all()
    return {str(sw).strip(): int(uid) for sw, uid in rows if sw and uid}


async def _load_orders_rows(
    db: AsyncSession, *, since: datetime
) -> list[tuple[Any, Any, Any, Any]]:
    """一次拉出窗口内有效订单：(order_time, staff_uuid, pay_amount, pay_type_name)。"""
    return list(
        (
            await db.execute(
                select(
                    RawOrder.order_time,
                    RawOrder.staff_uuid,
                    RawOrder.pay_amount,
                    RawOrder.pay_type_name,
                ).where(
                    RawOrder.order_time.isnot(None),
                    RawOrder.order_time >= since,
                    _valid_order_filter(),
                )
            )
        ).all()
    )


def _bucket_orders(
    rows: list[tuple[Any, Any, Any, Any]],
    *,
    since: datetime,
    until: datetime | None,
    uuid_to_uid: dict[str, int],
    include_unassigned: bool,
) -> tuple[float, int, dict[int, tuple[float, int]], float, int]:
    per_uid: dict[int, list[float]] = {}
    un_gmv = 0.0
    un_cnt = 0
    for order_time, staff_uuid, pay_amount, _pay_type in rows:
        if order_time is None or order_time < since:
            continue
        if until is not None and order_time >= until:
            continue
        g = _money(pay_amount)
        key = (staff_uuid or "").strip()
        uid = uuid_to_uid.get(key) if key else None
        if uid is None:
            un_gmv += g
            un_cnt += 1
            continue
        bucket = per_uid.setdefault(uid, [0.0, 0.0])
        bucket[0] += g
        bucket[1] += 1

    matched_gmv = sum(v[0] for v in per_uid.values())
    matched_cnt = int(sum(v[1] for v in per_uid.values()))
    total_gmv = matched_gmv + (un_gmv if include_unassigned else 0.0)
    total_cnt = matched_cnt + (un_cnt if include_unassigned else 0)
    out_per = {uid: (vals[0], int(vals[1])) for uid, vals in per_uid.items()}
    return total_gmv, total_cnt, out_per, un_gmv, un_cnt


def _orders_pay_types(
    rows: list[tuple[Any, Any, Any, Any]],
    *,
    since: datetime,
    until: datetime | None,
    uuid_to_uid: dict[str, int],
    include_unassigned: bool,
) -> list[dict[str, Any]]:
    agg: dict[str, list[float]] = {}
    for order_time, staff_uuid, pay_amount, pay_type in rows:
        if order_time is None or order_time < since:
            continue
        if until is not None and order_time >= until:
            continue
        key = (staff_uuid or "").strip()
        uid = uuid_to_uid.get(key) if key else None
        if uid is None and not include_unassigned:
            continue
        label = (pay_type or "").strip() or "未知"
        bucket = agg.setdefault(label, [0.0, 0.0])
        bucket[0] += _money(pay_amount)
        bucket[1] += 1
    items = [
        {"name": name, "gmv": round(vals[0], 2), "count": int(vals[1])}
        for name, vals in agg.items()
    ]
    items.sort(key=lambda x: x["gmv"], reverse=True)
    return items[:12]


def _orders_daily_trend(
    rows: list[tuple[Any, Any, Any, Any]],
    *,
    days: int,
    uuid_to_uid: dict[str, int],
    include_unassigned: bool,
) -> dict[str, list]:
    by_day: dict[str, list[float]] = {}
    since = window_since(days)
    for order_time, staff_uuid, pay_amount, _ in rows:
        if order_time is None or order_time < since:
            continue
        key_day = order_time.strftime("%Y-%m-%d")
        key = (staff_uuid or "").strip()
        uid = uuid_to_uid.get(key) if key else None
        if uid is None and not include_unassigned:
            continue
        bucket = by_day.setdefault(key_day, [0.0, 0.0])
        bucket[0] += _money(pay_amount)
        bucket[1] += 1

    labels: list[str] = []
    gmv_series: list[float] = []
    cnt_series: list[int] = []
    for i in range(days):
        d0 = shanghai_day_start(days - 1 - i)
        full = d0.strftime("%Y-%m-%d")
        labels.append(d0.strftime("%m-%d"))
        vals = by_day.get(full, [0.0, 0.0])
        gmv_series.append(round(vals[0], 2))
        cnt_series.append(int(vals[1]))
    return {"labels": labels, "gmv": gmv_series, "count": cnt_series}


async def _load_friend_rows(
    db: AsyncSession, *, since: datetime
) -> list[tuple[Any, Any]]:
    return list(
        (
            await db.execute(
                select(
                    RawCustomerSalesWechat.add_time,
                    RawCustomerSalesWechat.sales_wechat_id,
                ).where(
                    RawCustomerSalesWechat.add_time.isnot(None),
                    RawCustomerSalesWechat.add_time >= since,
                    RawCustomerSalesWechat.is_deleted.is_(False),
                )
            )
        ).all()
    )


def _bucket_friends(
    rows: list[tuple[Any, Any]],
    *,
    since: datetime,
    until: datetime | None,
    sw_to_uid: dict[str, int],
    include_unassigned: bool,
) -> tuple[int, dict[int, int], int]:
    per_uid: dict[int, int] = {}
    un_cnt = 0
    for add_time, sw in rows:
        if add_time is None or add_time < since:
            continue
        if until is not None and add_time >= until:
            continue
        key = (sw or "").strip()
        uid = sw_to_uid.get(key) if key else None
        if uid is None:
            un_cnt += 1
            continue
        per_uid[uid] = per_uid.get(uid, 0) + 1
    matched = sum(per_uid.values())
    total = matched + (un_cnt if include_unassigned else 0)
    return total, per_uid, un_cnt


def _friends_daily_trend(
    rows: list[tuple[Any, Any]],
    *,
    days: int,
    sw_to_uid: dict[str, int],
    include_unassigned: bool,
) -> dict[str, list]:
    by_day: dict[str, int] = {}
    since = window_since(days)
    for add_time, sw in rows:
        if add_time is None or add_time < since:
            continue
        key_day = add_time.strftime("%Y-%m-%d")
        key = (sw or "").strip()
        uid = sw_to_uid.get(key) if key else None
        if uid is None and not include_unassigned:
            continue
        by_day[key_day] = by_day.get(key_day, 0) + 1

    labels: list[str] = []
    series: list[int] = []
    for i in range(days):
        d0 = shanghai_day_start(days - 1 - i)
        full = d0.strftime("%Y-%m-%d")
        labels.append(d0.strftime("%m-%d"))
        series.append(by_day.get(full, 0))
    return {"labels": labels, "friends": series}


async def _call_stats(
    db: AsyncSession,
    *,
    since: datetime,
    until: datetime | None,
    uuid_to_uid: dict[str, int],
    include_unassigned: bool,
) -> dict[str, Any]:
    conds = [PhoneCallRecord.create_time >= since]
    if until is not None:
        conds.append(PhoneCallRecord.create_time < until)

    rows = (
        await db.execute(
            select(
                PhoneCallRecord.staff_uuid,
                func.count(PhoneCallRecord.call_id),
                func.coalesce(func.sum(PhoneCallRecord.call_seconds), 0),
            )
            .where(and_(*conds))
            .group_by(PhoneCallRecord.staff_uuid)
        )
    ).all()

    matched_calls = 0
    matched_secs = 0
    un_calls = 0
    un_secs = 0
    for staff_uuid, cnt, secs in rows:
        c = int(cnt or 0)
        s = int(secs or 0)
        key = (staff_uuid or "").strip()
        uid = uuid_to_uid.get(key) if key else None
        if uid is None:
            un_calls += c
            un_secs += s
        else:
            matched_calls += c
            matched_secs += s

    return {
        "call_count": matched_calls + (un_calls if include_unassigned else 0),
        "call_seconds": matched_secs + (un_secs if include_unassigned else 0),
        "unassigned_call_count": un_calls if include_unassigned else 0,
        "unassigned_call_seconds": un_secs if include_unassigned else 0,
    }


async def _resolve_sales_dept_id(db: AsyncSession) -> int | None:
    """优先精确名「销售部」，再 kind=sales，再名称含「销售」。"""
    rows = (
        await db.execute(
            select(OpDepartment.id, OpDepartment.name, OpDepartment.kind).order_by(
                OpDepartment.sort_order, OpDepartment.id
            )
        )
    ).all()
    for did, name, _kind in rows:
        if (name or "").strip() == "销售部":
            return int(did)
    for did, _name, kind in rows:
        if (kind or "").strip().lower() == "sales":
            return int(did)
    for did, name, _kind in rows:
        if "销售" in (name or ""):
            return int(did)
    return None


async def _dept_gmv_bars(
    db: AsyncSession,
    *,
    per_uid: dict[int, tuple[float, int]],
    member_dept: dict[int, int | None],
    root_dept_id: int | None,
    unassigned_gmv: float,
    unassigned_cnt: int,
    include_unassigned: bool,
    people_unassigned: bool = False,
) -> list[dict[str, Any]]:
    # 筛选「未分配人员」时：按人员合计展示一桶
    if people_unassigned:
        gmv = sum(v[0] for v in per_uid.values())
        cnt = int(sum(v[1] for v in per_uid.values()))
        return [{"id": None, "name": "未分配人员", "gmv": round(gmv, 2), "count": cnt}]

    depts = (
        await db.execute(
            select(OpDepartment).order_by(OpDepartment.sort_order, OpDepartment.id)
        )
    ).scalars().all()
    dept_by_id = {int(d.id): d for d in depts}

    if root_dept_id is not None:
        child_ids = [
            int(d.id)
            for d in depts
            if d.parent_id is not None and int(d.parent_id) == int(root_dept_id)
        ]
        if not child_ids:
            child_ids = [int(root_dept_id)]
    else:
        child_ids = [int(d.id) for d in depts if d.parent_id is None]

    bars: list[dict[str, Any]] = []
    for did in child_ids:
        desc = set(await descendant_dept_ids(db, did))
        gmv = 0.0
        cnt = 0
        for uid, (ug, uc) in per_uid.items():
            md = member_dept.get(uid)
            if md is not None and int(md) in desc:
                gmv += ug
                cnt += uc
        name = dept_by_id[did].name if did in dept_by_id else f"dept#{did}"
        bars.append({"id": did, "name": name, "gmv": round(gmv, 2), "count": cnt})

    bars.sort(key=lambda x: x["gmv"], reverse=True)
    if include_unassigned and (unassigned_gmv > 0 or unassigned_cnt > 0):
        bars.append(
            {
                "id": None,
                "name": "未归属",
                "gmv": round(unassigned_gmv, 2),
                "count": unassigned_cnt,
            }
        )
    return bars[:15]


async def aggregate_usage_compact(
    db: AsyncSession,
    *,
    visible_user_ids: frozenset[int],
    days: int = 7,
) -> dict[str, Any]:
    """精简使用率：覆盖 KPI + 活跃构成，不建人员全表/部门树。"""
    since = window_since(days)
    ids = list(visible_user_ids)
    empty = {
        "scope_users": 0,
        "login_dau": 0,
        "work_dau": 0,
        "dau_union": 0,
        "zero_active": 0,
        "online_now": 0,
        "online_within_seconds": ONLINE_WITHIN_SECONDS,
        "active_mix": {
            "login_only": 0,
            "work_only": 0,
            "both": 0,
            "zero": 0,
        },
    }
    if not ids:
        return empty

    users = (
        await db.execute(
            select(User.id, User.last_seen_at).where(User.id.in_(ids))
        )
    ).all()
    now_naive = datetime.now()

    login_by = {
        int(r[0])
        for r in (
            await db.execute(
                select(UserActivityEvent.user_id)
                .where(
                    UserActivityEvent.occurred_at >= since,
                    UserActivityEvent.user_id.in_(ids),
                    UserActivityEvent.event_type == "login_success",
                )
                .distinct()
            )
        ).all()
        if r[0] is not None
    }

    chat_uids = {
        int(r[0])
        for r in (
            await db.execute(
                select(ChatMessage.user_id)
                .where(ChatMessage.created_at >= since, ChatMessage.user_id.in_(ids))
                .distinct()
            )
        ).all()
        if r[0] is not None
    }
    out_uids = {
        int(r[0])
        for r in (
            await db.execute(
                select(WechatOutboundAction.actor_user_id)
                .where(
                    WechatOutboundAction.created_at >= since,
                    WechatOutboundAction.actor_user_id.in_(ids),
                )
                .distinct()
            )
        ).all()
        if r[0] is not None
    }
    task_uids = {
        int(r[0])
        for r in (
            await db.execute(
                select(ContactTask.completed_by_user_id)
                .where(
                    ContactTask.completed_at >= since,
                    ContactTask.completed_by_user_id.in_(ids),
                    ContactTask.completed_at.isnot(None),
                )
                .distinct()
            )
        ).all()
        if r[0] is not None
    }
    blast_uids = {
        int(r[0])
        for r in (
            await db.execute(
                select(CampaignBlastJob.user_id)
                .select_from(CampaignBlastRecipient)
                .join(CampaignBlastJob, CampaignBlastJob.id == CampaignBlastRecipient.job_id)
                .where(
                    CampaignBlastJob.user_id.in_(ids),
                    CampaignBlastRecipient.status == "sent",
                    CampaignBlastRecipient.sent_at >= since,
                )
                .distinct()
            )
        ).all()
        if r[0] is not None
    }
    work_evt_uids = {
        int(r[0])
        for r in (
            await db.execute(
                select(UserActivityEvent.user_id)
                .where(
                    UserActivityEvent.occurred_at >= since,
                    UserActivityEvent.user_id.in_(ids),
                    UserActivityEvent.event_type.in_(
                        (
                            "product_search",
                            "product_copy_image",
                            "product_copy_link",
                            "product_open_url",
                            "phone_dial",
                        )
                    ),
                )
                .distinct()
            )
        ).all()
        if r[0] is not None
    }

    work_uids = chat_uids | out_uids | task_uids | blast_uids | work_evt_uids
    both = login_by & work_uids
    login_only = login_by - work_uids
    work_only = work_uids - login_by
    dau_union = login_by | work_uids
    scope_users = len(ids)
    zero = scope_users - len(dau_union)
    online_now = sum(
        1 for _, last_seen in users if is_user_online(last_seen, now=now_naive)
    )

    return {
        "scope_users": scope_users,
        "login_dau": len(login_by),
        "work_dau": len(work_uids),
        "dau_union": len(dau_union),
        "zero_active": max(0, zero),
        "online_now": online_now,
        "online_within_seconds": ONLINE_WITHIN_SECONDS,
        "active_mix": {
            "login_only": len(login_only),
            "work_only": len(work_only),
            "both": len(both),
            "zero": max(0, zero),
        },
    }


async def aggregate_overview(
    db: AsyncSession,
    *,
    visible_user_ids: frozenset[int],
    days: int = 7,
    scope: dict[str, Any] | None = None,
    root_dept_id: int | None = None,
    gmv_root_dept_id: int | None = None,
    unassigned_only: bool = False,
    is_admin: bool = False,
) -> dict[str, Any]:
    """经营大屏主数据。"""
    days = max(1, min(90, int(days or 7)))
    since = window_since(days)
    # 上一段同等窗口：再往前 days 天
    prev_since = window_since(days * 2)
    prev_until = since
    today0 = shanghai_day_start(0)
    tomorrow0 = today0 + timedelta(days=1)
    yesterday0 = shanghai_day_start(1)

    scope_info = scope or {
        "role": None,
        "dept_id": None,
        "dept_name": None,
        "include_descendants": True,
        "unassigned": False,
    }

    # 全部视角（管理员未筛部门/未分配人员）才把未归属订单计入总额
    include_unassigned = bool(
        is_admin and not unassigned_only and scope_info.get("dept_id") is None
    )

    # 部门成单柱图根：显式 gmv_root > 页头筛选根 > 默认销售部（仅全部视角）
    gmv_root = gmv_root_dept_id if gmv_root_dept_id is not None else root_dept_id
    if gmv_root is None and include_unassigned and not unassigned_only:
        gmv_root = await _resolve_sales_dept_id(db)

    gmv_root_name: str | None = None
    if gmv_root is not None:
        dept = await db.get(OpDepartment, int(gmv_root))
        gmv_root_name = dept.name if dept else None

    uuid_to_uid, member_dept, uid_to_name = await _staff_maps(db, visible_user_ids)
    sw_to_uid = await _sales_wechat_to_user(db, visible_user_ids)

    # 一次拉 prev_since～今：覆盖上期、本期、今日、昨日、趋势、支付方式
    order_rows = await _load_orders_rows(db, since=prev_since)
    friend_rows = await _load_friend_rows(db, since=prev_since)

    gmv, order_cnt, per_uid, un_gmv, un_cnt = _bucket_orders(
        order_rows,
        since=since,
        until=None,
        uuid_to_uid=uuid_to_uid,
        include_unassigned=include_unassigned,
    )
    prev_gmv, prev_cnt, _, _, _ = _bucket_orders(
        order_rows,
        since=prev_since,
        until=prev_until,
        uuid_to_uid=uuid_to_uid,
        include_unassigned=include_unassigned,
    )
    today_gmv, today_cnt, _, _, _ = _bucket_orders(
        order_rows,
        since=today0,
        until=tomorrow0,
        uuid_to_uid=uuid_to_uid,
        include_unassigned=include_unassigned,
    )
    yday_gmv, _, _, _, _ = _bucket_orders(
        order_rows,
        since=yesterday0,
        until=today0,
        uuid_to_uid=uuid_to_uid,
        include_unassigned=include_unassigned,
    )

    friends, friend_by_uid, un_friends = _bucket_friends(
        friend_rows,
        since=since,
        until=None,
        sw_to_uid=sw_to_uid,
        include_unassigned=include_unassigned,
    )
    prev_friends, _, _ = _bucket_friends(
        friend_rows,
        since=prev_since,
        until=prev_until,
        sw_to_uid=sw_to_uid,
        include_unassigned=include_unassigned,
    )

    aov = round(gmv / order_cnt, 2) if order_cnt else 0.0

    calls = await _call_stats(
        db,
        since=since,
        until=None,
        uuid_to_uid=uuid_to_uid,
        include_unassigned=include_unassigned,
    )

    pay_types = _orders_pay_types(
        order_rows,
        since=since,
        until=None,
        uuid_to_uid=uuid_to_uid,
        include_unassigned=include_unassigned,
    )

    order_trend = _orders_daily_trend(
        order_rows,
        days=days,
        uuid_to_uid=uuid_to_uid,
        include_unassigned=include_unassigned,
    )
    friend_trend = _friends_daily_trend(
        friend_rows,
        days=days,
        sw_to_uid=sw_to_uid,
        include_unassigned=include_unassigned,
    )

    dept_gmv = await _dept_gmv_bars(
        db,
        per_uid=per_uid,
        member_dept=member_dept,
        root_dept_id=gmv_root,
        unassigned_gmv=un_gmv,
        unassigned_cnt=un_cnt,
        include_unassigned=include_unassigned,
        people_unassigned=unassigned_only,
    )

    top_staff = []
    for uid, (ug, uc) in per_uid.items():
        top_staff.append(
            {
                "user_id": uid,
                "name": uid_to_name.get(uid, f"user#{uid}"),
                "gmv": round(ug, 2),
                "order_count": uc,
                "friends": friend_by_uid.get(uid, 0),
            }
        )
    top_staff.sort(key=lambda x: x["gmv"], reverse=True)
    top_staff = top_staff[:10]

    usage = await aggregate_usage_compact(
        db, visible_user_ids=visible_user_ids, days=days
    )

    return {
        "days": days,
        "since": since.isoformat(sep=" "),
        "scope": scope_info,
        "gmv_root_dept_id": gmv_root,
        "gmv_root_dept_name": gmv_root_name,
        "kpis": {
            "gmv": round(gmv, 2),
            "gmv_change": _pct_change(gmv, prev_gmv),
            "today_gmv": round(today_gmv, 2),
            "today_gmv_change": _pct_change(today_gmv, yday_gmv),
            "today_order_count": today_cnt,
            "order_count": order_cnt,
            "order_count_change": _pct_change(float(order_cnt), float(prev_cnt)),
            "aov": aov,
            "friends": friends,
            "friends_change": _pct_change(float(friends), float(prev_friends)),
            "unassigned_gmv": round(un_gmv, 2) if include_unassigned else 0.0,
            "unassigned_order_count": un_cnt if include_unassigned else 0,
            "unassigned_friends": un_friends if include_unassigned else 0,
        },
        "calls": calls,
        "pay_types": pay_types,
        "order_trend": order_trend,
        "friend_trend": friend_trend,
        "dept_gmv": dept_gmv,
        "top_staff": top_staff,
        "usage": usage,
    }
