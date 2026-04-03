import os
import sys
from loguru import logger

# 确保 desktop/logs 目录存在
log_dir = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(log_dir, exist_ok=True)

# 清除默认输出
logger.remove()

# 桌面端：只在控制台输出警告及以上，保持终端清爽
logger.add(
    sys.stderr,
    colorize=True,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
    level="INFO"
)

# 保存文件，带函数级追溯，轮换时间也设在零点
logger.add(
    os.path.join(log_dir, "desktop_client_{time:YYYY-MM-DD}.log"),
    rotation="00:00",
    retention="30 days",
    enqueue=True, # 异步支持防阻塞GUI卡顿
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    level="DEBUG"
)

__all__ = ["logger"]
