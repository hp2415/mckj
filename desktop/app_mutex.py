"""Windows 应用互斥量：供 Inno Setup CloseApplications / AppMutex 识别并关闭旧进程。"""
from __future__ import annotations

import sys

_APP_MUTEX_NAME = "WeChatAI.Assistant.AppMutex"
_WINDOW_TITLE_MARKER = "微企 AI"
_handle = None


def acquire_app_mutex() -> bool:
    """
    创建应用互斥量。返回 True 表示当前进程已持有；False 表示已有其它实例在运行。
    非 Windows 环境始终返回 True。
    """
    global _handle
    if not sys.platform.startswith("win"):
        return True
    if _handle is not None:
        return True

    import ctypes

    kernel32 = ctypes.windll.kernel32
    ERROR_ALREADY_EXISTS = 183
    handle = kernel32.CreateMutexW(None, True, _APP_MUTEX_NAME)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _handle = handle
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
        if _WINDOW_TITLE_MARKER in buf.value:
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
