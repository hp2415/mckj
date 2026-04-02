import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import sessionmaker

from models import User
from core.security import get_password_hash
from database import DATABASE_URL, engine, AsyncSessionLocal

async def create_superuser():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.username == "admin"))
        existing_user = result.scalars().first()
        
        if existing_user:
            print("【跳过】管理员账号 admin 已经存在。")
            return
            
        hashed_password = get_password_hash("123456") # 默认密码
        admin_user = User(
            username="admin",
            password_hash=hashed_password,
            real_name="系统管理员",
            role="admin",
            is_active=True
        )
        session.add(admin_user)
        await session.commit()
        print("\n超级管理员创建成功！\n登录后台账号：admin\n 登录后台密码：123456\n")
    
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(create_superuser())
