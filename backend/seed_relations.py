import asyncio
from database import AsyncSessionLocal
from sqlalchemy import select
from models import User, Customer, UserCustomerRelation

async def seed_relations():
    """
    为 admin 账号注入几条客户关联数据，方便桌面端侧边栏调试。
    """
    async with AsyncSessionLocal() as db:
        # 1. 获取 admin 用户 
        res_user = await db.execute(select(User).where(User.username == "admin"))
        admin_user = res_user.scalar_one_or_none()
        
        if not admin_user:
            print(" [ERROR] 未找到 admin 账号，请先运行 create_superuser.py")
            return

        # 2. 获取前 3 个客户
        res_cust = await db.execute(select(Customer).limit(3))
        customers = res_cust.scalars().all()
        
        if not customers:
            print(" [ERROR] 客户库为空，请先运行 seed_data.py")
            return

        # 3. 创建关联关系 (使用 Phase 6 后的自然键模式)
        new_relations = []
        for cust in customers:
            # 检查是否已存在
            check_stmt = select(UserCustomerRelation).where(
                UserCustomerRelation.username == admin_user.username,
                UserCustomerRelation.customer_phone == cust.phone
            )
            exists = (await db.execute(check_stmt)).scalar()
            
            if not exists:
                new_relations.append(UserCustomerRelation(
                    username=admin_user.username,
                    customer_phone=cust.phone,
                    relation_type="active",
                    title=f"{cust.customer_name}总",
                    budget_amount=5000.0,
                    ai_profile="这是一个优质的潜在客户，对价格敏感度低。"
                ))
        
        if new_relations:
            db.add_all(new_relations)
            await db.commit()
            print(f" [OK] 成功为 admin 注入 {len(new_relations)} 条客户关联数据。")
        else:
            print(" [INFO] 关联数据已存在，无需重复注入。")

if __name__ == "__main__":
    asyncio.run(seed_relations())
