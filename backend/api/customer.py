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
    【桌面端核心接口】
    当桌面端检测到正在与某位客户沟通（或录入客户）时，发送整合数据。
    后端将：
    1. 根据手机号创建 / 更新 `Customer` （客观存在）
    2. 创建 / 更新 当前登录员工与该客户的 `UserCustomerRelation`（预算、画像等主观属性）
    """
    result = await crud.sync_customer_info(db, user_id=current_user.id, schema=customer_data)
    return result
