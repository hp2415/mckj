"""使用率聚合（上海自然日）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CampaignBlastJob,
    CampaignBlastRecipient,
    ChatMessage,
    ContactTask,
    OpDepartment,
    OpDepartmentMember,
    User,
    UserActivityEvent,
    WechatOutboundAction,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 桌面心跳约 45–60s 一次；超过 2 分钟无 last_seen 视为离线
ONLINE_WITHIN_SECONDS = 120


def shanghai_day_start(days_ago: int = 0) -> datetime:
    now = datetime.now(SHANGHAI)
    day = (now - timedelta(days=days_ago)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return day.replace(tzinfo=None)


def is_user_online(last_seen_at: datetime | None, *, now: datetime | None = None) -> bool:
    if not last_seen_at:
        return False
    ref = now or datetime.now()
    try:
        delta = (ref - last_seen_at).total_seconds()
    except Exception:
        return False
    return 0 <= delta <= ONLINE_WITHIN_SECONDS


def window_since(days: int) -> datetime:
    days = max(1, min(365, int(days or 7)))
    return shanghai_day_start(days - 1)


def _empty_trend() -> dict[str, list]:
    return {
        "labels": [],
        "chat": [],
        "outbound": [],
        "outbound_sent": [],
        "login": [],
        "task": [],
        "blast": [],
        "phone": [],
        "product_search": [],
    }


async def aggregate_summary(
    db: AsyncSession,
    *,
    visible_user_ids: frozenset[int],
    days: int = 7,
    scope: dict[str, Any] | None = None,
    root_dept_id: int | None = None,
    unassigned_only: bool = False,
) -> dict[str, Any]:
    since = window_since(days)
    ids = list(visible_user_ids)
    scope_info = scope or {
        "role": None,
        "dept_id": None,
        "dept_name": None,
        "include_descendants": True,
        "unassigned": False,
    }
    empty = {
        "days": days,
        "since": since.isoformat(sep=" "),
        "scope": scope_info,
        "scope_users": len(ids),
        "login_dau": 0,
        "work_dau": 0,
        "dau_union": 0,
        "chat_msgs": 0,
        "chat_ai": 0,
        "chat_adopted": 0,
        "outbound_total": 0,
        "outbound_sent": 0,
        "outbound_failed": 0,
        "outbound_blocked": 0,
        "outbound_direct": 0,
        "outbound_edit": 0,
        "outbound_edit_rate": 0.0,
        "task_completed": 0,
        "blast_sent": 0,
        "login_events": 0,
        "product_search": 0,
        "product_copy": 0,
        "product_open": 0,
        "phone_dial": 0,
        "zero_active": 0,
        "online_now": 0,
        "online_within_seconds": ONLINE_WITHIN_SECONDS,
        "people": [],
        "dept_tree": [],
        "unassigned": None,
        "trend": _empty_trend(),
    }
    if not ids:
        return empty

    # chat
    chat_row = (
        await db.execute(
            select(
                func.count(ChatMessage.id),
                func.sum(case((ChatMessage.role == "assistant", 1), else_=0)),
                func.sum(case((ChatMessage.is_copied.is_(True), 1), else_=0)),
            ).where(
                ChatMessage.created_at >= since,
                ChatMessage.user_id.in_(ids),
            )
        )
    ).one()
    chat_msgs = int(chat_row[0] or 0)
    chat_ai = int(chat_row[1] or 0)
    chat_adopted = int(chat_row[2] or 0)

    out_row = (
        await db.execute(
            select(
                func.count(WechatOutboundAction.id),
                func.sum(case((WechatOutboundAction.status == "sent", 1), else_=0)),
                func.sum(case((WechatOutboundAction.status == "failed", 1), else_=0)),
                func.sum(case((WechatOutboundAction.status == "blocked", 1), else_=0)),
                func.sum(case((WechatOutboundAction.action_type == "send", 1), else_=0)),
                func.sum(case((WechatOutboundAction.action_type == "edit_send", 1), else_=0)),
            ).where(
                WechatOutboundAction.created_at >= since,
                WechatOutboundAction.actor_user_id.in_(ids),
            )
        )
    ).one()
    outbound_total = int(out_row[0] or 0)
    outbound_sent = int(out_row[1] or 0)
    outbound_failed = int(out_row[2] or 0)
    outbound_blocked = int(out_row[3] or 0)
    outbound_direct = int(out_row[4] or 0)
    outbound_edit = int(out_row[5] or 0)
    _typed = outbound_direct + outbound_edit
    outbound_edit_rate = round(outbound_edit / _typed, 4) if _typed else 0.0

    task_completed = int(
        (
            await db.execute(
                select(func.count(ContactTask.id)).where(
                    ContactTask.completed_at >= since,
                    ContactTask.completed_by_user_id.in_(ids),
                    ContactTask.status.in_(("done", "completed")),
                )
            )
        ).scalar()
        or 0
    )
    # 兼容 status=completed 以外的命名
    if task_completed == 0:
        task_completed = int(
            (
                await db.execute(
                    select(func.count(ContactTask.id)).where(
                        ContactTask.completed_at >= since,
                        ContactTask.completed_by_user_id.in_(ids),
                        ContactTask.completed_at.isnot(None),
                    )
                )
            ).scalar()
            or 0
        )

    blast_sent = int(
        (
            await db.execute(
                select(func.count(CampaignBlastRecipient.id))
                .select_from(CampaignBlastRecipient)
                .join(CampaignBlastJob, CampaignBlastJob.id == CampaignBlastRecipient.job_id)
                .where(
                    CampaignBlastJob.user_id.in_(ids),
                    CampaignBlastRecipient.status == "sent",
                    CampaignBlastRecipient.sent_at >= since,
                )
            )
        ).scalar()
        or 0
    )

    # activity events
    async def _evt_count(etype: str | list[str]) -> int:
        if isinstance(etype, str):
            cond = UserActivityEvent.event_type == etype
        else:
            cond = UserActivityEvent.event_type.in_(etype)
        return int(
            (
                await db.execute(
                    select(func.count(UserActivityEvent.id)).where(
                        UserActivityEvent.occurred_at >= since,
                        UserActivityEvent.user_id.in_(ids),
                        cond,
                    )
                )
            ).scalar()
            or 0
        )

    login_events = await _evt_count("login_success")
    product_search = await _evt_count("product_search")
    product_copy = await _evt_count(["product_copy_image", "product_copy_link"])
    product_open = await _evt_count("product_open_url")
    phone_dial = await _evt_count("phone_dial")

    # per-user
    users = (
        await db.execute(
            select(User.id, User.username, User.real_name, User.last_seen_at).where(
                User.id.in_(ids)
            )
        )
    ).all()
    now_naive = datetime.now()

    chat_by = {
        int(r[0]): (int(r[1] or 0), int(r[2] or 0), int(r[3] or 0))
        for r in (
            await db.execute(
                select(
                    ChatMessage.user_id,
                    func.count(ChatMessage.id),
                    func.sum(case((ChatMessage.role == "assistant", 1), else_=0)),
                    func.sum(case((ChatMessage.is_copied.is_(True), 1), else_=0)),
                )
                .where(ChatMessage.created_at >= since, ChatMessage.user_id.in_(ids))
                .group_by(ChatMessage.user_id)
            )
        ).all()
        if r[0] is not None
    }
    out_by = {
        int(r[0]): (
            int(r[1] or 0),
            int(r[2] or 0),
            int(r[3] or 0),
            int(r[4] or 0),
            int(r[5] or 0),
            int(r[6] or 0),
        )
        for r in (
            await db.execute(
                select(
                    WechatOutboundAction.actor_user_id,
                    func.count(WechatOutboundAction.id),
                    func.sum(case((WechatOutboundAction.status == "sent", 1), else_=0)),
                    func.sum(case((WechatOutboundAction.status == "failed", 1), else_=0)),
                    func.sum(case((WechatOutboundAction.status == "blocked", 1), else_=0)),
                    func.sum(case((WechatOutboundAction.action_type == "send", 1), else_=0)),
                    func.sum(case((WechatOutboundAction.action_type == "edit_send", 1), else_=0)),
                )
                .where(
                    WechatOutboundAction.created_at >= since,
                    WechatOutboundAction.actor_user_id.in_(ids),
                )
                .group_by(WechatOutboundAction.actor_user_id)
            )
        ).all()
        if r[0] is not None
    }
    task_by = {
        int(r[0]): int(r[1] or 0)
        for r in (
            await db.execute(
                select(ContactTask.completed_by_user_id, func.count(ContactTask.id))
                .where(
                    ContactTask.completed_at >= since,
                    ContactTask.completed_by_user_id.in_(ids),
                    ContactTask.completed_at.isnot(None),
                )
                .group_by(ContactTask.completed_by_user_id)
            )
        ).all()
        if r[0] is not None
    }
    blast_by = {
        int(r[0]): int(r[1] or 0)
        for r in (
            await db.execute(
                select(CampaignBlastJob.user_id, func.count(CampaignBlastRecipient.id))
                .select_from(CampaignBlastRecipient)
                .join(CampaignBlastJob, CampaignBlastJob.id == CampaignBlastRecipient.job_id)
                .where(
                    CampaignBlastJob.user_id.in_(ids),
                    CampaignBlastRecipient.status == "sent",
                    CampaignBlastRecipient.sent_at >= since,
                )
                .group_by(CampaignBlastJob.user_id)
            )
        ).all()
        if r[0] is not None
    }
    async def _evt_by(etype: str | list[str]) -> dict[int, int]:
        if isinstance(etype, str):
            cond = UserActivityEvent.event_type == etype
        else:
            cond = UserActivityEvent.event_type.in_(etype)
        return {
            int(r[0]): int(r[1] or 0)
            for r in (
                await db.execute(
                    select(UserActivityEvent.user_id, func.count(UserActivityEvent.id))
                    .where(
                        UserActivityEvent.occurred_at >= since,
                        UserActivityEvent.user_id.in_(ids),
                        cond,
                    )
                    .group_by(UserActivityEvent.user_id)
                )
            ).all()
            if r[0] is not None
        }

    login_by = await _evt_by("login_success")
    search_by = await _evt_by("product_search")
    copy_by = await _evt_by(["product_copy_image", "product_copy_link"])
    open_by = await _evt_by("product_open_url")
    dial_by = await _evt_by("phone_dial")

    member_dept = {
        int(m.user_id): int(m.department_id)
        for m in (
            await db.execute(
                select(OpDepartmentMember).where(OpDepartmentMember.user_id.in_(ids))
            )
        ).scalars().all()
    }
    depts = (
        await db.execute(
            select(OpDepartment).order_by(OpDepartment.sort_order, OpDepartment.id)
        )
    ).scalars().all()
    dept_by_id = {int(d.id): d for d in depts}

    people = []
    login_uids: set[int] = set()
    work_uids: set[int] = set()
    online_uids: set[int] = set()
    for uid, username, real_name, last_seen_at in users:
        uid = int(uid)
        c_total, c_ai, c_adopt = chat_by.get(uid, (0, 0, 0))
        o_total, o_sent, o_fail, o_block, o_direct, o_edit = out_by.get(
            uid, (0, 0, 0, 0, 0, 0)
        )
        t_done = task_by.get(uid, 0)
        b_sent = blast_by.get(uid, 0)
        logins = login_by.get(uid, 0)
        p_search = search_by.get(uid, 0)
        p_copy = copy_by.get(uid, 0)
        p_open = open_by.get(uid, 0)
        p_dial = dial_by.get(uid, 0)
        o_typed = o_direct + o_edit
        o_edit_rate = round(o_edit / o_typed, 4) if o_typed else 0.0
        score = (
            3 * (1 if logins else 0)
            + 2 * o_sent
            + 2 * t_done
            + 1 * c_total
            + 1 * b_sent
            + 1 * p_search
            + 1 * p_copy
            + 1 * p_open
            + 1 * p_dial
        )
        if logins:
            login_uids.add(uid)
        if any(
            [c_total, o_total, t_done, b_sent, p_search, p_copy, p_open, p_dial]
        ):
            work_uids.add(uid)
        online = is_user_online(last_seen_at, now=now_naive)
        if online:
            online_uids.add(uid)
        did = member_dept.get(uid)
        dept = dept_by_id.get(did) if did else None
        people.append(
            {
                "user_id": uid,
                "username": username or "",
                "name": real_name or username or f"user#{uid}",
                "department_id": did,
                "department_name": dept.name if dept else None,
                "is_online": online,
                "last_seen_at": (
                    last_seen_at.isoformat(sep=" ") if last_seen_at else None
                ),
                "login_count": logins,
                "chat_msgs": c_total,
                "chat_ai": c_ai,
                "chat_adopted": c_adopt,
                "outbound_total": o_total,
                "outbound_sent": o_sent,
                "outbound_failed": o_fail,
                "outbound_blocked": o_block,
                "outbound_direct": o_direct,
                "outbound_edit": o_edit,
                "outbound_edit_rate": o_edit_rate,
                "task_completed": t_done,
                "blast_sent": b_sent,
                "product_search": p_search,
                "product_copy": p_copy,
                "product_open": p_open,
                "phone_dial": p_dial,
                "score": score,
            }
        )
    people.sort(key=lambda x: (-x["score"], -x["chat_msgs"], x["name"]))
    active_uids = login_uids | work_uids
    login_dau = len(login_uids)
    work_dau = len(work_uids)
    online_now = len(online_uids)

    dept_tree, unassigned = _build_dept_usage_tree(
        depts=depts,
        people=people,
        visible_ids=set(ids),
        root_dept_id=root_dept_id,
        unassigned_only=unassigned_only,
    )

    trend = await _build_daily_trend(db, ids=ids, days=days)

    return {
        "days": days,
        "since": since.isoformat(sep=" "),
        "scope": scope_info,
        "scope_users": len(ids),
        "login_dau": login_dau,
        "work_dau": work_dau,
        "dau_union": len(active_uids),
        "chat_msgs": chat_msgs,
        "chat_ai": chat_ai,
        "chat_adopted": chat_adopted,
        "outbound_total": outbound_total,
        "outbound_sent": outbound_sent,
        "outbound_failed": outbound_failed,
        "outbound_blocked": outbound_blocked,
        "outbound_direct": outbound_direct,
        "outbound_edit": outbound_edit,
        "outbound_edit_rate": outbound_edit_rate,
        "task_completed": task_completed,
        "blast_sent": blast_sent,
        "login_events": login_events,
        "product_search": product_search,
        "product_copy": product_copy,
        "product_open": product_open,
        "phone_dial": phone_dial,
        "zero_active": max(0, len(ids) - len(active_uids)),
        "online_now": online_now,
        "online_within_seconds": ONLINE_WITHIN_SECONDS,
        "people": people,
        "dept_tree": dept_tree,
        "unassigned": unassigned,
        "trend": trend,
    }


def _day_key(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


async def _counts_by_day(
    db: AsyncSession,
    *,
    day_col: Any,
    where_clauses: list[Any],
) -> dict[str, int]:
    rows = (
        await db.execute(
            select(day_col, func.count()).where(*where_clauses).group_by(day_col)
        )
    ).all()
    out: dict[str, int] = {}
    for day_val, cnt in rows:
        key = _day_key(day_val)
        if key:
            out[key] = int(cnt or 0)
    return out


async def _build_daily_trend(
    db: AsyncSession,
    *,
    ids: list[int],
    days: int,
) -> dict[str, list]:
    """按日 group by 一次查出各系列，避免逐日多次 count。"""
    labels: list[str] = []
    day_keys: list[str] = []
    for i in range(days - 1, -1, -1):
        d0 = shanghai_day_start(i)
        labels.append(d0.strftime("%m-%d"))
        day_keys.append(d0.strftime("%Y-%m-%d"))

    start = shanghai_day_start(days - 1)
    end = shanghai_day_start(0) + timedelta(days=1)

    chat_day = func.date(ChatMessage.created_at)
    chat_by = await _counts_by_day(
        db,
        day_col=chat_day,
        where_clauses=[
            ChatMessage.user_id.in_(ids),
            ChatMessage.created_at >= start,
            ChatMessage.created_at < end,
        ],
    )

    out_day = func.date(WechatOutboundAction.created_at)
    # outbound：总量（兼容）；outbound_sent：成功口径
    out_total_by = await _counts_by_day(
        db,
        day_col=out_day,
        where_clauses=[
            WechatOutboundAction.actor_user_id.in_(ids),
            WechatOutboundAction.created_at >= start,
            WechatOutboundAction.created_at < end,
        ],
    )
    out_sent_by = await _counts_by_day(
        db,
        day_col=out_day,
        where_clauses=[
            WechatOutboundAction.actor_user_id.in_(ids),
            WechatOutboundAction.status == "sent",
            WechatOutboundAction.created_at >= start,
            WechatOutboundAction.created_at < end,
        ],
    )

    login_day = func.date(UserActivityEvent.occurred_at)
    login_by = await _counts_by_day(
        db,
        day_col=login_day,
        where_clauses=[
            UserActivityEvent.user_id.in_(ids),
            UserActivityEvent.event_type == "login_success",
            UserActivityEvent.occurred_at >= start,
            UserActivityEvent.occurred_at < end,
        ],
    )
    phone_by = await _counts_by_day(
        db,
        day_col=login_day,
        where_clauses=[
            UserActivityEvent.user_id.in_(ids),
            UserActivityEvent.event_type == "phone_dial",
            UserActivityEvent.occurred_at >= start,
            UserActivityEvent.occurred_at < end,
        ],
    )
    search_by = await _counts_by_day(
        db,
        day_col=login_day,
        where_clauses=[
            UserActivityEvent.user_id.in_(ids),
            UserActivityEvent.event_type == "product_search",
            UserActivityEvent.occurred_at >= start,
            UserActivityEvent.occurred_at < end,
        ],
    )

    task_day = func.date(ContactTask.completed_at)
    task_by = await _counts_by_day(
        db,
        day_col=task_day,
        where_clauses=[
            ContactTask.completed_by_user_id.in_(ids),
            ContactTask.completed_at.isnot(None),
            ContactTask.completed_at >= start,
            ContactTask.completed_at < end,
        ],
    )

    blast_day = func.date(CampaignBlastRecipient.sent_at)
    blast_rows = (
        await db.execute(
            select(blast_day, func.count(CampaignBlastRecipient.id))
            .select_from(CampaignBlastRecipient)
            .join(CampaignBlastJob, CampaignBlastJob.id == CampaignBlastRecipient.job_id)
            .where(
                CampaignBlastJob.user_id.in_(ids),
                CampaignBlastRecipient.status == "sent",
                CampaignBlastRecipient.sent_at.isnot(None),
                CampaignBlastRecipient.sent_at >= start,
                CampaignBlastRecipient.sent_at < end,
            )
            .group_by(blast_day)
        )
    ).all()
    blast_by: dict[str, int] = {}
    for day_val, cnt in blast_rows:
        key = _day_key(day_val)
        if key:
            blast_by[key] = int(cnt or 0)

    def series(src: dict[str, int]) -> list[int]:
        return [int(src.get(k, 0)) for k in day_keys]

    return {
        "labels": labels,
        "chat": series(chat_by),
        "outbound": series(out_total_by),
        "outbound_sent": series(out_sent_by),
        "login": series(login_by),
        "task": series(task_by),
        "blast": series(blast_by),
        "phone": series(phone_by),
        "product_search": series(search_by),
    }


def _empty_dept_metrics() -> dict[str, Any]:
    return {
        "member_count": 0,
        "login_dau": 0,
        "work_dau": 0,
        "chat_msgs": 0,
        "outbound_sent": 0,
        "outbound_failed": 0,
        "task_completed": 0,
        "blast_sent": 0,
        "product_search": 0,
        "phone_dial": 0,
        "score": 0,
        "zero_active": 0,
    }


def _accumulate_person(bucket: dict[str, Any], person: dict[str, Any]) -> None:
    bucket["member_count"] += 1
    if person.get("login_count"):
        bucket["login_dau"] += 1
    work = any(
        person.get(k)
        for k in (
            "chat_msgs",
            "outbound_total",
            "task_completed",
            "blast_sent",
            "product_search",
            "product_copy",
            "product_open",
            "phone_dial",
        )
    )
    if work:
        bucket["work_dau"] += 1
    if not person.get("login_count") and not work:
        bucket["zero_active"] += 1
    for k in (
        "chat_msgs",
        "outbound_sent",
        "outbound_failed",
        "task_completed",
        "blast_sent",
        "product_search",
        "phone_dial",
        "score",
    ):
        bucket[k] += int(person.get(k) or 0)


def _merge_metrics(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k in (
        "member_count",
        "login_dau",
        "work_dau",
        "chat_msgs",
        "outbound_sent",
        "outbound_failed",
        "task_completed",
        "blast_sent",
        "product_search",
        "phone_dial",
        "score",
        "zero_active",
    ):
        dst[k] += int(src.get(k) or 0)


def _build_dept_usage_tree(
    *,
    depts: list[Any],
    people: list[dict[str, Any]],
    visible_ids: set[int],
    root_dept_id: int | None = None,
    unassigned_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按部门树汇总可见人员指标；父节点含子孙合计。"""
    by_dept: dict[int | None, list[dict[str, Any]]] = {}
    for p in people:
        if int(p["user_id"]) not in visible_ids:
            continue
        by_dept.setdefault(p.get("department_id"), []).append(p)

    unassigned_metrics = _empty_dept_metrics()
    for p in by_dept.get(None, []):
        _accumulate_person(unassigned_metrics, p)
    unassigned = {
        "name": "未分配",
        **unassigned_metrics,
        "people": [
            {
                "user_id": p["user_id"],
                "name": p["name"],
                "username": p["username"],
                "score": p["score"],
            }
            for p in by_dept.get(None, [])
        ],
    }

    if unassigned_only:
        return [], unassigned

    children_map: dict[int | None, list[Any]] = {}
    for d in depts:
        children_map.setdefault(d.parent_id, []).append(d)

    def walk(dept: Any) -> dict[str, Any]:
        direct = _empty_dept_metrics()
        for p in by_dept.get(int(dept.id), []):
            _accumulate_person(direct, p)
        child_nodes: list[dict[str, Any]] = []
        for ch in children_map.get(int(dept.id), []):
            child_nodes.append(walk(ch))
            walked.add(int(ch.id))
        rolled = dict(direct)
        for ch in child_nodes:
            _merge_metrics(rolled, ch)
        return {
            "id": int(dept.id),
            "parent_id": dept.parent_id,
            "name": dept.name,
            "kind": dept.kind,
            **rolled,
            "direct_member_count": direct["member_count"],
            "children": child_nodes,
        }

    walked: set[int] = set()
    roots: list[dict[str, Any]] = []

    if root_dept_id is not None:
        root = next((d for d in depts if int(d.id) == int(root_dept_id)), None)
        if root is not None:
            roots.append(walk(root))
            walked.add(int(root.id))
        return roots, unassigned

    for d in children_map.get(None, []):
        roots.append(walk(d))
        walked.add(int(d.id))
    for d in depts:
        if int(d.id) not in walked:
            roots.append(walk(d))
            walked.add(int(d.id))
    return roots, unassigned


