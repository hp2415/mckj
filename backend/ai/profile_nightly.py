"""
夜间增量画像 - 候选选择器与调度逻辑
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Iterable, Any

from sqlalchemy import and_, or_, func
from sqlalchemy.future import select

from database import AsyncSessionLocal
from models import RawChatLog, RawCustomerSalesWechat, SalesCustomerProfile
from core.logger import logger
from ai.chat_log_filter import raw_chat_log_meaningful_clause


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


def calendar_day_window_ms(day: datetime | None = None) -> tuple[int, int]:
    """返回 [day 00:00, next-day 00:00) 在 Asia/Shanghai 下的毫秒区间。"""
    base = (day or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    start = base.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


async def collect_nightly_candidates(
    since_ms: int,
    until_ms: int,
    *,
    sales_wechat_ids: Iterable[str] | None = None,
    respect_watermark: bool = True,
) -> list[NightlyCandidate]:
    """收集窗口内待重画像候选；强约束 profile_status=1（仅"已分析"才走增量）。"""
    sw_filter = [s.strip() for s in (sales_wechat_ids or []) if s and s.strip()]

    # 用 dict 聚合 (raw_id, sw) -> (max_time_ms, count)
    buckets: dict[tuple[str, str], tuple[int, int]] = {}

    async with AsyncSessionLocal() as db:
        # 段 A: 销售→客户 (rcl.wechat_id == sales, rcl.talker == raw)
        stmt_a = (
            select(
                RawCustomerSalesWechat.raw_customer_id,
                RawCustomerSalesWechat.sales_wechat_id,
                func.max(RawChatLog.time_ms).label("latest"),
                func.count(RawChatLog.id).label("cnt"),
            )
            .join(
                RawChatLog,
                and_(
                    RawChatLog.wechat_id == RawCustomerSalesWechat.sales_wechat_id,
                    RawChatLog.talker == RawCustomerSalesWechat.raw_customer_id,
                    RawChatLog.time_ms >= since_ms,
                    RawChatLog.time_ms < until_ms,
                    raw_chat_log_meaningful_clause(RawChatLog.text),
                ),
            )
            .join(
                SalesCustomerProfile,
                and_(
                    SalesCustomerProfile.raw_customer_id
                    == RawCustomerSalesWechat.raw_customer_id,
                    SalesCustomerProfile.sales_wechat_id
                    == RawCustomerSalesWechat.sales_wechat_id,
                    SalesCustomerProfile.profile_status == 1,
                ),
            )
            .where(
                or_(
                    RawCustomerSalesWechat.is_deleted.is_(False),
                    RawCustomerSalesWechat.is_deleted.is_(None),
                )
            )
            .group_by(
                RawCustomerSalesWechat.raw_customer_id,
                RawCustomerSalesWechat.sales_wechat_id,
            )
        )
        # 段 B: 客户→销售 (rcl.wechat_id == raw, rcl.talker == sales)
        stmt_b = (
            select(
                RawCustomerSalesWechat.raw_customer_id,
                RawCustomerSalesWechat.sales_wechat_id,
                func.max(RawChatLog.time_ms).label("latest"),
                func.count(RawChatLog.id).label("cnt"),
            )
            .join(
                RawChatLog,
                and_(
                    RawChatLog.wechat_id == RawCustomerSalesWechat.raw_customer_id,
                    RawChatLog.talker == RawCustomerSalesWechat.sales_wechat_id,
                    RawChatLog.time_ms >= since_ms,
                    RawChatLog.time_ms < until_ms,
                    raw_chat_log_meaningful_clause(RawChatLog.text),
                ),
            )
            .join(
                SalesCustomerProfile,
                and_(
                    SalesCustomerProfile.raw_customer_id
                    == RawCustomerSalesWechat.raw_customer_id,
                    SalesCustomerProfile.sales_wechat_id
                    == RawCustomerSalesWechat.sales_wechat_id,
                    SalesCustomerProfile.profile_status == 1,
                ),
            )
            .where(
                or_(
                    RawCustomerSalesWechat.is_deleted.is_(False),
                    RawCustomerSalesWechat.is_deleted.is_(None),
                )
            )
            .group_by(
                RawCustomerSalesWechat.raw_customer_id,
                RawCustomerSalesWechat.sales_wechat_id,
            )
        )
        if sw_filter:
            stmt_a = stmt_a.where(RawCustomerSalesWechat.sales_wechat_id.in_(sw_filter))
            stmt_b = stmt_b.where(RawCustomerSalesWechat.sales_wechat_id.in_(sw_filter))

        for stmt in (stmt_a, stmt_b):
            for rid, sw, latest, cnt in (await db.execute(stmt)).all():
                if not rid or not sw:
                    continue
                key = (str(rid), str(sw))
                prev_latest, prev_cnt = buckets.get(key, (0, 0))
                buckets[key] = (
                    max(int(latest or 0), prev_latest),
                    int(cnt or 0) + prev_cnt,
                )

        if not buckets:
            return []

        # 拉这些对的 profiled_at（同时把不满足水位的过滤掉）
        scp_res = await db.execute(
            select(
                SalesCustomerProfile.raw_customer_id,
                SalesCustomerProfile.sales_wechat_id,
                SalesCustomerProfile.profiled_at,
            ).where(
                SalesCustomerProfile.raw_customer_id.in_({k[0] for k in buckets}),
                SalesCustomerProfile.sales_wechat_id.in_({k[1] for k in buckets}),
                SalesCustomerProfile.profile_status == 1,
            )
        )
        profiled_at_map = {
            (str(rid), str(sw)): pat for rid, sw, pat in scp_res.all()
        }
        until_dt = datetime.fromtimestamp(until_ms / 1000)

        out: list[NightlyCandidate] = []
        for key, (latest_ms, cnt) in buckets.items():
            pat = profiled_at_map.get(key)
            if pat is None:
                continue
            if respect_watermark and pat >= until_dt:
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
    """每天凌晨跑：把"昨日 00:00 ~ 今日 00:00"有聊天更新且已分析过的对入队。"""
    from ai.raw_profiling import enqueue_profile_sales_pairs

    now = datetime.now(SHANGHAI_TZ)
    yday = now - timedelta(days=1)
    since_ms, until_ms = calendar_day_window_ms(yday)
    pairs = await collect_pairs_updated_in_window(since_ms, until_ms)

    if not pairs:
        logger.info("[Nightly Profile] 昨日无符合条件的对（已分析且有更新），跳过")
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
