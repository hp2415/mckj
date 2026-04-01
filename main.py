from fastapi import FastAPI
from database import engine

from api import auth, product, customer
from sqladmin import Admin
from core.admin_auth import admin_auth
from admin_views import admin_views
import os

app = FastAPI(title="微企AI助手核心服务")

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
    return {"message": " FastAPI 启动成功！请访问 /docs 查看API文档，或访问 /admin 进入管理后台！"}