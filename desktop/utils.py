import os
import sys

def mask_phone(phone: str) -> str:
    """
    Mask phone for display: 138****1234 for common 11-digit numbers.
    Keeps search/logic data untouched; only use for UI display.
    """
    p = (str(phone or "")).strip()
    if not p:
        return ""
    if len(p) >= 11:
        return f"{p[:3]}****{p[-4:]}"
    if len(p) <= 4:
        return "*" * len(p)
    head = p[:2]
    tail = p[-2:]
    mid = "*" * max(1, len(p) - 4)
    return f"{head}{mid}{tail}"

def get_resource_path(relative_path):
    """
    获取资源的绝对路径，兼容源码运行模式和 PyInstaller 打包模式。
    打包后资源会被解压到 sys._MEIPASS 目录下。
    """
    try:
        # PyInstaller 打包后会记录 _MEIPASS 环境变量
        base_path = sys._MEIPASS
    except Exception:
        # 源码运行模式下，以当前 main.py 所在目录为准
        # 这里假设执行 main.py 时，cwd 就是其所在目录，或者通过 __file__ 定位
        if getattr(sys, 'frozen', False):
            base_path = sys._MEIPASS
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))

    return os.path.join(base_path, relative_path)
