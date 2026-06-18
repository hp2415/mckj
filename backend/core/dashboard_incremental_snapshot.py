"""看板夜间增量 KPI 预聚合快照（进程内缓存 + 定时刷新）。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from ai.profile_nightly import (
    calendar_day_window_ms,
    get_cached_nightly_candidates,
    nightly_counts_from_candidates,
)
from core.logger import logger
from database import AsyncSessionLocal
from models import SalesCustomerProfile

SNAPSHOT_REFRESH_INTERVAL_MIN = 30
# 略长于定时刷新间隔，避免任务稍有延迟时 API 落到 live 重算
SNAPSHOT_TTL_SEC = float((SNAPSHOT_REFRESH_INTERVAL_MIN + 10) * 60)


@dataclass
class _Snapshot:
    stats: dict[str, int]
    computed_at: float


_snapshot: _Snapshot | None = None
_lock = asyncio.Lock()


async def compute_incremental_stats() -> dict[str, int]:
    day_t0, _ = calendar_day_window_ms()
    now_ms = int(time.time() * 1000)
    today_cands, _ = await get_cached_nightly_candidates(
        day_t0,
        now_ms,
        respect_watermark=False,
    )
    updated_pairs_count, pending_pairs_count = nightly_counts_from_candidates(today_cands)

    last_24h = datetime.now() - timedelta(days=1)
    async with AsyncSessionLocal() as db:
        profiled_24h = int(
            (
                await db.execute(
                    select(func.count(SalesCustomerProfile.id)).where(
                        SalesCustomerProfile.profiled_at >= last_24h
                    )
                )
            ).scalar()
            or 0
        )

    return {
        "incremental_updated": updated_pairs_count,
        "incremental_pending": pending_pairs_count,
        "incremental_completed_24h": profiled_24h,
    }


async def refresh_dashboard_incremental_snapshot() -> dict[str, int]:
    """强制重算并写入快照（供定时任务 / 启动预热）。"""
    global _snapshot
    stats = await compute_incremental_stats()
    _snapshot = _Snapshot(stats=stats, computed_at=time.time())
    return stats


async def get_dashboard_incremental_stats() -> tuple[dict[str, int], str]:
    """返回 (stats, source)；source 为 snapshot 或 live。"""
    global _snapshot
    now = time.time()
    if _snapshot and now - _snapshot.computed_at < SNAPSHOT_TTL_SEC:
        return _snapshot.stats, "snapshot"

    async with _lock:
        now = time.time()
        if _snapshot and now - _snapshot.computed_at < SNAPSHOT_TTL_SEC:
            return _snapshot.stats, "snapshot"
        stats = await compute_incremental_stats()
        _snapshot = _Snapshot(stats=stats, computed_at=time.time())
        return stats, "live"


async def scheduled_dashboard_incremental_snapshot() -> None:
    try:
        stats = await refresh_dashboard_incremental_snapshot()
        logger.info(
            "[APScheduler] 看板夜间增量 KPI 快照已刷新 updated={} pending={} profiled_24h={}",
            stats.get("incremental_updated"),
            stats.get("incremental_pending"),
            stats.get("incremental_completed_24h"),
        )
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.exception("[APScheduler] 看板夜间增量 KPI 快照刷新失败: %s", e)
