"""跨模块互斥：避免 raw_chat_logs 大批量写入与夜间候选重扫同时压垮 MySQL。

聊天增量 upsert 与 collect_nightly_candidates 都打同一张大表；在本机
innodb_buffer_pool 偏小（常见 128M）时同刻运行会直接把 mysqld 打崩。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

_LOCK = asyncio.Lock()


@asynccontextmanager
async def heavy_db_section(label: str = "") -> AsyncIterator[None]:
    """串行化「重 DB」临界区；label 仅用于日志定位。"""
    from core.logger import logger

    waited = _LOCK.locked()
    if waited:
        logger.info("[db_heavy_gate] 等待互斥 label={}", label or "-")
    async with _LOCK:
        if waited:
            logger.info("[db_heavy_gate] 获得互斥 label={}", label or "-")
        yield
