from fastapi import APIRouter, Depends, BackgroundTasks, Query
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
    supplier_name: str = "",
    cat1: str = "",
    cat2: str = "",
    cat3: str = "",
    province: str = "",
    city: str = "",
    district: str = "",
    min_price: float = None,
    max_price: float = None,
    skip: int = 0, 
    limit: int = 20, 
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    【高阶搜索】支持关键词、供应商、三级分类、产地以及价格区间过滤。
    """
    config_res = await db.execute(select(SystemConfig).where(SystemConfig.config_key == "supplier_ids"))
    config_obj = config_res.scalars().first()
    active_ids = []
    if config_obj and config_obj.config_value.strip():
        active_ids = [s.strip() for s in config_obj.config_value.split(",") if s.strip()]

    query = select(Product)
    if active_ids:
        query = query.where(Product.supplier_id.in_(active_ids))
    else:
        return {"code": 200, "data": {"items": [], "total": 0, "has_more": False}}

    if keyword:
        keywords = [k.strip() for k in keyword.replace(",", " ").replace("，", " ").split() if k.strip()]
        for kw in keywords:
            pattern = f"%{kw}%"
            query = query.where(or_(
                Product.product_name.ilike(pattern), 
                Product.product_id.ilike(pattern),
                Product.supplier_name.ilike(pattern)
            ))
    
    if supplier_name: query = query.where(Product.supplier_name.ilike(f"%{supplier_name}%"))
    if cat1: query = query.where(Product.category_name_one == cat1)
    if cat2: query = query.where(Product.category_name_two == cat2)
    if cat3: query = query.where(Product.category_name_three == cat3)
    if province: query = query.where(Product.origin_province == province)
    if city: query = query.where(Product.origin_city == city)
    if district: query = query.where(Product.origin_district == district)
    if min_price is not None: query = query.where(Product.price >= min_price)
    if max_price is not None: query = query.where(Product.price <= max_price)

    count_stmt = select(func.count()).select_from(query.subquery())
    total_res = await db.execute(count_stmt)
    total_count = total_res.scalar() or 0

    query = query.order_by(Product.id.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    products = result.scalars().all()
    
    items = [
        {
            "id": p.id,
            "product_name": p.product_name,
            "price": float(p.price) if p.price else 0.0,
            "cover_img": p.cover_img,
            "product_url": p.product_url,
            "unit": p.unit,
            "supplier_name": p.supplier_name,
            "cat1": p.category_name_one,
            "cat2": p.category_name_two,
            "cat3": p.category_name_three,
            "province": p.origin_province,
            "city": p.origin_city,
            "district": p.origin_district
        }
        for p in products
    ]

    return {
        "code": 200,
        "data": {
            "items": items,
            "total": total_count,
            "skip": skip,
            "limit": limit,
            "has_more": total_count > skip + limit
        }
    }

@router.get("/metadata")
async def get_product_metadata(
    supplier_name: str = Query(None),
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """获取筛选元数据，支持按店铺联动过滤选项"""
    config_res = await db.execute(select(SystemConfig).where(SystemConfig.config_key == "supplier_ids"))
    config_obj = config_res.scalars().first()
    active_ids = [s.strip() for s in config_obj.config_value.split(",") if s.strip()] if config_obj else []

    suppliers_res = await db.execute(select(Product.supplier_name).where(Product.supplier_id.in_(active_ids)).distinct())
    suppliers = [s for s in suppliers_res.scalars().all() if s]

    def build_tree(rows):
        tree = {}
        for r1, r2, r3 in rows:
            if not r1: continue
            if r1 not in tree: tree[r1] = {}
            if not r2: continue
            if r2 not in tree[r1]: tree[r1][r2] = set()
            if r3: tree[r1][r2].add(r3)
        res = []
        for v1, v2_map in tree.items():
            children1 = []
            for v2, v3_set in v2_map.items():
                children1.append({"value": v2, "label": v2, "children": [{"value": v3, "label": v3} for v3 in sorted(list(v3_set))]})
            res.append({"value": v1, "label": v1, "children": sorted(children1, key=lambda x: x["value"])})
        return sorted(res, key=lambda x: x["value"])

    # 种类提取
    cat_stmt = select(Product.category_name_one, Product.category_name_two, Product.category_name_three).distinct()
    if supplier_name:
        cat_stmt = cat_stmt.where(Product.supplier_name == supplier_name)
    cat_rows = (await db.execute(cat_stmt)).all()

    # 产地提取
    org_stmt = select(Product.origin_province, Product.origin_city, Product.origin_district).distinct()
    if supplier_name:
        org_stmt = org_stmt.where(Product.supplier_name == supplier_name)
    origin_rows = (await db.execute(org_stmt)).all()

    return {
        "code": 200,
        "data": {
            "suppliers": sorted(suppliers),
            "categories": build_tree(cat_rows),
            "origins": build_tree(origin_rows)
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
