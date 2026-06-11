"""夜间增量画像预览缓存（进程内 TTL，减轻重复刷新时的 DB 压力）。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

_TTL_TODAY_SEC = 90.0
_TTL_HISTORY_SEC = 86400.0


@dataclass
class _CacheEntry:
    expires_at: float
    value: Any


_ENTRIES: dict[str, _CacheEntry] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def _cache_key(
    *,
    day_str: str,
    is_today: bool,
    sw_filter: tuple[str, ...],
    respect_watermark: bool,
) -> str:
    if is_today:
        minute_bucket = int(time.time()) // 60
        return f"today|{day_str}|{minute_bucket}|{sw_filter}|{respect_watermark}"
    return f"history|{day_str}|{sw_filter}|{respect_watermark}"


def _ttl(is_today: bool) -> float:
    return _TTL_TODAY_SEC if is_today else _TTL_HISTORY_SEC


async def get_or_compute(
    key: str,
    *,
    is_today: bool,
    compute: Callable[[], Awaitable[T]],
) -> tuple[T, bool]:
    """返回 (value, from_cache)。"""
    now = time.time()
    ent = _ENTRIES.get(key)
    if ent and ent.expires_at > now:
        return ent.value, True

    lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        ent = _ENTRIES.get(key)
        if ent and ent.expires_at > now:
            return ent.value, True
        value = await compute()
        _ENTRIES[key] = _CacheEntry(expires_at=now + _ttl(is_today), value=value)
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
    minute_bucket = int(time.time()) // 60
    # 今日窗口用分钟桶；历史日期 since/until 固定
    if until_ms - since_ms <= 86_400_000 + 60_000:
        from ai.profile_nightly import SHANGHAI_TZ, calendar_day_window_ms
        from datetime import datetime

        today_start, _ = calendar_day_window_ms(datetime.now(SHANGHAI_TZ))
        if since_ms == today_start:
            return f"candidates|today|{minute_bucket}"
    return f"candidates|history|{since_ms}|{until_ms}"
