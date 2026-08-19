from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import timedelta
from jose import JWTError, jwt
import uuid

from database import get_db
from models import User, UserSalesWechat
from core.auth_throttle import (
    clear_login_failures,
    login_guard,
    record_login_failure,
    register_guard,
)
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
async def register(
    body: schemas.RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    桌面端自助注册：创建员工账号并绑定至少一个业务微信标识（现改为 alias_name）。
    """
    register_guard(request)
    exists = await db.execute(select(User).where(User.username == body.username))
    if exists.scalars().first():
        raise HTTPException(status_code=400, detail="用户名已存在")

    # 注册时同样支持 alias_name 输入：统一解析成 sales_wechat_id（wxid_...）再落库
    from api.me_bindings import resolve_sales_wechat_id_from_input

    resolved: list[str] = []
    for raw in body.sales_wechat_ids:
        sw = await resolve_sales_wechat_id_from_input(db, raw)
        sw = (sw or "").strip()
        if not sw:
            raise HTTPException(status_code=400, detail=f"无法解析该别名对应的销售微信号: {raw}")
        # 若输入不是 wxid_ 且未解析到别名映射，则认为无效（避免把昵称/随手输内容当成 wxid 落库）
        if not str(raw).strip().startswith("wxid_") and sw == str(raw).strip():
            raise HTTPException(status_code=400, detail=f"无法解析该别名对应的销售微信号: {raw}")
        if sw not in resolved:
            resolved.append(sw)

    for sw in resolved:
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

    for i, sw in enumerate(resolved):
        db.add(
            UserSalesWechat(
                user_id=user.id,
                sales_wechat_id=sw,
                label=None,
                is_primary=(i == 0),
            )
        )

    user.wechat_id = resolved[0]
    await db.commit()

    return {"code": 200, "message": "注册成功", "data": {"user_id": user.id}}

@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    """
    桌面端登录专用接口
    接收表单类型的 username 和 password，返回带角色的 JWT 令牌
    """
    login_guard(form_data.username)
    result = await db.execute(select(User).where(User.username == form_data.username))
    user = result.scalars().first()
    
    if not user or not verify_password(form_data.password, user.password_hash):
        record_login_failure(form_data.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码不正确",
            headers={"WWW-Authenticate": "Bearer"},
        )

    clear_login_failures(form_data.username)
    if not user.is_active:
        raise HTTPException(status_code=400, detail="该账号已被禁用。")

    # 生成唯一的 JTI (JWT ID) 用于单端登录校验
    jti = uuid.uuid4().hex

    # 员工：单端登录。管理员：多端；改密/停用时写入的作废 jti 在此清空。
    if user.role != "admin":
        user.active_token_jti = jti
    else:
        user.active_token_jti = None
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
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="该账号已被禁用。",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 员工单端登录；管理员在改密/停用后 active_token_jti 会被写成作废值，此处一并拒绝旧令牌
    if user.active_token_jti:
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
