import asyncio
import datetime
from sqlalchemy import select
from database import AsyncSessionLocal
from models import Order, Customer

async def seed_orders():
    """
    为新重构的订单表注入模拟数据。
    包含：已匹配客户的订单、未匹配（潜在）客户的订单。
    """
    async with AsyncSessionLocal() as db:
        # 1. 尝试获取数据库中已有的客户电话，用于建立关联演示
        res = await db.execute(select(Customer.phone).limit(1))
        existing_phone = res.scalar() or "13800138000"
        
        # 2. 构建模拟订单列表 (按同事提供的 PHP 数组结构转化)
        mock_orders = [
            Order(
                consignee_phone=existing_phone, # 匹配库中已有客户
                store="STORE_001",
                order_id="API_ORD_1001",
                dddh="20260402000001",
                pay_type_name="微信支付",
                pay_amount=680.50,
                freight=15.00,
                status_name="已支付",
                order_time=datetime.datetime.now() - datetime.timedelta(days=1),
                update_time=datetime.datetime.now(),
                remark="请尽快发货，急用",
                product_title="精选黄花菜 2kg + 黑木耳礼盒",
                consignee="张晓明",
                consignee_address="北京市朝阳区幸福大街1号院",
                province_code="110000",
                city_code="110100",
                district_code="110105",
                buyer_name="北京某大型国企采购部",
                buyer_phone="010-88889999",
                purchase_type=1 # 官网采购
            ),
            Order(
                consignee_phone="19988887777", # 这是一个未在客户表中出现的手机号 (逻辑关联测试)
                store="STORE_002",
                order_id="API_ORD_1002",
                dddh="20260402000002",
                pay_type_name="对公转账",
                pay_amount=12500.00,
                freight=0.00,
                status_name="待审批",
                order_time=datetime.datetime.now() - datetime.timedelta(hours=2),
                update_time=datetime.datetime.now(),
                remark="大额批量采购，需增值税专票",
                product_title="五常大米 100袋 (5kg/袋)",
                consignee="王处长",
                consignee_address="天津市和平区滨江道300号",
                buyer_name="天津XX政府保障中心",
                buyer_phone="13900001111",
                purchase_type=2 # 代客下单
            )
        ]
        
        try:
            db.add_all(mock_orders)
            await db.commit()
            print(f" ✅ 成功注入 {len(mock_orders)} 条订单模拟数据！")
            print(f"    - 已关联客户订单手机号: {existing_phone}")
            print(f"    - 未关联潜在订单手机号: 19988887777")
        except Exception as e:
            await db.rollback()
            print(f" ❌ 注入失败: {str(e)}")

if __name__ == "__main__":
    asyncio.run(seed_orders())
