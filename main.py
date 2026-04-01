from fastapi import FastAPI, Depends
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy import Column, Integer, String, select
from pydantic import BaseModel

# --- 1. 数据库配置 ---
DATABASE_URL = "mysql+aiomysql://root:root@localhost:3306/ai_assistant_db"

# 创建异步引擎
engine = create_async_engine(DATABASE_URL, echo=True)
# 创建异步会话工厂
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
# 声明基类
Base = declarative_base()

# --- 2. 数据库模型 (Table) ---
class TestUser(Base):
    __tablename__ = "test_users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True)
    email = Column(String(100))

# --- 3. Pydantic 模型 (用于接收和返回数据) ---
class UserCreate(BaseModel):
    username: str
    email: str

# --- 4. 初始化数据库 (自动建表) ---
app = FastAPI()

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        # 这个操作会根据上面的模型在数据库中创建表
        await conn.run_sync(Base.metadata.create_all)

# 获取数据库会话的依赖函数
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# --- 5. 接口 (API Routes) ---

@app.get("/")
async def root():
    return {"message": "FastAPI 运行正常！"}

# 接口：向数据库存入一个测试用户
@app.post("/test_db")
async def create_user(user: UserCreate, db: AsyncSession = Depends(get_db)):
    new_user = TestUser(username=user.username, email=user.email)
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return {"status": "成功存入数据库", "user_id": new_user.id}

# 接口：从数据库读取所有测试用户
@app.get("/test_db")
async def read_users(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TestUser))
    users = result.scalars().all()
    return {"users": users}