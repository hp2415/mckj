"""客户与 raw_orders 关联：收件人电话（多号按逗号等拆分后任一命中）；
可选采购单位名称（双向包含）；可选 staff_uuid（= 账号 mibuddy_uuid）归属过滤。

订单归属字段 wechat_idx = sales_wechat_accounts.alias_name（见 core.data_visibility）。

单位名匹配默认关闭（不规范命名易串单）；管理后台 order_match_by_unit_name
或环境变量 ORDER_MATCH_BY_UNIT_NAME=1 可重新开启。

员工 UUID 匹配默认关闭；开启后仅保留 raw_orders.staff_uuid 等于客户所属
销售微信号绑定员工账号 User.mibuddy_uuid 的订单。管理后台
order_match_by_staff_uuid 或环境变量 ORDER_MATCH_BY_STAFF_UUID=1。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import RawOrder

# buyer → wechat_idx_key → consignee_phone_key → (amount, count)
# idx 空 = 未归属；phone 空 = 订单无收件人电话
BuyerIdxAggMap = dict[str, dict[str, dict[str, tuple[float, int]]]]
BuyerIdxMonthMap = dict[str, dict[str, dict[str, set[str]]]]
BuyerIdxFlagsMap = dict[str, dict[str, dict[str, tuple[bool, bool]]]]

# 包含匹配误伤面大于精确匹配，单位名过短（如「学校」「幼儿园」）易串单
_MIN_UNIT_NAME_LEN = 4

# 单位名匹配开关（默认关）；SystemConfig 优先，否则回退环境变量
ORDER_MATCH_BY_UNIT_NAME_CONFIG_KEY = "order_match_by_unit_name"
_UNIT_MATCH_FLAG_TTL_SEC = 30.0
_UNIT_MATCH_FLAG_CACHE_AT: float = 0.0
_UNIT_MATCH_FLAG_CACHE_VAL: bool | None = None

# 员工 UUID 匹配开关（默认关）；SystemConfig 优先，否则回退环境变量
ORDER_MATCH_BY_STAFF_UUID_CONFIG_KEY = "order_match_by_staff_uuid"
_STAFF_MATCH_FLAG_TTL_SEC = 30.0
_STAFF_MATCH_FLAG_CACHE_AT: float = 0.0
_STAFF_MATCH_FLAG_CACHE_VAL: bool | None = None

# 「近期未采」窗口：约两个月
RECENT_ORDER_DAYS = 60
# 「去年临近月份」：相对当前月 ±N（跨年按月份环绕，如 1 月 → 11/12/1/2/3）
LAST_YEAR_NEARBY_MONTH_DELTA = 2

# 按 buyer_name + wechat_idx + consignee_phone 预聚合（列表统计按可见性折叠）
_BUYER_AGG_CACHE_VERSION = 2  # 结构变更时递增，使旧缓存失效
_BUYER_AGG_CACHE: BuyerIdxAggMap | None = None
_BUYER_MONTH_CACHE: BuyerIdxMonthMap | None = None
_BUYER_YEAR_FLAGS_CACHE: BuyerIdxFlagsMap | None = None
_BUYER_AGG_CACHE_AT: float = 0.0
_BUYER_AGG_CACHE_BUILT_VERSION: int = 0
_BUYER_AGG_CACHE_TTL_SEC = 600.0
_BUYER_AGG_REFRESH_LOCK = asyncio.Lock()
_BUYER_AGG_REFRESH_TASK: asyncio.Task | None = None


def _parse_enabled_flag(raw: Any, *, default: bool = False) -> bool:
    v = str(raw or "").strip().lower()
    if not v:
        return default
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _env_unit_name_order_match_enabled() -> bool:
    return _parse_enabled_flag(os.getenv("ORDER_MATCH_BY_UNIT_NAME"), default=False)


def is_unit_name_order_match_enabled() -> bool:
    """是否按单位名匹配订单。默认关闭；缓存命中则用缓存，否则回退环境变量。"""
    now = time.monotonic()
    if (
        _UNIT_MATCH_FLAG_CACHE_VAL is not None
        and (now - _UNIT_MATCH_FLAG_CACHE_AT) < _UNIT_MATCH_FLAG_TTL_SEC
    ):
        return bool(_UNIT_MATCH_FLAG_CACHE_VAL)
    return _env_unit_name_order_match_enabled()


async def resolve_unit_name_order_match_enabled(db: AsyncSession) -> bool:
    """从 SystemConfig 刷新开关（优先）；无配置则回退环境变量。短 TTL 缓存供同步路径。"""
    global _UNIT_MATCH_FLAG_CACHE_AT, _UNIT_MATCH_FLAG_CACHE_VAL

    now = time.monotonic()
    if (
        _UNIT_MATCH_FLAG_CACHE_VAL is not None
        and (now - _UNIT_MATCH_FLAG_CACHE_AT) < _UNIT_MATCH_FLAG_TTL_SEC
    ):
        return bool(_UNIT_MATCH_FLAG_CACHE_VAL)

    from models import SystemConfig

    res = await db.execute(
        select(SystemConfig.config_value).where(
            SystemConfig.config_key == ORDER_MATCH_BY_UNIT_NAME_CONFIG_KEY
        )
    )
    row = res.first()
    if row is not None and str(row[0] or "").strip() != "":
        enabled = _parse_enabled_flag(row[0], default=False)
    else:
        enabled = _env_unit_name_order_match_enabled()
    _UNIT_MATCH_FLAG_CACHE_VAL = enabled
    _UNIT_MATCH_FLAG_CACHE_AT = time.monotonic()
    return enabled


def _env_staff_uuid_order_match_enabled() -> bool:
    return _parse_enabled_flag(os.getenv("ORDER_MATCH_BY_STAFF_UUID"), default=False)


def is_staff_uuid_order_match_enabled() -> bool:
    """是否按订单 staff_uuid 与账号 mibuddy_uuid 对应过滤。默认关闭。"""
    now = time.monotonic()
    if (
        _STAFF_MATCH_FLAG_CACHE_VAL is not None
        and (now - _STAFF_MATCH_FLAG_CACHE_AT) < _STAFF_MATCH_FLAG_TTL_SEC
    ):
        return bool(_STAFF_MATCH_FLAG_CACHE_VAL)
    return _env_staff_uuid_order_match_enabled()


async def resolve_staff_uuid_order_match_enabled(db: AsyncSession) -> bool:
    """从 SystemConfig 刷新员工 UUID 匹配开关；无配置则回退环境变量。短 TTL 缓存。"""
    global _STAFF_MATCH_FLAG_CACHE_AT, _STAFF_MATCH_FLAG_CACHE_VAL

    now = time.monotonic()
    if (
        _STAFF_MATCH_FLAG_CACHE_VAL is not None
        and (now - _STAFF_MATCH_FLAG_CACHE_AT) < _STAFF_MATCH_FLAG_TTL_SEC
    ):
        return bool(_STAFF_MATCH_FLAG_CACHE_VAL)

    from models import SystemConfig

    res = await db.execute(
        select(SystemConfig.config_value).where(
            SystemConfig.config_key == ORDER_MATCH_BY_STAFF_UUID_CONFIG_KEY
        )
    )
    row = res.first()
    if row is not None and str(row[0] or "").strip() != "":
        enabled = _parse_enabled_flag(row[0], default=False)
    else:
        enabled = _env_staff_uuid_order_match_enabled()
    _STAFF_MATCH_FLAG_CACHE_VAL = enabled
    _STAFF_MATCH_FLAG_CACHE_AT = time.monotonic()
    return enabled


def usable_staff_uuid(value: Any) -> Optional[str]:
    s = str(value or "").strip()
    return s or None


def requires_staff_uuid_order_match(role: Any) -> bool:
    """是否对该角色启用 staff_uuid 过滤。

    老客户（及可看全量订单的角色）不要求米城 UUID 绑定，也不按 staff_uuid 收窄。
    """
    from core.data_visibility import can_view_all_orders

    return not can_view_all_orders(role)


async def resolve_mibuddy_uuid_for_sales_wechat(
    db: AsyncSession,
    sales_wechat_id: Any,
) -> Optional[str]:
    """客户所属销售微信号 → 绑定员工账号的 User.mibuddy_uuid。"""
    sw = str(sales_wechat_id or "").strip()
    if not sw:
        return None
    from models import User, UserSalesWechat

    res = await db.execute(
        select(User.mibuddy_uuid)
        .join(UserSalesWechat, UserSalesWechat.user_id == User.id)
        .where(UserSalesWechat.sales_wechat_id == sw)
        .limit(1)
    )
    return usable_staff_uuid(res.scalar_one_or_none())


async def resolve_owner_role_and_mibuddy_for_sales_wechat(
    db: AsyncSession,
    sales_wechat_id: Any,
) -> tuple[str, Optional[str]]:
    """销售微信号 → (归属用户 role, mibuddy_uuid)。无归属时 role=staff、uuid=None。"""
    from core.data_visibility import ROLE_STAFF, normalize_role
    from models import User, UserSalesWechat

    sw = str(sales_wechat_id or "").strip()
    if not sw:
        return ROLE_STAFF, None
    res = await db.execute(
        select(User.role, User.mibuddy_uuid)
        .join(UserSalesWechat, UserSalesWechat.user_id == User.id)
        .where(UserSalesWechat.sales_wechat_id == sw)
        .limit(1)
    )
    row = res.first()
    if not row:
        return ROLE_STAFF, None
    return normalize_role(row[0]), usable_staff_uuid(row[1])


def _wechat_idx_cache_key(value: Any) -> str:
    return str(value or "").strip()


def _consignee_phone_cache_key(value: Any) -> str:
    """与列表电话聚合一致：仅保留数字；过短视为空。"""
    digits = "".join(filter(str.isdigit, str(value or "")))
    return digits if len(digits) >= 7 else ""


def fold_buyer_idx_aggregates(
    buyer_name: str,
    agg_by_idx: BuyerIdxAggMap,
    month_by_idx: BuyerIdxMonthMap,
    flags_by_idx: BuyerIdxFlagsMap,
    *,
    view_all: bool,
    allowed_aliases: Sequence[str] | frozenset[str] | None = None,
    exclude_phones: Sequence[str] | frozenset[str] | None = None,
) -> tuple[float, int, set[str], bool, bool]:
    """按可见性折叠某一 buyer_name 下各 wechat_idx×电话 桶。

    exclude_phones：排除已由「电话匹配」计入的收件人电话，避免电话+单位双重计数；
    换号场景下，旧电话订单仍可通过单位名补齐进来。
    """
    key = normalize_unit_name(buyer_name)
    buckets = agg_by_idx.get(key) or {}
    if not buckets:
        return 0.0, 0, set(), False, False
    allowed = {
        _wechat_idx_cache_key(a)
        for a in (allowed_aliases or [])
        if _wechat_idx_cache_key(a)
    }
    excluded = {
        _consignee_phone_cache_key(p)
        for p in (exclude_phones or [])
        if _consignee_phone_cache_key(p)
    }
    total_amount = 0.0
    total_count = 0
    months: set[str] = set()
    had_ly = False
    has_recent = False
    month_buckets = month_by_idx.get(key) or {}
    flag_buckets = flags_by_idx.get(key) or {}
    for idx_key, phone_buckets in buckets.items():
        if not view_all:
            if idx_key and idx_key not in allowed:
                continue
        month_by_phone = month_buckets.get(idx_key) or {}
        flag_by_phone = flag_buckets.get(idx_key) or {}
        for phone_key, (amt, cnt) in (phone_buckets or {}).items():
            if phone_key and phone_key in excluded:
                continue
            total_amount += float(amt or 0)
            total_count += int(cnt or 0)
            months |= set(month_by_phone.get(phone_key) or set())
            ly, recent = flag_by_phone.get(phone_key, (False, False))
            had_ly = had_ly or bool(ly)
            has_recent = has_recent or bool(recent)
    return total_amount, total_count, months, had_ly, has_recent


def nearby_calendar_months(month: int, delta: int = LAST_YEAR_NEARBY_MONTH_DELTA) -> list[int]:
    """当前月 ±delta 的日历月份列表（1–12，可跨年环绕）。"""
    m = max(1, min(12, int(month)))
    d = max(0, int(delta))
    return [((m - 1 + i) % 12) + 1 for i in range(-d, d + 1)]


def order_window_bounds(
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime, datetime, list[int]]:
    """返回 (去年初, 今年初, 近 N 天起点, 去年临近月份列表)，供列表订单窗口统计复用。"""
    dt = now or datetime.now()
    year = dt.year
    return (
        datetime(year - 1, 1, 1),
        datetime(year, 1, 1),
        dt - timedelta(days=RECENT_ORDER_DAYS),
        nearby_calendar_months(dt.month),
    )


def digits_phone(value: Any) -> str:
    return "".join(filter(str.isdigit, str(value or "")))


def normalize_unit_name(value: Any) -> str:
    return str(value or "").strip()


# 客户多电话分隔：逗号/分号/顿号等（不按空格拆，避免「138 0013 8000」被截断）
_PHONE_LIST_SEP_RE = re.compile(r"[,;，；、|/\\]+")


def usable_phones(value: Any) -> list[str]:
    """客户电话字段 → 可用号码列表（多号拆分、仅保留数字、去重保序）。"""
    raw = str(value or "").strip()
    if not raw:
        return []
    parts = _PHONE_LIST_SEP_RE.split(raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        p = digits_phone(part.strip().strip("()（）[]【】\"'"))
        if len(p) < 7 or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def usable_phone(value: Any) -> Optional[str]:
    """取客户电话字段中第一个可用号码（兼容单号调用）。"""
    phones = usable_phones(value)
    return phones[0] if phones else None


def usable_unit_name(value: Any) -> Optional[str]:
    u = normalize_unit_name(value)
    return u if len(u) >= _MIN_UNIT_NAME_LEN else None


def unit_names_fuzzy_match(a: Any, b: Any) -> bool:
    """
    单位名双向包含匹配。
    例：「未央区第五幼儿园」↔「西安市未央区第五幼儿园」
    """
    if not is_unit_name_order_match_enabled():
        return False
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
    if not is_unit_name_order_match_enabled():
        return {}
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
    if not is_unit_name_order_match_enabled():
        return None
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
    staff_uuid: Any = None,
):
    """
    客户 → 订单：consignee_phone 精确匹配（多号拆分后任一命中即可）；
    若开启单位名匹配，另可 buyer_name（采购单位/人）与客户 unit_name 双向包含。
    若开启员工 UUID 匹配且传入 staff_uuid，则 AND raw_orders.staff_uuid 精确相等。
    老客户/可看全量角色不启用该过滤（见 requires_staff_uuid_order_match）。
    """
    clauses = []
    phones = usable_phones(phone)
    if len(phones) == 1:
        clauses.append(RawOrder.consignee_phone == phones[0])
    elif len(phones) > 1:
        clauses.append(RawOrder.consignee_phone.in_(phones))
    unit_clause = unit_name_column_match_clause(RawOrder.buyer_name, unit_name)
    if unit_clause is not None:
        clauses.append(unit_clause)
    if not clauses:
        return None
    identity = or_(*clauses) if len(clauses) > 1 else clauses[0]
    if is_staff_uuid_order_match_enabled():
        su = usable_staff_uuid(staff_uuid)
        if su:
            return and_(identity, RawOrder.staff_uuid == su)
    return identity


async def load_orders_for_customer(
    db: AsyncSession,
    *,
    phone: Any = None,
    unit_name: Any = None,
    staff_uuid: Any = None,
    limit: Optional[int] = None,
    view_all: bool = True,
    allowed_aliases: Sequence[str] | frozenset[str] | None = None,
) -> list[RawOrder]:
    """按电话和/或单位名称拉取客户订单，按下单时间倒序。

    单位名匹配受 order_match_by_unit_name / ORDER_MATCH_BY_UNIT_NAME 开关控制（默认关）。
    员工 UUID 匹配受 order_match_by_staff_uuid / ORDER_MATCH_BY_STAFF_UUID 开关控制（默认关）；
    开启且传入 staff_uuid 时仅返回 raw_orders.staff_uuid 相等的订单。
    view_all=False 时仅返回未归属（wechat_idx 空）或 wechat_idx∈allowed_aliases 的订单。
    allowed_aliases 对应 sales_wechat_accounts.alias_name。
    """
    from core.data_visibility import order_visibility_clause

    await resolve_unit_name_order_match_enabled(db)
    await resolve_staff_uuid_order_match_enabled(db)
    clause = customer_order_match_clause(
        phone=phone, unit_name=unit_name, staff_uuid=staff_uuid
    )
    if clause is None:
        return []
    vis = order_visibility_clause(view_all=view_all, allowed_aliases=allowed_aliases)
    where = and_(clause, vis) if vis is not None else clause
    stmt = select(RawOrder).where(where).order_by(RawOrder.order_time.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    res = await db.execute(stmt)
    return list(res.scalars().all())


def peek_buyer_order_aggregates() -> (
    tuple[BuyerIdxAggMap, BuyerIdxMonthMap, BuyerIdxFlagsMap] | None
):
    """缓存命中则立即返回；未命中返回 None（请求路径勿同步重建）。"""
    now = time.monotonic()
    if (
        _BUYER_AGG_CACHE is not None
        and _BUYER_MONTH_CACHE is not None
        and _BUYER_YEAR_FLAGS_CACHE is not None
        and _BUYER_AGG_CACHE_BUILT_VERSION == _BUYER_AGG_CACHE_VERSION
        and (now - _BUYER_AGG_CACHE_AT) < _BUYER_AGG_CACHE_TTL_SEC
    ):
        return _BUYER_AGG_CACHE, _BUYER_MONTH_CACHE, _BUYER_YEAR_FLAGS_CACHE
    return None


def invalidate_buyer_order_agg_cache() -> None:
    """订单同步后清空缓存；仅 order_match_by_unit_name 开启时再安排后台重建。"""
    global _BUYER_AGG_CACHE, _BUYER_MONTH_CACHE, _BUYER_YEAR_FLAGS_CACHE
    global _BUYER_AGG_CACHE_AT, _BUYER_AGG_CACHE_BUILT_VERSION, _BUYER_AGG_REFRESH_TASK
    _BUYER_AGG_CACHE = None
    _BUYER_MONTH_CACHE = None
    _BUYER_YEAR_FLAGS_CACHE = None
    _BUYER_AGG_CACHE_AT = 0.0
    _BUYER_AGG_CACHE_BUILT_VERSION = 0
    task = _BUYER_AGG_REFRESH_TASK
    if task is not None and not task.done():
        task.cancel()
        _BUYER_AGG_REFRESH_TASK = None
    if not is_unit_name_order_match_enabled():
        return
    schedule_buyer_order_agg_refresh()


async def _rebuild_buyer_order_aggregates() -> None:
    global _BUYER_AGG_CACHE, _BUYER_MONTH_CACHE, _BUYER_YEAR_FLAGS_CACHE
    global _BUYER_AGG_CACHE_AT, _BUYER_AGG_CACHE_BUILT_VERSION
    from database import AsyncSessionLocal
    from core.logger import logger

    async with _BUYER_AGG_REFRESH_LOCK:
        # 双重检查：可能已被其它协程建好
        if peek_buyer_order_aggregates() is not None:
            return
        t0 = time.monotonic()
        try:
            last_year_start, this_year_start, recent_start, nearby_months = order_window_bounds()
            async with AsyncSessionLocal() as db:
                # 单位名匹配关闭时无需重建（避免订单同步路径白扫全表）
                if not await resolve_unit_name_order_match_enabled(db):
                    return
                # 按 buyer_name + wechat_idx + consignee_phone 分桶：
                # 列表可「电话命中后仍用单位名补齐换号订单」，并排除同电话双重计数
                idx_key_expr = func.coalesce(func.trim(RawOrder.wechat_idx), "")
                phone_key_expr = func.coalesce(RawOrder.consignee_phone, "")
                agg_rows = (
                    await db.execute(
                        select(
                            RawOrder.buyer_name,
                            idx_key_expr,
                            phone_key_expr,
                            func.coalesce(func.sum(RawOrder.pay_amount), 0),
                            func.count(RawOrder.id),
                            func.coalesce(
                                func.sum(
                                    case(
                                        (
                                            and_(
                                                RawOrder.order_time >= last_year_start,
                                                RawOrder.order_time < this_year_start,
                                                func.month(RawOrder.order_time).in_(
                                                    nearby_months
                                                ),
                                            ),
                                            1,
                                        ),
                                        else_=0,
                                    )
                                ),
                                0,
                            ),
                            func.coalesce(
                                func.sum(
                                    case(
                                        (RawOrder.order_time >= recent_start, 1),
                                        else_=0,
                                    )
                                ),
                                0,
                            ),
                        )
                        .where(RawOrder.buyer_name.is_not(None))
                        .where(RawOrder.buyer_name != "")
                        .group_by(RawOrder.buyer_name, idx_key_expr, phone_key_expr)
                    )
                ).all()
                agg_map: BuyerIdxAggMap = {}
                flags_map: BuyerIdxFlagsMap = {}
                for name, idx_raw, phone_raw, total, cnt, ly_cnt, recent_cnt in agg_rows:
                    key = normalize_unit_name(name)
                    if len(key) < _MIN_UNIT_NAME_LEN:
                        continue
                    idx_key = _wechat_idx_cache_key(idx_raw)
                    phone_key = _consignee_phone_cache_key(phone_raw)
                    agg_map.setdefault(key, {}).setdefault(idx_key, {})[phone_key] = (
                        float(total or 0),
                        int(cnt or 0),
                    )
                    flags_map.setdefault(key, {}).setdefault(idx_key, {})[phone_key] = (
                        int(ly_cnt or 0) > 0,
                        int(recent_cnt or 0) > 0,
                    )

                month_rows = (
                    await db.execute(
                        select(
                            RawOrder.buyer_name,
                            idx_key_expr,
                            phone_key_expr,
                            func.month(RawOrder.order_time),
                        )
                        .where(RawOrder.buyer_name.is_not(None))
                        .where(RawOrder.buyer_name != "")
                        .where(RawOrder.order_time.is_not(None))
                        .group_by(
                            RawOrder.buyer_name,
                            idx_key_expr,
                            phone_key_expr,
                            func.month(RawOrder.order_time),
                        )
                    )
                ).all()
                month_map: BuyerIdxMonthMap = {}
                for name, idx_raw, phone_raw, month_num in month_rows:
                    key = normalize_unit_name(name)
                    if len(key) < _MIN_UNIT_NAME_LEN or not month_num:
                        continue
                    idx_key = _wechat_idx_cache_key(idx_raw)
                    phone_key = _consignee_phone_cache_key(phone_raw)
                    month_map.setdefault(key, {}).setdefault(idx_key, {}).setdefault(
                        phone_key, set()
                    ).add(f"{int(month_num)}月")

            _BUYER_AGG_CACHE = agg_map
            _BUYER_MONTH_CACHE = month_map
            _BUYER_YEAR_FLAGS_CACHE = flags_map
            _BUYER_AGG_CACHE_AT = time.monotonic()
            _BUYER_AGG_CACHE_BUILT_VERSION = _BUYER_AGG_CACHE_VERSION
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
    """后台预热/重建单位名聚合缓存（不阻塞 HTTP）。单位名匹配关闭时跳过。"""
    global _BUYER_AGG_REFRESH_TASK
    if not is_unit_name_order_match_enabled():
        return
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
) -> tuple[BuyerIdxAggMap, BuyerIdxMonthMap, BuyerIdxFlagsMap]:
    """
    同步加载预聚合（仅后台预热或显式 force 时使用）。
    客户列表请求请用 peek_buyer_order_aggregates + schedule_buyer_order_agg_refresh。
    返回结构按 buyer → wechat_idx 分桶，调用方需按可见性折叠。
    """
    if not force:
        hit = peek_buyer_order_aggregates()
        if hit is not None:
            return hit
    await _rebuild_buyer_order_aggregates()
    hit = peek_buyer_order_aggregates()
    if hit is not None:
        return hit
    return {}, {}, {}
