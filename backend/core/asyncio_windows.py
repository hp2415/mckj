"""
Windows + uvicorn --reload 使用 SelectorEventLoop 时的 asyncio 兼容处理。

uvicorn 在 reload/多 worker 子进程里会强制 SelectorEventLoop（避免 WinError 87），
在客户端并发连接或提前断开时，_SelectorSocketTransport._write_send 可能因
重复调度而出现空 buffer 竞态（AssertionError: Data should not be empty）。
"""
from __future__ import annotations

import asyncio
import sys
from typing import Callable

from core.logger import logger

_PATCHED = False


def _wrap_write_method(method: Callable) -> Callable:
    def _safe(self, *args, **kwargs):
        if not self._buffer:
            try:
                self._loop._remove_writer(self._sock_fd)
            except Exception:  # noqa: BLE001
                pass
            return None
        return method(self, *args, **kwargs)

    return _safe


def apply_windows_selector_write_race_patch() -> None:
    """将 assert 改为早退，避免竞态时刷 ERROR 日志。"""
    global _PATCHED
    if _PATCHED or sys.platform != "win32":
        return

    import asyncio.selector_events as selector_events

    transport_cls = selector_events._SelectorSocketTransport
    for name in ("_write_send", "_write_sendmsg"):
        original = getattr(transport_cls, name, None)
        if original is None or getattr(original, "_wq_patched", False):
            continue
        wrapped = _wrap_write_method(original)
        wrapped._wq_patched = True  # type: ignore[attr-defined]
        setattr(transport_cls, name, wrapped)

    _PATCHED = True
    logger.debug("已应用 Windows SelectorEventLoop 写缓冲竞态补丁")


def install_asyncio_exception_handler() -> None:
    """兜底：未命中补丁时把已知竞态降为 DEBUG。"""
    if sys.platform != "win32":
        return

    def _handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        msg = str(context.get("message") or "")
        if isinstance(exc, AssertionError) and "Data should not be empty" in str(exc):
            logger.debug("asyncio 写缓冲竞态（已忽略）: {}", msg)
            return
        if "_SelectorSocketTransport._write_send" in msg:
            logger.debug("asyncio 写回调竞态（已忽略）: {}", msg)
            return
        loop.default_exception_handler(context)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.set_exception_handler(_handler)
