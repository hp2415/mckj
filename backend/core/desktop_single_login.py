"""桌面端员工单端登录开关（system_configs / 环境变量）。

默认开启：同一员工账号新登录会作废旧令牌（避免多地同时操作出错）。
关闭后允许多地同时在线；改密 / 停用写入的作废 jti 仍会拒绝旧令牌。
"""

from __future__ import annotations

import os
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.system_config_store import upsert_system_config_row
from models import SystemConfig

DESKTOP_SINGLE_LOGIN_KEY = "desktop_single_login"
DESKTOP_SINGLE_LOGIN_GROUP = "desktop"
DESKTOP_SINGLE_LOGIN_DESC = (
    "桌面端：是否限制员工账号单端登录（true=开启，异地登录踢旧会话；"
    "false=关闭，允许多地同时在线）。改密/停用仍作废旧令牌。默认 true。"
)

_FLAG_TTL_SEC = 15.0
_FLAG_CACHE_AT: float = 0.0
_FLAG_CACHE_VAL: bool | None = None


def _parse_enabled_flag(raw: Any, *, default: bool = True) -> bool:
    v = str(raw or "").strip().lower()
    if not v:
        return default
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _env_desktop_single_login_enabled() -> bool:
    return _parse_enabled_flag(os.getenv("DESKTOP_SINGLE_LOGIN"), default=True)


def invalidate_desktop_single_login_cache() -> None:
    global _FLAG_CACHE_AT, _FLAG_CACHE_VAL
    _FLAG_CACHE_AT = 0.0
    _FLAG_CACHE_VAL = None


def is_desktop_single_login_enabled() -> bool:
    """缓存命中则用缓存，否则回退环境变量（默认开）。"""
    now = time.monotonic()
    if _FLAG_CACHE_VAL is not None and (now - _FLAG_CACHE_AT) < _FLAG_TTL_SEC:
        return bool(_FLAG_CACHE_VAL)
    return _env_desktop_single_login_enabled()


async def resolve_desktop_single_login_enabled(
    db: AsyncSession,
    *,
    force_refresh: bool = False,
) -> bool:
    """SystemConfig 优先；无配置则回退环境变量。默认开启。"""
    global _FLAG_CACHE_AT, _FLAG_CACHE_VAL

    now = time.monotonic()
    if (
        not force_refresh
        and _FLAG_CACHE_VAL is not None
        and (now - _FLAG_CACHE_AT) < _FLAG_TTL_SEC
    ):
        return bool(_FLAG_CACHE_VAL)

    res = await db.execute(
        select(SystemConfig.config_value).where(
            SystemConfig.config_key == DESKTOP_SINGLE_LOGIN_KEY
        )
    )
    row = res.first()
    if row is not None and str(row[0] or "").strip() != "":
        enabled = _parse_enabled_flag(row[0], default=True)
    else:
        enabled = _env_desktop_single_login_enabled()
    _FLAG_CACHE_VAL = enabled
    _FLAG_CACHE_AT = time.monotonic()
    return enabled


async def set_desktop_single_login_enabled(db: AsyncSession, enabled: bool) -> bool:
    """写入开关并立刻刷新缓存。调用方负责 commit。"""
    flag = bool(enabled)
    await upsert_system_config_row(
        db,
        config_key=DESKTOP_SINGLE_LOGIN_KEY,
        config_value="true" if flag else "false",
        config_group=DESKTOP_SINGLE_LOGIN_GROUP,
        description=DESKTOP_SINGLE_LOGIN_DESC,
        update_description=True,
    )
    invalidate_desktop_single_login_cache()
    _remember_flag(flag)
    return flag


def _remember_flag(enabled: bool) -> None:
    global _FLAG_CACHE_AT, _FLAG_CACHE_VAL
    _FLAG_CACHE_VAL = bool(enabled)
    _FLAG_CACHE_AT = time.monotonic()
