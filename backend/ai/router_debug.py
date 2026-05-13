"""场景路由 / Prompt 装配测试期详细日志（由 system_configs.ai_router_debug_log 开关）。"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from core.logger import logger
from models import SystemConfig

_CONFIG_KEY = "ai_router_debug_log"
_TEXT_PREVIEW = 600
_CTX_PREVIEW = 400


async def router_debug_enabled(db: AsyncSession) -> bool:
    try:
        stmt = select(SystemConfig).where(SystemConfig.config_key == _CONFIG_KEY)
        res = await db.execute(stmt)
        cfg = res.scalars().first()
        if not cfg:
            return False
        v = str(cfg.config_value or "").strip().lower()
        return v in ("1", "true", "on", "yes")
    except Exception:
        return False


def _preview_text(text: str, limit: int = _TEXT_PREVIEW) -> str:
    raw = (text or "").strip()
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "…"


def _json_preview(data: Any, limit: int = 4000) -> str:
    try:
        raw = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        raw = str(data)
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "…"


def log_route_context(*, query: str, ui_category: str, hint: str, route_context: Optional[dict]) -> None:
    logger.info(
        "RouterDebug[context] query={} ui_category={} hint={} route_context={}",
        _preview_text(query, 200),
        ui_category,
        hint or "(none)",
        _json_preview(route_context or {}),
    )


def log_route_candidates(
    *,
    candidates: list[dict],
    filtered_out: list[dict],
) -> None:
    logger.info(
        "RouterDebug[candidates] eligible={} filtered_out={}",
        _json_preview(candidates),
        _json_preview(filtered_out),
    )


def log_route_decision(*, decision: dict) -> None:
    logger.info("RouterDebug[decision] {}", _json_preview(decision))


def log_prompt_resolution(
    *,
    primary_key: str,
    auxiliary_keys: list[str],
    meta: dict,
    system_text: str,
    messages: list[dict],
) -> None:
    system_len = len(system_text or "")
    logger.info(
        "RouterDebug[prompt] primary={} auxiliary={} meta={} system_len={}",
        primary_key,
        auxiliary_keys or [],
        _json_preview(meta),
        system_len,
    )
    logger.info("RouterDebug[prompt.system] {}", _preview_text(system_text))
    if system_len < 80:
        logger.warning(
            "RouterDebug[prompt.system] system 过短（{} 字符），请检查提示词版本是否已发布且 System 模板非空",
            system_len,
        )
    frames: list[dict] = []
    for msg in messages or []:
        role = str(msg.get("role") or "")
        content = str(msg.get("content") or "")
        frames.append({
            "role": role,
            "len": len(content),
            "preview": _preview_text(content, _CTX_PREVIEW),
        })
    logger.info("RouterDebug[prompt.messages] {}", _json_preview(frames))
