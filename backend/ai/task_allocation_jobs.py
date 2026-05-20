"""
管理后台「任务分配」异步作业状态（内存，单机进程内有效；重启后丢失）。

用于避免 HTTP 长时间阻塞：提交后立即返回 job_id，前端轮询 /admin/task-allocation?format=job。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

_lock = asyncio.Lock()
_STORE: dict[str, dict[str, Any]] = {}
_TTL_SEC = 7200


def _prune() -> None:
    now = time.time()
    dead = [k for k, v in _STORE.items() if now - float(v.get("created_at", 0)) > _TTL_SEC]
    for k in dead:
        _STORE.pop(k, None)


def create_job(sales_wechat_id: str, period: str) -> str:
    _prune()
    jid = uuid.uuid4().hex[:20]
    _STORE[jid] = {
        "job_id": jid,
        "sales_wechat_id": sales_wechat_id,
        "period": period,
        "status": "queued",
        "phase": "排队中",
        "detail": "",
        "pct": 0.0,
        "batch_id": None,
        "task_count": None,
        "error": None,
        "created_at": time.time(),
    }
    return jid


async def update_job(job_id: str, **kw: Any) -> None:
    async with _lock:
        row = _STORE.get(job_id)
        if row is None:
            return
        row.update(kw)


async def get_job(job_id: str) -> dict[str, Any] | None:
    async with _lock:
        r = _STORE.get(job_id)
        return dict(r) if r else None
