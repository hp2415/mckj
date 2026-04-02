import asyncio
from sqlalchemy import text
from database import engine

async def run_migration():
    """
    为现有的数据表补全物理外键约束，并开启 ON UPDATE CASCADE。
    """
    print("开始执行 MySQL 物理外键迁移 (Phase 10)...")
    
    async with engine.begin() as conn:
        # 1. 禁用外键检查以便操作
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0;"))
        
        # 2. 为 orders 表增加对 customers(phone) 的级联更新约束
        # 先尝试删除旧约束（如果存在）
        try:
            await conn.execute(text("ALTER TABLE orders DROP FOREIGN KEY fk_orders_customer_phone;"))
        except: pass
        
        await conn.execute(text("""
            ALTER TABLE orders 
            ADD CONSTRAINT fk_orders_customer_phone 
            FOREIGN KEY (consignee_phone) REFERENCES customers(phone) 
            ON UPDATE CASCADE;
        """))
        print("- [OK] 为 orders 表同步了手机号级联更新。")

        # 3. 为 user_customer_relations 表增加对 users(username) 的级联更新约束
        try:
            await conn.execute(text("ALTER TABLE user_customer_relations DROP FOREIGN KEY fk_rel_username;"))
        except: pass
        
        await conn.execute(text("""
            ALTER TABLE user_customer_relations 
            ADD CONSTRAINT fk_rel_username 
            FOREIGN KEY (username) REFERENCES users(username) 
            ON UPDATE CASCADE;
        """))
        print("- [OK] 为关系表同步了工号级联更新。")

        # 4. 为 user_customer_relations 表增加对 customers(phone) 的级联更新约束
        try:
            await conn.execute(text("ALTER TABLE user_customer_relations DROP FOREIGN KEY fk_rel_phone;"))
        except: pass
        
        await conn.execute(text("""
            ALTER TABLE user_customer_relations 
            ADD CONSTRAINT fk_rel_phone 
            FOREIGN KEY (customer_phone) REFERENCES customers(phone) 
            ON UPDATE CASCADE;
        """))
        print("- [OK] 为关系表同步了手机号级联更新。")

        # 5. 恢复外键检查
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1;"))
        
    print("MySQL 物理迁移已圆满完成！现在您可以尝试在后台修改工号或手机号，关联数据将自动随之迁移。")

if __name__ == "__main__":
    asyncio.run(run_migration())
