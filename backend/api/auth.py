from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import timedelta
from jose import JWTError, jwt
import uuid

from database import get_db
from models import User, UserSalesWechat
from core.security import (
    verify_password,
    create_access_token,
    get_password_hash,
    SECRET_KEY,
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_MINUTES,
)
import schemas

router = APIRouter(prefix="/api/auth", tags=["Authentication"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


@router.post("/register")
async def register(body: schemas.RegisterRequest, db: AsyncSession = Depends(get_db)):
    """
    桌面端自助注册：创建员工账号并绑定至少一个业务微信标识（现改为 alias_name）。
    """
    exists = await db.execute(select(User).where(User.username == body.username))
    if exists.scalars().first():
        raise HTTPException(status_code=400, detail="用户名已存在")

    for sw in body.sales_wechat_ids:
        taken = await db.execute(select(UserSalesWechat).where(UserSalesWechat.sales_wechat_id == sw))
        if taken.scalar_one_or_none():
            raise HTTPException(status_code=400, detail=f"销售微信号已被占用: {sw}")

    user = User(
        username=body.username,
        password_hash=get_password_hash(body.password),
        real_name=body.real_name.strip(),
        role="staff",
        is_active=True,
    )
    db.add(user)
    await db.flush()

    for i, sw in enumerate(body.sales_wechat_ids):
        db.add(
            UserSalesWechat(
                user_id=user.id,
                sales_wechat_id=sw,
                label=None,
                is_primary=(i == 0),
            )
        )

    user.wechat_id = body.sales_wechat_ids[0]
    await db.commit()

    return {"code": 200, "message": "注册成功", "data": {"user_id": user.id}}

@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    """
    桌面端登录专用接口
    接收表单类型的 username 和 password，返回带角色的 JWT 令牌
    """
    result = await db.execute(select(User).where(User.username == form_data.username))
    user = result.scalars().first()
    
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码不正确",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    if not user.is_active:
        raise HTTPException(status_code=400, detail="该账号已被禁用。")

    # 生成唯一的 JTI (JWT ID) 用于单端登录校验
    jti = uuid.uuid4().hex
    
    # [互斥逻辑] 非管理员账号，登录时刷新数据库中的 active_token_jti
    if user.role != "admin":
        user.active_token_jti = jti
        await db.commit()
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": str(user.id), "role": user.role}, 
        expires_delta=access_token_expires,
        jti=jti
    )
    return {
        "access_token": access_token, 
        "token_type": "bearer", 
        "user_id": user.id, 
        "role": user.role, 
        "real_name": user.real_name
    }

async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> User:
    """
    解析 JWT，提取当前用户的依赖方法。用于保护你的其他 API（如搜索、查客户）。
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="认证凭据无效",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id_str: str = payload.get("sub")
        jti: str = payload.get("jti")
        if user_id_str is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
        
    result = await db.execute(select(User).where(User.id == int(user_id_str)))
    user = result.scalars().first()
    if user is None:
        raise credentials_exception
        
    # [互斥校验] 如果是普通员工，需校验令牌中的 jti 是否为当前库中存储的最新标识
    if user.role != "admin":
        if not jti or user.active_token_jti != jti:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="您的账号已在其他地方登录，当前会话已失效。",
                headers={"WWW-Authenticate": "Bearer"},
            )
            
    return user

async def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    """
    【专享管理员守卫】仅限角色为 admin 的用户通过。
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="权限不足，该操作仅限系统管理员执行。"
        )
    return current_user
