from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from database import engine

from api import auth, product, customer, system, prompt_admin, me_bindings
from api.wechat_outbound import router as wechat_outbound_router
from sqladmin import Admin
from core.admin_auth import admin_auth
from admin_views import admin_views
from core.tasks import start_scheduler
from fastapi.middleware.cors import CORSMiddleware
import os
import asyncio

# 自动从项目根目录读取 .env（用于桌面端更新发布信息等）
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None

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


# 路径基准：不要依赖 uvicorn 启动工作目录（--reload 时更容易混乱）
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))

# 优先读取 backend/.env（更符合线上单独部署 backend 的形态）
if load_dotenv:
    load_dotenv(dotenv_path=os.path.join(_BACKEND_DIR, ".env"), override=False)

# 可选：通过环境变量覆盖静态目录位置（线上更灵活）
_MEDIA_DIR = os.getenv("MEDIA_DIR") or os.path.join(_BACKEND_DIR, "media")
_DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR") or os.path.join(_BACKEND_DIR, "downloads")

# 创建并挂载公共静态图片目录，桌面端可以直接通过 /media/* 获取图片
os.makedirs(os.path.join(_MEDIA_DIR, "products"), exist_ok=True)
app.mount("/media", StaticFiles(directory=_MEDIA_DIR), name="media")

# 桌面端安装包下载目录（自动更新用）
os.makedirs(_DOWNLOADS_DIR, exist_ok=True)
app.mount("/downloads", StaticFiles(directory=_DOWNLOADS_DIR), name="downloads")

@app.on_event("startup")
async def on_startup():
    start_scheduler()
    from ai.doc_loader import load_all_docs
    load_all_docs()
    # 首次启动自动把"写死的提示词/话术文档"迁入 DB（幂等 upsert，不覆盖已存在的版本）
    from ai.prompt_seed import seed_prompts_if_needed
    await seed_prompts_if_needed()
    # AI 画像 worker（DB 队列）：通过环境变量开关启用，多进程可并行消费
    # PROFILE_WORKER_ENABLED=1
    # PROFILE_WORKER_CONCURRENCY=4
    try:
        v = str(os.getenv("PROFILE_WORKER_ENABLED") or "").strip()
        enabled = v not in ("", "0", "false", "False", "off", "OFF")
        if enabled:
            from ai.profile_queue import run_worker_loop

            conc = int(os.getenv("PROFILE_WORKER_CONCURRENCY") or "4")
            asyncio.create_task(run_worker_loop(concurrency=conc))
    except Exception:
        # 启动失败不阻塞主进程（可由独立 worker 进程运行）
        pass

# 挂载业务路由
app.include_router(auth.router)
app.include_router(me_bindings.router)
app.include_router(product.router)
app.include_router(customer.router)
app.include_router(system.router)
from api.ai_chat import router as ai_router
app.include_router(ai_router)
app.include_router(prompt_admin.router)
app.include_router(wechat_outbound_router)

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
