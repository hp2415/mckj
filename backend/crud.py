from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update
from models import Customer, UserCustomerRelation, User, ChatMessage
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

    # 2. 处理员工主观关系 (基于 ID 的关系锁定)
    user_res = await db.execute(select(User).where(User.username == username))
    user = user_res.scalars().first()
    
    if not user:
        return {"error": "User not found"}

    rel_result = await db.execute(
        select(UserCustomerRelation)
        .where(UserCustomerRelation.user_id == user.id)
        .where(UserCustomerRelation.customer_id == customer.id)
    )
    relation = rel_result.scalars().first()
    
    if not relation:
        relation = UserCustomerRelation(
            user_id=user.id,
            customer_id=customer.id,
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
    
    # 1. 先定位员工 ID
    user_res = await db.execute(select(User).where(User.username == username))
    user = user_res.scalars().first()
    if not user:
        return []

    # 2. 范式化关联查询
    stmt = (
        select(Customer, UserCustomerRelation)
        .join(UserCustomerRelation, Customer.id == UserCustomerRelation.customer_id)
        .where(UserCustomerRelation.user_id == user.id)
    )
    result = await db.execute(stmt)
    records = result.all()
    
    customers = []
    if not records:
        return customers

    phones = [customer.phone for customer, _ in records if customer.phone]
    
    # 批量聚合订单统计
    agg_map = {}
    month_map = {}
    if records:
        customer_ids = [c.id for c, _ in records]
        agg_stmt = (
            select(
                Order.customer_id, 
                func.sum(Order.pay_amount), 
                func.count(Order.id)
            )
            .where(Order.customer_id.in_(customer_ids))
            .group_by(Order.customer_id)
        )
        agg_res = await db.execute(agg_stmt)
        agg_map = {row[0]: (row[1], row[2]) for row in agg_res.all()}
        
        # 批量获取月份分布
        month_stmt = (
            select(Order.customer_id, Order.order_time)
            .where(Order.customer_id.in_(customer_ids))
            .where(Order.order_time.is_not(None))
        )
        month_res = await db.execute(month_stmt)
        for r in month_res.all():
            cid = r[0]
            if r[1]:
                month_str = f"{r[1].month}月"
                if cid not in month_map:
                    month_map[cid] = set()
                month_map[cid].add(month_str)

    for customer, relation in records:
        total_amount, total_count = agg_map.get(customer.id, (0.0, 0))
        
        p_months = customer.purchase_months
        if not p_months and total_count and total_count > 0:
            m_set = month_map.get(customer.id, set())
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
    user_res = await db.execute(select(User).where(User.username == username))
    user = user_res.scalars().first()
    
    if not (user and customer):
        return False

    rel_stmt = (
        select(UserCustomerRelation)
        .where(UserCustomerRelation.user_id == user.id)
        .where(UserCustomerRelation.customer_id == customer.id)
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
        dify_conv_id=msg_in.dify_conv_id,
        is_regenerated=getattr(msg_in, 'is_regenerated', False)
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
    user_res = await db.execute(select(User).where(User.username == username))
    user = user_res.scalars().first()
    cust_res = await db.execute(select(Customer).where(Customer.phone == customer_phone))
    customer = cust_res.scalars().first()
    
    if not (user and customer):
        return None
        
    stmt = (
        select(UserCustomerRelation)
        .where(UserCustomerRelation.user_id == user.id)
        .where(UserCustomerRelation.customer_id == customer.id)
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
    u_from_res = await db.execute(select(User).where(User.username == from_user))
    u_from = u_from_res.scalars().first()
    u_to_res = await db.execute(select(User).where(User.username == to_user))
    u_to = u_to_res.scalars().first()
    
    if not (u_from and u_to):
        return 0
        
    stmt = (
        update(UserCustomerRelation)
        .where(UserCustomerRelation.user_id == u_from.id)
        .values(user_id=u_to.id)
    )
    result = await db.execute(stmt)
    await db.commit()
    logger.warning(f"管理员正在执行业务强行划转: 将员工 '{from_user}' 名下的 {result.rowcount} 名客户完全移交给了员工 '{to_user}'")
    return result.rowcount
