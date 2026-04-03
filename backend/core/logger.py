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
