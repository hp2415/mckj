"""
夜间增量画像 - 候选选择器与调度逻辑
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Iterable, Any

from sqlalchemy import and_, func, tuple_
from sqlalchemy.future import select

from database import AsyncSessionLocal
from models import (
    RawChatLog,
    RawCustomerSalesWechat,
    SalesCustomerProfile,
    SalesWechatAccount,
    UserSalesWechat,
)
from core.logger import logger
from ai.chat_log_filter import raw_chat_log_meaningful_clause
from ai.raw_profiling import rcsw_active_for_profile_where


SHANGHAI_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class NightlyCandidate:
    """单对候选画像任务的可观测元信息。预览页和日志都用这个结构。"""

    raw_customer_id: str
    sales_wechat_id: str
    # 窗口内最新一条聊天的 time_ms
    latest_chat_ms: int
    # 窗口内该对的聊天条数
    chat_count: int
    # 当前画像时间
    profiled_at: datetime | None


@dataclass(frozen=True)
class NightlyIdSets:
    """预加载的过滤 ID 集合，避免 SQL 中 correlated EXISTS。"""

    bound_sales: frozenset[str]
    sales_master: frozenset[str]


def calendar_day_window_ms(day: datetime | None = None) -> tuple[int, int]:
    """返回 [day 00:00, next-day 00:00) 在 Asia/Shanghai 下的毫秒区间。"""
    base = (day or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    start = base.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


async def load_nightly_id_sets(
    db,
    *,
    sales_wechat_ids: Iterable[str] | None = None,
) -> NightlyIdSets:
    sw_filter = [s.strip() for s in (sales_wechat_ids or []) if s and s.strip()]
    if sw_filter:
        bound_sales = frozenset(sw_filter)
    else:
        bind_res = await db.execute(select(UserSalesWechat.sales_wechat_id))
        bound_sales = frozenset(
            s.strip() for s in bind_res.scalars().all() if s and str(s).strip()
        )
    master_res = await db.execute(select(SalesWechatAccount.sales_wechat_id))
    sales_master = frozenset(
        s.strip() for s in master_res.scalars().all() if s and str(s).strip()
    )
    return NightlyIdSets(bound_sales=bound_sales, sales_master=sales_master)


def _chat_in_window_clause(since_ms: int, until_ms: int):
    return and_(
        RawChatLog.time_ms >= since_ms,
        RawChatLog.time_ms < until_ms,
        raw_chat_log_meaningful_clause(RawChatLog.text),
    )


def _eligible_rcsw_subquery(id_sets: NightlyIdSets):
    """预筛好友关系：有效、销售号已绑定、好友非主数据销售号。"""
    clauses = [
        rcsw_active_for_profile_where(),
        RawCustomerSalesWechat.sales_wechat_id.in_(id_sets.bound_sales),
    ]
    if id_sets.sales_master:
        clauses.append(~RawCustomerSalesWechat.raw_customer_id.in_(id_sets.sales_master))
    return (
        select(
            RawCustomerSalesWechat.raw_customer_id.label("raw_customer_id"),
            RawCustomerSalesWechat.sales_wechat_id.label("sales_wechat_id"),
        )
        .where(and_(*clauses))
        .subquery("eligible_rcsw")
    )


def _aggregate_chat_buckets(
    rows: Iterable[tuple[Any, ...]],
    buckets: dict[tuple[str, str], tuple[int, int]],
) -> None:
    for rid, sw, latest, cnt in rows:
        if not rid or not sw:
            continue
        key = (str(rid), str(sw))
        prev_latest, prev_cnt = buckets.get(key, (0, 0))
        buckets[key] = (
            max(int(latest or 0), prev_latest),
            int(cnt or 0) + prev_cnt,
        )


async def _load_profile_map(db, pair_keys: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[Any, int]]:
    profile_map: dict[tuple[str, str], tuple[Any, int]] = {}
    chunk_size = 800
    for i in range(0, len(pair_keys), chunk_size):
        chunk = pair_keys[i : i + chunk_size]
        scp_res = await db.execute(
            select(
                SalesCustomerProfile.raw_customer_id,
                SalesCustomerProfile.sales_wechat_id,
                SalesCustomerProfile.profiled_at,
                SalesCustomerProfile.profile_status,
            ).where(
                tuple_(
                    SalesCustomerProfile.raw_customer_id,
                    SalesCustomerProfile.sales_wechat_id,
                ).in_(chunk)
            )
        )
        for rid, sw, pat, status in scp_res.all():
            profile_map[(str(rid), str(sw))] = (pat, int(status or 0))
    return profile_map


async def collect_nightly_candidates(
    since_ms: int,
    until_ms: int,
    *,
    sales_wechat_ids: Iterable[str] | None = None,
    respect_watermark: bool = True,
) -> list[NightlyCandidate]:
    """收集窗口内待画像候选：有意义聊天、销售号已绑定、好友非主数据销售号；已画像的再按水位过滤。"""
    sw_filter = [s.strip() for s in (sales_wechat_ids or []) if s and s.strip()]

    buckets: dict[tuple[str, str], tuple[int, int]] = {}

    async with AsyncSessionLocal() as db:
        id_sets = await load_nightly_id_sets(db, sales_wechat_ids=sw_filter or None)
        if not id_sets.bound_sales:
            return []

        eligible = _eligible_rcsw_subquery(id_sets)
        chat_filters = _chat_in_window_clause(since_ms, until_ms)
        bound_list = list(id_sets.bound_sales)
        sales_on_chat = RawChatLog.wechat_id.in_(bound_list)
        sales_on_talker = RawChatLog.talker.in_(bound_list)

        stmt_a = (
            select(
                eligible.c.raw_customer_id,
                eligible.c.sales_wechat_id,
                func.max(RawChatLog.time_ms).label("latest"),
                func.count(RawChatLog.id).label("cnt"),
            )
            .select_from(RawChatLog)
            .join(
                eligible,
                and_(
                    eligible.c.sales_wechat_id == RawChatLog.wechat_id,
                    eligible.c.raw_customer_id == RawChatLog.talker,
                ),
            )
            .where(chat_filters, sales_on_chat)
            .group_by(eligible.c.raw_customer_id, eligible.c.sales_wechat_id)
        )
        stmt_b = (
            select(
                eligible.c.raw_customer_id,
                eligible.c.sales_wechat_id,
                func.max(RawChatLog.time_ms).label("latest"),
                func.count(RawChatLog.id).label("cnt"),
            )
            .select_from(RawChatLog)
            .join(
                eligible,
                and_(
                    eligible.c.raw_customer_id == RawChatLog.wechat_id,
                    eligible.c.sales_wechat_id == RawChatLog.talker,
                ),
            )
            .where(chat_filters, sales_on_talker)
            .group_by(eligible.c.raw_customer_id, eligible.c.sales_wechat_id)
        )

        async def _fetch_rows(stmt):
            async with AsyncSessionLocal() as sess:
                return (await sess.execute(stmt)).all()

        res_a, res_b = await asyncio.gather(_fetch_rows(stmt_a), _fetch_rows(stmt_b))
        _aggregate_chat_buckets(res_a, buckets)
        _aggregate_chat_buckets(res_b, buckets)

        if not buckets:
            return []

        profile_map = await _load_profile_map(db, list(buckets.keys()))
        until_dt = datetime.fromtimestamp(until_ms / 1000)

        out: list[NightlyCandidate] = []
        for key, (latest_ms, cnt) in buckets.items():
            pat, profile_status = profile_map.get(key, (None, 0))
            if (
                respect_watermark
                and profile_status == 1
                and pat is not None
                and pat >= until_dt
            ):
                continue
            out.append(
                NightlyCandidate(
                    raw_customer_id=key[0],
                    sales_wechat_id=key[1],
                    latest_chat_ms=latest_ms,
                    chat_count=cnt,
                    profiled_at=pat,
                )
            )

    out.sort(key=lambda c: (c.latest_chat_ms, c.raw_customer_id), reverse=True)
    return out


async def collect_pairs_updated_in_window(
    since_ms: int,
    until_ms: int,
    *,
    sales_wechat_ids: Iterable[str] | None = None,
    respect_watermark: bool = True,
) -> list[tuple[str, str]]:
    """裸 pair 列表入口（给调度器 / CLI 用）。"""
    cands = await collect_nightly_candidates(
        since_ms,
        until_ms,
        sales_wechat_ids=sales_wechat_ids,
        respect_watermark=respect_watermark,
    )
    return [(c.raw_customer_id, c.sales_wechat_id) for c in cands]


async def scheduled_nightly_profile_refresh() -> None:
    """每天凌晨跑：把"昨日 00:00 ~ 今日 00:00"有聊天且销售号已绑定的对入队（含未画像新客户）。"""
    from ai.raw_profiling import enqueue_profile_sales_pairs

    now = datetime.now(SHANGHAI_TZ)
    yday = now - timedelta(days=1)
    since_ms, until_ms = calendar_day_window_ms(yday)
    pairs = await collect_pairs_updated_in_window(since_ms, until_ms)

    if not pairs:
        logger.info("[Nightly Profile] 昨日无符合条件的对（销售号已绑定且有聊天），跳过")
        return

    label = f"夜间增量画像 {yday.strftime('%Y-%m-%d')}（共{len(pairs)}对）"
    logger.info("[Nightly Profile] 入队 {} 对, label={}", len(pairs), label)
    await enqueue_profile_sales_pairs(pairs, label=label)


async def enqueue_candidates(
    cands: list[NightlyCandidate],
    *,
    label: str,
) -> int:
    """预览页"立刻入队"专用。"""
    from ai.raw_profiling import enqueue_profile_sales_pairs

    pairs = [(c.raw_customer_id, c.sales_wechat_id) for c in cands]
    if not pairs:
        return 0
    await enqueue_profile_sales_pairs(pairs, label=label)
    return len(pairs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", help="YYYY-MM-DD, 默认昨日")
    parser.add_argument("--force", action="store_true", help="忽略水位")
    args = parser.parse_args()

    async def main():
        day = None
        if args.day:
            day = datetime.strptime(args.day, "%Y-%m-%d").replace(tzinfo=SHANGHAI_TZ)
        else:
            day = datetime.now(SHANGHAI_TZ) - timedelta(days=1)
        
        since_ms, until_ms = calendar_day_window_ms(day)
        pairs = await collect_pairs_updated_in_window(
            since_ms, until_ms, respect_watermark=not args.force
        )
        if not pairs:
            print("无候选")
            return
        
        label = f"CLI 增量画像 {day.strftime('%Y-%m-%d')}（{len(pairs)}对）"
        from ai.raw_profiling import enqueue_profile_sales_pairs
        await enqueue_profile_sales_pairs(pairs, label=label)
        print(f"已入队 {len(pairs)} 对")

    asyncio.run(main())
