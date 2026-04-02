import asyncio
from sqlalchemy import text
from database import AsyncSessionLocal
from models import Base

async def migrate_orders():
    """
    由于 Order 表结构发生了推倒重来式的变更，
    我们将采取：删除旧表 -> 重新根据新模型创建表 的策略。
    """
    async with AsyncSessionLocal() as session:
        try:
            # 1. 物理删除旧表（注意：这会清空历史测试订单数据）
            print("正在清理旧版订单表结构...")
            await session.execute(text("DROP TABLE IF EXISTS orders;"))
            
            # 2. 提交删除操作
            await session.commit()
            
            # 3. 利用 AsyncEngine 的 run_sync 方法在异步循环中执行同步 DDL
            from database import engine as async_engine
            
            def create_tables(conn):
                # 这里执行具体的表创建逻辑
                import models
                models.Order.__table__.create(conn)
            
            async with async_engine.begin() as conn:
                await conn.run_sync(create_tables)
                
            print(" [OK] 订单表 (Orders) 已成功重构为 20+ 字段的大表结构！")
            
        except Exception as e:
            await session.rollback()
            print(f" [ERROR] 迁移失败: {str(e)}")

if __name__ == "__main__":
    asyncio.run(migrate_orders())
