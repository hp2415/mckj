"""
夜间增量画像 - 候选选择器与调度逻辑
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from typing import Iterable, Any

from sqlalchemy import and_, case, func, tuple_
from sqlalchemy.future import select

from database import AsyncSessionLocal
from models import (
    ContactTask,
    RawChatLog,
    RawCustomerSalesWechat,
    SalesCustomerProfile,
    SalesWechatAccount,
    UserSalesWechat,
)
from core.logger import logger
from ai.chat_log_filter import raw_chat_log_meaningful_clause
from ai.profile_staff_tag import load_staff_tagged_pair_keys
from ai.raw_chat_time import (
    calendar_day_window_ms,
    profiled_at_to_ms,
    raw_chat_event_time_ms_expr,
    raw_chat_in_event_window_clause,
    scp_profiled_at_ms_expr,
)
from ai.raw_profiling import rcsw_active_for_profile_where


SHANGHAI_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class NightlyCandidate:
    """单对候选画像任务的可观测元信息。预览页和日志都用这个结构。"""

    raw_customer_id: str
    sales_wechat_id: str
    # 窗口内最新一条聊天的发送时间（send_timestamp_ms，无则回退 time_ms）
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


def _merge_chat_bucket_maps(
    *maps: dict[tuple[str, str], tuple[int, int]],
) -> dict[tuple[str, str], tuple[int, int]]:
    out: dict[tuple[str, str], tuple[int, int]] = {}
    for m in maps:
        _aggregate_chat_buckets(
            ((rid, sw, latest, cnt) for (rid, sw), (latest, cnt) in m.items()),
            out,
        )
    return out


async def _fetch_chat_buckets_in_window(
    db,
    eligible,
    *,
    since_ms: int,
    until_ms: int,
    bound_sales: frozenset[str],
) -> dict[tuple[str, str], tuple[int, int]]:
    """日历窗口内（按发送时间）有有效聊天的 (raw_customer_id, sales_wechat_id)。"""
    chat_filters = raw_chat_in_event_window_clause(since_ms, until_ms)
    event_ms = raw_chat_event_time_ms_expr()
    bound_list = list(bound_sales)
    sales_on_chat = RawChatLog.wechat_id.in_(bound_list)
    sales_on_talker = RawChatLog.talker.in_(bound_list)

    stmt_a = (
        select(
            eligible.c.raw_customer_id,
            eligible.c.sales_wechat_id,
            func.max(event_ms).label("latest"),
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
            func.max(event_ms).label("latest"),
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
    buckets: dict[tuple[str, str], tuple[int, int]] = {}
    _aggregate_chat_buckets(res_a, buckets)
    _aggregate_chat_buckets(res_b, buckets)
    return buckets


async def _fetch_chat_buckets_newer_than_profile(
    db,
    eligible,
    *,
    bound_sales: frozenset[str],
) -> dict[tuple[str, str], tuple[int, int]]:
    """
    不限日历窗口：全局最新发送时间晚于 SCP.profiled_at 的对（画像后有新聊）。
    覆盖「发送日在窗口内但保存日已跨天」等仅按 time_ms 会漏掉的场景。
    """
    event_ms = raw_chat_event_time_ms_expr()
    profiled_ms = scp_profiled_at_ms_expr(SalesCustomerProfile.profiled_at)
    meaningful = raw_chat_log_meaningful_clause(RawChatLog.text)
    bound_list = list(bound_sales)
    sales_on_chat = RawChatLog.wechat_id.in_(bound_list)
    sales_on_talker = RawChatLog.talker.in_(bound_list)
    join_scp = and_(
        SalesCustomerProfile.raw_customer_id == eligible.c.raw_customer_id,
        SalesCustomerProfile.sales_wechat_id == eligible.c.sales_wechat_id,
        SalesCustomerProfile.profile_status == 1,
        SalesCustomerProfile.profiled_at.isnot(None),
    )
    new_msg_cnt = func.sum(case((event_ms > profiled_ms, 1), else_=0))

    stmt_a = (
        select(
            eligible.c.raw_customer_id,
            eligible.c.sales_wechat_id,
            func.max(event_ms).label("latest"),
            new_msg_cnt.label("cnt"),
        )
        .select_from(eligible)
        .join(SalesCustomerProfile, join_scp)
        .join(
            RawChatLog,
            and_(
                eligible.c.sales_wechat_id == RawChatLog.wechat_id,
                eligible.c.raw_customer_id == RawChatLog.talker,
                meaningful,
            ),
        )
        .where(sales_on_chat)
        .group_by(eligible.c.raw_customer_id, eligible.c.sales_wechat_id)
        # MySQL HAVING 须用聚合列，不能裸引 join 表的 profiled_at
        .having(func.max(event_ms) > func.max(profiled_ms))
    )
    stmt_b = (
        select(
            eligible.c.raw_customer_id,
            eligible.c.sales_wechat_id,
            func.max(event_ms).label("latest"),
            new_msg_cnt.label("cnt"),
        )
        .select_from(eligible)
        .join(SalesCustomerProfile, join_scp)
        .join(
            RawChatLog,
            and_(
                eligible.c.raw_customer_id == RawChatLog.wechat_id,
                eligible.c.sales_wechat_id == RawChatLog.talker,
                meaningful,
            ),
        )
        .where(sales_on_talker)
        .group_by(eligible.c.raw_customer_id, eligible.c.sales_wechat_id)
        .having(func.max(event_ms) > func.max(profiled_ms))
    )

    async def _fetch_rows(stmt):
        async with AsyncSessionLocal() as sess:
            return (await sess.execute(stmt)).all()

    res_a, res_b = await asyncio.gather(_fetch_rows(stmt_a), _fetch_rows(stmt_b))
    buckets: dict[tuple[str, str], tuple[int, int]] = {}
    _aggregate_chat_buckets(res_a, buckets)
    _aggregate_chat_buckets(res_b, buckets)
    return buckets


def _window_ref_date(since_ms: int) -> date:
    """日历窗口起始日（Asia/Shanghai），与 calendar_day_window_ms 对齐。"""
    return datetime.fromtimestamp(since_ms / 1000, tz=SHANGHAI_TZ).date()


async def _fetch_skipped_task_buckets_in_window(
    db,
    eligible,
    *,
    since_ms: int,
    bound_sales: frozenset[str],
) -> dict[tuple[str, str], tuple[int, int]]:
    """
    窗口对应日当天被跳过的联系任务（含申诉 skipped）。
    latest 取 updated_at 毫秒，便于与 profiled_at 比较；chat_count 恒为 0。
    """
    ref_date = _window_ref_date(since_ms)
    bound_list = list(bound_sales)
    stmt = (
        select(
            eligible.c.raw_customer_id,
            eligible.c.sales_wechat_id,
            ContactTask.updated_at,
        )
        .select_from(ContactTask)
        .join(
            eligible,
            and_(
                eligible.c.raw_customer_id == ContactTask.raw_customer_id,
                eligible.c.sales_wechat_id == ContactTask.sales_wechat_id,
            ),
        )
        .where(
            ContactTask.status == "skipped",
            ContactTask.due_date == ref_date,
            ContactTask.sales_wechat_id.in_(bound_list),
        )
    )
    res = await db.execute(stmt)
    buckets: dict[tuple[str, str], tuple[int, int]] = {}
    for rid, sw, updated_at in res.all():
        if not rid or not sw:
            continue
        key = (str(rid), str(sw))
        skip_ms = profiled_at_to_ms(updated_at)
        prev_latest, _ = buckets.get(key, (0, 0))
        buckets[key] = (max(skip_ms, prev_latest), 0)
    return buckets


def _should_enqueue_nightly_pair(
    *,
    latest_ms: int,
    profile_status: int,
    profiled_at: datetime | None,
    respect_watermark: bool,
) -> bool:
    """是否入队：未画像 / 强制 / 或最新聊天发送时间晚于画像完成时间。"""
    if not respect_watermark:
        return True
    if profile_status != 1 or profiled_at is None:
        return True
    return latest_ms > profiled_at_to_ms(profiled_at)


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


def _filter_respect_watermark(cands: list[NightlyCandidate]) -> list[NightlyCandidate]:
    return [
        c
        for c in cands
        if c.profiled_at is None
        or c.latest_chat_ms > profiled_at_to_ms(c.profiled_at)
    ]


def nightly_counts_from_candidates(cands: list[NightlyCandidate]) -> tuple[int, int]:
    """返回 (今日活跃对数, 待画像对数)。"""
    updated = len(cands)
    pending = len(_filter_respect_watermark(cands))
    return updated, pending


async def get_cached_nightly_candidates(
    since_ms: int,
    until_ms: int,
    *,
    sales_wechat_ids: Iterable[str] | None = None,
    respect_watermark: bool = True,
) -> tuple[list[NightlyCandidate], bool]:
    """
    带进程内缓存的候选收集：DB 侧始终拉全量（respect_watermark=False），
    水印/销售号过滤在内存完成，供看板计数与预览页共用。
    """
    from ai.profile_nightly_cache import get_or_compute, nightly_candidates_cache_key

    sw_filter = [s.strip() for s in (sales_wechat_ids or []) if s and s.strip()]
    today_start, _ = calendar_day_window_ms(datetime.now(SHANGHAI_TZ))
    is_today = since_ms == today_start

    cache_key = nightly_candidates_cache_key(since_ms=since_ms, until_ms=until_ms)

    async def _compute() -> list[NightlyCandidate]:
        return await collect_nightly_candidates(
            since_ms,
            until_ms,
            respect_watermark=False,
        )

    all_cands, from_cache = await get_or_compute(
        cache_key,
        is_today=is_today,
        compute=_compute,
    )

    cands = all_cands
    if sw_filter:
        sw_set = frozenset(sw_filter)
        cands = [c for c in cands if c.sales_wechat_id in sw_set]
    if respect_watermark:
        cands = _filter_respect_watermark(cands)
    return cands, from_cache


async def collect_nightly_candidates(
    since_ms: int,
    until_ms: int,
    *,
    sales_wechat_ids: Iterable[str] | None = None,
    respect_watermark: bool = True,
) -> list[NightlyCandidate]:
    """
    收集待画像候选（销售号已绑定、非工作人员）：
    1) 日历窗口 [since_ms, until_ms) 内按发送时间有有效聊天；
    2) 或全局最新发送时间晚于 SCP.profiled_at（画像后有新聊，含跨天入库）；
    3) 或窗口对应日当天有 status=skipped 的联系任务（用跳过时间作水位比较）。
    respect_watermark 时仅排除「已画像且最新活动时间不晚于 profiled_at」的对。
    """
    sw_filter = [s.strip() for s in (sales_wechat_ids or []) if s and s.strip()]

    async with AsyncSessionLocal() as db:
        id_sets = await load_nightly_id_sets(db, sales_wechat_ids=sw_filter or None)
        if not id_sets.bound_sales:
            return []

        eligible = _eligible_rcsw_subquery(id_sets)
        window_buckets, stale_buckets, skip_buckets = await asyncio.gather(
            _fetch_chat_buckets_in_window(
                db,
                eligible,
                since_ms=since_ms,
                until_ms=until_ms,
                bound_sales=id_sets.bound_sales,
            ),
            _fetch_chat_buckets_newer_than_profile(
                db,
                eligible,
                bound_sales=id_sets.bound_sales,
            ),
            _fetch_skipped_task_buckets_in_window(
                db,
                eligible,
                since_ms=since_ms,
                bound_sales=id_sets.bound_sales,
            ),
        )
        buckets = _merge_chat_bucket_maps(window_buckets, stale_buckets, skip_buckets)
        if not buckets:
            return []

        profile_map = await _load_profile_map(db, list(buckets.keys()))
        staff_pairs = await load_staff_tagged_pair_keys(db, buckets.keys())

        out: list[NightlyCandidate] = []
        for key, (latest_ms, cnt) in buckets.items():
            if key in staff_pairs:
                continue
            pat, profile_status = profile_map.get(key, (None, 0))
            if not _should_enqueue_nightly_pair(
                latest_ms=latest_ms,
                profile_status=profile_status,
                profiled_at=pat,
                respect_watermark=respect_watermark,
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
    """每天凌晨跑：昨日有聊天、任务跳过或画像后有新聊且销售号已绑定的对入队。"""
    from ai.raw_profiling import enqueue_profile_sales_pairs

    now = datetime.now(SHANGHAI_TZ)
    yday = now - timedelta(days=1)
    since_ms, until_ms = calendar_day_window_ms(yday)
    pairs = await collect_pairs_updated_in_window(since_ms, until_ms)

    if not pairs:
        logger.info(
            "[Nightly Profile] 昨日无符合条件的对（聊天/任务跳过/画像后有新聊），跳过"
        )
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
