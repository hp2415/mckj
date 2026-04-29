from typing import Optional, Tuple, Any
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update, desc, and_, or_, delete, insert, func

from models import (
    RawCustomer,
    RawCustomerSalesWechat,
    SalesCustomerProfile,
    User,
    UserSalesWechat,
    ChatMessage,
    SalesWechatAccount,
    ProfileTagDefinition,
    scp_profile_tags,
)
import schemas
from datetime import date
from core.logger import logger


async def ucr_visibility_clause_for_user(db: AsyncSession, user_id: int):
    """
    当前登录用户可见的 per-sales 客户关系条件：
    - 任一已绑定销售微信号上的关系（与云客 sales_wechat_id 对齐，绑定移交后仍可见）；
    - 或历史数据：user_id 匹配且 sales_wechat_id 为空。
    """
    bind_res = await db.execute(
        select(UserSalesWechat.sales_wechat_id).where(UserSalesWechat.user_id == user_id)
    )
    bound_ids = [r[0] for r in bind_res.all() if r[0]]
    parts = [
        and_(SalesCustomerProfile.user_id == user_id, SalesCustomerProfile.sales_wechat_id.is_(None))
    ]
    if bound_ids:
        parts.append(SalesCustomerProfile.sales_wechat_id.in_(bound_ids))
    return or_(*parts)


