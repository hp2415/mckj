from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from database import get_db
from api.auth import get_current_user
from models import User, SystemConfig
from core.tasks import fetch_and_sync_832_products
from ai.chat_models_catalog import chat_models_for_api_payload

router = APIRouter(prefix="/api/system", tags=["System"])

@router.get("/config/ai")
async def get_ai_config(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    动态获取 AI 对话配置 (Dify URL / Key)。
    """
    stmt = select(SystemConfig).where(SystemConfig.config_group == "ai")
    result = await db.execute(stmt)
    configs = result.scalars().all()
    config_map = {c.config_key: c.config_value for c in configs}
    
    return {
        "code": 200,
        "data": {
            "api_url": config_map.get("dify_api_url", "https://api.dify.ai/v1"),
            "api_key": config_map.get("dify_api_key", "")
        }
    }

@router.get("/sync/status")
async def get_sync_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    查询云端货源同步新鲜度。
    """
    stmt = select(SystemConfig).where(SystemConfig.config_group == "sync")
    result = await db.execute(stmt)
    configs = result.scalars().all()
    
    config_map = {c.config_key: c.config_value for c in configs}
    max_updated = max([c.updated_at for c in configs]) if configs else None
    
    failed_suppliers_str = config_map.get("sync_failed_suppliers", "")
    failed_suppliers = [s.strip() for s in failed_suppliers_str.split(",") if s.strip()]
    
    return {
        "status": config_map.get("sync_status", "idle"),
        "last_success": config_map.get("sync_last_success", "从未同步"),
        "message": config_map.get("sync_last_message", "就绪"),
        "failed_count": len(failed_suppliers),
        "failed_suppliers": failed_suppliers,
        "last_updated_at": max_updated.strftime("%Y-%m-%d %H:%M:%S") if max_updated else "无记录"
    }

@router.post("/sync/trigger")
async def trigger_sync(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user)
):
    """
    手动触发全量货源同步 (异步执行)。
    """
    if current_user.role != "admin":
        return {"code": 403, "message": "权限不足"}
        
    background_tasks.add_task(fetch_and_sync_832_products)
    return {"code": 200, "message": "全量同步任务已拉起，请稍后查看状态"}

@router.post("/sync/supplier/{supplier_id}")
async def trigger_sync_supplier(
    supplier_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user)
):
    """
    手动修复/同步单个特定供货商 (异步执行)。
    """
    if current_user.role != "admin":
        return {"code": 403, "message": "权限不足"}
        
    background_tasks.add_task(fetch_and_sync_832_products, supplier_id)
    return {"code": 200, "message": f"供货商 {supplier_id} 的同步任务已拉起"}

@router.get("/configs_dict")
async def get_configs_dict(db: AsyncSession = Depends(get_db)):
    """
    拉取系统级的配置字典选项列表，用于给客户端渲染多级菜单。
    """
    keys = ["unit_type_choices", "admin_division_choices", "purchase_type_choices", "llm_chat_models_list"]
    stmt = select(SystemConfig).where(SystemConfig.config_key.in_(keys))
    res = await db.execute(stmt)
    configs = res.scalars().all()

    raw_map = {c.config_key: c.config_value for c in configs}
    config_map = {
        k: [x.strip() for x in raw_map[k].split(",") if x.strip()]
        for k in ("unit_type_choices", "admin_division_choices", "purchase_type_choices")
        if k in raw_map
    }

    llm_chat_models = chat_models_for_api_payload(raw_map)

    # 填充一些默认的 fallback 配置以防数据库没来及配置
    return {
        "code": 200,
        "data": {
            "unit_type_choices": config_map.get("unit_type_choices", ["学校", "医院", "消防", "街道办", "银行", "税务局", "其他"]),
            "admin_division_choices": config_map.get("admin_division_choices", ["越秀区", "天河区", "海珠区", "荔湾区", "其他"]),
            "purchase_type_choices": config_map.get("purchase_type_choices", ["食堂采购", "工会采购", "食堂+工会采购", "其他"]),
            "llm_chat_models": llm_chat_models,
        }
    }
