"""原始客户 AI 画像批任务：进度与取消（DB 队列版本）。"""
from __future__ import annotations

import time
import uuid
from typing import Any


def new_batch_meta(kind: str, count: int, label: str) -> dict[str, Any]:
    return {
        "batch_id": uuid.uuid4().hex[:12],
        "kind": kind,
        "count": int(count),
        "label": (label or "").strip() or kind,
        "enqueued_at": time.time(),
    }


async def snapshot() -> dict[str, Any]:
    from ai.profile_queue import snapshot_queue

    return await snapshot_queue()


def request_cancel() -> None:
    """跨进程中断：写入 system_configs.profile_cancel_requested=1（不中断正在进行的单条）。"""
    try:
        loop = None
        try:
            import asyncio

            loop = asyncio.get_running_loop()
        except Exception:
            loop = None
        from ai.profile_queue import request_cancel_db

        if loop and loop.is_running():
            loop.create_task(request_cancel_db())
            return
    except Exception:
        pass
    # 无事件循环时：不抛错（管理后台 POST 触发时通常有 loop）
    return
