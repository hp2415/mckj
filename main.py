from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy import Column, Integer, String, select
from pydantic import BaseModel
import httpx

# --- 1. 数据库配置 ---
DATABASE_URL = "mysql+aiomysql://root:root@localhost:3306/ai_assistant_db"

engine = create_async_engine(DATABASE_URL, echo=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

# --- 2. 数据库模型 ---
class TestUser(Base):
    __tablename__ = "test_users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True)
    email = Column(String(100))

class UserCreate(BaseModel):
    username: str
    email: str

# --- 3. 辅助函数：解析 832 平台返回的数据 ---
def parse_832_data(raw_json):
    """
    在这里提取你真正需要的字段，防止返回太多冗余数据
    """
    if "data" in raw_json and "list" in raw_json["data"]:
        products = raw_json["data"]["list"]
        result = []
        for p in products:
            result.append({
                "productId": p.get("productId"),
                "productName": p.get("productName"),
                "price": p.get("price"),
                "imagePath": p.get("imagePath"),
                "supplierName": p.get("supplierName")
            })
        return result
    return raw_json # 如果结构不对，返回原始数据

# --- 4. 初始化 ---
app = FastAPI()

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# --- 5. 接口 ---

@app.get("/")
async def root():
    return {"message": "FastAPI 运行正常！"}

@app.post("/test_db")
async def create_user(user: UserCreate, db: AsyncSession = Depends(get_db)):
    new_user = TestUser(username=user.username, email=user.email)
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return {"status": "成功", "user_id": new_user.id}

# 模拟请求 832 平台的逻辑 (修复版)
@app.post("/search_832_products")
async def search_832_products(supplier_id: str, page: int = 1):
    url = "https://ys.fupin832.com/frontweb/search/searchProduct"
    
    # 对齐你提供的 cURL 中的 Headers
    headers = {
        "origin": "https://ys.fupin832.com",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    payload = {
        "nowPage": page,
        "pageShow": 20,
        "sortType": "DESC",
        "supplierId": supplier_id,
        "sortName": "",
        "shopCategoryId": ""
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            # 检查 HTTP 状态码
            response.raise_for_status() 
            
            # 调用解析函数
            return parse_832_data(response.json())
            
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"外部接口返回错误: {e}")
    except Exception as e:
        # 在这里捕获代码中的 NameError 或其他错误
        raise HTTPException(status_code=500, detail=f"内部逻辑错误: {str(e)}")