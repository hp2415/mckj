from typing import Optional, Tuple, Any
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update, desc, and_, or_, delete, insert, func, case

from models import (
    RawCustomer,
    RawCustomerSalesWechat,
    SalesCustomerProfile,
    User,
    UserSalesWechat,
    ChatMessage,
    RawChatLog,
    SalesWechatAccount,
    ProfileTagDefinition,
    scp_profile_tags,
)
from ai.chat_log_filter import raw_chat_log_meaningful_clause
from ai.raw_chat_time import raw_chat_event_time_ms_expr
import schemas
from datetime import date
from core.logger import logger


def chat_message_thread_clause(
    session_sales_wechat_id: Optional[str],
    primary_sales_wechat_id: Optional[str],
):
    """
    将 AI 聊天记录限定在「客户 × 业务微信」线程内。
    - 指定当前号线程且为主号：该号 + 历史遗留 NULL（旧数据视为跟主号同一线程）
    - 指定当前号线程且非主号：仅该号
    - 未指定 session（旧客户端）：主号 + NULL
    """
    sw = (session_sales_wechat_id or "").strip() or None
    primary_s = (primary_sales_wechat_id or "").strip() or None
    if sw:
        if primary_s and sw == primary_s:
            return or_(ChatMessage.sales_wechat_id == sw, ChatMessage.sales_wechat_id.is_(None))
        return ChatMessage.sales_wechat_id == sw
    if primary_s:
        return or_(ChatMessage.sales_wechat_id.is_(None), ChatMessage.sales_wechat_id == primary_s)
    return ChatMessage.sales_wechat_id.is_(None)


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

    按 diff 删除/插入（按 tag_id 排序），避免全量 DELETE+INSERT 在热标签上放大死锁。
    """
    if relation.id is None:
        await db.flush()
    parsed = parse_profile_tag_ids(raw_ids)
    wanted: set[int] = set()
    if parsed:
        stmt_ok = select(ProfileTagDefinition.id).where(ProfileTagDefinition.id.in_(parsed))
        if require_active:
            stmt_ok = stmt_ok.where(ProfileTagDefinition.is_active.is_(True))
        res_ok = await db.execute(stmt_ok)
        wanted = {int(row[0]) for row in res_ok.all()}

    res_cur = await db.execute(
        select(scp_profile_tags.c.profile_tag_id).where(
            scp_profile_tags.c.sales_customer_profile_id == relation.id
        )
    )
    current = {int(row[0]) for row in res_cur.all()}
    to_del = sorted(current - wanted)
    to_add = sorted(wanted - current)
    if to_del:
        await db.execute(
            delete(scp_profile_tags).where(
                scp_profile_tags.c.sales_customer_profile_id == relation.id,
                scp_profile_tags.c.profile_tag_id.in_(to_del),
            )
        )
    if to_add:
        await db.execute(
            insert(scp_profile_tags),
            [
                {"sales_customer_profile_id": relation.id, "profile_tag_id": tid}
                for tid in to_add
            ],
        )


async def get_user_bound_sales_wechat_ids(db: AsyncSession, user_id: int) -> list[str]:
    """当前用户绑定的销售微信号列表（去重、有序）。"""
    res = await db.execute(
        select(UserSalesWechat.sales_wechat_id).where(UserSalesWechat.user_id == user_id)
    )
    ids = sorted({(r[0] or "").strip() for r in res.all() if (r[0] or "").strip()})
    if ids:
        return ids
    user_res = await db.execute(select(User.username).where(User.id == user_id))
    username = user_res.scalar_one_or_none()
    if not username:
        return []
    acc_res = await db.execute(
        select(SalesWechatAccount.sales_wechat_id).where(SalesWechatAccount.account_code == username)
    )
    return sorted({(r[0] or "").strip() for r in acc_res.all() if (r[0] or "").strip()})


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

async def get_user_customers(
    db: AsyncSession,
    username: str,
    *,
    skip: int = 0,
    limit: Optional[int] = None,
):
    """基干工号获取该员工负责的客户列表，聚合订单金额。

    skip/limit：可选分页（按 客户ID+销售号 稳定排序）；limit 为空时保持全量返回（兼容旧桌面端）。
    """
    
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
                    "ai_profile": None,
                    "has_ai_profile": bool((rel.ai_profile or "").strip()),
                    "wechat_remark": rel.wechat_remark,
                    "dify_conversation_id": rel.dify_conversation_id,
                    "contact_date": rel.contact_date,
                    "suggested_followup_date": rel.suggested_followup_date,
                    "sales_wechat_id": None,
                    "sales_wechat_label": None,
                    "historical_amount": 0.0,
                    "historical_order_count": 0,
                    "had_order_last_year": False,
                    "has_recent_order_2m": False,
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
    if limit is not None:
        stmt = (
            stmt.order_by(
                RawCustomerSalesWechat.raw_customer_id,
                RawCustomerSalesWechat.sales_wechat_id,
            )
            .offset(max(0, int(skip)))
            .limit(max(1, int(limit)))
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

    from core.order_match import (
        fold_buyer_idx_aggregates,
        is_staff_uuid_order_match_enabled,
        is_unit_name_order_match_enabled,
        map_units_to_buyer_names,
        order_window_bounds,
        peek_buyer_order_aggregates,
        requires_staff_uuid_order_match,
        resolve_staff_uuid_order_match_enabled,
        resolve_unit_name_order_match_enabled,
        schedule_buyer_order_agg_refresh,
        usable_phones,
        usable_staff_uuid,
        usable_unit_name,
    )
    from core.data_visibility import (
        order_visibility_clause,
        resolve_order_viewer_for_user,
    )
    from models import RawOrder

    viewer = await resolve_order_viewer_for_user(db, user)
    vis_clause = order_visibility_clause(
        view_all=viewer.view_all,
        allowed_aliases=viewer.allowed_aliases,
    )

    await resolve_staff_uuid_order_match_enabled(db)
    staff_uuid_filter: str | None = None
    staff_match_blocks_all = False
    if is_staff_uuid_order_match_enabled() and requires_staff_uuid_order_match(
        getattr(user, "role", None)
    ):
        staff_uuid_filter = usable_staff_uuid(getattr(user, "mibuddy_uuid", None))
        if not staff_uuid_filter:
            # 普通员工未绑定米城 UUID：历史总额与订单列表一致，视为无单
            staff_match_blocks_all = True

    phones = []
    seen_phones: set[str] = set()
    if not staff_match_blocks_all:
        for rc, _, _ in records:
            for p in usable_phones(rc.phone_normalized or rc.phone):
                if p not in seen_phones:
                    seen_phones.add(p)
                    phones.append(p)

    # 列表订单统计（轻量）：
    # 1) 电话：分片 IN + SQL GROUP BY（按 wechat_idx 可见性过滤；可选 staff_uuid）
    # 2) 单位：预热缓存按 wechat_idx 分桶后折叠
    agg_map = {}
    month_map = {}
    year_flag_map: dict[Any, tuple[bool, bool]] = {}
    phone_agg_map: dict[str, tuple[float, int]] = {}
    phone_month_map: dict[str, set[str]] = {}
    phone_year_flag_map: dict[str, tuple[bool, bool]] = {}

    last_year_start, this_year_start, recent_start, nearby_months = order_window_bounds()
    phone_chunk = 400
    if phones:
        for i in range(0, len(phones), phone_chunk):
            chunk = phones[i : i + phone_chunk]
            phone_where = [RawOrder.consignee_phone.in_(chunk)]
            if vis_clause is not None:
                phone_where.append(vis_clause)
            if staff_uuid_filter:
                phone_where.append(RawOrder.staff_uuid == staff_uuid_filter)
            agg_res = await db.execute(
                select(
                    RawOrder.consignee_phone,
                    func.coalesce(func.sum(RawOrder.pay_amount), 0),
                    func.count(RawOrder.id),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    and_(
                                        RawOrder.order_time >= last_year_start,
                                        RawOrder.order_time < this_year_start,
                                        func.month(RawOrder.order_time).in_(nearby_months),
                                    ),
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (RawOrder.order_time >= recent_start, 1),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                )
                .where(and_(*phone_where))
                .group_by(RawOrder.consignee_phone)
            )
            for phone, total, cnt, ly_cnt, recent_cnt in agg_res.all():
                if phone:
                    phone_agg_map[str(phone)] = (float(total or 0), int(cnt or 0))
                    phone_year_flag_map[str(phone)] = (
                        int(ly_cnt or 0) > 0,
                        int(recent_cnt or 0) > 0,
                    )
            month_res = await db.execute(
                select(RawOrder.consignee_phone, func.month(RawOrder.order_time))
                .where(and_(*phone_where))
                .where(RawOrder.order_time.is_not(None))
                .group_by(RawOrder.consignee_phone, func.month(RawOrder.order_time))
            )
            for phone, month_num in month_res.all():
                if phone and month_num:
                    phone_month_map.setdefault(str(phone), set()).add(f"{int(month_num)}月")

    # 单位名匹配受开关控制（默认关）；开启时电话命中后仍补齐换号/旧号订单
    # 启用 staff_uuid 过滤时不做单位名补齐（缓存未按 staff 分桶，避免与订单列表不一致）
    await resolve_unit_name_order_match_enabled(db)
    need_unit_names: list[str] = []
    seen_need_units: set[str] = set()
    if (
        not staff_match_blocks_all
        and not staff_uuid_filter
        and is_unit_name_order_match_enabled()
    ):
        for rc, _, _ in records:
            u = usable_unit_name(rc.unit_name)
            if u and u not in seen_need_units:
                seen_need_units.add(u)
                need_unit_names.append(u)

    buyer_agg_map = {}
    buyer_month_map = {}
    buyer_year_flag_map = {}
    unit_to_buyers: dict[str, list[str]] = {}
    if need_unit_names:
        cached = peek_buyer_order_aggregates()
        if cached is None:
            schedule_buyer_order_agg_refresh()
        else:
            buyer_agg_map, buyer_month_map, buyer_year_flag_map = cached
            unit_to_buyers = map_units_to_buyer_names(
                need_unit_names, list(buyer_agg_map.keys())
            )

    if phones or need_unit_names:
        for rc, _, _ in records:
            cust_phones = usable_phones(rc.phone_normalized or rc.phone)
            u = usable_unit_name(rc.unit_name)
            if not cust_phones and not u:
                continue

            total_amount = 0.0
            total_count = 0
            months: set[str] = set()
            had_last_year = False
            has_recent = False

            for p in cust_phones:
                if p not in phone_agg_map:
                    continue
                amt, cnt = phone_agg_map[p]
                total_amount += float(amt or 0)
                total_count += int(cnt or 0)
                months |= set(phone_month_map.get(p, set()))
                ly, recent = phone_year_flag_map.get(p, (False, False))
                had_last_year = had_last_year or ly
                has_recent = has_recent or recent

            # 单位名始终合并：排除当前客户全部电话，避免与电话路径双重计数；换号订单靠此补齐
            if u and unit_to_buyers:
                exclude_phones = cust_phones
                for bn in unit_to_buyers.get(u, []):
                    ba, bc, bm, ly, recent = fold_buyer_idx_aggregates(
                        bn,
                        buyer_agg_map,
                        buyer_month_map,
                        buyer_year_flag_map,
                        view_all=viewer.view_all,
                        allowed_aliases=viewer.allowed_aliases,
                        exclude_phones=exclude_phones,
                    )
                    total_amount += float(ba or 0)
                    total_count += int(bc or 0)
                    months |= bm
                    had_last_year = had_last_year or ly
                    has_recent = has_recent or recent

            if total_count:
                agg_map[rc.id] = (total_amount, total_count)
                month_map[rc.id] = months
                year_flag_map[rc.id] = (had_last_year, has_recent)

    for rc, rcsw, rel in records:
        total_amount, total_count = agg_map.get(rc.id, (0.0, 0))
        had_last_year, has_recent = year_flag_map.get(rc.id, (False, False))

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
            "ai_profile": None,
            "has_ai_profile": bool((rel.ai_profile or "").strip()) if rel else False,
            "wechat_remark": (rel.wechat_remark if rel else None) or (rcsw.remark if rcsw else None),
            "dify_conversation_id": rel.dify_conversation_id if rel else None,
            "contact_date": rel.contact_date if rel else None,
            "suggested_followup_date": rel.suggested_followup_date if rel else None,
            "sales_wechat_id": rcsw.sales_wechat_id,
            "sales_wechat_label": sw_account_display.get((rcsw.sales_wechat_id or "").strip())
            if (rcsw.sales_wechat_id or "").strip()
            else None,
            "historical_amount": total_amount or 0.0,
            "historical_order_count": total_count or 0,
            "had_order_last_year": bool(had_last_year),
            "has_recent_order_2m": bool(has_recent),
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
    skip: int = 0,
    sales_wechat_id: Optional[str] = None,
):
    """调取该业务员与该客户的 AI 互动记录（多业务微信时按侧栏行隔离）。"""
    primary = await primary_sales_wechat_for_user(db, user_id)
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.raw_customer_id == customer_id)
        .where(ChatMessage.user_id == user_id)
        .where(chat_message_thread_clause(sales_wechat_id, primary))
        .order_by(desc(ChatMessage.created_at))
        .offset(skip)
        .limit(limit)
    )
    res = await db.execute(stmt)
    history = res.scalars().all()
    # 返回给前端时，需要按时间正序排列
    return sorted(history, key=lambda x: x.created_at)

async def get_raw_wechat_chat_logs(
    db: AsyncSession,
    raw_customer_id: str,
    sales_wechat_id: str,
    limit: int = 50,
    skip: int = 0,
):
    """按「业务微信 × 客户」拉取云客同步的微信原始聊天记录（时间正序）。"""
    cid = (raw_customer_id or "").strip()
    sw = (sales_wechat_id or "").strip()
    if not cid or not sw:
        return []
    event_ms = raw_chat_event_time_ms_expr()
    stmt = (
        select(RawChatLog)
        .where(
            and_(
                or_(
                    and_(RawChatLog.wechat_id == sw, RawChatLog.talker == cid),
                    and_(RawChatLog.wechat_id == cid, RawChatLog.talker == sw),
                ),
                raw_chat_log_meaningful_clause(RawChatLog.text),
            )
        )
        .order_by(event_ms.asc())
        .offset(skip)
        .limit(limit)
    )
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def user_can_view_customer_wechat_logs(
    db: AsyncSession,
    user_id: int,
    raw_customer_id: str,
    sales_wechat_id: Optional[str] = None,
) -> bool:
    """校验当前用户是否可见该客户（及可选的业务微信行）。

    old_customer/admin：只要「我的客户」可见该客户（任一自有绑定号），
    即可只读查看该客户在任意销售号下的微信聊天记录。
    """
    from core.data_visibility import can_read_others_chat_summary

    cid = (raw_customer_id or "").strip()
    if not cid:
        return False

    user_res = await db.execute(select(User).where(User.id == user_id))
    user = user_res.scalars().first()
    if not user:
        return False

    # 先确认客户在「我的客户」可见范围内（绑定号 RCSW 或历史 SCP）
    bound_ids = await bound_sales_wechat_ids_for_user(db, user.id, user.username)
    has_customer = False
    if bound_ids:
        rcsw_res = await db.execute(
            select(RawCustomerSalesWechat.raw_customer_id)
            .where(RawCustomerSalesWechat.raw_customer_id == cid)
            .where(RawCustomerSalesWechat.sales_wechat_id.in_(bound_ids))
            .limit(1)
        )
        has_customer = rcsw_res.first() is not None
    if not has_customer:
        vis = await ucr_visibility_clause_for_user(db, user_id)
        scp_res = await db.execute(
            select(SalesCustomerProfile.id)
            .where(SalesCustomerProfile.raw_customer_id == cid, vis)
            .limit(1)
        )
        has_customer = scp_res.scalars().first() is not None
    if not has_customer:
        return False

    sw = (sales_wechat_id or "").strip()
    if not sw:
        return True

    # 本号：始终允许
    if sw in set(bound_ids or []):
        return True

    # 他人号：仅 old_customer/admin 只读
    return can_read_others_chat_summary(getattr(user, "role", None))


async def create_chat_message(
    db: AsyncSession,
    user_id: int,
    customer_id: str,
    msg_in: schemas.ChatMessageCreate
):
    """保存单条对话记录"""
    sw_save = getattr(msg_in, "sales_wechat_id", None)
    sw_save = (str(sw_save).strip() if sw_save is not None and str(sw_save).strip() else None)
    db_msg = ChatMessage(
        user_id=user_id,
        raw_customer_id=customer_id,
        role=msg_in.role,
        content=msg_in.content,
        dify_conv_id=msg_in.dify_conv_id,
        is_regenerated=getattr(msg_in, "is_regenerated", False),
        sales_wechat_id=sw_save,
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
