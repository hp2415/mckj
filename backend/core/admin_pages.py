"""管理后台自定义页面：统一 SQLAdmin 壳层渲染。"""
from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import Response

_admin_instance: Any = None

# 侧栏内无刷新加载的自定义工具页（非 CRUD list/edit）
ADMIN_PANEL_PATHS = frozenset(
    {
        "/admin/dashboard",
        "/admin/profiling-progress",
        "/admin/task-allocation",
        "/admin/profile-nightly",
        "/admin/sales-wechat-accounts/import-xlsx",
        "/admin/raw-customer-wechat-sync",
        "/admin/raw-chat-wechat-sync",
        "/admin/voice-transcribe-console",
    }
)


def register_admin(admin: Any) -> None:
    global _admin_instance
    _admin_instance = admin


def is_admin_panel_path(path: str) -> bool:
    p = (path or "").split("?")[0].rstrip("/") or "/"
    if p in ADMIN_PANEL_PATHS:
        return True
    return any(p.startswith(base + "/") for base in ADMIN_PANEL_PATHS if base != "/admin/dashboard")


async def render_admin_page(
    request: Request,
    template_name: str,
    *,
    title: str,
    subtitle: str = "",
    status_code: int = 200,
    **context: Any,
) -> Response:
    """使用 sqladmin 同款 Jinja 引擎渲染（含 url_for、sqladmin 模板路径）。"""
    if _admin_instance is None:
        raise RuntimeError("call register_admin(admin) from main.py before rendering admin pages")
    ctx = {
        "request": request,
        "admin": _admin_instance,
        "title": title,
        "subtitle": subtitle,
        **context,
    }
    return await _admin_instance.templates.TemplateResponse(
        request,
        template_name,
        ctx,
        status_code=status_code,
    )
