"""Windows 应用互斥量：供 Inno Setup CloseApplications / AppMutex 识别并关闭旧进程。"""
from __future__ import annotations

import sys

from app_identity import (
    APP_MUTEX_NAME,
    LEGACY_APP_MUTEX_NAME,
    LEGACY_WINDOW_TITLE_MARKERS,
    WINDOW_TITLE_MARKER,
)

_APP_MUTEX_NAME = APP_MUTEX_NAME
_LEGACY_APP_MUTEX_NAME = LEGACY_APP_MUTEX_NAME
_WINDOW_TITLE_MARKER = WINDOW_TITLE_MARKER
_WINDOW_TITLE_MARKERS = (WINDOW_TITLE_MARKER, *LEGACY_WINDOW_TITLE_MARKERS)
_handle = None
_legacy_handle = None


def acquire_app_mutex() -> bool:
    """
    创建应用互斥量。返回 True 表示当前进程已持有；False 表示已有其它实例在运行。
    非 Windows 环境始终返回 True。
    """
    global _handle, _legacy_handle
    if not sys.platform.startswith("win"):
        return True
    if _handle is not None:
        return True

    import ctypes

    kernel32 = ctypes.windll.kernel32
    ERROR_ALREADY_EXISTS = 183
    acquired: list[int] = []

    try:
        for mutex_name in (_LEGACY_APP_MUTEX_NAME, _APP_MUTEX_NAME):
            handle = kernel32.CreateMutexW(None, True, mutex_name)
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                for held in acquired:
                    kernel32.CloseHandle(held)
                return False
            acquired.append(handle)

        _legacy_handle, _handle = acquired[0], acquired[1]
        return True
    except Exception:
        for held in acquired:
            try:
                kernel32.CloseHandle(held)
            except Exception:
                pass
        return True


def activate_existing_instance() -> bool:
    """
    尝试将已运行的客户端窗口拉到前台。
    返回 True 表示找到了窗口并已尝试激活。
    """
    if not sys.platform.startswith("win"):
        return False

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    def _enum_proc(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd) + 1
        if length <= 1:
            return True
        buf = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, buf, length)
        if any(marker in buf.value for marker in _WINDOW_TITLE_MARKERS):
            found.append(hwnd)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(_enum_proc), 0)
    if not found:
        return False

    hwnd = found[0]
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True
