from typing import Optional, Tuple, Any
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update, desc, and_, or_, delete, insert

from models import (
    Customer,
    UserCustomerRelation,
    User,
    UserSalesWechat,
    ChatMessage,
    SalesWechatAccount,
    ProfileTagDefinition,
    ucr_profile_tags,
)
import schemas
from datetime import date
from core.logger import logger


async def ucr_visibility_clause_for_user(db: AsyncSession, user_id: int):
    """
    当前登录用户可见的跟进线条件：
    - 任一已绑定销售微信号上的关系（与云客 sales_wechat_id 对齐，绑定移交后仍可见）；
    - 或历史数据：user_id 匹配且 sales_wechat_id 为空。
    """
    bind_res = await db.execute(
        select(UserSalesWechat.sales_wechat_id).where(UserSalesWechat.user_id == user_id)
    )
    bound_ids = [r[0] for r in bind_res.all() if r[0]]
    parts = [
        and_(UserCustomerRelation.user_id == user_id, UserCustomerRelation.sales_wechat_id.is_(None))
    ]
    if bound_ids:
        parts.append(UserCustomerRelation.sales_wechat_id.in_(bound_ids))
    return or_(*parts)


async def profile_tags_by_relation_ids(
    db: AsyncSession, relation_ids: list[int]
) -> dict[int, list[dict]]:
    """跟进关系 id → 已绑定的动态标签列表（用于列表/详情 API）。"""
    if not relation_ids:
        return {}
    stmt = (
        select(
            ucr_profile_tags.c.user_customer_relation_id,
            ProfileTagDefinition.id,
            ProfileTagDefinition.name,
            ProfileTagDefinition.feature_note,
            ProfileTagDefinition.strategy_note,
        )
        .join(ProfileTagDefinition, ProfileTagDefinition.id == ucr_profile_tags.c.profile_tag_id)
        .where(ucr_profile_tags.c.user_customer_relation_id.in_(relation_ids))
        .order_by(ProfileTagDefinition.sort_order, ProfileTagDefinition.id)
    )
    res = await db.execute(stmt)
    out: dict[int, list[dict]] = defaultdict(list)
    for row in res.all():
        rid, tid, name, feat, strat = row[0], row[1], row[2], row[3], row[4]
        out[rid].append(
            {
                "id": tid,
                "name": name,
                "feature_note": feat,
                "strategy_note": strat,
            }
        )
    return dict(out)


async def profile_tags_for_relation(db: AsyncSession, relation_id: int) -> list[dict]:
    m = await profile_tags_by_relation_ids(db, [relation_id])
    return m.get(relation_id, [])


