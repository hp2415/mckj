"""夜间增量画像预览缓存（进程内 TTL，减轻重复刷新时的 DB 压力）。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

# 与聊天增量同步（15min）及看板快照刷新（30min）对齐，减轻候选重算
_TTL_TODAY_SEC = 1800.0
_TODAY_BUCKET_SEC = 1800
_TTL_HISTORY_SEC = 86400.0


@dataclass
class _CacheEntry:
    expires_at: float
    value: Any


_ENTRIES: dict[str, _CacheEntry] = {}
_LOCKS: dict[str, asyncio.Lock] = {}
# 后台刷新任务（stale-while-revalidate 单飞，避免同一 key 并发重算）
_REFRESH_TASKS: dict[str, "asyncio.Task[Any]"] = {}


def _cache_key(
    *,
    day_str: str,
    is_today: bool,
    sw_filter: tuple[str, ...],
    respect_watermark: bool,
) -> str:
    if is_today:
        bucket = int(time.time()) // _TODAY_BUCKET_SEC
        return f"today|{day_str}|{bucket}|{sw_filter}|{respect_watermark}"
    return f"history|{day_str}|{sw_filter}|{respect_watermark}"


def _ttl(is_today: bool) -> float:
    return _TTL_TODAY_SEC if is_today else _TTL_HISTORY_SEC


def _schedule_refresh(
    key: str,
    *,
    is_today: bool,
    compute: Callable[[], Awaitable[T]],
) -> None:
    """后台单飞刷新：过期但有旧值时调用，重算成功后替换缓存；失败保留旧值。"""
    existing = _REFRESH_TASKS.get(key)
    if existing and not existing.done():
        return

    async def _runner() -> None:
        try:
            value = await compute()
            _ENTRIES[key] = _CacheEntry(
                expires_at=time.time() + _ttl(is_today), value=value
            )
        except Exception:
            from core.logger import logger

            logger.exception("[nightly cache] 后台刷新失败 key={}（保留旧缓存）", key)
        finally:
            _REFRESH_TASKS.pop(key, None)

    try:
        _REFRESH_TASKS[key] = asyncio.create_task(_runner())
    except RuntimeError:
        # 无运行中的事件循环（极少见，如脚本环境）：忽略后台刷新
        pass


async def get_or_compute(
    key: str,
    *,
    is_today: bool,
    compute: Callable[[], Awaitable[T]],
) -> tuple[T, bool]:
    """返回 (value, from_cache)。

    stale-while-revalidate：
    - 命中未过期：直接返回；
    - 命中已过期：立即返回旧值，并在后台单飞重算（不阻塞当前请求）；
    - 冷启动（无任何旧值）：才同步计算（单飞，避免并发击穿）。
    """
    now = time.time()
    ent = _ENTRIES.get(key)
    if ent and ent.expires_at > now:
        return ent.value, True
    if ent is not None:
        # 过期但有旧值：先返回旧值，后台刷新
        _schedule_refresh(key, is_today=is_today, compute=compute)
        return ent.value, True

    lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        ent = _ENTRIES.get(key)
        if ent is not None:
            # 等锁期间已被其他协程填充（无论是否过期都先用，过期项会在后续请求触发后台刷新）
            return ent.value, ent.expires_at > now
        value = await compute()
        _ENTRIES[key] = _CacheEntry(
            expires_at=time.time() + _ttl(is_today), value=value
        )
        return value, False


def invalidate_prefix(prefix: str) -> None:
    dead = [k for k in _ENTRIES if k.startswith(prefix)]
    for k in dead:
        _ENTRIES.pop(k, None)


def preview_cache_key(
    *,
    day_str: str,
    is_today: bool,
    sw_filter: list[str],
    respect_watermark: bool,
) -> str:
    return _cache_key(
        day_str=day_str,
        is_today=is_today,
        sw_filter=tuple(sorted(sw_filter)),
        respect_watermark=respect_watermark,
    )


def nightly_candidates_cache_key(*, since_ms: int, until_ms: int) -> str:
    """候选列表统一缓存键（全量销售号，不含 respect_watermark / sw 过滤）。"""
    bucket = int(time.time()) // _TODAY_BUCKET_SEC
    # 今日窗口用 30 分钟桶；历史日期 since/until 固定
    if until_ms - since_ms <= 86_400_000 + 60_000:
        from ai.profile_nightly import SHANGHAI_TZ, calendar_day_window_ms
        from datetime import datetime

        today_start, _ = calendar_day_window_ms(datetime.now(SHANGHAI_TZ))
        if since_ms == today_start:
            return f"candidates|today|{bucket}"
    return f"candidates|history|{since_ms}|{until_ms}"
