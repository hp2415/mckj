"""事件驱动画像触发：新消息/转写完成/订单/任务跳过申诉超时后，按冷却窗口把 (客户,销售号) 入画像队列。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import literal, or_, tuple_
from sqlalchemy.future import select

from ai.phone_call_profile import digits_phone, phone_match_or_clauses
from ai.raw_profiling import enqueue_profile_sales_pairs, is_group_chat_customer
from ai.task_allocation_limits import get_task_allocation_limits
from core.logger import logger
from database import AsyncSessionLocal
from models import RawCustomer, RawCustomerSalesWechat, SalesCustomerProfile, UserSalesWechat


def dedupe_sales_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for rid, sw in pairs or []:
        rid = (rid or "").strip()
        sw = (sw or "").strip()
        if not rid or not sw:
            continue
        key = (rid, sw)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def pairs_from_chat_rows(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """聊天增量行：wechat_id=销售号，talker=客户 raw_customer_id。"""
    out: list[tuple[str, str]] = []
    for row in rows or []:
        sw = str(row.get("wechat_id") or "").strip()
        rid = str(row.get("talker") or "").strip()
        if not sw or not rid:
            continue
        if row.get("roomid"):
            continue
        if is_group_chat_customer(rid):
            continue
        out.append((rid, sw))
    return dedupe_sales_pairs(out)


def pairs_from_voice_rows(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """微信语音通话行：we_chat_id=销售号，talker=客户 raw_customer_id。"""
    out: list[tuple[str, str]] = []
    for row in rows or []:
        if int(row.get("is_room") or 0) != 0:
            continue
        sw = str(row.get("we_chat_id") or "").strip()
        rid = str(row.get("talker") or "").strip()
        if not sw or not rid or is_group_chat_customer(rid):
            continue
        out.append((rid, sw))
    return dedupe_sales_pairs(out)


async def resolve_pairs_from_phone_rows(
    db,
    rows: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """电话外呼行：user_wechat_account=销售号，callee 匹配客户电话。"""
    out: list[tuple[str, str]] = []
    seen_req: set[tuple[str, str]] = set()
    for row in rows or []:
        sw = str(row.get("user_wechat_account") or "").strip()
        callee = str(row.get("callee") or "").strip()
        if not sw or not callee:
            continue
        req_key = (sw, callee)
        if req_key in seen_req:
            continue
        seen_req.add(req_key)
        phone_match = phone_match_or_clauses(
            literal(callee),
            RawCustomer.phone,
            RawCustomer.phone_normalized,
            RawCustomerSalesWechat.phone,
        )
        if phone_match is None:
            continue
        res = await db.execute(
            select(
                RawCustomerSalesWechat.raw_customer_id,
                RawCustomerSalesWechat.sales_wechat_id,
            )
            .join(RawCustomer, RawCustomer.id == RawCustomerSalesWechat.raw_customer_id)
            .where(RawCustomerSalesWechat.sales_wechat_id == sw)
            .where(phone_match)
            .limit(8)
        )
        for rid, sw2 in res.all():
            rid_s = str(rid or "").strip()
            sw_s = str(sw2 or "").strip()
            if rid_s and sw_s:
                out.append((rid_s, sw_s))
    return dedupe_sales_pairs(out)


async def resolve_pairs_from_order_items(
    db,
    items: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """订单增量：wechat_idx=销售号（若有），consignee_phone 匹配客户电话。"""
    out: list[tuple[str, str]] = []
    seen_req: set[tuple[str, str]] = set()
    for item in items or []:
        sw = str(item.get("wechat_idx") or "").strip()
        phone = digits_phone(item.get("consignee_phone"))
        if len(phone) < 7:
            continue
        req_key = (sw, phone)
        if req_key in seen_req:
            continue
        seen_req.add(req_key)
        phone_match = phone_match_or_clauses(
            literal(phone),
            RawCustomer.phone,
            RawCustomer.phone_normalized,
            RawCustomerSalesWechat.phone,
        )
        if phone_match is None:
            continue
        stmt = (
            select(
                RawCustomerSalesWechat.raw_customer_id,
                RawCustomerSalesWechat.sales_wechat_id,
            )
            .join(RawCustomer, RawCustomer.id == RawCustomerSalesWechat.raw_customer_id)
            .where(phone_match)
        )
        if sw:
            stmt = stmt.where(RawCustomerSalesWechat.sales_wechat_id == sw)
        res = await db.execute(stmt.limit(12))
        for rid, sw2 in res.all():
            rid_s = str(rid or "").strip()
            sw_s = str(sw2 or "").strip()
            if rid_s and sw_s:
                out.append((rid_s, sw_s))
    return dedupe_sales_pairs(out)


async def load_bound_sales_wechat_ids(db) -> frozenset[str]:
    """已绑定登录用户的销售微信号（与夜间画像候选一致）。"""
    res = await db.execute(select(UserSalesWechat.sales_wechat_id))
    return frozenset(s.strip() for s in res.scalars().all() if s and str(s).strip())


def filter_pairs_by_bound_sales(
    pairs: list[tuple[str, str]],
    bound_sales: frozenset[str],
) -> list[tuple[str, str]]:
    if not bound_sales:
        return []
    return [(rid, sw) for rid, sw in pairs if sw in bound_sales]


async def trigger_profile_for_pairs(
    db,
    pairs: list[tuple[str, str]],
    *,
    reason: str,
) -> int:
    """pairs: [(raw_customer_id, sales_wechat_id)]。冷却窗口内已画像的跳过。"""
    limits = await get_task_allocation_limits(db)
    if not limits.get("event_profile_enabled"):
        return 0
    cooldown_min = int(limits.get("event_profile_cooldown_minutes") or 120)
    cleaned = dedupe_sales_pairs(pairs)
    if not cleaned:
        return 0

    bound_sales = await load_bound_sales_wechat_ids(db)
    before_bound = len(cleaned)
    cleaned = filter_pairs_by_bound_sales(cleaned, bound_sales)
    if before_bound and not cleaned:
        logger.debug(
            "事件触发画像 reason={} 全部跳过：销售号未绑定 user_sales_wechats（{} 对）",
            reason,
            before_bound,
        )
        return 0
    if before_bound > len(cleaned):
        logger.debug(
            "事件触发画像 reason={} 过滤未绑定销售号 {} -> {} 对",
            reason,
            before_bound,
            len(cleaned),
        )
    if not cleaned:
        return 0

    now = datetime.now()
    cutoff = now - timedelta(minutes=max(1, cooldown_min))
    res = await db.execute(
        select(
            SalesCustomerProfile.raw_customer_id,
            SalesCustomerProfile.sales_wechat_id,
            SalesCustomerProfile.profiled_at,
        ).where(
            tuple_(
                SalesCustomerProfile.raw_customer_id,
                SalesCustomerProfile.sales_wechat_id,
            ).in_(cleaned)
        )
    )
    profiled_map = {
        (str(r[0] or "").strip(), str(r[1] or "").strip()): r[2] for r in res.all()
    }

    keep: list[tuple[str, str]] = []
    for key in cleaned:
        pa = profiled_map.get(key)
        if pa is not None:
            pa_naive = pa.replace(tzinfo=None) if getattr(pa, "tzinfo", None) else pa
            if pa_naive >= cutoff:
                continue
        keep.append(key)
    if not keep:
        return 0

    label = f"事件触发画像({reason})"
    await enqueue_profile_sales_pairs(keep, label=label)
    logger.info("事件触发画像 reason={} pairs={}", reason, len(keep))
    return len(keep)


def pair_from_contact_task(task: Any) -> tuple[str, str] | None:
    """从联系任务提取 (raw_customer_id, sales_wechat_id)。"""
    rid = str(getattr(task, "raw_customer_id", None) or "").strip()
    sw = str(getattr(task, "sales_wechat_id", None) or "").strip()
    if rid and sw:
        return (rid, sw)
    return None


async def trigger_profile_for_contact_task(
    db,
    task: Any,
    *,
    reason: str,
) -> int:
    pair = pair_from_contact_task(task)
    if not pair:
        return 0
    return await trigger_profile_for_pairs(db, [pair], reason=reason)


async def safe_trigger_profile_for_pairs(
    pairs: list[tuple[str, str]],
    *,
    reason: str,
) -> int:
    """独立短事务触发画像入队；失败不影响调用方主流程。返回实际入队对数。"""
    if not pairs:
        return 0
    try:
        async with AsyncSessionLocal() as db:
            return await trigger_profile_for_pairs(db, pairs, reason=reason)
    except Exception:
        logger.exception("事件触发画像失败 reason={}", reason)
        return 0


async def safe_trigger_profile_for_contact_task(task: Any, *, reason: str) -> int:
    """任务状态变更后触发画像（skip/申诉等）。"""
    pair = pair_from_contact_task(task)
    if not pair:
        return 0
    return await safe_trigger_profile_for_pairs([pair], reason=reason)
