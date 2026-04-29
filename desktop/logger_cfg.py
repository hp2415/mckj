import os
import sys
from typing import Optional

from loguru import logger
from config_loader import cfg

def _app_name() -> str:
    if getattr(sys, "frozen", False):
        return os.path.splitext(os.path.basename(sys.executable))[0] or "WeChatAI_Assistant"
    return "WeChatAI_Assistant"


def _local_appdata_dir() -> str:
    # Windows: %LOCALAPPDATA% 优先；否则退回用户目录
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(root, _app_name())


def _pick_log_dir() -> str:
    """
    打包后优先写入 exe 同级 logs（若可写）；否则回退到用户可写目录，
    解决安装在 Program Files 等目录的 WinError 5 权限问题。
    """
    candidates: list[str] = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        if exe_dir:
            candidates.append(os.path.join(exe_dir, "logs"))
    else:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))

    candidates.append(os.path.join(_local_appdata_dir(), "logs"))
    candidates.append(os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"), _app_name(), "logs"))

    last_err: Optional[Exception] = None
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            # 尝试写权限（避免仅创建成功但不可写的边缘情况）
            probe = os.path.join(d, ".write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            try:
                os.remove(probe)
            except OSError:
                pass
            return d
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError("Unable to create writable log dir")


log_dir = _pick_log_dir()

logger.remove()

# 核心热修复：在 --noconsole 模式下，sys.stderr 为 None，会导致 loguru 崩溃
if sys.stderr is not None:
    logger.add(
        sys.stderr,
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
        level=cfg.log_level,
    )

file_sink = os.path.join(log_dir, "desktop_client_{time:YYYY-MM-DD}.log")
file_sink_kwargs = dict(
    rotation="00:00",
    retention="30 days",
    enqueue=True,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    level="DEBUG", # 文件始终保留 DEBUG 级别，方便排查
)

try:
    logger.add(file_sink, **file_sink_kwargs)
except PermissionError:
    file_sink_kwargs["enqueue"] = False
    logger.add(file_sink, **file_sink_kwargs)
    logger.warning("Log queue init failed; falling back to sync file logging")

__all__ = ["logger"]
