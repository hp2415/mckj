from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_
import schemas
import crud
from database import get_db
from api.auth import get_current_user
from models import User, RawCustomer, RawOrder, RawOrderItem, SalesCustomerProfile, RawChatLog

router = APIRouter(prefix="/api/customer", tags=["Customer"])

@router.post("/sync", response_model=schemas.CustomerResponse)
async def sync_customer(
    customer_data: schemas.CustomerSync,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    当桌面端检测到正在与某位客户沟通时，同步基本信息（基于自然键）。
    """
    result = await crud.sync_customer_info(db, username=current_user.username, schema=customer_data)
    return result

@router.get("/my", response_model=schemas.CustomerListResponse)
async def get_my_customers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    获取当前登录员工负责的客户列表。
    """
    customers = await crud.get_user_customers(db, username=current_user.username)
    return {"code": 200, "message": "获取成功", "data": customers}


@router.get("/profile_tag_options")
async def get_profile_tag_options(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """桌面端：可选动态标签列表（仅启用项）。"""
    data = await crud.list_active_profile_tag_options(db)
    return {"code": 200, "message": "ok", "data": data}


@router.post("/handover")
async def handover_business(
    from_user: str,
    to_user: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    业务大移交：仅限管理员操作，将 A 员工的所有客户数据包转给 B 员工。
    """
    if current_user.role != "admin":
        return {"code": 403, "message": "权限不足，仅限管理员操作"}
        
    count = await crud.transfer_user_customers(db, from_user, to_user)
    return {"code": 200, "message": f"移交成功，共处理 {count} 个客户关系"}

@router.patch("/relation")
async def update_relation(
    customer_phone: str,
    update_data: schemas.RelationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    更新当前登录员工对特定客户的动态备注信息。
    """
    relation, rel_err = await crud.update_user_customer_relation(
        db,
        username=current_user.username,
        customer_phone=customer_phone,
        update_data=update_data,
    )
    if rel_err:
        return {"code": 400, "message": rel_err}
    if not relation:
        return {"code": 404, "message": "关联关系不存在"}
    return {"code": 200, "message": "更新成功"}

@router.put("/id/{customer_id}/info")
async def update_customer_info_by_id(
    customer_id: str,
    update_data: schemas.CustomerDataUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """无手机号客户：按主键更新面板（桌面端在 phone 为空时使用）。"""
    ok, msg = await crud.update_customer_full_info(
        db,
        username=current_user.username,
        update_data=update_data,
        customer_id=customer_id,
    )
    return {"code": 200 if ok else 400, "message": msg or ("更新成功" if ok else "更新失败")}


@router.put("/{phone}/info")
async def update_customer_info(
    phone: str,
    update_data: schemas.CustomerDataUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    桌面端提交全量的面板修改数据 (客观单位资料 + 主观建联日)
    """
    ok, msg = await crud.update_customer_full_info(
        db,
        username=current_user.username,
        update_data=update_data,
        customer_phone=phone,
    )
    return {"code": 200 if ok else 400, "message": msg or ("更新成功" if ok else "更新失败")}

@router.get("/orders/{customer_id}")
async def get_customer_orders(
    customer_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    拉取某个客户的所有历史订单明细，在桌面端以弹窗下钻展示
    """
    from sqlalchemy.future import select

    # 1. Get customer phone from raw_customers
    stmt_c = select(RawCustomer.phone_normalized, RawCustomer.phone).where(RawCustomer.id == customer_id)
    res_c = await db.execute(stmt_c)
    row = res_c.first()
    phone = (row[0] if row else None) or (row[1] if row else None)
    
    if not phone:
        return {"code": 200, "message": "success", "data": []}
    
    # Clean phone for matching (remove non-digits if needed, though search_phone should already be clean)
    clean_phone = "".join(filter(str.isdigit, phone))
    
    # 2. Fetch RawOrders
    stmt = select(RawOrder).where(RawOrder.search_phone == clean_phone).order_by(RawOrder.order_time.desc())
    res = await db.execute(stmt)
    orders = res.scalars().all()
    
    order_list = []
    for o in orders:
        # Fetch items
        stmt_i = select(RawOrderItem.product_name).where(RawOrderItem.raw_order_id == o.id)
        res_i = await db.execute(stmt_i)
        items = res_i.scalars().all()
        
        order_list.append({
            "dddh": o.dddh,
            "order_time": o.order_time.strftime("%Y-%m-%d %H:%M:%S") if o.order_time else "-",
            "product_title": " | ".join(items) if items else "未指定商品",
            "pay_amount": float(o.pay_amount) if o.pay_amount else 0.0,
            "status_name": o.status_name,
            "consignee": f"{o.consignee or ''} ({o.consignee_phone or ''})",
            "store": o.store or "未知店铺",
            "consignee_address": o.consignee_address or "-",
            "freight": float(o.freight) if o.freight else 0.0,
            "buyer_name": o.buyer_name or "-",
            "pay_type_name": o.pay_type_name or "未记录",
            "remark": o.remark or ""
        })
    return {"code": 200, "message": "success", "data": order_list}

from fastapi import UploadFile, File
import pandas as pd
import io
import datetime
from sqlalchemy import select

@router.post("/upload_wechat")
async def upload_wechat_history(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    接收微信聊天记录 Excel/CSV 并通过 (username, wechat_remark) 宽泛/精细匹配挂载至客户流水库
    """
    if not file.filename.endswith(('.csv', '.xlsx')):
        return {"code": 400, "message": "仅支持 .csv 或 .xlsx 格式文件"}
    
    contents = await file.read()
    try:
        if file.filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(contents))
        else:
            df = pd.read_excel(io.BytesIO(contents))
    except Exception as e:
        return {"code": 400, "message": f"文件解析失败: {str(e)}"}
        
    expected_cols = ["聊天内容", "时间", "发送方", "客户微信备注名", "销售微信名"]
    for col in expected_cols:
        if col not in df.columns:
            return {"code": 400, "message": f"缺少必须的数据列: {col}"}
            
    df = df.dropna(subset=expected_cols)
    
    # 缓存匹配查询：与「我的客户」一致，含绑定销售号维度
    vis = await crud.ucr_visibility_clause_for_user(db, current_user.id)
    rel_stmt = select(SalesCustomerProfile).where(vis)
    rel_res = await db.execute(rel_stmt)
    relations = rel_res.scalars().all()
    
    # 建立映射: wechat_remark -> raw_customer_id（同备注可能多客户时，先取第一条）
    remark_to_customer: dict[str, str] = {}
    for r in relations:
        k = (r.wechat_remark or "").strip()
        if k and r.raw_customer_id and k not in remark_to_customer:
            remark_to_customer[k] = r.raw_customer_id
    
    success_count = 0
    fail_count = 0
    
    new_logs = []

    primary_sw = await crud.primary_sales_wechat_for_user(db, current_user.id)
    
    for _, row in df.iterrows():
        sales_name = str(row["销售微信名"]).strip()
        customer_remark = str(row["客户微信备注名"]).strip()
        chat_content = str(row["聊天内容"]).strip()
        sender = str(row["发送方"]).strip()
        try:
            chat_time = pd.to_datetime(row["时间"])
        except:
            chat_time = datetime.datetime.now()
            
        # 1. 如果该条记录不是属于当前登录销售的（通过各种宽松匹配）
        # 如果销售微信名根本不是他，就跳过
        
        # 2. 匹配 customer_id
        target_raw_customer_id = remark_to_customer.get(customer_remark)
        if not target_raw_customer_id:
            fail_count += 1
            continue

        # raw_chat_logs: talker 用 raw_customer_id；wechat_id 用当前用户主销售号（无法从文件稳定拿到 wxid）
        is_send = 0
        if sender and (sender == sales_name or sender in ("我", "自己", "销售", "客服")):
            is_send = 1
        ts_ms = int(chat_time.timestamp() * 1000) if chat_time else int(datetime.datetime.now().timestamp() * 1000)

        log = RawChatLog(
            talker=target_raw_customer_id,
            wechat_id=primary_sw or "",
            text=chat_content,
            timestamp=ts_ms,
            is_send=is_send,
            message_type=1,
            name=sender or None,
            file_source=file.filename,
        )
        new_logs.append(log)
        success_count += 1
        
    if new_logs:
        db.add_all(new_logs)
        await db.commit()
        
    return {"code": 200, "message": f"成功导入 {success_count} 条，失败屏蔽 {fail_count} 条（未对应微信备注）。"}

@router.get("/{phone}/chat_history", response_model=schemas.ChatHistoryResponse)
async def get_customer_chat_history(
    phone: str,
    limit: int = 20,
    skip: int = 0,
    sales_wechat_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取该客户与当前用户的 AI 聊天历史"""
    stmt = select(RawCustomer).where(or_(RawCustomer.phone == phone, RawCustomer.phone_normalized == phone))
    res = await db.execute(stmt)
    customer = res.scalars().first()
    if not customer:
        return {"code": 404, "message": "客户不存在"}
        
    history = await crud.get_chat_history(
        db,
        current_user.id,
        customer.id,
        limit=limit,
        skip=skip,
        sales_wechat_id=sales_wechat_id,
    )
    return {"code": 200, "data": history}


@router.get("/id/{raw_customer_id}/chat_history", response_model=schemas.ChatHistoryResponse)
async def get_customer_chat_history_by_id(
    raw_customer_id: str,
    limit: int = 20,
    skip: int = 0,
    sales_wechat_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """按 raw_customer_id 获取该客户与当前用户的 AI 聊天历史（不依赖手机号）。"""
    history = await crud.get_chat_history(
        db,
        current_user.id,
        raw_customer_id,
        limit=limit,
        skip=skip,
        sales_wechat_id=sales_wechat_id,
    )
    return {"code": 200, "data": history}

@router.post("/{phone}/chat_message")
async def save_customer_chat_message(
    phone: str,
    msg_in: schemas.ChatMessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """保存单条对话记录 (用户手动备份模式)"""
    stmt = select(RawCustomer).where(or_(RawCustomer.phone == phone, RawCustomer.phone_normalized == phone))
    res = await db.execute(stmt)
    customer = res.scalars().first()
    if not customer:
        return {"code": 404, "message": "客户不存在"}
        
    msg = await crud.create_chat_message(db, current_user.id, customer.id, msg_in)
    return {"code": 200, "message": "已落盘", "data": {"id": msg.id}}

@router.post("/message/{msg_id}/feedback")
async def update_chat_feedback(
    msg_id: int,
    rating: int, # 1 或 -1
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """更新某条 AI 回复的采纳度评价"""
    from models import ChatMessage
    from sqlalchemy import update
    
    stmt = update(ChatMessage).where(ChatMessage.id == msg_id).values(
        rating=rating,
        feedback_at=datetime.datetime.now()
    )
    try:
        await db.execute(stmt)
        await db.commit()
    except Exception as e:
        return {"code": 500, "message": f"评价更新失败: {str(e)}"}
    return {"code": 200, "message": "评价成功"}

@router.post("/message/{msg_id}/copy")
async def update_chat_copy(
    msg_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """记录消息被复制的采纳行为"""
    from models import ChatMessage
    from sqlalchemy import update
    import datetime
    
    stmt = update(ChatMessage).where(ChatMessage.id == msg_id).values(
        is_copied=True,
        copied_at=datetime.datetime.now()
    )
    try:
        await db.execute(stmt)
        await db.commit()
    except Exception as e:
        return {"code": 500, "message": f"采纳上报失败: {str(e)}"}
    return {"code": 200, "message": "采纳记录已上报"}

@router.post("/import_manual_followup")
async def import_manual_followup(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    手动导入本周需跟进的客户，打上专用动态标签以生成独立分组。
    过渡功能方案：完全使用 ProfileTag 实现，不修改核心数据库字段。
    """
    if not file.filename.endswith(('.csv', '.xlsx', '.xls')):
        return {"code": 400, "message": "仅支持 .xlsx 或 .csv 格式文件"}
    
    contents = await file.read()
    try:
        if file.filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(contents))
        else:
            df = pd.read_excel(io.BytesIO(contents))
    except Exception as e:
        return {"code": 400, "message": f"文件解析失败: {str(e)}"}
        
    df.columns = df.columns.str.strip()
    
    has_phone = "客户手机号" in df.columns
    has_remark = "客户微信备注名" in df.columns
    if not has_phone and not has_remark:
        return {"code": 400, "message": "缺少必须的列，请确保包含【客户手机号】或【客户微信备注名】列"}
    
    # 1. 确保系统存在专属标签
    TAG_NAME = "📌 手动导入跟进"
    from models import ProfileTagDefinition, scp_profile_tags
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError
    
    tag_stmt = select(ProfileTagDefinition).where(ProfileTagDefinition.name == TAG_NAME)
    tag_res = await db.execute(tag_stmt)
    tag = tag_res.scalars().first()
    
    if not tag:
        tag = ProfileTagDefinition(
            name=TAG_NAME, 
            feature_note="手动导入的待跟进客户（系统生成，不由模型为客户打上标签）", 
            strategy_note="请在本周内优先进行沟通", 
            is_active=True, 
            sort_order=999
        )
        db.add(tag)
        await db.flush()
        
    tag_id = tag.id
    
    # 2. 获取当前用户管辖的所有客户关系
    vis = await crud.ucr_visibility_clause_for_user(db, current_user.id)
    # Join with RawCustomer to match phone/remark
    rel_stmt = (
        select(SalesCustomerProfile, RawCustomer)
        .join(RawCustomer, RawCustomer.id == SalesCustomerProfile.raw_customer_id)
        .where(vis)
    )
    rel_res = await db.execute(rel_stmt)
    relations = rel_res.all()
    
    # 构建查找字典
    phone_to_rel_id = {}
    remark_to_rel_id = {}
    for rel, rc in relations:
        if rc.phone:
            phone_to_rel_id[str(rc.phone).strip()] = rel.id
        if rc.phone_normalized:
            phone_to_rel_id[str(rc.phone_normalized).strip()] = rel.id
        if rel.wechat_remark:
            remark_to_rel_id[str(rel.wechat_remark).strip()] = rel.id
        elif rc.remark:
            remark_to_rel_id[str(rc.remark).strip()] = rel.id
            
    success_count = 0
    fail_count = 0
    
    for _, row in df.iterrows():
        matched_rel_id = None
        
        # 优先按手机号匹配
        if has_phone and pd.notna(row["客户手机号"]):
            phone_val = str(row["客户手机号"]).strip()
            # 尝试提取纯数字进行匹配
            digits = "".join(filter(str.isdigit, phone_val))
            if digits in phone_to_rel_id:
                matched_rel_id = phone_to_rel_id[digits]
            elif phone_val in phone_to_rel_id:
                matched_rel_id = phone_to_rel_id[phone_val]
                
        # 其次按微信备注匹配
        if not matched_rel_id and has_remark and pd.notna(row["客户微信备注名"]):
            remark_val = str(row["客户微信备注名"]).strip()
            if remark_val in remark_to_rel_id:
                matched_rel_id = remark_to_rel_id[remark_val]
                
        if matched_rel_id:
            # 给该关系打上标签
            try:
                # 使用 INSERT IGNORE 机制或捕获异常避免重复主键
                await db.execute(insert(scp_profile_tags).values(
                    sales_customer_profile_id=matched_rel_id, 
                    profile_tag_id=tag_id
                ))
                success_count += 1
            except IntegrityError:
                # 已经打过标签了，不作为失败，但也不重复计算
                pass
        else:
            fail_count += 1
            
    await db.commit()
    
    return {
        "code": 200, 
        "message": f"导入完成！成功标记 {success_count} 位客户为手动跟进，失败或未找到 {fail_count} 条（可能不在您的管辖列表）。"
    }

@router.post("/clear_manual_followup")
async def clear_manual_followup(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    一键清空当前用户管辖范围内的所有客户的“📌 手动导入跟进”标签。
    """
    TAG_NAME = "📌 手动导入跟进"
    from models import ProfileTagDefinition, scp_profile_tags
    from sqlalchemy import delete
    
    # 1. 查找标签 ID
    tag_stmt = select(ProfileTagDefinition.id).where(ProfileTagDefinition.name == TAG_NAME)
    tag_res = await db.execute(tag_stmt)
    tag_id = tag_res.scalar_one_or_none()
    
    if not tag_id:
        return {"code": 200, "message": "标签不存在，无需清理。"}
        
    # 2. 获取当前用户管辖的客户关系 ID 列表
    vis = await crud.ucr_visibility_clause_for_user(db, current_user.id)
    rel_stmt = select(SalesCustomerProfile.id).where(vis)
    rel_res = await db.execute(rel_stmt)
    rel_ids = rel_res.scalars().all()
    
    if not rel_ids:
        return {"code": 200, "message": "没有发现可清理的客户数据。"}
        
    # 3. 删除中间表中的关联
    del_stmt = (
        delete(scp_profile_tags)
        .where(scp_profile_tags.c.profile_tag_id == tag_id)
        .where(scp_profile_tags.c.sales_customer_profile_id.in_(rel_ids))
    )
    
    try:
        await db.execute(del_stmt)
        await db.commit()
    except Exception as e:
        return {"code": 500, "message": f"清空失败: {str(e)}"}
        
    return {"code": 200, "message": "本周导入名单已清空。"}
