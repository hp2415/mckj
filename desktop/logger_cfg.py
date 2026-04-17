import os
import sys

from loguru import logger
from config_loader import cfg

# 计算日志存储路径：打包后应存储在 .exe 同级目录的 logs 文件夹下，而非临时目录 _MEIPASS 中
if getattr(sys, 'frozen', False):
    # 打包运行模式：路径位于 exe 所在目录
    base_dir = os.path.dirname(sys.executable)
else:
    # 源码运行模式
    base_dir = os.path.dirname(os.path.abspath(__file__))

log_dir = os.path.join(base_dir, "logs")
os.makedirs(log_dir, exist_ok=True)

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
