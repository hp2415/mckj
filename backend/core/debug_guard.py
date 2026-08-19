"""生产默认关闭调试代理接口；需显式 ENABLE_DEBUG_ENDPOINTS=1。"""
from __future__ import annotations

import os

from fastapi import HTTPException, status


def debug_endpoints_enabled() -> bool:
    return str(os.getenv("ENABLE_DEBUG_ENDPOINTS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def require_debug_endpoints() -> None:
    if not debug_endpoints_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
