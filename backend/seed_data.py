import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from models import User, Product
from database import engine, AsyncSessionLocal
from core.security import get_password_hash

async def seed():
    async with AsyncSessionLocal() as session:
        # 添加测试商品
        p1 = Product(
            product_id="SKU1001", 
            product_name="扶贫特供五常大米 10kg", 
            price=128.00, 
            cover_img="https://placehold.co/200", 
            unit="袋", 
            supplier_name="黑龙江五常农行"
        )
        p2 = Product(
            product_id="SKU1002", 
            product_name="云南特色小粒咖啡 500g", 
            price=56.00, 
            cover_img="https://placehold.co/200", 
            unit="包", 
            supplier_name="云南保山咖啡庄园"
        )
        
        # 添加一个普通销售员工（用于桌面端账号演示）
        staff = User(
            username="staff_01", 
            password_hash=get_password_hash("123456"), 
            real_name="张销售", 
            role="staff", 
            is_active=True
        )
        
        session.add_all([p1, p2, staff])
        # 忽略由于多次重复运行导致的唯一键重复冲突
        try:
            await session.commit()
            print(" 测试员工(staff_01) 及 测试商品 成功注入数据库！")
        except Exception as e:
            await session.rollback()
            print(" 忽略：测试数据可能已存在！")
    
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(seed())
