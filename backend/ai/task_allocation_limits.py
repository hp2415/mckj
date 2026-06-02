"""
任务分配数量与刷新策略：存 system_configs（JSON），管理后台总览页维护。
环境变量仅作库中无配置时的兜底，日常请在总览「任务数量与刷新策略」中调整。

主线任务按触达渠道分为「微信任务」「电话任务」，各周期分别配置上限。
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

from sqlalchemy.future import select

from models import SystemConfig

TASK_ALLOCATION_LIMITS_KEY = "task_allocation_limits_json"

CONTACT_CHANNEL_WECHAT = "wechat"
CONTACT_CHANNEL_PHONE = "phone"

DEFAULT_TASK_ALLOCATION_LIMITS: dict[str, Any] = {
    "daily_wechat_cap": 12,
    "daily_phone_cap": 3,
    "weekly_wechat_cap": 24,
    "weekly_phone_cap": 6,
    "icebreaker_cap": 25,
    "max_customers_main": 120,
    "icebreaker_max_candidates": 200,
    "icebreaker_enabled": True,
    # 为 true 时：每日 06:00 日任务后会重算当周计划（月任务分配已停用，月视图仅作进度统计）
    "weekly_refresh_daily": True,
    "monthly_refresh_daily": False,
    # 可扩展分配管线（Phase A/B/C + 聚合器）
    "scalable_pipeline_enabled": True,
    "selection_pool_multiplier": 3.0,
    "llm_batch_size": 30,
    "prompt_char_budget": 120000,
}


def _env_int(key: str, default: int) -> int:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _split_legacy_cap(total: int, *, phone_ratio: float = 0.2) -> tuple[int, int]:
    """将旧版单一 cap 拆为微信 + 电话（电话约占 20%，至少 0 条）。"""
    cap = max(0, int(total))
    if cap <= 0:
        return 0, 0
    phone = min(cap, max(0, int(round(cap * phone_ratio))))
    wechat = max(0, cap - phone)
    if wechat == 0 and cap > 0:
        wechat, phone = cap, 0
    return wechat, phone


def _bootstrap_from_env() -> dict[str, Any]:
    """首次无库配置时，用旧环境变量填充默认值。"""
    base = deepcopy(DEFAULT_TASK_ALLOCATION_LIMITS)
    legacy_daily = _env_int("TASK_DAILY_CONTACT_CAP", base["daily_wechat_cap"] + base["daily_phone_cap"])
    legacy_weekly = _env_int("TASK_WEEKLY_CONTACT_CAP", base["weekly_wechat_cap"] + base["weekly_phone_cap"])
    dw, dp = _split_legacy_cap(legacy_daily)
    ww, wp = _split_legacy_cap(legacy_weekly)
    base["daily_wechat_cap"] = dw
    base["daily_phone_cap"] = dp
    base["weekly_wechat_cap"] = ww
    base["weekly_phone_cap"] = wp
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


def _migrate_channel_caps(raw: dict[str, Any], base: dict[str, Any]) -> None:
    """兼容旧版 daily_cap / weekly_cap 字段。"""
    if "daily_wechat_cap" not in raw and "daily_cap" in raw:
        w, p = _split_legacy_cap(raw.get("daily_cap", 0))
        raw.setdefault("daily_wechat_cap", w)
        raw.setdefault("daily_phone_cap", p)
    if "weekly_wechat_cap" not in raw and "weekly_cap" in raw:
        w, p = _split_legacy_cap(raw.get("weekly_cap", 0))
        raw.setdefault("weekly_wechat_cap", w)
        raw.setdefault("weekly_phone_cap", p)


def normalize_limits(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = _bootstrap_from_env()
    if not raw or not isinstance(raw, dict):
        return base
    merged = deepcopy(raw)
    _migrate_channel_caps(merged, base)
    out = deepcopy(base)
    out["daily_wechat_cap"] = _clamp_int(
        merged.get("daily_wechat_cap"), base["daily_wechat_cap"], 0, 200
    )
    out["daily_phone_cap"] = _clamp_int(
        merged.get("daily_phone_cap"), base["daily_phone_cap"], 0, 100
    )
    out["weekly_wechat_cap"] = _clamp_int(
        merged.get("weekly_wechat_cap"), base["weekly_wechat_cap"], 0, 300
    )
    out["weekly_phone_cap"] = _clamp_int(
        merged.get("weekly_phone_cap"), base["weekly_phone_cap"], 0, 150
    )
    out["icebreaker_cap"] = _clamp_int(merged.get("icebreaker_cap"), base["icebreaker_cap"], 0, 200)
    out["max_customers_main"] = _clamp_int(
        merged.get("max_customers_main"), base["max_customers_main"], 20, 2500
    )
    out["icebreaker_max_candidates"] = _clamp_int(
        merged.get("icebreaker_max_candidates"), base["icebreaker_max_candidates"], 20, 800
    )
    out["icebreaker_enabled"] = bool(merged.get("icebreaker_enabled", base["icebreaker_enabled"]))
    out["weekly_refresh_daily"] = bool(merged.get("weekly_refresh_daily", base["weekly_refresh_daily"]))
    out["monthly_refresh_daily"] = bool(merged.get("monthly_refresh_daily", base["monthly_refresh_daily"]))
    out["scalable_pipeline_enabled"] = bool(
        merged.get("scalable_pipeline_enabled", base.get("scalable_pipeline_enabled", True))
    )
    try:
        out["selection_pool_multiplier"] = max(
            1.0, min(10.0, float(merged.get("selection_pool_multiplier", base.get("selection_pool_multiplier", 3.0))))
        )
    except (TypeError, ValueError):
        out["selection_pool_multiplier"] = base.get("selection_pool_multiplier", 3.0)
    out["llm_batch_size"] = _clamp_int(
        merged.get("llm_batch_size"), base.get("llm_batch_size", 30), 5, 80
    )
    out["prompt_char_budget"] = _clamp_int(
        merged.get("prompt_char_budget"), base.get("prompt_char_budget", 120000), 20000, 500000
    )
    # 便于前端展示合计
    out["daily_cap"] = out["daily_wechat_cap"] + out["daily_phone_cap"]
    out["weekly_cap"] = out["weekly_wechat_cap"] + out["weekly_phone_cap"]
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
    channel_keys = (
        "daily_wechat_cap",
        "daily_phone_cap",
        "weekly_wechat_cap",
        "weekly_phone_cap",
        "icebreaker_cap",
        "max_customers_main",
        "icebreaker_max_candidates",
        "icebreaker_enabled",
        "weekly_refresh_daily",
        "monthly_refresh_daily",
        "scalable_pipeline_enabled",
        "selection_pool_multiplier",
        "llm_batch_size",
        "prompt_char_budget",
    )
    for k in channel_keys:
        if k in patch:
            merged[k] = patch[k]
    final = normalize_limits(merged)
    val = json.dumps(final, ensure_ascii=False)
    res = await db.execute(
        select(SystemConfig).where(SystemConfig.config_key == TASK_ALLOCATION_LIMITS_KEY)
    )
    cfg = res.scalars().first()
    desc = "任务分配：微信/电话主线上限、破冰、LLM 候选数、周/月是否每日滚动刷新"
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


def channel_caps_for_period(period_type: str, limits: dict[str, Any]) -> tuple[int, int]:
    """返回 (微信主线 cap, 电话主线 cap)。"""
    if period_type == "weekly":
        return int(limits["weekly_wechat_cap"]), int(limits["weekly_phone_cap"])
    return int(limits["daily_wechat_cap"]), int(limits["daily_phone_cap"])


def task_cap_for_period(period_type: str, limits: dict[str, Any]) -> int:
    wechat, phone = channel_caps_for_period(period_type, limits)
    return wechat + phone
