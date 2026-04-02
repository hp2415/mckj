from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_

from database import get_db
from models import Product
from api.auth import get_current_user
from models import User

router = APIRouter(prefix="/api/product", tags=["Products"])

@router.post("/trigger_sync")
async def manual_trigger_sync(background_tasks: BackgroundTasks, current_user: User = Depends(get_current_user)):
    """
    【人工干预干涉权】
    如果你发现供应商上了新商品等不及晚上3点，直接调用这个接口！
    它通过并发后台任务池（BackgroundTasks）即刻下发洗数命令，完全不阻塞你的当前操作。
    """
    from core.tasks import fetch_and_sync_832_products
    # 把它扔到 FastAPI 的后台异步执行队列中去，接口立刻响应
    background_tasks.add_task(fetch_and_sync_832_products)
    return {"code": 200, "message": "系统收到！全量数据更新指令已强行抛给调度器，请视店铺大小耐心等待后台完工！"}

@router.get("/search")
async def search_local_products(
    keyword: str = "", 
    skip: int = 0, 
    limit: int = 20, 
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    【桌面端高频入口】由于后端已建立异步定时调度器在后台洗库，
    这个搜索接口直接挂钩本地数据库实现毫秒级拉取，规避了慢网卡死和风控验证码。
    """
    query = select(Product)
    
    if keyword:
        search_pattern = f"%{keyword}%"
        query = query.where(
            or_(
                Product.product_name.ilike(search_pattern),
                Product.product_id.ilike(search_pattern)
            )
        )
    
    # 按照价格或时间倒序均可
    query = query.order_by(Product.id.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    products = result.scalars().all()
    
    return [
        {
            "id": p.id,
            "product_id": p.product_id,
            "product_name": p.product_name,
            "price": float(p.price) if p.price else 0.0,
            "cover_img": p.cover_img,
            "product_url": p.product_url,
            "unit": p.unit,
            "supplier_name": p.supplier_name
        }
        for p in products
    ]

import httpx
from fastapi import HTTPException

@router.post("/debug_832_raw")
async def debug_832_raw_response(supplier_id: str = "1090698369754404144", page: int = 1):
    """
    【开发调试专用】
    你可以用 Apifox 直接调用这个接口，它会原封不动地返回 832 平台的最原始数据结构！
    方便你查看每个字段到底叫什么名字（如 retData, results, 等）
    """
    url = "https://ys.fupin832.com/frontweb/search/searchProduct"
    headers = {
        "origin": "https://ys.fupin832.com",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    payload = {
        "nowPage": page,
        "pageShow": 10,
        "sortType": "DESC",
        "supplierId": supplier_id,
        "sortName": "",
        "shopCategoryId": ""
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
