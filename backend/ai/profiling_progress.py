"""原始客户 AI 画像批任务：统一队列、进度、待处理批次、错误列表与取消（单进程内存；多 worker 各自独立）。"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from typing import Any

_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": "idle",
    "total": 0,
    "done": 0,
    "failed": 0,
    "skipped": 0,
    "current_raw_id": None,
    "message": "",
    "started_at": None,
    "finished_at": None,
    "batch_id": "",
    "batch_label": "",
    "active_batch": None,
    "cancel_requested": False,
}
_recent_errors: deque[dict[str, Any]] = deque(maxlen=80)
_pending_display: deque[dict[str, Any]] = deque(maxlen=64)

_batch_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None
_thread_lock = threading.Lock()


def _get_queue() -> asyncio.Queue:
    global _batch_queue
    if _batch_queue is None:
        _batch_queue = asyncio.Queue()
    return _batch_queue


def new_batch_meta(kind: str, count: int, label: str) -> dict[str, Any]:
    return {
        "batch_id": uuid.uuid4().hex[:12],
        "kind": kind,
        "count": int(count),
        "label": (label or "").strip() or kind,
        "enqueued_at": time.time(),
    }


def snapshot() -> dict[str, Any]:
    with _lock:
        raw = dict(_state)
        pending = list(_pending_display)
        errors = list(_recent_errors)
    total = int(raw.get("total") or 0)
    done = int(raw.get("done") or 0)
    failed = int(raw.get("failed") or 0)
    skipped = int(raw.get("skipped") or 0)
    processed = done + failed + skipped
    percent = round(100.0 * processed / total, 1) if total > 0 else 0.0
    raw["processed"] = processed
    raw["percent"] = percent
    raw["pending_batches"] = pending
    raw["recent_errors"] = errors[-40:]
    raw["queue_size"] = _batch_queue.qsize() if _batch_queue is not None else 0
    return raw


def reset_for_start(total: int, batch_meta: dict[str, Any] | None = None) -> None:
    meta = batch_meta or {}
    with _lock:
        _state.update(
            {
                "status": "running",
                "total": int(total),
                "done": 0,
                "failed": 0,
                "skipped": 0,
                "current_raw_id": None,
                "message": "",
                "started_at": time.time(),
                "finished_at": None,
                "batch_id": str(meta.get("batch_id") or ""),
                "batch_label": str(meta.get("label") or ""),
                "cancel_requested": False,
            }
        )


def set_current(raw_id: str) -> None:
    with _lock:
        _state["current_raw_id"] = raw_id


def record_success() -> None:
    with _lock:
        _state["done"] = int(_state.get("done") or 0) + 1


def record_skip() -> None:
    with _lock:
        _state["skipped"] = int(_state.get("skipped") or 0) + 1


def record_fail(hint: str = "", target: str | None = None, detail: str | None = None) -> None:
    msg = (hint or "").strip() or "失败"
    if detail:
        d = str(detail).strip()
        if d:
            msg = f"{msg}: {d}"[:4000]
    with _lock:
        _state["failed"] = int(_state.get("failed") or 0) + 1
        _state["message"] = msg[:800]
        _recent_errors.append(
            {
                "at": time.time(),
                "target": (target or "").strip()[:500],
                "message": msg[:4000],
            }
        )


def complete(*, cancelled: bool = False) -> None:
    with _lock:
        _state["status"] = "cancelled" if cancelled else "completed"
        _state["current_raw_id"] = None
        _state["finished_at"] = time.time()
        _state["active_batch"] = None
        _state["batch_id"] = ""
        _state["batch_label"] = ""


def fail_job(msg: str) -> None:
    with _lock:
        _state["status"] = "failed"
        _state["message"] = str(msg)[:800]
        _state["current_raw_id"] = None
        _state["finished_at"] = time.time()
        _state["active_batch"] = None
        _state["batch_id"] = ""
        _state["batch_label"] = ""
        _recent_errors.append(
            {
                "at": time.time(),
                "target": "__batch__",
                "message": str(msg)[:4000],
            }
        )


def request_cancel() -> None:
    """请求在当前批次下一条开始前停止（不中断正在进行的单条 LLM 调用）。"""
    with _lock:
        if _state.get("status") == "running":
            _state["cancel_requested"] = True
            _state["message"] = "已请求中断，将在当前条目完成后停止后续任务"


def is_cancel_requested() -> bool:
    with _lock:
        return bool(_state.get("cancel_requested"))


def pop_pending_if_match(meta: dict[str, Any]) -> None:
    bid = (meta or {}).get("batch_id")
    if not bid:
        return
    with _lock:
        if _pending_display and _pending_display[0].get("batch_id") == bid:
            _pending_display.popleft()


def set_active_batch(meta: dict[str, Any] | None) -> None:
    with _lock:
        _state["active_batch"] = dict(meta) if meta else None


def ensure_profile_worker() -> None:
    """在事件循环中启动唯一消费者（重复调用安全）。"""
    global _worker_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    with _thread_lock:
        if _worker_task is not None and not _worker_task.done():
            return
        _worker_task = loop.create_task(_profile_worker_loop())


async def enqueue_profile_batch(batch: dict[str, Any]) -> None:
    """将一批画像任务放入队列；由后台 worker 顺序执行。"""
    meta = batch.get("meta")
    if not meta:
        batch["meta"] = new_batch_meta(str(batch.get("kind") or "unknown"), 0, "")
        meta = batch["meta"]
    ensure_profile_worker()
    with _lock:
        _pending_display.append(dict(meta))
    await _get_queue().put(batch)


async def _profile_worker_loop() -> None:
    while True:
        batch = await _get_queue().get()
        try:
            pop_pending_if_match(batch.get("meta") or {})
            set_active_batch(batch.get("meta"))
            from ai.raw_profiling import execute_profile_batch

            await execute_profile_batch(batch)
        except Exception:
            import logging
            import traceback

            logging.getLogger(__name__).exception("画像队列批次异常")
            fail_job(traceback.format_exc()[:1200])
        finally:
            set_active_batch(None)
            _get_queue().task_done()