async def person_timeline(
    db: AsyncSession,
    *,
    user_id: int,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    days: int | None = None,
    page: int = 1,
    page_size: int = 50,
    fetch_cap: int = 2000,
) -> dict[str, Any]:
    """按日期范围拉取个人动态，合并后分页。

    date_from/date_to 为上海自然日（含首尾）；未传时可用 days 回退。
    """
    page = max(1, int(page or 1))
    page_size = max(1, min(200, int(page_size or 50)))

    if date_from is not None or date_to is not None:
        start = date_from or shanghai_day_start(0)
        end_day = date_to or start
        # 右开区间：次日 00:00
        end = end_day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        d = max(1, min(90, int(days or 1)))
        start = window_since(d)
        end = shanghai_day_start(0) + timedelta(days=1)

    items: list[dict[str, Any]] = []
    cap = max(page * page_size, min(fetch_cap, 5000))

    for row in (
        await db.execute(
            select(ChatMessage.id, ChatMessage.role, ChatMessage.created_at, ChatMessage.is_copied)
            .where(
                ChatMessage.user_id == user_id,
                ChatMessage.created_at >= start,
                ChatMessage.created_at < end,
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(cap)
        )
    ).all():
        items.append(
            {
                "at": row[2].isoformat(sep=" ") if row[2] else None,
                "type": "chat",
                "summary": f"{row[1]} 消息#{row[0]}"
                + ("（已采纳）" if row[3] else ""),
            }
        )

    for row in (
        await db.execute(
            select(
                WechatOutboundAction.id,
                WechatOutboundAction.action_type,
                WechatOutboundAction.status,
                WechatOutboundAction.created_at,
            )
            .where(
                WechatOutboundAction.actor_user_id == user_id,
                WechatOutboundAction.created_at >= start,
                WechatOutboundAction.created_at < end,
            )
            .order_by(WechatOutboundAction.created_at.desc())
            .limit(cap)
        )
    ).all():
        items.append(
            {
                "at": row[3].isoformat(sep=" ") if row[3] else None,
                "type": "outbound",
                "summary": f"外发#{row[0]} {row[1]}/{row[2]}",
            }
        )

    for row in (
        await db.execute(
            select(
                UserActivityEvent.event_type,
                UserActivityEvent.occurred_at,
                UserActivityEvent.object_id,
            )
            .where(
                UserActivityEvent.user_id == user_id,
                UserActivityEvent.occurred_at >= start,
                UserActivityEvent.occurred_at < end,
            )
            .order_by(UserActivityEvent.occurred_at.desc())
            .limit(cap)
        )
    ).all():
        items.append(
            {
                "at": row[1].isoformat(sep=" ") if row[1] else None,
                "type": row[0],
                "summary": f"{row[0]}" + (f" {row[2]}" if row[2] else ""),
            }
        )

    items.sort(key=lambda x: x.get("at") or "", reverse=True)
    total = len(items)
    offset = (page - 1) * page_size
    page_items = items[offset : offset + page_size]
    return {
        "items": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "date_from": start.strftime("%Y-%m-%d"),
        "date_to": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
    }
