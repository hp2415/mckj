"""原始客户 AI 画像批任务的进度状态（单进程内存；多 worker 时每个进程独立）。"""
from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": "idle",
    "total": 0,
    "done": 0,
    "failed": 0,
    "current_raw_id": None,
    "message": "",
    "started_at": None,
    "finished_at": None,
}


def snapshot() -> dict[str, Any]:
    with _lock:
        raw = dict(_state)
    total = int(raw.get("total") or 0)
    done = int(raw.get("done") or 0)
    failed = int(raw.get("failed") or 0)
    processed = done + failed
    percent = round(100.0 * processed / total, 1) if total > 0 else 0.0
    raw["processed"] = processed
    raw["percent"] = percent
    return raw


def reset_for_start(total: int) -> None:
    with _lock:
        _state.update(
            {
                "status": "running",
                "total": int(total),
                "done": 0,
                "failed": 0,
                "current_raw_id": None,
                "message": "",
                "started_at": time.time(),
                "finished_at": None,
            }
        )


def set_current(raw_id: str) -> None:
    with _lock:
        _state["current_raw_id"] = raw_id


def record_success() -> None:
    with _lock:
        _state["done"] = int(_state.get("done") or 0) + 1


def record_fail(hint: str = "") -> None:
    with _lock:
        _state["failed"] = int(_state.get("failed") or 0) + 1
        if hint:
            _state["message"] = str(hint)[:500]


def complete() -> None:
    with _lock:
        _state["status"] = "completed"
        _state["current_raw_id"] = None
        _state["finished_at"] = time.time()


def fail_job(msg: str) -> None:
    with _lock:
        _state["status"] = "failed"
        _state["message"] = str(msg)[:500]
        _state["current_raw_id"] = None
        _state["finished_at"] = time.time()
