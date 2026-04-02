from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from database import get_db
from api.auth import get_current_user
from models import User, SystemConfig
from core.tasks import fetch_and_sync_832_products # 导入同步逻辑

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
    
    # 逻辑修正：如果还没跑过任务，数据库可能没这些键，此时要给个稳健的默认返回
    config_map = {c.config_key: c.config_value for c in configs}
    # 从已有记录中挑出最新的物理更新时间
    max_updated = max([c.updated_at for c in configs]) if configs else None
    
    return {
        "status": config_map.get("sync_status", "idle"),
        "last_success": config_map.get("sync_last_success", "从未同步"),
        "message": config_map.get("sync_last_message", "就绪"),
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
    # 鉴权限管理员或特定角色
    if current_user.role != "admin":
        return {"code": 403, "msg": "权限不足"}
        
    background_tasks.add_task(fetch_and_sync_832_products)
    return {"code": 200, "msg": "同步任务已拉起，请稍后查看状态"}
