import os
from loguru import logger

# 确保 logs 目录存在
log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(log_dir, exist_ok=True)

# 移除默认的全局配置，防止重复输出
logger.remove()

# 配置控制台彩色输出（标准INFO 以上）
logger.add(
    import_sys_for_stderr:=__import__("sys").stderr,
    colorize=True,
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="INFO"
)

# 配置文件按天滚存输出（所有级别记录），保存最近 30 天
logger.add(
    os.path.join(log_dir, "backend_system_{time:YYYY-MM-DD}.log"),
    rotation="00:00",
    retention="30 days",
    enqueue=True, # 异步写入防阻塞
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    level="DEBUG"
)

# 抛出单例供其它模块引用
__all__ = ["logger"]

import logging

class InterceptHandler(logging.Handler):
    """
    将标准 logging 库的日志重定向到 Loguru 中
    """
    def emit(self, record):
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame and (frame.f_code.co_filename == logging.__file__ or "importlib" in frame.f_code.co_filename):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

# 应用拦截器到所有标准日志（包括 uvicorn, httpx, sqlalchemy）
logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

# 显式接管 uvicorn 的专用日志流，防止其绕过全局配置
for log_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx"):
    _log = logging.getLogger(log_name)
    _log.handlers = [InterceptHandler()]
    _log.propagate = False
