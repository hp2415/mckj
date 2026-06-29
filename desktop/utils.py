import os
import re
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


def resolve_display_phone(data: dict | None) -> str:
    """优先规范化电话，其次联系电话；与后端 RawCustomer 展示逻辑一致。"""
    if not isinstance(data, dict):
        return ""
    for key in ("phone_normalized", "phone"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


_PHONE_LIST_SEP_RE = re.compile(r"[,;，；、|/\\\s]+")


def parse_phone_list(phone: str) -> list[str]:
    """将逗号/分号等分隔的多号码字符串拆成单个号码列表，去重保序。

    兼容规范化字段（英文逗号）与原始联系电话（中文逗号、顿号等）。
    """
    raw = str(phone or "").strip()
    if not raw:
        return []
    # 先将全角/非常见分隔符归一为英文逗号，再统一拆分
    normalized = raw.translate(
        str.maketrans(
            {
                "，": ",",
                "；": ",",
                "、": ",",
                "｜": ",",
                "|": ",",
                "/": ",",
                "\\": ",",
                "\n": ",",
                "\r": ",",
                "\t": ",",
            }
        )
    )
    parts = _PHONE_LIST_SEP_RE.split(normalized)
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        p = part.strip().strip("()（）[]【】\"'")
        if not p or p in seen:
            continue
        seen.add(p)
        result.append(p)
    return result

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
