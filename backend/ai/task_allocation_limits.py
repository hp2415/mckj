"""
任务分配数量与刷新策略：存 system_configs（JSON），管理后台总览页维护。
环境变量仅作库中无配置时的兜底，日常请在总览「任务数量与刷新策略」中调整。

主线任务按触达渠道分为「微信任务」「电话任务」，各周期分别配置上限；
开启动态调整时，下调不得低于上限 × adaptive_cap_min_factor（默认 60%，可配置为 0）。
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

from sqlalchemy.future import select

from models import SystemConfig

from ai.task_allocation_ranking import (
    DEFAULT_CONTACT_INTERVAL,
    DEFAULT_SCORING_WEIGHTS,
)

TASK_ALLOCATION_LIMITS_KEY = "task_allocation_limits_json"

CONTACT_CHANNEL_WECHAT = "wechat"
CONTACT_CHANNEL_PHONE = "phone"

DEFAULT_TASK_ALLOCATION_LIMITS: dict[str, Any] = {
    "daily_wechat_cap": 12,
    "daily_phone_cap": 3,
    "weekly_wechat_cap": 24,
    "weekly_phone_cap": 6,
    "icebreaker_cap": 50,
    "max_customers_main": 240,
    "icebreaker_max_candidates": 200,
    "icebreaker_enabled": True,
    # 为 false 时：激活池不纳入「近期新加好友」，仅沉默/变少/从未回复
    "icebreaker_include_new": False,
    "icebreaker_new_days": 7,
    "icebreaker_stale_days": 30,
    "icebreaker_lapsed_days": 14,
    "icebreaker_cooldown_days": 1,
    # 销售近 N 日已主动发消息（raw_chat_logs.is_send=1）则不进激活池
    "icebreaker_outbound_quiet_days": 10,
    # 为 true 时：历史遗留开关；周任务已改为画像跟进日期动态汇总，不再触发 LLM 分配
    "weekly_refresh_daily": True,
    "monthly_refresh_daily": False,
    # 可扩展分配管线（Phase A/B/C + 聚合器）
    "scalable_pipeline_enabled": True,
    "selection_pool_multiplier": 4.0,
    "llm_batch_size": 30,
    "prompt_char_budget": 120000,
    # 打分权重（见 task_allocation_ranking.DEFAULT_SCORING_WEIGHTS）
    "scoring_weights": deepcopy(DEFAULT_SCORING_WEIGHTS),
    # 沉积加分
    "stale_boost_days": 14,
    "stale_boost_cap": 22.0,
    # 自适应联系间隔（天）
    "contact_interval": deepcopy(DEFAULT_CONTACT_INTERVAL),
    # Phase B 探索位比例 0~0.3；聚合器按同比例预留最终席位
    "exploration_ratio": 0.25,
    # 销售个性化 cap 浮动（相对全局上限；下调不得低于上限 × min_factor）
    "adaptive_cap_enabled": True,
    "adaptive_cap_min_factor": 0.6,  # 下限比例：不能低于配置上限 × 该值（0~1，0 表示允许动态下调至 0）
    "adaptive_cap_max_factor": 1.25,
    "adaptive_cap_completion_high": 0.75,
    "adaptive_cap_completion_low": 0.35,
    # 高 A/B 客户占比高时电话额度上浮
    "adaptive_phone_ab_ratio_threshold": 0.35,
    "adaptive_phone_cap_boost": 1,
    # 反馈报表回溯天数
    "feedback_lookback_days": 30,
    # 准确性度量
    "accuracy_metrics_enabled": True,
    # P0：候选特征快照（离线回放）
    "input_snapshot_enabled": True,
    "input_snapshot_sample_ratio": 1.0,
    # 画像渠道主导
    "followup_channel_authority": True,
    "followup_channel_authority_min_conf": "any",
    # 画像策略兜底 instruction
    "followup_strategy_fallback": True,
    "followup_strategy_min_chars": 8,
    # 跟进日期一等调度信号
    "followup_due_signal_enabled": True,
    "followup_overdue_boost": 8.0,
    "followup_dueday_boost": 10.0,
    "followup_upcoming_days": 2,
    "followup_upcoming_boost": 8.0,
    "followup_due_guaranteed_daily": False,
    # rule/LLM 偏差治理
    "structured_field_authority": True,
    # 事件驱动画像
    "event_profile_enabled": True,
    "event_profile_cooldown_minutes": 60,
    # 储备任务池
    "surplus_enabled": True,
    "surplus_ratio": 0.5,
    "reserve_cap": 20,
    # 储备任务认领
    "claim_enabled": True,
    "claim_daily_limit": 10,
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


def _clamp_float(val: Any, default: float, lo: float, hi: float) -> float:
    try:
        n = float(val)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _normalize_nested_floats(raw: dict | None, defaults: dict[str, float]) -> dict[str, float]:
    out = deepcopy(defaults)
    if not isinstance(raw, dict):
        return out
    for k, default in defaults.items():
        if k in raw:
            out[k] = _clamp_float(raw[k], default, 0.0, 200.0)
    return out


def _normalize_contact_interval(raw: dict | None) -> dict[str, int]:
    out = deepcopy(DEFAULT_CONTACT_INTERVAL)
    if not isinstance(raw, dict):
        return out
    for k, default in DEFAULT_CONTACT_INTERVAL.items():
        if k in raw:
            out[k] = _clamp_int(raw[k], default, 1, 60)
    return out


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
    out["icebreaker_include_new"] = bool(
        merged.get("icebreaker_include_new", base.get("icebreaker_include_new", False))
    )
    out["icebreaker_new_days"] = _clamp_int(
        merged.get("icebreaker_new_days"), base.get("icebreaker_new_days", 7), 1, 30
    )
    out["icebreaker_stale_days"] = _clamp_int(
        merged.get("icebreaker_stale_days"), base.get("icebreaker_stale_days", 21), 7, 120
    )
    out["icebreaker_lapsed_days"] = _clamp_int(
        merged.get("icebreaker_lapsed_days"), base.get("icebreaker_lapsed_days", 10), 3, 60
    )
    out["icebreaker_cooldown_days"] = _clamp_int(
        merged.get("icebreaker_cooldown_days"), base.get("icebreaker_cooldown_days", 1), 0, 7
    )
    out["icebreaker_outbound_quiet_days"] = _clamp_int(
        merged.get("icebreaker_outbound_quiet_days"),
        base.get("icebreaker_outbound_quiet_days", 10),
        0,
        60,
    )
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
    out["scoring_weights"] = _normalize_nested_floats(
        merged.get("scoring_weights"), base.get("scoring_weights", DEFAULT_SCORING_WEIGHTS)
    )
    out["stale_boost_days"] = _clamp_int(
        merged.get("stale_boost_days"), base.get("stale_boost_days", 14), 3, 60
    )
    out["stale_boost_cap"] = _clamp_float(
        merged.get("stale_boost_cap"), base.get("stale_boost_cap", 22.0), 5.0, 50.0
    )
    out["contact_interval"] = _normalize_contact_interval(merged.get("contact_interval"))
    out["exploration_ratio"] = _clamp_float(
        merged.get("exploration_ratio"), base.get("exploration_ratio", 0.1), 0.0, 0.3
    )
    out["adaptive_cap_enabled"] = bool(
        merged.get("adaptive_cap_enabled", base.get("adaptive_cap_enabled", True))
    )
    out["adaptive_cap_min_factor"] = _clamp_float(
        merged.get("adaptive_cap_min_factor"), base.get("adaptive_cap_min_factor", 0.6), 0.0, 1.0
    )
    out["adaptive_cap_max_factor"] = _clamp_float(
        merged.get("adaptive_cap_max_factor"), base.get("adaptive_cap_max_factor", 1.25), 1.0, 2.0
    )
    out["adaptive_cap_completion_high"] = _clamp_float(
        merged.get("adaptive_cap_completion_high"), base.get("adaptive_cap_completion_high", 0.75), 0.5, 1.0
    )
    out["adaptive_cap_completion_low"] = _clamp_float(
        merged.get("adaptive_cap_completion_low"), base.get("adaptive_cap_completion_low", 0.35), 0.0, 0.7
    )
    out["adaptive_phone_ab_ratio_threshold"] = _clamp_float(
        merged.get("adaptive_phone_ab_ratio_threshold"),
        base.get("adaptive_phone_ab_ratio_threshold", 0.35),
        0.1,
        0.8,
    )
    out["adaptive_phone_cap_boost"] = _clamp_int(
        merged.get("adaptive_phone_cap_boost"), base.get("adaptive_phone_cap_boost", 1), 0, 5
    )
    out["feedback_lookback_days"] = _clamp_int(
        merged.get("feedback_lookback_days"), base.get("feedback_lookback_days", 30), 7, 180
    )
    out["accuracy_metrics_enabled"] = bool(
        merged.get("accuracy_metrics_enabled", base.get("accuracy_metrics_enabled", True))
    )
    out["input_snapshot_enabled"] = bool(
        merged.get("input_snapshot_enabled", base.get("input_snapshot_enabled", True))
    )
    out["input_snapshot_sample_ratio"] = _clamp_float(
        merged.get("input_snapshot_sample_ratio"),
        base.get("input_snapshot_sample_ratio", 1.0),
        0.0,
        1.0,
    )
    out["followup_channel_authority"] = bool(
        merged.get("followup_channel_authority", base.get("followup_channel_authority", False))
    )
    min_conf = str(
        merged.get("followup_channel_authority_min_conf")
        or base.get("followup_channel_authority_min_conf")
        or "phone"
    ).strip().lower()
    out["followup_channel_authority_min_conf"] = (
        min_conf if min_conf in ("phone", "wechat", "any") else "phone"
    )
    out["followup_strategy_fallback"] = bool(
        merged.get("followup_strategy_fallback", base.get("followup_strategy_fallback", False))
    )
    out["followup_strategy_min_chars"] = _clamp_int(
        merged.get("followup_strategy_min_chars"),
        base.get("followup_strategy_min_chars", 8),
        1,
        500,
    )
    out["followup_due_signal_enabled"] = bool(
        merged.get("followup_due_signal_enabled", base.get("followup_due_signal_enabled", False))
    )
    out["followup_overdue_boost"] = _clamp_float(
        merged.get("followup_overdue_boost"),
        base.get("followup_overdue_boost", 28.0),
        0.0,
        60.0,
    )
    out["followup_dueday_boost"] = _clamp_float(
        merged.get("followup_dueday_boost"),
        base.get("followup_dueday_boost", 18.0),
        0.0,
        60.0,
    )
    out["followup_upcoming_days"] = _clamp_int(
        merged.get("followup_upcoming_days"),
        base.get("followup_upcoming_days", 2),
        0,
        14,
    )
    out["followup_upcoming_boost"] = _clamp_float(
        merged.get("followup_upcoming_boost"),
        base.get("followup_upcoming_boost", 8.0),
        0.0,
        40.0,
    )
    out["followup_due_guaranteed_daily"] = bool(
        merged.get("followup_due_guaranteed_daily", base.get("followup_due_guaranteed_daily", False))
    )
    out["structured_field_authority"] = bool(
        merged.get("structured_field_authority", base.get("structured_field_authority", False))
    )
    out["event_profile_enabled"] = bool(
        merged.get("event_profile_enabled", base.get("event_profile_enabled", False))
    )
    out["event_profile_cooldown_minutes"] = _clamp_int(
        merged.get("event_profile_cooldown_minutes"),
        base.get("event_profile_cooldown_minutes", 60),
        15,
        1440,
    )
    out["surplus_enabled"] = bool(
        merged.get("surplus_enabled", base.get("surplus_enabled", True))
    )
    try:
        out["surplus_ratio"] = max(
            0.0, min(2.0, float(merged.get("surplus_ratio", base.get("surplus_ratio", 0.5))))
        )
    except (TypeError, ValueError):
        out["surplus_ratio"] = base.get("surplus_ratio", 0.5)
    out["reserve_cap"] = _clamp_int(
        merged.get("reserve_cap"), base.get("reserve_cap", 20), 0, 200
    )
    out["claim_enabled"] = bool(
        merged.get("claim_enabled", base.get("claim_enabled", False))
    )
    out["claim_daily_limit"] = _clamp_int(
        merged.get("claim_daily_limit"), base.get("claim_daily_limit", 10), 0, 100
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
        "icebreaker_include_new",
        "icebreaker_new_days",
        "icebreaker_stale_days",
        "icebreaker_lapsed_days",
        "icebreaker_cooldown_days",
        "icebreaker_outbound_quiet_days",
        "weekly_refresh_daily",
        "monthly_refresh_daily",
        "scalable_pipeline_enabled",
        "selection_pool_multiplier",
        "llm_batch_size",
        "prompt_char_budget",
        "scoring_weights",
        "stale_boost_days",
        "stale_boost_cap",
        "contact_interval",
        "exploration_ratio",
        "adaptive_cap_enabled",
        "adaptive_cap_min_factor",
        "adaptive_cap_max_factor",
        "adaptive_cap_completion_high",
        "adaptive_cap_completion_low",
        "adaptive_phone_ab_ratio_threshold",
        "adaptive_phone_cap_boost",
        "feedback_lookback_days",
        "accuracy_metrics_enabled",
        "input_snapshot_enabled",
        "input_snapshot_sample_ratio",
        "followup_channel_authority",
        "followup_channel_authority_min_conf",
        "followup_strategy_fallback",
        "followup_strategy_min_chars",
        "followup_due_signal_enabled",
        "followup_overdue_boost",
        "followup_dueday_boost",
        "followup_upcoming_days",
        "followup_upcoming_boost",
        "followup_due_guaranteed_daily",
        "structured_field_authority",
        "event_profile_enabled",
        "event_profile_cooldown_minutes",
        "surplus_enabled",
        "surplus_ratio",
        "reserve_cap",
        "claim_enabled",
        "claim_daily_limit",
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


def scale_channel_caps_to_task_cap(
    task_cap: int,
    wechat_cap: int,
    phone_cap: int,
) -> tuple[int, int]:
    """
    将渠道上限缩放到不超过 task_cap，保持微信:电话比例。
    用于分批 LLM（本批 cap < 周期 cap）及 prompt 构建。
    """
    cap = max(0, int(task_cap))
    w = max(0, int(wechat_cap))
    p = max(0, int(phone_cap))
    if cap <= 0:
        return 0, 0
    if w + p <= cap:
        return w, p
    if w + p <= 0:
        return cap, 0
    w_scaled = int(round(cap * w / (w + p)))
    w_scaled = max(0, min(w, w_scaled))
    p_scaled = max(0, min(p, cap - w_scaled))
    # 舍入余量补给较大渠道
    while w_scaled + p_scaled < cap:
        if w_scaled < w and (w_scaled >= p_scaled or p_scaled >= p):
            w_scaled += 1
        elif p_scaled < p:
            p_scaled += 1
        else:
            break
    return w_scaled, p_scaled


def _adaptive_cap_min_factor(limits: dict[str, Any] | None, default: float = 0.6) -> float:
    """读取下限比例；显式 0 合法，不可用 `or default`（0 会被当成假值）。"""
    raw = (limits or {}).get("adaptive_cap_min_factor", default)
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _channel_cap_floor(base: int, min_factor: float) -> int:
    """主线渠道动态调整后的下限：不少于配置上限 × min_factor。"""
    b = max(0, int(base))
    if b <= 0:
        return 0
    floor = int(round(b * float(min_factor)))
    return max(0, min(b, floor))


def main_channel_floor_caps(
    base_wechat: int,
    base_phone: int,
    limits: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """按配置上限 × adaptive_cap_min_factor 计算微信/电话主线硬下限。"""
    min_factor = _adaptive_cap_min_factor(limits)
    return _channel_cap_floor(base_wechat, min_factor), _channel_cap_floor(base_phone, min_factor)


def adaptive_channel_caps_for_sales(
    base_wechat: int,
    base_phone: int,
    *,
    limits: dict[str, Any],
    completion_rate: float | None = None,
    pending_overdue: int = 0,
    ab_customer_ratio: float | None = None,
) -> tuple[int, int, dict[str, Any]]:
    """
    按销售历史表现与客户结构微调渠道 cap。
    下调不得低于配置上限 × adaptive_cap_min_factor（默认 60%，可为 0）。
    返回 (wechat_cap, phone_cap, meta)。
    """
    w, p = int(base_wechat), int(base_phone)
    min_factor = _adaptive_cap_min_factor(limits)
    meta: dict[str, Any] = {
        "base_wechat": w,
        "base_phone": p,
        "min_factor": min_factor,
        "adaptive_applied": False,
    }
    if not limits.get("adaptive_cap_enabled"):
        return w, p, meta

    factor = 1.0
    cr = completion_rate
    if cr is not None:
        if cr >= float(limits.get("adaptive_cap_completion_high", 0.75)) and pending_overdue <= 2:
            factor = float(limits.get("adaptive_cap_max_factor", 1.25))
        elif cr <= float(limits.get("adaptive_cap_completion_low", 0.35)) or pending_overdue >= 8:
            factor = min_factor
    meta["factor"] = factor

    w_adj = max(0, int(round(w * factor)))
    p_adj = max(0, int(round(p * factor)))

    ab_ratio = ab_customer_ratio
    if ab_ratio is not None and ab_ratio >= float(limits.get("adaptive_phone_ab_ratio_threshold", 0.35)):
        boost = int(limits.get("adaptive_phone_cap_boost", 1))
        p_adj = min(p + boost, p_adj + boost)
        if w_adj + p_adj > w + p + boost:
            w_adj = max(0, w_adj - boost)
        meta["phone_ab_boost"] = boost

    # 硬兜底：微信/电话均不得低于各自上限的下限比例（min_factor=0 时下限为 0）
    w_floor = _channel_cap_floor(w, min_factor)
    p_floor = _channel_cap_floor(p, min_factor)
    if w_adj < w_floor:
        meta["wechat_floor_applied"] = True
        w_adj = w_floor
    if p_adj < p_floor:
        meta["phone_floor_applied"] = True
        p_adj = p_floor
    meta["wechat_floor"] = w_floor
    meta["phone_floor"] = p_floor

    meta["adaptive_applied"] = (w_adj, p_adj) != (w, p)
    meta["wechat_cap"] = w_adj
    meta["phone_cap"] = p_adj
    return w_adj, p_adj, meta
