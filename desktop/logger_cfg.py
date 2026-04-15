import os
import sys

from loguru import logger


log_dir = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(log_dir, exist_ok=True)

logger.remove()

logger.add(
    sys.stderr,
    colorize=True,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
    level="INFO",
)

file_sink = os.path.join(log_dir, "desktop_client_{time:YYYY-MM-DD}.log")
file_sink_kwargs = dict(
    rotation="00:00",
    retention="30 days",
    enqueue=True,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    level="DEBUG",
)

try:
    logger.add(file_sink, **file_sink_kwargs)
except PermissionError:
    file_sink_kwargs["enqueue"] = False
    logger.add(file_sink, **file_sink_kwargs)
    logger.warning("Log queue init failed; falling back to sync file logging")

__all__ = ["logger"]
