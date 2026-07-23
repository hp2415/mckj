"""事件驱动画像触发：新消息/转写完成/订单/任务跳过申诉超时后，按冷却窗口把 (客户,销售号) 入画像队列。

冷静期内若再次触发：写入 event_profile_deferred_at 暂存，冷静期结束后立即入队（不等夜间）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import literal, tuple_, update
from sqlalchemy.future import select

from ai.phone_call_profile import digits_phone, phone_match_or_clauses
from ai.raw_profiling import enqueue_profile_sales_pairs, is_group_chat_customer
from ai.task_allocation_limits import get_task_allocation_limits
from core.logger import logger
from database import AsyncSessionLocal
from models import RawCustomer, RawCustomerSalesWechat, SalesCustomerProfile, UserSalesWechat

# 冷静期结束后的精确 flush 定时器（进程内；重启由 APScheduler 扫尾兜底）
_flush_tasks: dict[tuple[str, str], asyncio.Task] = {}


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
    """订单增量：wechat_idx=销售账号 alias_name（若有），consignee_phone 匹配客户电话。

    wechat_idx 对应 sales_wechat_accounts.alias_name，需先解析为 sales_wechat_id 再匹配 RCSW。
    """
    from core.data_visibility import sales_wechat_id_for_order_wechat_idx

    out: list[tuple[str, str]] = []
    seen_req: set[tuple[str, str]] = set()
    for item in items or []:
        wechat_idx = str(item.get("wechat_idx") or "").strip()
        phone = digits_phone(item.get("consignee_phone"))
        if len(phone) < 7:
            continue
        sw = ""
        if wechat_idx:
            resolved = await sales_wechat_id_for_order_wechat_idx(db, wechat_idx)
            sw = (resolved or "").strip()
            # 有 wechat_idx 但解析不到销售号时，不强行按全号匹配，避免串到错误 pair
            if not sw:
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


def _naive_dt(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt


def _cooldown_ready_at(profiled_at: datetime, cooldown_min: int) -> datetime:
    pa = _naive_dt(profiled_at)
    if pa is None:
        pa = datetime.now()
    return pa + timedelta(minutes=max(1, cooldown_min))


def _schedule_deferred_flush(
    pair: tuple[str, str],
    *,
    ready_at: datetime,
    reason: str,
) -> None:
    """在冷静期结束时刻精确触发 flush；同一 pair 只保留最新定时器。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    key = pair
    delay = max(0.0, (ready_at - datetime.now()).total_seconds()) + 0.3
    old = _flush_tasks.get(key)
    if old and not old.done():
        old.cancel()

    async def _run() -> None:
        try:
            await asyncio.sleep(delay)
            n = await flush_deferred_event_profiles(pairs=[key], reason=reason)
            if n:
                logger.info(
                    "事件触发画像冷静期结束入队 reason={} pair={} count={}",
                    reason,
                    key,
                    n,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("事件触发画像冷静期 flush 失败 pair={}", key)
        finally:
            cur = _flush_tasks.get(key)
            if cur is asyncio.current_task():
                _flush_tasks.pop(key, None)

    _flush_tasks[key] = loop.create_task(_run())


async def _mark_pairs_deferred(
    pairs: list[tuple[str, str]],
    *,
    now: datetime,
) -> None:
    """独立短事务写入暂存标记，不干扰调用方会话。"""
    if not pairs:
        return
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(SalesCustomerProfile)
            .where(
                tuple_(
                    SalesCustomerProfile.raw_customer_id,
                    SalesCustomerProfile.sales_wechat_id,
                ).in_(pairs)
            )
            .values(event_profile_deferred_at=now)
        )
        await db.commit()


async def _clear_pairs_deferred(pairs: list[tuple[str, str]]) -> None:
    if not pairs:
        return
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(SalesCustomerProfile)
            .where(
                tuple_(
                    SalesCustomerProfile.raw_customer_id,
                    SalesCustomerProfile.sales_wechat_id,
                ).in_(pairs)
            )
            .values(event_profile_deferred_at=None)
        )
        await db.commit()


