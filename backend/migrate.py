import asyncio
from sqlalchemy import text
from database import AsyncSessionLocal

async def upgrade_db():
    async with AsyncSessionLocal() as session:
        try:
            # 尝试给表增加一列，防止报错可以做 catch
            await session.execute(text("ALTER TABLE products ADD COLUMN product_url VARCHAR(500);"))
            await session.commit()
            print("✅ 数据库成功热更新：为 products 表添加了 product_url 字段！")
        except Exception as e:
            print("⚠️ 更新失败或字段已存在：", str(e))
            
if __name__ == "__main__":
    asyncio.run(upgrade_db())
