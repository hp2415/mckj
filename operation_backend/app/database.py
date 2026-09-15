from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import DATABASE_URL, DB_MAX_OVERFLOW, DB_POOL_SIZE

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_recycle=280,
    pool_pre_ping=True,
    pool_timeout=10,
    pool_use_lifo=True,
    connect_args={"connect_timeout": 5},
)

AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
