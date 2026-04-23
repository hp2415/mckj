from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from database import engine

from api import auth, product, customer, system
from sqladmin import Admin
from core.admin_auth import admin_auth
from admin_views import admin_views
from core.tasks import start_scheduler
from fastapi.middleware.cors import CORSMiddleware
import os

app = FastAPI(title="微企AI助手核心服务")

# 增强：配置 CORS 中间件，允许未来网页前端跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境建议替换为具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    # 针对 /api/auth/login 的 OAuth2 表单认证要求特殊处理
    if request.url.path == "/api/auth/login":
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers
        )
    # 其他 API 统一规范为 {code, message, data}
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": exc.detail, "data": None},
        headers=exc.headers
    )


# 创建并挂载公共静态图片目录，桌面端可以直接通过 /media/* 获取图片
os.makedirs("media/products", exist_ok=True)
app.mount("/media", StaticFiles(directory="media"), name="media")

@app.on_event("startup")
async def on_startup():
    start_scheduler()
    from ai.doc_loader import load_all_docs
    load_all_docs()

# 挂载业务路由
app.include_router(auth.router)
app.include_router(product.router)
app.include_router(customer.router)
app.include_router(system.router)
from api.ai import router as ai_router
app.include_router(ai_router)

# 挂载 sqladmin 管理后台
admin = Admin(
    app, 
    engine, 
    authentication_backend=admin_auth,
    title="微企AI助手管理后台",
    base_url="/admin",
    templates_dir="templates"
)

# 注册所有模型的 Admin 视图
for view in admin_views:
    admin.add_view(view)

@app.get("/")
async def root():
    return {"message": "FastAPI 启动成功！请访问 /docs 查看API文档，或访问 /admin 进入管理后台！"}
