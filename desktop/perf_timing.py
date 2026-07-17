"""
桌面端性能耗时诊断。

在 config.ini 中开启：
  [Runtime]
  perf_timing = true

开启后记录并打印热点操作耗时（本地 DB、网络、渲染、图片等）。
关闭时所有 API 为几乎零开销的空操作。
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import time
from typing import Any, Callable, Optional, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

_depth: contextvars.ContextVar[int] = contextvars.ContextVar("perf_depth", default=0)
_action: contextvars.ContextVar[str] = contextvars.ContextVar("perf_action", default="")

# 低于该阈值的单步耗时不打印（动作起止与显式 force 除外），减少噪音
_MIN_LOG_MS = 1.0


def enabled() -> bool:
    try:
        from config_loader import cfg
        return bool(getattr(cfg, "perf_timing", False))
    except Exception:
        return False


def _indent() -> str:
    d = _depth.get()
    if d <= 0:
        return ""
    return "  " * d


def _fmt_extra(extra: dict) -> str:
    if not extra:
        return ""
    parts = []
    for k, v in extra.items():
        if v is None or v == "":
            continue
        if k == "bytes" and isinstance(v, (int, float)):
            parts.append(f"bytes={_fmt_bytes(int(v))}")
        elif k == "size" and isinstance(v, (int, float)):
            parts.append(f"size={_fmt_bytes(int(v))}")
        else:
            parts.append(f"{k}={v}")
    return (" " + " ".join(parts)) if parts else ""


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.2f}MB"


def _emit(message: str) -> None:
    try:
        from logger_cfg import logger
        logger.info(f"[PERF] {message}")
    except Exception:
        print(f"[PERF] {message}", flush=True)


def log(name: str, ms: float, *, force: bool = False, **extra: Any) -> None:
    """直接打印一条耗时记录（关闭时无操作）。"""
    if not enabled():
        return
    if not force and ms < _MIN_LOG_MS:
        return
    act = _action.get()
    prefix = f"{act}|" if act else ""
    _emit(f"{_indent()}{prefix}{name} {ms:.1f}ms{_fmt_extra(extra)}")


@contextlib.contextmanager
def span(name: str, *, force: bool = False, **extra: Any):
    """同步耗时区间。"""
    if not enabled():
        yield
        return
    token = _depth.set(_depth.get() + 1)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        _depth.reset(token)
        log(name, ms, force=force, **extra)


@contextlib.asynccontextmanager
async def async_span(name: str, *, force: bool = False, **extra: Any):
    """异步耗时区间。"""
    if not enabled():
        yield
        return
    token = _depth.set(_depth.get() + 1)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        _depth.reset(token)
        log(name, ms, force=force, **extra)


@contextlib.contextmanager
def action(name: str, **extra: Any):
    """高层动作：打印起止并汇总总耗时，内部 span 会缩进归属到该动作。"""
    if not enabled():
        yield
        return
    prev = _action.set(name)
    _emit(f"{_indent()}▶ action:{name}{_fmt_extra(extra)}")
    depth_tok = _depth.set(_depth.get() + 1)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        _depth.reset(depth_tok)
        _emit(f"{_indent()}◀ action:{name} total={ms:.1f}ms{_fmt_extra(extra)}")
        _action.reset(prev)


@contextlib.asynccontextmanager
async def async_action(name: str, **extra: Any):
    """异步高层动作。"""
    if not enabled():
        yield
        return
    prev = _action.set(name)
    _emit(f"{_indent()}▶ action:{name}{_fmt_extra(extra)}")
    depth_tok = _depth.set(_depth.get() + 1)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        _depth.reset(depth_tok)
        _emit(f"{_indent()}◀ action:{name} total={ms:.1f}ms{_fmt_extra(extra)}")
        _action.reset(prev)


def timed(name: Optional[str] = None, *, force: bool = False, **fixed_extra: Any) -> Callable[[F], F]:
    """装饰同步或异步函数，自动记录耗时。"""

    def decorator(fn: F) -> F:
        label = name or getattr(fn, "__qualname__", None) or fn.__name__

        if contextlib.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                if not enabled():
                    return await fn(*args, **kwargs)
                async with async_span(label, force=force, **fixed_extra):
                    return await fn(*args, **kwargs)
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            if not enabled():
                return fn(*args, **kwargs)
            with span(label, force=force, **fixed_extra):
                return fn(*args, **kwargs)
        return sync_wrapper  # type: ignore[return-value]

    return decorator


def mark(name: str, **extra: Any) -> None:
    """打印瞬时标记（无耗时），用于动作边界或状态点。"""
    if not enabled():
        return
    act = _action.get()
    prefix = f"{act}|" if act else ""
    _emit(f"{_indent()}{prefix}{name}{_fmt_extra(extra)}")


_ui_lag_timer = None


def start_ui_lag_monitor(parent=None, *, interval_ms: int = 500, warn_ms: float = 50.0):
    """
    用 QTimer 心跳估算 UI/事件循环延迟。
    仅在 perf_timing 开启时启动；返回 timer 或 None。
    """
    global _ui_lag_timer
    if not enabled():
        return None
    try:
        from PySide6.QtCore import QTimer
    except Exception:
        return None

    if _ui_lag_timer is not None:
        return _ui_lag_timer

    expected = time.perf_counter()
    state = {"expected": expected, "interval": max(50, int(interval_ms)) / 1000.0}

    def _tick():
        now = time.perf_counter()
        lag_ms = (now - state["expected"]) * 1000.0
        state["expected"] = now + state["interval"]
        # 正常调度抖动通常 < 几 ms；超过 warn 才记
        if lag_ms >= warn_ms:
            log("ui.event_loop_lag", lag_ms, force=True, warn_ms=warn_ms)

    timer = QTimer(parent)
    timer.setInterval(max(50, int(interval_ms)))
    timer.timeout.connect(_tick)
    timer.start()
    _ui_lag_timer = timer
    mark("ui.lag_monitor_started", interval_ms=interval_ms, warn_ms=warn_ms)
    return timer


def stop_ui_lag_monitor() -> None:
    global _ui_lag_timer
    timer = _ui_lag_timer
    _ui_lag_timer = None
    if timer is None:
        return
    try:
        timer.stop()
        timer.deleteLater()
    except Exception:
        pass


__all__ = [
    "enabled",
    "log",
    "span",
    "async_span",
    "action",
    "async_action",
    "timed",
    "mark",
    "start_ui_lag_monitor",
    "stop_ui_lag_monitor",
]
