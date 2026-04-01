from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from models import Customer, UserCustomerRelation
import schemas
from datetime import date

async def sync_customer_info(db: AsyncSession, user_id: int, schema: schemas.CustomerSync):
    """
    根据方案 B：
    1. 查找或创建客观客户实体 (Customer)
    2. 查找或创建员工与客户的主观互动记录 (UserCustomerRelation)
    """
    # 1. 客观实体
    result = await db.execute(select(Customer).where(Customer.phone == schema.phone))
    customer = result.scalars().first()
    
    if not customer:
        customer = Customer(
            phone=schema.phone,
            customer_name=schema.customer_name,
            unit_name=schema.unit_name,
            unit_type=schema.unit_type,
            admin_division=schema.admin_division,
            external_id=schema.external_id
        )
        db.add(customer)
        await db.commit()
        await db.refresh(customer)
    else:
        # 如果已存在，可以选择性更新客观信息
        update_needed = False
        if schema.customer_name and customer.customer_name != schema.customer_name:
            customer.customer_name = schema.customer_name
            update_needed = True
        if schema.unit_name and customer.unit_name != schema.unit_name:
            customer.unit_name = schema.unit_name
            update_needed = True
        if update_needed:
            await db.commit()
            await db.refresh(customer)

    # 2. 员工主观互动记录
    rel_result = await db.execute(
        select(UserCustomerRelation)
        .where(UserCustomerRelation.user_id == user_id)
        .where(UserCustomerRelation.customer_id == customer.id)
    )
    relation = rel_result.scalars().first()
    
    if not relation:
        relation = UserCustomerRelation(
            user_id=user_id,
            customer_id=customer.id,
            relation_type="active",
            title=schema.title,
            budget_amount=schema.budget_amount,
            ai_profile=schema.ai_profile,
            contact_date=date.today()
        )
        db.add(relation)
    else:
        # 如果已有交集，更新该员工对客户的最新跟进属性
        if schema.title is not None: relation.title = schema.title
        if schema.budget_amount is not None: relation.budget_amount = schema.budget_amount
        if schema.ai_profile is not None: relation.ai_profile = schema.ai_profile
        
    await db.commit()
    await db.refresh(relation)
    
    # 组装返回给桌面的数据
    return {
        "id": customer.id,
        "phone": customer.phone,
        "customer_name": customer.customer_name,
        "unit_name": customer.unit_name,
        "title": relation.title,
        "budget_amount": relation.budget_amount,
        "ai_profile": relation.ai_profile,
        "contact_date": relation.contact_date
    }
