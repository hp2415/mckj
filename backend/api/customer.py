from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
import schemas
import crud
from database import get_db
from api.auth import get_current_user
from models import User

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
    relation = await crud.update_user_customer_relation(
        db, 
        username=current_user.username, 
        customer_phone=customer_phone, 
        update_data=update_data
    )
    if not relation:
        return {"code": 404, "message": "关联关系不存在"}
    return {"code": 200, "message": "更新成功"}

@router.put("/id/{customer_id}/info")
async def update_customer_info_by_id(
    customer_id: int,
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
    customer_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    拉取某个客户的所有历史订单明细，在桌面端以弹窗下钻展示
    """
    from sqlalchemy.future import select
    from models import Customer, RawOrder, RawOrderItem
    
    # 1. Get customer phone
    stmt_c = select(Customer.phone).where(Customer.id == customer_id)
    res_c = await db.execute(stmt_c)
    phone = res_c.scalar_one_or_none()
    
    if not phone:
        return []
    
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
from models import WechatHistory, UserCustomerRelation

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
    
    # 缓存匹配查询：为了提速，先获取当前该销售的所有关系
    rel_stmt = select(UserCustomerRelation).where(UserCustomerRelation.user_id == current_user.id)
    rel_res = await db.execute(rel_stmt)
    relations = rel_res.scalars().all()
    
    # 建立映射: wechat_remark -> customer_id
    remark_to_customer = {r.wechat_remark.strip(): r.customer_id for r in relations if r.wechat_remark and r.customer_id}
    
    success_count = 0
    fail_count = 0
    
    new_histories = []
    
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
        target_customer_id = remark_to_customer.get(customer_remark)
        if not target_customer_id:
            fail_count += 1
            continue
            
        history = WechatHistory(
            user_id=current_user.id,
            customer_id=target_customer_id,
            sender_name=sender,
            chat_time=chat_time,
            content=chat_content
        )
        new_histories.append(history)
        success_count += 1
        
    if new_histories:
        db.add_all(new_histories)
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
    from models import Customer
    stmt = select(Customer).where(Customer.phone == phone)
    res = await db.execute(stmt)
    customer = res.scalars().first()
    if not customer:
        return {"code": 404, "message": "客户不存在"}
        
    history = await crud.get_chat_history(db, current_user.id, customer.id, limit=limit, skip=skip)
    return {"code": 200, "data": history}

@router.post("/{phone}/chat_message")
async def save_customer_chat_message(
    phone: str,
    msg_in: schemas.ChatMessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """保存单条对话记录 (用户手动备份模式)"""
    from models import Customer
    stmt = select(Customer).where(Customer.phone == phone)
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
