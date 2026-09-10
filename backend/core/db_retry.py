"""MySQL 锁等待 / 死锁重试。

InnoDB 在 1205（lock wait timeout）与 1213（deadlock）时会回滚当前事务，
官方建议 restart transaction。仅用于短写库，不要包住 LLM 调用。
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from core.logger import logger

T = TypeVar("T")

MYSQL_LOCK_ERRNOS = frozenset({1205, 1213})
DEFAULT_LOCK_RETRY_ATTEMPTS = 3


def mysql_lock_errno(exc: BaseException | None) -> int | None:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        args = getattr(cur, "args", None)
        if args:
            try:
                code = int(args[0])
            except (TypeError, ValueError):
                code = None
            if code in MYSQL_LOCK_ERRNOS:
                return code
        orig = getattr(cur, "orig", None)
        if isinstance(orig, BaseException) and orig is not cur:
            code = mysql_lock_errno(orig)
            if code is not None:
                return code
        nxt = cur.__cause__ or cur.__context__
        cur = nxt if nxt is not cur else None
    text = str(exc or "")
    if "Lock wait timeout exceeded" in text:
        return 1205
    if "Deadlock found when trying to get lock" in text:
        return 1213
    return None


def is_mysql_lock_error(exc: BaseException | None) -> bool:
    return mysql_lock_errno(exc) is not None


def is_mysql_lock_error_text(text: str | None) -> bool:
    s = str(text or "")
    return (
        "Lock wait timeout exceeded" in s
        or "Deadlock found when trying to get lock" in s
        or "(1205," in s
        or "(1213," in s
    )


def lock_retry_delay_s(attempt_index: int) -> float:
    """attempt_index 为已失败次数（0 = 第一次重试前）。"""
    return 0.05 * (2 ** max(0, attempt_index)) + random.random() * 0.05


async def run_with_mysql_lock_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = DEFAULT_LOCK_RETRY_ATTEMPTS,
    what: str = "db write",
) -> T:
    n = max(1, int(attempts))
    last: BaseException | None = None
    for i in range(n):
        try:
            return await fn()
        except Exception as e:
            last = e
            if not is_mysql_lock_error(e) or i + 1 >= n:
                raise
            delay = lock_retry_delay_s(i)
            logger.warning(
                "MySQL 锁冲突将重试 {}/{} after {:.0f}ms ({}) errno={} err={}",
                i + 1,
                n - 1,
                delay * 1000,
                what,
                mysql_lock_errno(e),
                e,
            )
            await asyncio.sleep(delay)
    raise last  # pragma: no cover
