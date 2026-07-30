from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
import os
from dotenv import load_dotenv

load_dotenv()  # 从 .env 文件加载环境变量 

from models import Base

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+aiomysql://root:root@localhost:3306/ai_assistant_db"  # 仅作本地开发的最终 fallback
)

def _env_int(key: str, default: int) -> int:
    try:
        return max(1, int(str(os.getenv(key) or default).strip()))
    except ValueError:
        return default


engine = create_async_engine(
    DATABASE_URL, 
    echo=False,
    pool_size=_env_int("DB_POOL_SIZE", 20),
    max_overflow=_env_int("DB_MAX_OVERFLOW", 10),
    pool_recycle=_env_int("DB_POOL_RECYCLE", 280),  # 必须小于 MySQL wait_timeout
    pool_pre_ping=True,
    # 建连/排队都必须有上限：DB 抖动时若无 connect_timeout，aiomysql 会挂到内核 SYN
    # 重试耗尽（约 130s），池内槽位全被"正在连接"占死，全站请求随之雪崩
    pool_timeout=_env_int("DB_POOL_TIMEOUT", 10),
    pool_use_lifo=True,
    connect_args={"connect_timeout": _env_int("DB_CONNECT_TIMEOUT", 5)},
)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
