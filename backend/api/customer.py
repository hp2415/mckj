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
    当桌面端检测到正在与某位客户沟通时，同步基本信息。
    """
    result = await crud.sync_customer_info(db, user_id=current_user.id, schema=customer_data)
    return result

@router.get("/my")
async def get_my_customers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    获取当前登录员工负责的客户列表。
    """
    customers = await crud.get_user_customers(db, username=current_user.username)
    return {"code": 200, "message": "获取成功", "data": customers}

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
