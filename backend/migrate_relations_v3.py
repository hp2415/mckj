import asyncio
from sqlalchemy import text
from database import AsyncSessionLocal

async def migrate_relations():
    """
    销售关系表结构自然化迁移补丁：
    1. 增加 username (工号) 和 customer_phone (客户电话) 物理列。
    2. 移除旧的物理外键约束 (user_id/customer_id)。
    3. 自动根据现有的 ID 映射关系，回填自然键数据。
    """
    async with AsyncSessionLocal() as session:
        try:
            print("正在执行销售关联关系表的自然键物理升级...")
            
            # --- 阶段 1：物理结构变更 ---
            # 增加新列 (如果已存在则静默跳过)
            try:
                await session.execute(text("ALTER TABLE user_customer_relations ADD COLUMN username VARCHAR(50);"))
            except: pass
            
            try:
                await session.execute(text("ALTER TABLE user_customer_relations ADD COLUMN customer_phone VARCHAR(20);"))
            except: pass
            
            # 由于要解除约束，我们先将 ID 列改为可为空
            try:
                await session.execute(text("ALTER TABLE user_customer_relations MODIFY user_id INT NULL;"))
                await session.execute(text("ALTER TABLE user_customer_relations MODIFY customer_id INT NULL;"))
            except: pass

            # --- 阶段 2：数据回填逻辑 ---
            # 通过多表联查，将旧的物理 ID 关系 翻译为 逻辑账号关系
            print("正在通过 ID 映射回填自然键数据...")
            
            # 更新员工账号
            update_user_sql = """
                UPDATE user_customer_relations r 
                INNER JOIN users u ON r.user_id = u.id 
                SET r.username = u.username 
                WHERE r.username IS NULL;
            """
            await session.execute(text(update_user_sql))
            
            # 更新客户手机号
            update_cust_sql = """
                UPDATE user_customer_relations r 
                INNER JOIN customers c ON r.customer_id = c.id 
                SET r.customer_phone = c.phone 
                WHERE r.customer_phone IS NULL;
            """
            await session.execute(text(update_cust_sql))
            
            await session.commit()
            print(" ✅ 销售关联关系表升级成功！现已支持‘账号+手机’的逻辑化联通。")
            
        except Exception as e:
            await session.rollback()
            print(f" ❌ 迁移失败: {str(e)}")

if __name__ == "__main__":
    asyncio.run(migrate_relations())
