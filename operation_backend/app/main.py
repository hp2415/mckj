"""运营后台 FastAPI 入口。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import auth, campaigns, dashboard, org
from app.config import BACKEND_BASE_URL, CORS_ALLOW_ORIGINS, ENABLE_API_DOCS, MEDIA_DIR, WEB_DIST
from app.core.backend_media import proxy_backend_media
from app.core.campaign_media import CampaignMediaError
from app.core.permissions import ensure_dept_role_perms_table, seed_dept_role_perms_from_kinds
from app.database import AsyncSessionLocal

app = FastAPI(
    title="米宝运营后台",
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
)

if CORS_ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOW_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.on_event("startup")
async def _startup_dept_perms() -> None:
    async with AsyncSessionLocal() as db:
        try:
            await ensure_dept_role_perms_table(db)
            await seed_dept_role_perms_from_kinds(db)
        except Exception as exc:
            # 启动不因种子失败阻断服务；日志留给控制台
            print(f"[op] dept role perms init skipped: {exc}")


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": exc.detail, "data": None},
        headers=exc.headers,
    )


app.include_router(auth.router)
app.include_router(org.router)
app.include_router(dashboard.router)
app.include_router(campaigns.router)


@app.get("/api/op/health")
async def health():
    return {"code": 200, "message": "ok", "data": {"service": "operation_backend"}}


# 海报：有 BACKEND_BASE_URL 时同源代理到核心 backend；否则读本地 media。
# 文件名含随机前缀，可长期缓存，减少刷新时重复下载。
_MEDIA_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=604800, immutable",
}


@app.api_route("/media/{full_path:path}", methods=["GET", "HEAD"])
async def media_serve(full_path: str, request: Request):
    if ".." in full_path.split("/"):
        raise HTTPException(status_code=400, detail="非法路径")

    if BACKEND_BASE_URL:
        try:
            body, ctype, status = await proxy_backend_media(full_path)
        except CampaignMediaError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if request.method == "HEAD":
            headers = {"Content-Length": str(len(body))}
            if status == 200:
                headers.update(_MEDIA_CACHE_HEADERS)
            return Response(
                content=b"",
                media_type=ctype,
                status_code=status,
                headers=headers,
            )
        headers = dict(_MEDIA_CACHE_HEADERS) if status == 200 else {}
        return Response(content=body, media_type=ctype, status_code=status, headers=headers)

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    target = (MEDIA_DIR / full_path).resolve()
    try:
        target.relative_to(MEDIA_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法路径") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(
        target,
        headers=_MEDIA_CACHE_HEADERS,
        media_type=None,
    )

# 生产：挂载 Vue dist
if WEB_DIST.is_dir():
    assets = WEB_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # 不拦截 API / 媒体
        if full_path.startswith("api/") or full_path.startswith("media/"):
            raise HTTPException(status_code=404, detail="Not Found")
        index = WEB_DIST / "index.html"
        candidate = WEB_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="前端未构建")
