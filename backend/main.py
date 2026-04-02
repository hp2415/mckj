from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from database import engine

from api import auth, product, customer
from sqladmin import Admin
from core.admin_auth import admin_auth
from admin_views import admin_views
from core.tasks import start_scheduler
import os

app = FastAPI(title="微企AI助手核心服务")

# 创建并挂载公共静态图片目录，桌面端可以直接通过 /media/* 获取图片
os.makedirs("media/products", exist_ok=True)
app.mount("/media", StaticFiles(directory="media"), name="media")

@app.on_event("startup")
async def on_startup():
    start_scheduler()

# 挂载业务路由
app.include_router(auth.router)
app.include_router(product.router)
app.include_router(customer.router)

# 挂载 sqladmin 管理后台
admin = Admin(
    app, 
    engine, 
    authentication_backend=admin_auth,
    title="微企AI助手管理后台",
    base_url="/admin"
)

# 注册所有模型的 Admin 视图
for view in admin_views:
    admin.add_view(view)

@app.get("/")
async def root():
    return {"message": "FastAPI 启动成功！请访问 /docs 查看API文档，或访问 /admin 进入管理后台！"}
