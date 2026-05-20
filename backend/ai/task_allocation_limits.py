"""
任务分配数量与刷新策略：存 system_configs（JSON），管理后台总览页维护。
环境变量仅作库中无配置时的兜底，日常请在总览「任务数量与刷新策略」中调整。
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

from sqlalchemy.future import select

from models import SystemConfig

TASK_ALLOCATION_LIMITS_KEY = "task_allocation_limits_json"

DEFAULT_TASK_ALLOCATION_LIMITS: dict[str, Any] = {
    "daily_cap": 15,
    "weekly_cap": 30,
    "monthly_cap": 50,
    "icebreaker_cap": 25,
    "max_customers_main": 120,
    "icebreaker_max_candidates": 200,
    "icebreaker_enabled": True,
    # 为 true 时：每日 06:00 日任务后会重算当周/当月计划（归档同周期旧批次再生成）
    "weekly_refresh_daily": True,
    "monthly_refresh_daily": True,
}


def _env_int(key: str, default: int) -> int:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bootstrap_from_env() -> dict[str, Any]:
    """首次无库配置时，用旧环境变量填充默认值。"""
    base = deepcopy(DEFAULT_TASK_ALLOCATION_LIMITS)
    base["daily_cap"] = _env_int("TASK_DAILY_CONTACT_CAP", base["daily_cap"])
    base["weekly_cap"] = _env_int("TASK_WEEKLY_CONTACT_CAP", base["weekly_cap"])
    base["monthly_cap"] = _env_int("TASK_MONTHLY_CONTACT_CAP", base["monthly_cap"])
    base["icebreaker_cap"] = _env_int("TASK_ICEBREAKER_CAP", base["icebreaker_cap"])
    base["max_customers_main"] = _env_int("TASK_ALLOCATION_MAX_CUSTOMERS", base["max_customers_main"])
    base["icebreaker_max_candidates"] = _env_int(
        "TASK_ICEBREAKER_MAX_CANDIDATES", base["icebreaker_max_candidates"]
    )
    ice = (os.getenv("TASK_ICEBREAKER_ENABLED") or "1").strip().lower()
    base["icebreaker_enabled"] = ice not in ("0", "false", "off")
    return base


def _clamp_int(val: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def normalize_limits(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = _bootstrap_from_env()
    if not raw or not isinstance(raw, dict):
        return base
    out = deepcopy(base)
    out["daily_cap"] = _clamp_int(raw.get("daily_cap"), base["daily_cap"], 1, 200)
    out["weekly_cap"] = _clamp_int(raw.get("weekly_cap"), base["weekly_cap"], 1, 300)
    out["monthly_cap"] = _clamp_int(raw.get("monthly_cap"), base["monthly_cap"], 1, 500)
    out["icebreaker_cap"] = _clamp_int(raw.get("icebreaker_cap"), base["icebreaker_cap"], 0, 200)
    out["max_customers_main"] = _clamp_int(
        raw.get("max_customers_main"), base["max_customers_main"], 20, 500
    )
    out["icebreaker_max_candidates"] = _clamp_int(
        raw.get("icebreaker_max_candidates"), base["icebreaker_max_candidates"], 20, 800
    )
    out["icebreaker_enabled"] = bool(raw.get("icebreaker_enabled", base["icebreaker_enabled"]))
    out["weekly_refresh_daily"] = bool(raw.get("weekly_refresh_daily", base["weekly_refresh_daily"]))
    out["monthly_refresh_daily"] = bool(raw.get("monthly_refresh_daily", base["monthly_refresh_daily"]))
    return out


async def get_task_allocation_limits(db) -> dict[str, Any]:
    res = await db.execute(
        select(SystemConfig).where(SystemConfig.config_key == TASK_ALLOCATION_LIMITS_KEY)
    )
    cfg = res.scalars().first()
    if not cfg or not (cfg.config_value or "").strip():
        return normalize_limits(None)
    try:
        data = json.loads(cfg.config_value)
    except json.JSONDecodeError:
        data = None
    return normalize_limits(data if isinstance(data, dict) else None)


async def set_task_allocation_limits(db, patch: dict[str, Any]) -> dict[str, Any]:
    current = await get_task_allocation_limits(db)
    merged = deepcopy(current)
    for k in DEFAULT_TASK_ALLOCATION_LIMITS:
        if k in patch:
            merged[k] = patch[k]
    final = normalize_limits(merged)
    val = json.dumps(final, ensure_ascii=False)
    res = await db.execute(
        select(SystemConfig).where(SystemConfig.config_key == TASK_ALLOCATION_LIMITS_KEY)
    )
    cfg = res.scalars().first()
    desc = "任务分配：各周期产出上限、LLM 候选数、破冰开关、周/月是否每日滚动刷新"
    if cfg:
        cfg.config_value = val
        cfg.config_group = "task"
        cfg.description = cfg.description or desc
    else:
        db.add(
            SystemConfig(
                config_key=TASK_ALLOCATION_LIMITS_KEY,
                config_value=val,
                config_group="task",
                description=desc,
            )
        )
    await db.commit()
    return final


def task_cap_for_period(period_type: str, limits: dict[str, Any]) -> int:
    if period_type == "weekly":
        return int(limits["weekly_cap"])
    if period_type == "monthly":
        return int(limits["monthly_cap"])
    return int(limits["daily_cap"])
