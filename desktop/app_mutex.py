"""Windows 应用互斥量：供 Inno Setup CloseApplications / AppMutex 识别并关闭旧进程。"""
from __future__ import annotations

import sys

_APP_MUTEX_NAME = "WeChatAI.Assistant.AppMutex"
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
