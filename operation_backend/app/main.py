"""运营后台 FastAPI 入口。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, dashboard, org
from app.config import CORS_ALLOW_ORIGINS, ENABLE_API_DOCS, WEB_DIST

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


@app.get("/api/op/health")
async def health():
    return {"code": 200, "message": "ok", "data": {"service": "operation_backend"}}


# 生产：挂载 Vue dist
if WEB_DIST.is_dir():
    assets = WEB_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # 不拦截 API
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        index = WEB_DIST / "index.html"
        candidate = WEB_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="前端未构建")
