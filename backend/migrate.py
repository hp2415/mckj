import asyncio
from sqlalchemy import text
from database import AsyncSessionLocal

async def upgrade_db():
    async with AsyncSessionLocal() as session:
        try:
            # 尝试给表增加一列，防止重复添加报错
            try:
                await session.execute(text("ALTER TABLE products ADD COLUMN product_url VARCHAR(500);"))
            except: pass
            
            try:
                await session.execute(text("ALTER TABLE products ADD COLUMN supplier_id VARCHAR(50);"))
            except: pass
            
            await session.commit()
            print(" 数据库成功热更新：为 products 表添加了业务字段！")
        except Exception as e:
            print(" 更新失败或字段已存在：", str(e))
            
if __name__ == "__main__":
    asyncio.run(upgrade_db())
