"""
管理后台「任务分配」异步作业状态（内存，单机进程内有效；重启后丢失）。

用于避免 HTTP 长时间阻塞：提交后立即返回 job_id，前端轮询 /admin/task-allocation?format=job。
进程重启后可通过数据库 status=generating 的批次恢复进度展示。
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


def create_job(sales_wechat_id: str, period: str, *, batch_id: int | None = None) -> str:
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
        "batch_id": batch_id,
        "task_count": None,
        "error": None,
        "created_at": time.time(),
    }
    return jid


def find_active_job(sales_wechat_id: str, period: str) -> dict[str, Any] | None:
    """返回同一销售+周期仍在排队/执行中的 job（内存）。"""
    sw = (sales_wechat_id or "").strip()
    p = (period or "").strip()
    if not sw or not p:
        return None
    _prune()
    for row in _STORE.values():
        if (
            (row.get("sales_wechat_id") or "").strip() == sw
            and (row.get("period") or "").strip() == p
            and row.get("status") in ("queued", "running")
        ):
            return dict(row)
    return None


def try_acquire_job(
    sales_wechat_id: str,
    period: str,
    *,
    batch_id: int | None = None,
) -> tuple[str | None, str | None]:
    """
    若已有同销售+周期的活跃 job 则拒绝；否则创建并返回 (job_id, error_message)。
    """
    active = find_active_job(sales_wechat_id, period)
    if active:
        phase = active.get("phase") or "执行中"
        jid = active.get("job_id") or ""
        return None, f"该销售本周期已有分配任务进行中（{phase}，job={jid}），请等待完成后再试"
    return create_job(sales_wechat_id, period, batch_id=batch_id), None


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
