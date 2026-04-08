from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_, func

from database import get_db
from models import Product, User, SystemConfig
from api.auth import get_current_user, get_admin_user

router = APIRouter(prefix="/api/product", tags=["Products"])

@router.post("/trigger_sync")
async def manual_trigger_sync(background_tasks: BackgroundTasks, current_user: User = Depends(get_admin_user)):
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
    # 0. 动态读取当前配置中激活的供应商 ID
    # 增加更严谨的清洗：过滤掉多余的空格、回车符，确保 IN 子句的物理匹配性能
    config_res = await db.execute(select(SystemConfig).where(SystemConfig.config_key == "supplier_ids"))
    config_obj = config_res.scalars().first()
    active_ids = []
    if config_obj and config_obj.config_value.strip():
        # 这里进行二次洗涤，兼容各种非标准输入
        active_ids = [s.strip() for s in config_obj.config_value.replace("\r", "").replace("\n", "").split(",") if s.strip()]

    # 1. 先计算符合条件的总数（用于前端判断是否还有更多数据）
    count_query = select(func.count()).select_from(Product)
    
    # 增加供应商过滤：只显示配置中存在的供应商商品
    if active_ids:
        count_query = count_query.where(Product.supplier_id.in_(active_ids))
    else:
        # 如果配置为空，则不返回任何商品防止推送错误
        return {
            "code": 200, 
            "message": "系统未配置供应商ID", 
            "data": {"items": [], "total": 0, "skip": skip, "limit": limit, "has_more": False}
        }

    if keyword:
        # 支持多词拆分，实现渐进式过滤 (AND 逻辑)
        # 支持空格或半角/全角逗号拆分
        keywords = [k.strip() for k in keyword.replace(",", " ").replace("，", " ").split() if k.strip()]
        for kw in keywords:
            pattern = f"%{kw}%"
            count_query = count_query.where(
                or_(
                    Product.product_name.ilike(pattern),
                    Product.product_id.ilike(pattern),
                    Product.supplier_name.ilike(pattern)
                )
            )
    total_res = await db.execute(count_query)
    total_count = total_res.scalar() or 0

    # 2. 执行分页查询
    query = select(Product).where(Product.supplier_id.in_(active_ids))
    if keyword:
        keywords = [k.strip() for k in keyword.replace(",", " ").replace("，", " ").split() if k.strip()]
        for kw in keywords:
            pattern = f"%{kw}%"
            query = query.where(
                or_(
                    Product.product_name.ilike(pattern),
                    Product.product_id.ilike(pattern),
                    Product.supplier_name.ilike(pattern)
                )
            )
    query = query.order_by(Product.id.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    products = result.scalars().all()
    
    items = [
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

    return {
        "code": 200,
        "message": "success",
        "data": {
            "items": items,
            "total": total_count,
            "skip": skip,
            "limit": limit,
            "has_more": total_count > skip + limit
        }
    }

import httpx
from fastapi import HTTPException

@router.post("/debug_832_raw")
async def debug_832_raw_response(supplier_id: str = "1090698369754404144", page: int = 1):
    """
    【开发调试专用】
    可以用 Apifox 直接调用这个接口，它会原封不动地返回 832 平台的最原始数据结构！
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