async def flush_deferred_event_profiles(
    *,
    pairs: list[tuple[str, str]] | None = None,
    reason: str = "cooldown_flush",
) -> int:
    """
    将冷静期已结束且期间有触发的暂存对入队。
    pairs 为 None 时扫描全部 deferred 记录（调度扫尾用）。
    """
    async with AsyncSessionLocal() as db:
        limits = await get_task_allocation_limits(db)
        if not limits.get("event_profile_enabled"):
            return 0
        cooldown_min = int(limits.get("event_profile_cooldown_minutes") or 60)
        now = datetime.now()
        cutoff = now - timedelta(minutes=max(1, cooldown_min))

        stmt = select(
            SalesCustomerProfile.raw_customer_id,
            SalesCustomerProfile.sales_wechat_id,
            SalesCustomerProfile.profiled_at,
        ).where(SalesCustomerProfile.event_profile_deferred_at.is_not(None))
        cleaned = dedupe_sales_pairs(pairs or [])
        if cleaned:
            stmt = stmt.where(
                tuple_(
                    SalesCustomerProfile.raw_customer_id,
                    SalesCustomerProfile.sales_wechat_id,
                ).in_(cleaned)
            )
        res = await db.execute(stmt)
        rows = res.all()
        if not rows:
            return 0

        ready: list[tuple[str, str]] = []
        still_waiting: list[tuple[tuple[str, str], datetime]] = []
        for rid, sw, pa in rows:
            key = (str(rid or "").strip(), str(sw or "").strip())
            if not key[0] or not key[1]:
                continue
            pa_naive = _naive_dt(pa)
            if pa_naive is not None and pa_naive >= cutoff:
                still_waiting.append((key, _cooldown_ready_at(pa_naive, cooldown_min)))
                continue
            ready.append(key)

        for key, ready_at in still_waiting:
            _schedule_deferred_flush(key, ready_at=ready_at, reason=reason)

        if not ready:
            return 0

        bound_sales = await load_bound_sales_wechat_ids(db)
        ready = filter_pairs_by_bound_sales(ready, bound_sales)
        if not ready:
            return 0

    label = f"事件触发画像({reason})"
    await enqueue_profile_sales_pairs(ready, label=label)
    await _clear_pairs_deferred(ready)
    logger.info("事件触发画像冷静期 flush reason={} pairs={}", reason, len(ready))
    return len(ready)


async def scheduled_flush_deferred_event_profiles() -> None:
    """APScheduler 兜底：进程重启后仍能放出冷静期已结束的暂存触发。"""
    try:
        n = await flush_deferred_event_profiles(reason="cooldown_flush_sweep")
        if n:
            logger.info("事件触发画像冷静期扫尾入队 pairs={}", n)
    except Exception:
        logger.exception("事件触发画像冷静期扫尾失败")


async def trigger_profile_for_pairs(
    db,
    pairs: list[tuple[str, str]],
    *,
    reason: str,
) -> int:
    """pairs: [(raw_customer_id, sales_wechat_id)]。
    冷却窗口内已画像：暂存 deferred，冷静期结束立即入队；否则立刻入队。
    """
    limits = await get_task_allocation_limits(db)
    if not limits.get("event_profile_enabled"):
        return 0
    cooldown_min = int(limits.get("event_profile_cooldown_minutes") or 60)
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
    deferred: list[tuple[str, str]] = []
    deferred_ready_at: dict[tuple[str, str], datetime] = {}
    for key in cleaned:
        pa = profiled_map.get(key)
        if pa is not None:
            pa_naive = _naive_dt(pa)
            if pa_naive is not None and pa_naive >= cutoff:
                deferred.append(key)
                deferred_ready_at[key] = _cooldown_ready_at(pa_naive, cooldown_min)
                continue
        keep.append(key)

    if deferred:
        await _mark_pairs_deferred(deferred, now=now)
        for key in deferred:
            _schedule_deferred_flush(
                key,
                ready_at=deferred_ready_at[key],
                reason=f"cooldown_flush:{reason}",
            )
        logger.info(
            "事件触发画像冷静期暂存 reason={} pairs={} cooldown_min={}",
            reason,
            len(deferred),
            cooldown_min,
        )

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