async def profile_tags_by_relation_ids(
    db: AsyncSession, relation_ids: list[int]
) -> dict[int, list[dict]]:
    """SalesCustomerProfile id → 已绑定的动态标签列表（用于列表/详情 API）。"""
    if not relation_ids:
        return {}
    stmt = (
        select(
            scp_profile_tags.c.sales_customer_profile_id,
            ProfileTagDefinition.id,
            ProfileTagDefinition.name,
            ProfileTagDefinition.feature_note,
            ProfileTagDefinition.strategy_note,
        )
        .join(ProfileTagDefinition, ProfileTagDefinition.id == scp_profile_tags.c.profile_tag_id)
        .where(scp_profile_tags.c.sales_customer_profile_id.in_(relation_ids))
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
    relation: SalesCustomerProfile,
    raw_ids: Any,
    *,
    require_active: bool = True,
) -> None:
    """
    将标签 id 写回 per-sales 跟进线：仅保留库中存在的定义；require_active 为真时仅保留启用标签（画像 LLM）。
    桌面人工保存传 require_active=False，可保留已勾选但已在后台停用的标签。
    """
    if relation.id is None:
        await db.flush()
    parsed = parse_profile_tag_ids(raw_ids)
    await db.execute(
        delete(scp_profile_tags).where(
            scp_profile_tags.c.sales_customer_profile_id == relation.id
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
        insert(scp_profile_tags),
        [
            {"sales_customer_profile_id": relation.id, "profile_tag_id": tid}
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


async def bound_sales_wechat_ids_for_user(
    db: AsyncSession, user_id: int, username: str
) -> list[str]:
    """与 get_user_customers 一致：已绑定销售微信号（sales_wechat_id）；无绑定时用 account_code 兜底。"""
    bind_res = await db.execute(
        select(UserSalesWechat.sales_wechat_id).where(UserSalesWechat.user_id == user_id)
    )
    bound_ids = [r[0] for r in bind_res.all() if (r[0] or "").strip()]
    if not bound_ids:
        acc_res = await db.execute(
            select(SalesWechatAccount.sales_wechat_id).where(
                SalesWechatAccount.account_code == username
            )
        )
        bound_ids = [r[0] for r in acc_res.all() if (r[0] or "").strip()]
    return bound_ids


async def resolve_sales_wechat_for_profile_write(
    db: AsyncSession,
    user: User,
    raw_customer_id: str,
    body_sales_wechat_id: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """
    写入 SalesCustomerProfile 时解析 sales_wechat_id。
    - 请求体显式传入且已绑定：使用该号；
    - 未传：若员工有多枚绑定号，则在 raw_customer_sales_wechats 上与该客户求交；
      唯一命中则用该号（避免误落到「主号」新建一行）；
      多条命中则返回错误，要求客户端带 sales_wechat_id；
    - 无绑定号：与历史一致，回退主号（可为 None，对应 sales_wechat_id IS NULL 的旧关系）。
    """
    sw_from_body = body_sales_wechat_id
    if sw_from_body is not None:
        sw_from_body = str(sw_from_body).strip() or None
    if sw_from_body:
        bind_ok = await db.execute(
            select(UserSalesWechat.id).where(
                UserSalesWechat.user_id == user.id,
                UserSalesWechat.sales_wechat_id == sw_from_body,
            )
        )
        if bind_ok.first() is None:
            return None, "未绑定该业务微信，无法保存"
        return sw_from_body, None

    bound_ids = await bound_sales_wechat_ids_for_user(db, user.id, user.username)
    if not bound_ids:
        return await primary_sales_wechat_for_user(db, user.id), None

    stmt = (
        select(RawCustomerSalesWechat.sales_wechat_id)
        .where(RawCustomerSalesWechat.raw_customer_id == raw_customer_id)
        .where(RawCustomerSalesWechat.sales_wechat_id.in_(bound_ids))
        .distinct()
    )
    res = await db.execute(stmt)
    candidates = [r[0] for r in res.all() if (r[0] or "").strip()]

    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return None, (
            "该客户在您名下对应多条业务微信，请从侧栏选择具体客户行后再保存，"
            "以免跟进信息写入错误的销售微信号。"
        )
    return await primary_sales_wechat_for_user(db, user.id), None


async def effective_sales_wechat_for_customer_session(
    db: AsyncSession,
    user_id: int,
    sales_wechat_id_override: Optional[str],
) -> Optional[str]:
    """
    桌面/AI 对话与「我的客户」列表行对齐：可选传入该行 sales_wechat_id（须为当前用户已绑定号）。
    未传或非法时回退到主绑定号（与仅主号会话行为一致）。
    """
    if sales_wechat_id_override is not None:
        s = str(sales_wechat_id_override).strip()
        if s:
            bind_ok = await db.execute(
                select(UserSalesWechat.id).where(
                    UserSalesWechat.user_id == user_id,
                    UserSalesWechat.sales_wechat_id == s,
                )
            )
            if bind_ok.first() is not None:
                return s
            logger.warning(
                "sales_wechat_id_override 非当前用户绑定，已回退主号: user_id={} override_preview={}…",
                user_id,
                s[:20],
            )
    return await primary_sales_wechat_for_user(db, user_id)


async def sync_customer_info(db: AsyncSession, username: str, schema: schemas.CustomerSync):
    """
    基于逻辑自然键同步：
    1. 查找或创建客户实体 (RawCustomer，主键尽量用 external_id；否则使用 local_ 前缀)
    2. 查找或创建员工与客户的 per-sales 私域画像 (SalesCustomerProfile)
    """
    raw_customer_id = (schema.external_id or "").strip() or f"local_{schema.phone.strip()}"

    rc_res = await db.execute(select(RawCustomer).where(RawCustomer.id == raw_customer_id))
    raw_customer = rc_res.scalars().first()
    if not raw_customer:
        raw_customer = RawCustomer(id=raw_customer_id)
        db.add(raw_customer)
        await db.flush()

    raw_customer.phone = (schema.phone or "").strip() or raw_customer.phone
    raw_customer.phone_normalized = (schema.phone or "").strip() or raw_customer.phone_normalized
    raw_customer.customer_name = (schema.customer_name or "").strip() or raw_customer.customer_name
    raw_customer.unit_name = (schema.unit_name or "").strip() or raw_customer.unit_name
    raw_customer.unit_type = schema.unit_type or raw_customer.unit_type
    raw_customer.admin_division = schema.admin_division or raw_customer.admin_division
    if schema.purchase_months is not None:
        norm = schemas.normalize_purchase_months(schema.purchase_months)
        raw_customer.purchase_months = [p.strip() for p in norm.split(",") if p.strip()] if norm else []
    raw_customer.profile_status = 1

    # 2. 处理员工主观关系 (基于 ID 的关系锁定)
    user_res = await db.execute(select(User).where(User.username == username))
    user = user_res.scalars().first()
    
    if not user:
        return {"error": "User not found"}

    sales_wx = await primary_sales_wechat_for_user(db, user.id)
    vis = await ucr_visibility_clause_for_user(db, user.id)
    rel_stmt = select(SalesCustomerProfile).where(
        SalesCustomerProfile.raw_customer_id == raw_customer.id,
        SalesCustomerProfile.sales_wechat_id == sales_wx,
    )
    rel_result = await db.execute(rel_stmt)
    relation = rel_result.scalars().first()
    
    if not relation:
        relation = SalesCustomerProfile(
            user_id=user.id,
            raw_customer_id=raw_customer.id,
            sales_wechat_id=sales_wx,
            relation_type="active",
            title=schema.title,
            budget_amount=schema.budget_amount,
            ai_profile=schema.ai_profile,
            contact_date=date.today(),
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
        "id": raw_customer.id,
        "phone": raw_customer.phone,
        "customer_name": raw_customer.customer_name or "",
        "unit_name": raw_customer.unit_name or "",
        "title": relation.title,
        "budget_amount": relation.budget_amount,
        "ai_profile": relation.ai_profile,
        "dify_conversation_id": relation.dify_conversation_id,
        "contact_date": relation.contact_date,
        "profile_tags": tags,
    }

async def get_user_customers(db: AsyncSession, username: str):
    """基干工号获取该员工负责的客户列表，聚合订单金额"""
    
    # 1. 先定位员工 ID
    user_res = await db.execute(select(User).where(User.username == username))
    user = user_res.scalars().first()
    if not user:
        return []

    # 2. per-sales 列表：以 raw_customer_sales_wechats 为基表（每行 = 客户×销售号）
    bind_res = await db.execute(
        select(UserSalesWechat.sales_wechat_id).where(UserSalesWechat.user_id == user.id)
    )
    bound_ids = [r[0] for r in bind_res.all() if (r[0] or "").strip()]
    # 兜底：若用户还没维护绑定，则尝试用销售主数据推断（account_code==username）
    if not bound_ids:
        acc_res = await db.execute(
            select(SalesWechatAccount.sales_wechat_id).where(SalesWechatAccount.account_code == username)
        )
        bound_ids = [r[0] for r in acc_res.all() if (r[0] or "").strip()]
    if not bound_ids:
        # 再兜底：允许返回“历史关系”（SalesCustomerProfile.user_id==当前用户 且 sales_wechat_id 为空）
        rel_res = await db.execute(
            select(SalesCustomerProfile, RawCustomer)
            .join(RawCustomer, RawCustomer.id == SalesCustomerProfile.raw_customer_id)
            .where(SalesCustomerProfile.user_id == user.id)
            .where(SalesCustomerProfile.sales_wechat_id.is_(None))
        )
        pairs = rel_res.all()
        if not pairs:
            return []
        rel_ids = [rel.id for rel, _ in pairs if rel and rel.id]
        tag_by_rel = await profile_tags_by_relation_ids(db, rel_ids)
        customers = []
        for rel, rc in pairs:
            customers.append(
                {
                    "id": rc.id,
                    "phone": rc.phone,
                    "customer_name": rc.customer_name or "",
                    "unit_name": rc.unit_name or "",
                    "unit_type": rc.unit_type,
                    "admin_division": rc.admin_division,
                    "purchase_months": ", ".join(rc.purchase_months) if isinstance(rc.purchase_months, list) else rc.purchase_months,
                    "purchase_type": rel.purchase_type,
                    "title": rel.title,
                    "budget_amount": rel.budget_amount or 0.0,
                    "ai_profile": rel.ai_profile,
                    "wechat_remark": rel.wechat_remark,
                    "dify_conversation_id": rel.dify_conversation_id,
                    "contact_date": rel.contact_date,
                    "suggested_followup_date": rel.suggested_followup_date,
                    "sales_wechat_id": None,
                    "sales_wechat_label": None,
                    "historical_amount": 0.0,
                    "historical_order_count": 0,
                    "profile_tags": tag_by_rel.get(rel.id, []) if rel and rel.id else [],
                }
            )
        return customers

    # left join 私域画像
    stmt = (
        select(RawCustomer, RawCustomerSalesWechat, SalesCustomerProfile)
        .join(RawCustomer, RawCustomer.id == RawCustomerSalesWechat.raw_customer_id)
        .outerjoin(
            SalesCustomerProfile,
            and_(
                SalesCustomerProfile.raw_customer_id == RawCustomerSalesWechat.raw_customer_id,
                SalesCustomerProfile.sales_wechat_id == RawCustomerSalesWechat.sales_wechat_id,
            ),
        )
        .where(RawCustomerSalesWechat.sales_wechat_id.in_(bound_ids))
    )
    result = await db.execute(stmt)
    records = result.all()

    customers = []
    if not records:
        return customers

    rel_ids = [rel.id for _, _, rel in records if rel and rel.id]
    tag_by_rel = await profile_tags_by_relation_ids(db, rel_ids)

    sw_ids = {
        (rcsw.sales_wechat_id or "").strip()
        for _, rcsw, _ in records
        if (rcsw.sales_wechat_id or "").strip()
    }
    sw_account_display: dict[str, Optional[str]] = {}
    if sw_ids:
        acc_res = await db.execute(
            select(SalesWechatAccount).where(SalesWechatAccount.sales_wechat_id.in_(sw_ids))
        )
        for acc in acc_res.scalars().all():
            nick = (acc.nickname or "").strip()
            sw_account_display[acc.sales_wechat_id] = nick if nick else None

    phones = [
        (rc.phone_normalized or rc.phone)
        for rc, _, _ in records
        if (rc.phone_normalized or rc.phone)
    ]
    
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
                
        for rc, _, _ in records:
            p = (rc.phone_normalized or rc.phone)
            if p:
                agg_map[rc.id] = phone_agg_map.get(p, (0.0, 0))
                month_map[rc.id] = phone_month_map.get(p, set())

    for rc, rcsw, rel in records:
        total_amount, total_count = agg_map.get(rc.id, (0.0, 0))

        # purchase_months：优先 raw_customer.purchase_months(JSON list)，否则用订单反推
        p_months: Optional[str] = None
        if rc.purchase_months:
            if isinstance(rc.purchase_months, list):
                p_months = ", ".join([str(x).strip() for x in rc.purchase_months if str(x).strip()])
            else:
                p_months = str(rc.purchase_months)
        if not p_months and total_count and total_count > 0:
            m_set = month_map.get(rc.id, set())
            if m_set:
                p_months = ", ".join(sorted(list(m_set), key=lambda x: int(x.replace("月", ""))))
        
        # 注意：raw_customers 是“客户实体”去重快照；rcsw 是 per-sales 好友快照。
        # 为了保证多销售归属场景下可检索/可展示，列表字段需要对 rc 为空值做 rcsw 回退。
        phone_display = (rc.phone_normalized or rc.phone) or (rcsw.phone if rcsw else None)
        cust_name_display = (
            (rc.customer_name or "").strip()
            or ((rcsw.remark or "") if rcsw else "").strip()
            or ((rcsw.name or "") if rcsw else "").strip()
            or ""
        )

        customers.append({
            "id": rc.id,
            "phone": phone_display,
            "customer_name": cust_name_display,
            "unit_name": rc.unit_name or "",
            "unit_type": rc.unit_type,
            "admin_division": rc.admin_division,
            "purchase_months": p_months,
            "purchase_type": rel.purchase_type if rel else None,
            "title": rel.title if rel else None,
            "budget_amount": rel.budget_amount if rel else 0.0,
            "ai_profile": rel.ai_profile if rel else None,
            "wechat_remark": rel.wechat_remark if rel else (rcsw.remark or rc.remark),
            "dify_conversation_id": rel.dify_conversation_id if rel else None,
            "contact_date": rel.contact_date if rel else None,
            "suggested_followup_date": rel.suggested_followup_date if rel else None,
            "sales_wechat_id": rcsw.sales_wechat_id,
            "sales_wechat_label": sw_account_display.get((rcsw.sales_wechat_id or "").strip())
            if (rcsw.sales_wechat_id or "").strip()
            else None,
            "historical_amount": total_amount or 0.0,
            "historical_order_count": total_count or 0,
            "profile_tags": tag_by_rel.get(rel.id, []) if rel and rel.id else [],
        })
    return customers

async def update_customer_full_info(
    db: AsyncSession,
    username: str,
    update_data: schemas.CustomerDataUpdate,
    *,
    customer_phone: Optional[str] = None,
    customer_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """更新客户的大满贯综合面板(区分主客观数据)。按原手机号或客户 ID 定位记录。"""
    if customer_id is not None:
        cust_stmt = select(RawCustomer).where(RawCustomer.id == customer_id)
    elif customer_phone is not None:
        cust_stmt = select(RawCustomer).where(
            or_(RawCustomer.phone == customer_phone, RawCustomer.phone_normalized == customer_phone)
        )
    else:
        return False, "缺少客户定位信息"

    cust_res = await db.execute(cust_stmt)
    customer = cust_res.scalars().first()

    user_res = await db.execute(select(User).where(User.username == username))
    user = user_res.scalars().first()

    if not (user and customer):
        return False, "客户不存在或无权操作"

    sales_wx, sw_err = await resolve_sales_wechat_for_profile_write(
        db, user, customer.id, update_data.sales_wechat_id
    )
    if sw_err:
        return False, sw_err
    rel_stmt = select(SalesCustomerProfile).where(
        SalesCustomerProfile.raw_customer_id == customer.id,
        SalesCustomerProfile.sales_wechat_id == sales_wx,
    )
    rel_result = await db.execute(rel_stmt)
    relation = rel_result.scalars().first()
    if not relation:
        relation = SalesCustomerProfile(
            raw_customer_id=customer.id,
            sales_wechat_id=sales_wx,
            user_id=user.id,
            relation_type="active",
            contact_date=date.today(),
        )
        db.add(relation)
        await db.flush()

    if update_data.customer_name is not None:
        name = update_data.customer_name.strip()
        if not name:
            return False, "真实姓名不能为空"
        customer.customer_name = name

    if update_data.phone is not None:
        new_phone = update_data.phone.strip() or None
        customer.phone = new_phone
        customer.phone_normalized = new_phone

    if update_data.unit_type is not None:
        customer.unit_type = update_data.unit_type
    if update_data.admin_division is not None:
        customer.admin_division = update_data.admin_division
    if update_data.purchase_months is not None:
        norm = schemas.normalize_purchase_months(update_data.purchase_months)
        customer.purchase_months = [p.strip() for p in norm.split(",") if p.strip()] if norm else []

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

    customer.profile_updated_at = func.now()
    customer.profile_status = 1
    await db.commit()
    return True, ""

async def get_chat_history(
    db: AsyncSession, 
    user_id: int, 
    customer_id: str, 
    limit: int = 20,
    skip: int = 0
):
    """调取该业务员与该客户的 AI 互动记录"""
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.raw_customer_id == customer_id)
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
    customer_id: str,
    msg_in: schemas.ChatMessageCreate
):
    """保存单条对话记录"""
    db_msg = ChatMessage(
        user_id=user_id,
        raw_customer_id=customer_id,
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
    update_data: schemas.RelationUpdate,
) -> Tuple[Optional[SalesCustomerProfile], Optional[str]]:
    """局部更新动态互动数据，包括 Dify 会话 ID。第二项为错误说明（如多业务微信未指定）。"""
    user_res = await db.execute(select(User).where(User.username == username))
    user = user_res.scalars().first()
    cust_res = await db.execute(
        select(RawCustomer).where(
            or_(RawCustomer.phone == customer_phone, RawCustomer.phone_normalized == customer_phone)
        )
    )
    customer = cust_res.scalars().first()

    if not (user and customer):
        return None, None

    sales_wx, sw_err = await resolve_sales_wechat_for_profile_write(
        db, user, customer.id, None
    )
    if sw_err:
        return None, sw_err
    stmt = select(SalesCustomerProfile).where(
        SalesCustomerProfile.raw_customer_id == customer.id,
        SalesCustomerProfile.sales_wechat_id == sales_wx,
    )
    result = await db.execute(stmt)
    relation = result.scalars().first()
    
    if not relation:
        relation = SalesCustomerProfile(
            raw_customer_id=customer.id,
            sales_wechat_id=sales_wx,
            user_id=user.id,
            relation_type="active",
            contact_date=date.today(),
        )
        db.add(relation)
        await db.flush()
        
    if update_data.title is not None:
        relation.title = update_data.title
    if update_data.budget_amount is not None:
        relation.budget_amount = update_data.budget_amount
    if update_data.ai_profile is not None:
        relation.ai_profile = update_data.ai_profile
    if update_data.dify_conversation_id is not None:
        relation.dify_conversation_id = update_data.dify_conversation_id
    if update_data.wechat_remark is not None:
        relation.wechat_remark = update_data.wechat_remark
        
    await db.commit()
    await db.refresh(relation)
    return relation, None

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
        update(SalesCustomerProfile)
        .where(SalesCustomerProfile.user_id == u_from.id)
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