def parse_profile_tag_ids(raw: Any) -> list[int]:
    """解析 LLM 或 JSON 中的标签 id（去重前顺序保留）。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        out: list[int] = []
        for x in raw:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                pass
        return out
    try:
        return [int(raw)]
    except (TypeError, ValueError):
        return []


async def replace_ucr_profile_tags(
    db: AsyncSession,
    relation: UserCustomerRelation,
    raw_ids: Any,
    *,
    require_active: bool = True,
) -> None:
    """
    将标签 id 写回跟进关系：仅保留库中存在的定义；require_active 为真时仅保留启用标签（画像 LLM）。
    桌面人工保存传 require_active=False，可保留已勾选但已在后台停用的标签。
    """
    if relation.id is None:
        await db.flush()
    parsed = parse_profile_tag_ids(raw_ids)
    await db.execute(
        delete(ucr_profile_tags).where(
            ucr_profile_tags.c.user_customer_relation_id == relation.id
        )
    )
    if not parsed:
        return
    stmt_ok = select(ProfileTagDefinition.id).where(ProfileTagDefinition.id.in_(parsed))
    if require_active:
        stmt_ok = stmt_ok.where(ProfileTagDefinition.is_active.is_(True))
    res_ok = await db.execute(stmt_ok)
    valid = sorted({row[0] for row in res_ok.all()})
    if not valid:
        return
    await db.execute(
        insert(ucr_profile_tags),
        [
            {"user_customer_relation_id": relation.id, "profile_tag_id": tid}
            for tid in valid
        ],
    )


async def list_active_profile_tag_options(db: AsyncSession) -> list[dict]:
    """桌面端下拉：仅返回启用标签，按排序字段。"""
    stmt = (
        select(ProfileTagDefinition)
        .where(ProfileTagDefinition.is_active.is_(True))
        .order_by(ProfileTagDefinition.sort_order, ProfileTagDefinition.id)
    )
    res = await db.execute(stmt)
    rows = res.scalars().all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "feature_note": r.feature_note,
            "strategy_note": r.strategy_note,
        }
        for r in rows
    ]


async def primary_sales_wechat_for_user(db: AsyncSession, user_id: int) -> Optional[str]:
    res = await db.execute(
        select(UserSalesWechat)
        .where(UserSalesWechat.user_id == user_id)
        .order_by(UserSalesWechat.is_primary.desc(), UserSalesWechat.id.asc())
        .limit(1)
    )
    row = res.scalars().first()
    return row.sales_wechat_id if row else None


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

    sales_wx = await primary_sales_wechat_for_user(db, user.id)
    vis = await ucr_visibility_clause_for_user(db, user.id)
    rel_stmt = select(UserCustomerRelation).where(
        UserCustomerRelation.customer_id == customer.id,
        vis,
    )
    rel_result = await db.execute(rel_stmt)
    relation = rel_result.scalars().first()
    
    if not relation:
        relation = UserCustomerRelation(
            user_id=user.id,
            customer_id=customer.id,
            sales_wechat_id=sales_wx,
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
    
    tags = await profile_tags_for_relation(db, relation.id)
    return {
        "id": customer.id,
        "phone": customer.phone,
        "customer_name": customer.customer_name,
        "unit_name": customer.unit_name,
        "title": relation.title,
        "budget_amount": relation.budget_amount,
        "ai_profile": relation.ai_profile,
        "dify_conversation_id": relation.dify_conversation_id,
        "contact_date": relation.contact_date,
        "profile_tags": tags,
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

    # 2. 关联查询：按绑定销售号可见（移交绑定后不必改关系表 user_id 也能看到）
    vis = await ucr_visibility_clause_for_user(db, user.id)
    stmt = (
        select(Customer, UserCustomerRelation)
        .join(UserCustomerRelation, Customer.id == UserCustomerRelation.customer_id)
        .where(vis)
    )
    result = await db.execute(stmt)
    records = result.all()

    # 同一客户多条销售跟进线时合并为一行展示，优先主绑定销售号对应的关系
    primary_sw = await primary_sales_wechat_for_user(db, user.id)
    by_cust: dict[int, tuple] = {}
    for customer, relation in records:
        cid = customer.id
        if cid not in by_cust:
            by_cust[cid] = (customer, relation)
            continue
        _, old_rel = by_cust[cid]
        if primary_sw and relation.sales_wechat_id == primary_sw:
            by_cust[cid] = (customer, relation)
        elif primary_sw and old_rel.sales_wechat_id != primary_sw and relation.sales_wechat_id == primary_sw:
            by_cust[cid] = (customer, relation)
    records = list(by_cust.values())

    customers = []
    if not records:
        return customers

    rel_ids = [rel.id for _, rel in records if rel.id]
    tag_by_rel = await profile_tags_by_relation_ids(db, rel_ids)

    sw_ids = {(rel.sales_wechat_id or "").strip() for _, rel in records if (rel.sales_wechat_id or "").strip()}
    sw_account_display: dict[str, Optional[str]] = {}
    if sw_ids:
        acc_res = await db.execute(
            select(SalesWechatAccount).where(SalesWechatAccount.sales_wechat_id.in_(sw_ids))
        )
        for acc in acc_res.scalars().all():
            nick = (acc.nickname or "").strip()
            sw_account_display[acc.sales_wechat_id] = nick if nick else None

    phones = [customer.phone for customer, _ in records if customer.phone]
    
    # 批量聚合订单统计
    agg_map = {}
    month_map = {}
    if phones:
        from models import RawOrder
        agg_stmt = (
            select(
                RawOrder.search_phone, 
                func.sum(RawOrder.pay_amount), 
                func.count(RawOrder.id)
            )
            .where(RawOrder.search_phone.in_(phones))
            .group_by(RawOrder.search_phone)
        )
        agg_res = await db.execute(agg_stmt)
        # Create a phone -> (sum, count) map
        phone_agg_map = {row[0]: (row[1], row[2]) for row in agg_res.all()}
        
        # 批量获取月份分布
        month_stmt = (
            select(RawOrder.search_phone, RawOrder.order_time)
            .where(RawOrder.search_phone.in_(phones))
            .where(RawOrder.order_time.is_not(None))
        )
        month_res = await db.execute(month_stmt)
        phone_month_map = {}
        for r in month_res.all():
            phone = r[0]
            if r[1]:
                month_str = f"{r[1].month}月"
                if phone not in phone_month_map:
                    phone_month_map[phone] = set()
                phone_month_map[phone].add(month_str)
                
        # Map back to customer_id
        for customer, _ in records:
            if customer.phone:
                agg_map[customer.id] = phone_agg_map.get(customer.phone, (0.0, 0))
                month_map[customer.id] = phone_month_map.get(customer.phone, set())

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
            "suggested_followup_date": relation.suggested_followup_date,
            "sales_wechat_id": relation.sales_wechat_id,
            "sales_wechat_label": sw_account_display.get((relation.sales_wechat_id or "").strip())
            if relation.sales_wechat_id
            else None,
            "historical_amount": total_amount or 0.0,
            "historical_order_count": total_count or 0,
            "profile_tags": tag_by_rel.get(relation.id, []),
        })
    return customers

async def update_customer_full_info(
    db: AsyncSession,
    username: str,
    update_data: schemas.CustomerDataUpdate,
    *,
    customer_phone: Optional[str] = None,
    customer_id: Optional[int] = None,
) -> Tuple[bool, str]:
    """更新客户的大满贯综合面板(区分主客观数据)。按原手机号或客户 ID 定位记录。"""
    if customer_id is not None:
        cust_stmt = select(Customer).where(Customer.id == customer_id)
    elif customer_phone is not None:
        cust_stmt = select(Customer).where(Customer.phone == customer_phone)
    else:
        return False, "缺少客户定位信息"

    cust_res = await db.execute(cust_stmt)
    customer = cust_res.scalars().first()

    user_res = await db.execute(select(User).where(User.username == username))
    user = user_res.scalars().first()

    if not (user and customer):
        return False, "客户不存在或无权操作"

    vis = await ucr_visibility_clause_for_user(db, user.id)
    rel_stmt = select(UserCustomerRelation).where(
        UserCustomerRelation.customer_id == customer.id,
        vis,
    )
    rel_result = await db.execute(rel_stmt)
    relation = rel_result.scalars().first()
    if not relation:
        return False, "未找到与该客户的跟进关系"

    if update_data.customer_name is not None:
        name = update_data.customer_name.strip()
        if not name:
            return False, "真实姓名不能为空"
        customer.customer_name = name

    if update_data.phone is not None:
        new_phone = update_data.phone.strip() or None
        if new_phone != customer.phone:
            if new_phone is not None:
                clash = await db.execute(
                    select(Customer.id).where(
                        Customer.phone == new_phone,
                        Customer.id != customer.id,
                    )
                )
                if clash.scalar_one_or_none() is not None:
                    return False, "该手机号已被其他客户占用"
            customer.phone = new_phone

    if update_data.unit_type is not None:
        customer.unit_type = update_data.unit_type
    if update_data.admin_division is not None:
        customer.admin_division = update_data.admin_division
    if update_data.purchase_months is not None:
        customer.purchase_months = schemas.normalize_purchase_months(update_data.purchase_months)

    if update_data.contact_date is not None:
        relation.contact_date = update_data.contact_date
    if update_data.suggested_followup_date is not None:
        relation.suggested_followup_date = update_data.suggested_followup_date
    if update_data.purchase_type is not None:
        relation.purchase_type = update_data.purchase_type
    if update_data.title is not None:
        relation.title = update_data.title
    if update_data.budget_amount is not None:
        relation.budget_amount = update_data.budget_amount
    if update_data.ai_profile is not None:
        relation.ai_profile = update_data.ai_profile
    if update_data.wechat_remark is not None:
        relation.wechat_remark = update_data.wechat_remark
    if update_data.dify_conversation_id is not None:
        relation.dify_conversation_id = update_data.dify_conversation_id

    if update_data.profile_tag_ids is not None:
        await replace_ucr_profile_tags(
            db, relation, update_data.profile_tag_ids, require_active=False
        )

    await db.commit()
    return True, ""

async def get_chat_history(
    db: AsyncSession, 
    user_id: int, 
    customer_id: int, 
    limit: int = 20,
    skip: int = 0
):
    """调取该业务员与该客户的 AI 互动记录"""
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.customer_id == customer_id)
        .where(ChatMessage.user_id == user_id)
        .order_by(desc(ChatMessage.created_at))
        .offset(skip)
        .limit(limit)
    )
    res = await db.execute(stmt)
    history = res.scalars().all()
    # 返回给前端时，需要按时间正序排列
    return sorted(history, key=lambda x: x.created_at)

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
        
    vis = await ucr_visibility_clause_for_user(db, user.id)
    stmt = select(UserCustomerRelation).where(
        UserCustomerRelation.customer_id == customer.id,
        vis,
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
    await db.execute(
        update(UserSalesWechat)
        .where(UserSalesWechat.user_id == u_from.id)
        .values(user_id=u_to.id)
    )
    await db.commit()
    logger.warning(f"管理员正在执行业务强行划转: 将员工 '{from_user}' 名下的 {result.rowcount} 名客户完全移交给了员工 '{to_user}'")
    return result.rowcount
