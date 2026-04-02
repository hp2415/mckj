from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update
from models import Customer, UserCustomerRelation
import schemas
from datetime import date

async def sync_customer_info(db: AsyncSession, username: str, schema: schemas.CustomerSync):
    """
    基于逻辑自然键同步：
    1. 查找或创建客观客户实体 (Customer)
    2. 查找或创建员工与客户的主观互动记录 (UserCustomerRelation)
    """
    # 1. 处理客观客户库
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
        # 客观信息按需更新
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

    # 2. 处理员工主观关系 (基于 username + phone 的逻辑关联)
    rel_result = await db.execute(
        select(UserCustomerRelation)
        .where(UserCustomerRelation.username == username)
        .where(UserCustomerRelation.customer_phone == schema.phone)
    )
    relation = rel_result.scalars().first()
    
    if not relation:
        relation = UserCustomerRelation(
            username=username,
            customer_phone=schema.phone,
            relation_type="active",
            title=schema.title,
            budget_amount=schema.budget_amount,
            ai_profile=schema.ai_profile,
            contact_date=date.today()
        )
        db.add(relation)
    else:
        # 更新现有跟进属性
        if schema.title is not None: relation.title = schema.title
        if schema.budget_amount is not None: relation.budget_amount = schema.budget_amount
        if schema.ai_profile is not None: relation.ai_profile = schema.ai_profile
        
    await db.commit()
    await db.refresh(relation)
    
    return {
        "id": customer.id,
        "phone": customer.phone,
        "customer_name": customer.customer_name,
        "unit_name": customer.unit_name,
        "title": relation.title,
        "budget_amount": relation.budget_amount,
        "ai_profile": relation.ai_profile,
        "dify_conversation_id": relation.dify_conversation_id,
        "contact_date": relation.contact_date
    }

async def get_user_customers(db: AsyncSession, username: str):
    """基干工号获取该员工负责的客户列表"""
    stmt = (
        select(Customer, UserCustomerRelation)
        .join(UserCustomerRelation, Customer.phone == UserCustomerRelation.customer_phone)
        .where(UserCustomerRelation.username == username)
    )
    result = await db.execute(stmt)
    
    customers = []
    for customer, relation in result.all():
        customers.append({
            "id": customer.id,
            "phone": customer.phone,
            "customer_name": customer.customer_name,
            "unit_name": customer.unit_name,
            "title": relation.title,
            "budget_amount": relation.budget_amount,
            "ai_profile": relation.ai_profile,
            "dify_conversation_id": relation.dify_conversation_id,
            "contact_date": relation.contact_date
        })
    return customers

async def update_user_customer_relation(
    db: AsyncSession, 
    username: str, 
    customer_phone: str, 
    update_data: schemas.RelationUpdate
):
    """局部更新动态互动数据，包括 Dify 会话 ID"""
    stmt = (
        select(UserCustomerRelation)
        .where(UserCustomerRelation.username == username)
        .where(UserCustomerRelation.customer_phone == customer_phone)
    )
    result = await db.execute(stmt)
    relation = result.scalars().first()
    
    if not relation:
        return None
        
    if update_data.title is not None:
        relation.title = update_data.title
    if update_data.budget_amount is not None:
        relation.budget_amount = update_data.budget_amount
    if update_data.ai_profile is not None:
        relation.ai_profile = update_data.ai_profile
    if update_data.dify_conversation_id is not None:
        relation.dify_conversation_id = update_data.dify_conversation_id
        
    await db.commit()
    await db.refresh(relation)
    return relation

async def transfer_user_customers(db: AsyncSession, from_user: str, to_user: str):
    """
    一键移交业务：将原员工名下的所有客户关系（含 AI 笔记与会话 ID）批量转给新员工。
    """
    stmt = (
        update(UserCustomerRelation)
        .where(UserCustomerRelation.username == from_user)
        .values(username=to_user)
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount
