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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取该客户与当前用户的 AI 聊天历史"""
    stmt = select(RawCustomer).where(or_(RawCustomer.phone == phone, RawCustomer.phone_normalized == phone))
    res = await db.execute(stmt)
    customer = res.scalars().first()
    if not customer:
        return {"code": 404, "message": "客户不存在"}
        
    history = await crud.get_chat_history(db, current_user.id, customer.id, limit=limit, skip=skip)
    return {"code": 200, "data": history}


@router.get("/id/{raw_customer_id}/chat_history", response_model=schemas.ChatHistoryResponse)
async def get_customer_chat_history_by_id(
    raw_customer_id: str,
    limit: int = 20,
    skip: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """按 raw_customer_id 获取该客户与当前用户的 AI 聊天历史（不依赖手机号）。"""
    history = await crud.get_chat_history(
        db, current_user.id, raw_customer_id, limit=limit, skip=skip
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
