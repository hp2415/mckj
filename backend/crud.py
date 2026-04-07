from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update
from models import Customer, UserCustomerRelation
import schemas
from datetime import date
from core.logger import logger

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
    """基干工号获取该员工负责的客户列表，聚合订单金额"""
    from sqlalchemy import func
    from models import Order
    
    stmt = (
        select(Customer, UserCustomerRelation)
        .join(UserCustomerRelation, Customer.phone == UserCustomerRelation.customer_phone)
        .where(UserCustomerRelation.username == username)
    )
    result = await db.execute(stmt)
    
    customers = []
    for customer, relation in result.all():
        order_stmt = select(func.sum(Order.pay_amount), func.count(Order.id)).where(Order.consignee_phone == customer.phone)
        order_res = await db.execute(order_stmt)
        total_amount, total_count = order_res.first()
        
        p_months = customer.purchase_months
        if not p_months and total_count and total_count > 0:
            month_stmt = select(Order.order_time).where(Order.consignee_phone == customer.phone).where(Order.order_time.is_not(None))
            month_res = await db.execute(month_stmt)
            m_set = set()
            for r in month_res.all():
                if r[0]: m_set.add(f"{r[0].month}月")
            if m_set:
                p_months = ", ".join(sorted(list(m_set), key=lambda x: int(x.replace("月", ""))))
        
        customers.append({
            "id": customer.id,
            "phone": customer.phone,
            "customer_name": customer.customer_name,
            "unit_name": customer.unit_name,
            "unit_type": customer.unit_type,
            "admin_division": customer.admin_division,
            "purchase_months": p_months,
            "purchase_type": relation.purchase_type,
            "title": relation.title,
            "budget_amount": relation.budget_amount,
            "ai_profile": relation.ai_profile,
            "wechat_remark": relation.wechat_remark,
            "dify_conversation_id": relation.dify_conversation_id,
            "contact_date": relation.contact_date,
            "historical_amount": total_amount or 0.0,
            "historical_order_count": total_count or 0
        })
    return customers

async def update_customer_full_info(
    db: AsyncSession, 
    username: str, 
    customer_phone: str, 
    update_data: schemas.CustomerDataUpdate
):
    """更新客户的大满贯综合面板(区分主客观数据)"""
    # 1. 更新客观 Customer 记录
    cust_stmt = select(Customer).where(Customer.phone == customer_phone)
    cust_res = await db.execute(cust_stmt)
    customer = cust_res.scalars().first()
    
    if customer:
        if update_data.unit_type is not None: customer.unit_type = update_data.unit_type
        if update_data.admin_division is not None: customer.admin_division = update_data.admin_division
        if update_data.purchase_months is not None: customer.purchase_months = update_data.purchase_months

    # 2. 更新主观用户关联记录
    rel_stmt = (
        select(UserCustomerRelation)
        .where(UserCustomerRelation.username == username)
        .where(UserCustomerRelation.customer_phone == customer_phone)
    )
    rel_result = await db.execute(rel_stmt)
    relation = rel_result.scalars().first()
    
    if relation:
        if update_data.contact_date is not None: relation.contact_date = update_data.contact_date
        if update_data.purchase_type is not None: relation.purchase_type = update_data.purchase_type
        if update_data.title is not None: relation.title = update_data.title
        if update_data.budget_amount is not None: relation.budget_amount = update_data.budget_amount
        if update_data.ai_profile is not None: relation.ai_profile = update_data.ai_profile
        if update_data.wechat_remark is not None: relation.wechat_remark = update_data.wechat_remark
        if update_data.dify_conversation_id is not None: relation.dify_conversation_id = update_data.dify_conversation_id
        
    await db.commit()
    return True

from models import ChatMessage

async def get_chat_history(
    db: AsyncSession, 
    user_id: int, 
    customer_id: int, 
    limit: int = 50
):
    """调取该业务员与该客户的 AI 互动记录"""
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.customer_id == customer_id)
        .where(ChatMessage.user_id == user_id)
        .order_by(ChatMessage.created_at.asc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    return res.scalars().all()

async def create_chat_message(
    db: AsyncSession,
    user_id: int,
    customer_id: int,
    msg_in: schemas.ChatMessageCreate
):
    """保存单条对话记录"""
    db_msg = ChatMessage(
        user_id=user_id,
        customer_id=customer_id,
        role=msg_in.role,
        content=msg_in.content,
        dify_conv_id=msg_in.dify_conv_id
    )
    db.add(db_msg)
    await db.commit()
    await db.refresh(db_msg)
    return db_msg

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
    logger.warning(f"管理员正在执行业务强行划转: 将员工 '{from_user}' 名下的 {result.rowcount} 名客户完全移交给了员工 '{to_user}'")
    return result.rowcount
