"""客户与 raw_orders 关联：收件人电话 + 采购单位名称（双向包含）。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import RawOrder

# 包含匹配误伤面大于精确匹配，单位名过短（如「学校」「幼儿园」）易串单
_MIN_UNIT_NAME_LEN = 4

# 按 buyer_name 预聚合缓存（列表统计用，避免每次拉订单明细 / 阻塞请求）
_BUYER_AGG_CACHE: dict[str, tuple[float, int]] | None = None
_BUYER_MONTH_CACHE: dict[str, set[str]] | None = None
_BUYER_AGG_CACHE_AT: float = 0.0
_BUYER_AGG_CACHE_TTL_SEC = 600.0
_BUYER_AGG_REFRESH_LOCK = asyncio.Lock()
_BUYER_AGG_REFRESH_TASK: asyncio.Task | None = None


def digits_phone(value: Any) -> str:
    return "".join(filter(str.isdigit, str(value or "")))


def normalize_unit_name(value: Any) -> str:
    return str(value or "").strip()


def usable_phone(value: Any) -> Optional[str]:
    p = digits_phone(value)
    return p if len(p) >= 7 else None


def usable_unit_name(value: Any) -> Optional[str]:
    u = normalize_unit_name(value)
    return u if len(u) >= _MIN_UNIT_NAME_LEN else None


def unit_names_fuzzy_match(a: Any, b: Any) -> bool:
    """
    单位名双向包含匹配。
    例：「未央区第五幼儿园」↔「西安市未央区第五幼儿园」
    """
    left = normalize_unit_name(a)
    right = normalize_unit_name(b)
    if len(left) < _MIN_UNIT_NAME_LEN or len(right) < _MIN_UNIT_NAME_LEN:
        return False
    return left in right or right in left


def map_units_to_buyer_names(
    unit_names: list[str],
    buyer_names: list[str],
) -> dict[str, list[str]]:
    """
    内存双向包含：客户单位名 → 命中的订单 buyer_name 列表。
    供客户列表批量统计使用，避免上千条 LIKE 拖垮 SQL。
    """
    units = sorted({u for u in (usable_unit_name(x) for x in unit_names) if u}, key=len)
    buyers = [b for b in (normalize_unit_name(x) for x in buyer_names) if len(b) >= _MIN_UNIT_NAME_LEN]
    if not units or not buyers:
        return {}

    out: dict[str, list[str]] = {}
    # 短单位优先；每个 buyer 扫一遍（含精确相等）
    for bn in buyers:
        for u in units:
            ul = len(u)
            bl = len(bn)
            if ul <= bl:
                if u in bn:
                    out.setdefault(u, []).append(bn)
            elif bn in u:
                out.setdefault(u, []).append(bn)
    return out


def unit_name_column_match_clause(column, unit_name: Any):
    """
    SQL：column 包含 unit_name，或 unit_name 包含 column（两侧均达最短长度）。
    仅用于单客户/单订单场景；批量列表请用 buyer 预聚合 + 内存匹配。
    """
    u = usable_unit_name(unit_name)
    if not u:
        return None
    trimmed = func.trim(column)
    return or_(
        column.contains(u),
        and_(
            column.is_not(None),
            func.char_length(trimmed) >= _MIN_UNIT_NAME_LEN,
            literal(u).like(func.concat("%", trimmed, "%")),
        ),
    )


def customer_order_match_clause(
    *,
    phone: Any = None,
    unit_name: Any = None,
):
    """
    客户 → 订单：consignee_phone 精确匹配，
    或 buyer_name（采购单位/人）与客户 unit_name 双向包含匹配。
    """
    clauses = []
    p = usable_phone(phone)
    if p:
        clauses.append(RawOrder.consignee_phone == p)
    unit_clause = unit_name_column_match_clause(RawOrder.buyer_name, unit_name)
    if unit_clause is not None:
        clauses.append(unit_clause)
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return or_(*clauses)


async def load_orders_for_customer(
    db: AsyncSession,
    *,
    phone: Any = None,
    unit_name: Any = None,
    limit: Optional[int] = None,
) -> list[RawOrder]:
    """按电话和/或单位名称拉取客户订单，按下单时间倒序。"""
    clause = customer_order_match_clause(phone=phone, unit_name=unit_name)
    if clause is None:
        return []
    stmt = select(RawOrder).where(clause).order_by(RawOrder.order_time.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    res = await db.execute(stmt)
    return list(res.scalars().all())


def peek_buyer_order_aggregates() -> tuple[dict[str, tuple[float, int]], dict[str, set[str]]] | None:
    """缓存命中则立即返回；未命中返回 None（请求路径勿同步重建）。"""
    now = time.monotonic()
    if (
        _BUYER_AGG_CACHE is not None
        and _BUYER_MONTH_CACHE is not None
        and (now - _BUYER_AGG_CACHE_AT) < _BUYER_AGG_CACHE_TTL_SEC
    ):
        return _BUYER_AGG_CACHE, _BUYER_MONTH_CACHE
    return None


def invalidate_buyer_order_agg_cache() -> None:
    """订单同步后清空缓存，并安排后台重建。"""
    global _BUYER_AGG_CACHE, _BUYER_MONTH_CACHE, _BUYER_AGG_CACHE_AT, _BUYER_AGG_REFRESH_TASK
    _BUYER_AGG_CACHE = None
    _BUYER_MONTH_CACHE = None
    _BUYER_AGG_CACHE_AT = 0.0
    task = _BUYER_AGG_REFRESH_TASK
    if task is not None and not task.done():
        task.cancel()
        _BUYER_AGG_REFRESH_TASK = None
    schedule_buyer_order_agg_refresh()


async def _rebuild_buyer_order_aggregates() -> None:
    global _BUYER_AGG_CACHE, _BUYER_MONTH_CACHE, _BUYER_AGG_CACHE_AT
    from database import AsyncSessionLocal
    from core.logger import logger

    async with _BUYER_AGG_REFRESH_LOCK:
        # 双重检查：可能已被其它协程建好
        if peek_buyer_order_aggregates() is not None:
            return
        t0 = time.monotonic()
        try:
            async with AsyncSessionLocal() as db:
                agg_rows = (
                    await db.execute(
                        select(
                            RawOrder.buyer_name,
                            func.coalesce(func.sum(RawOrder.pay_amount), 0),
                            func.count(RawOrder.id),
                        )
                        .where(RawOrder.buyer_name.is_not(None))
                        .where(RawOrder.buyer_name != "")
                        .group_by(RawOrder.buyer_name)
                    )
                ).all()
                agg_map: dict[str, tuple[float, int]] = {}
                for name, total, cnt in agg_rows:
                    key = normalize_unit_name(name)
                    if len(key) < _MIN_UNIT_NAME_LEN:
                        continue
                    agg_map[key] = (float(total or 0), int(cnt or 0))

                month_rows = (
                    await db.execute(
                        select(RawOrder.buyer_name, func.month(RawOrder.order_time))
                        .where(RawOrder.buyer_name.is_not(None))
                        .where(RawOrder.buyer_name != "")
                        .where(RawOrder.order_time.is_not(None))
                        .group_by(RawOrder.buyer_name, func.month(RawOrder.order_time))
                    )
                ).all()
                month_map: dict[str, set[str]] = {}
                for name, month_num in month_rows:
                    key = normalize_unit_name(name)
                    if len(key) < _MIN_UNIT_NAME_LEN or not month_num:
                        continue
                    month_map.setdefault(key, set()).add(f"{int(month_num)}月")

            _BUYER_AGG_CACHE = agg_map
            _BUYER_MONTH_CACHE = month_map
            _BUYER_AGG_CACHE_AT = time.monotonic()
            logger.info(
                "订单单位名聚合缓存已重建 buyers={} months_keys={} cost={:.2f}s",
                len(agg_map),
                len(month_map),
                time.monotonic() - t0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("订单单位名聚合缓存重建失败: {}", e)


def schedule_buyer_order_agg_refresh() -> None:
    """后台预热/重建单位名聚合缓存（不阻塞 HTTP）。"""
    global _BUYER_AGG_REFRESH_TASK
    if peek_buyer_order_aggregates() is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = _BUYER_AGG_REFRESH_TASK
    if task is not None and not task.done():
        return

    async def _runner() -> None:
        try:
            await _rebuild_buyer_order_aggregates()
        except asyncio.CancelledError:
            return

    _BUYER_AGG_REFRESH_TASK = loop.create_task(_runner())


async def load_buyer_order_aggregates(
    db: AsyncSession,
    *,
    force: bool = False,
) -> tuple[dict[str, tuple[float, int]], dict[str, set[str]]]:
    """
    同步加载预聚合（仅后台预热或显式 force 时使用）。
    客户列表请求请用 peek_buyer_order_aggregates + schedule_buyer_order_agg_refresh。
    """
    if not force:
        hit = peek_buyer_order_aggregates()
        if hit is not None:
            return hit
    await _rebuild_buyer_order_aggregates()
    hit = peek_buyer_order_aggregates()
    if hit is not None:
        return hit
    return {}, {}
