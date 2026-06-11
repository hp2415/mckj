"""
画像 LLM 输入预算（A0-2）：聊天分层截断、订单聚合、增量/全量模式。

配置优先级：system_configs (config_group=ai) → 环境变量 → 代码默认值。
profile_input_budget_enabled=0 时关闭全部截断/增量，回退旧行为。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.future import select

from models import SystemConfig, SalesCustomerProfile
from ai.raw_chat_time import profiled_at_to_ms

SHANGHAI_TZ = timezone(timedelta(hours=8))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in ("0", "false", "off", "no")

"""配置键	默认值	说明
profile_input_budget_enabled    1      0 = 完全回退旧行为（不截断、不增量）
profile_chat_recent_count       10     近期条数
profile_chat_recent_chars       150    近期单条上限
profile_chat_older_chars        80     更早单条上限
profile_chat_total_chars        12000  聊天块总上限
profile_order_max_list          15     订单明细条数
profile_incremental_enabled     1      增量开关
profile_incremental_full_days   30     超期强制全量"""
@dataclass(frozen=True)
class ProfileInputBudget:
    enabled: bool = True
    chat_max_messages: int = 50
    chat_recent_count: int = 10
    chat_recent_chars: int = 150
    chat_older_chars: int = 80
    chat_total_chars: int = 12000
    order_max_list: int = 15
    incremental_enabled: bool = True
    incremental_full_days: int = 30

    @classmethod
    def defaults(cls) -> ProfileInputBudget:
        return cls(
            enabled=_env_bool("PROFILE_INPUT_BUDGET_ENABLED", True),
            chat_max_messages=_env_int("PROFILE_CHAT_MAX_MESSAGES", 50),
            chat_recent_count=_env_int("PROFILE_CHAT_RECENT_COUNT", 10),
            chat_recent_chars=_env_int("PROFILE_CHAT_RECENT_CHARS", 150),
            chat_older_chars=_env_int("PROFILE_CHAT_OLDER_CHARS", 80),
            chat_total_chars=_env_int("PROFILE_CHAT_TOTAL_CHARS", 12000),
            order_max_list=_env_int("PROFILE_ORDER_MAX_LIST", 15),
            incremental_enabled=_env_bool("PROFILE_INCREMENTAL_ENABLED", True),
            incremental_full_days=_env_int("PROFILE_INCREMENTAL_FULL_DAYS", 30),
        )


def _parse_config_bool(value: str | None, default: bool) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() not in ("0", "false", "off", "no")


def _parse_config_int(value: str | None, default: int) -> int:
    if value is None or not str(value).strip():
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        return default


async def load_profile_input_budget(db) -> ProfileInputBudget:
    """读取画像输入预算；未配置项回退环境变量/默认值。"""
    base = ProfileInputBudget.defaults()
    try:
        res = await db.execute(select(SystemConfig).where(SystemConfig.config_group == "ai"))
        cfg = {c.config_key: c.config_value for c in res.scalars().all()}
    except Exception:
        return base

    return ProfileInputBudget(
        enabled=_parse_config_bool(cfg.get("profile_input_budget_enabled"), base.enabled),
        chat_max_messages=_parse_config_int(cfg.get("profile_chat_max_messages"), base.chat_max_messages),
        chat_recent_count=_parse_config_int(cfg.get("profile_chat_recent_count"), base.chat_recent_count),
        chat_recent_chars=_parse_config_int(cfg.get("profile_chat_recent_chars"), base.chat_recent_chars),
        chat_older_chars=_parse_config_int(cfg.get("profile_chat_older_chars"), base.chat_older_chars),
        chat_total_chars=_parse_config_int(cfg.get("profile_chat_total_chars"), base.chat_total_chars),
        order_max_list=_parse_config_int(cfg.get("profile_order_max_list"), base.order_max_list),
        incremental_enabled=_parse_config_bool(
            cfg.get("profile_incremental_enabled"), base.incremental_enabled
        ),
        incremental_full_days=_parse_config_int(
            cfg.get("profile_incremental_full_days"), base.incremental_full_days
        ),
    )


def resolve_profile_mode(
    scp: SalesCustomerProfile | None,
    budget: ProfileInputBudget,
    *,
    force_full: bool = False,
) -> str:
    """
    返回 'full' | 'incremental'。
    全量：未画像、预算关闭增量、超期、或显式 force_full。
    """
    if force_full or _env_bool("PROFILE_FORCE_FULL", False):
        return "full"
    if not budget.enabled or not budget.incremental_enabled:
        return "full"
    if scp is None or scp.profile_status != 1 or scp.profiled_at is None:
        return "full"
    if not (scp.ai_profile or "").strip():
        return "full"
    days = max(1, budget.incremental_full_days)
    pat = scp.profiled_at
    if pat.tzinfo is None:
        pat = pat.replace(tzinfo=SHANGHAI_TZ)
    else:
        pat = pat.astimezone(SHANGHAI_TZ)
    if datetime.now(SHANGHAI_TZ) - pat > timedelta(days=days):
        return "full"
    return "incremental"


def profile_waterline_ms(scp: SalesCustomerProfile | None, mode: str) -> int | None:
    """增量模式下返回 profiled_at 毫秒水位；全量返回 None。"""
    if mode != "incremental" or scp is None or scp.profiled_at is None:
        return None
    return profiled_at_to_ms(scp.profiled_at)


async def ensure_profile_budget_config_defaults(db) -> None:
    """启动时写入画像预算默认配置（仅缺失时插入，不覆盖运营调整）。"""
    from core.system_config_store import upsert_system_config_row

    defaults = ProfileInputBudget.defaults()
    seeds = [
        ("profile_input_budget_enabled", "1" if defaults.enabled else "0", "画像输入预算总开关（0=回退旧行为）"),
        ("profile_chat_max_messages", str(defaults.chat_max_messages), "画像聊天最多条数"),
        ("profile_chat_recent_count", str(defaults.chat_recent_count), "画像聊天近期条数（用较长截断）"),
        ("profile_chat_recent_chars", str(defaults.chat_recent_chars), "画像近期单条字符上限"),
        ("profile_chat_older_chars", str(defaults.chat_older_chars), "画像更早单条字符上限"),
        ("profile_chat_total_chars", str(defaults.chat_total_chars), "画像聊天块总字符上限"),
        ("profile_order_max_list", str(defaults.order_max_list), "画像订单明细最多条数"),
        ("profile_incremental_enabled", "1" if defaults.incremental_enabled else "0", "画像增量更新开关"),
        ("profile_incremental_full_days", str(defaults.incremental_full_days), "超过 N 天强制全量画像"),
    ]
    for key, value, desc in seeds:
        res = await db.execute(
            select(SystemConfig).where(SystemConfig.config_key == key).limit(1)
        )
        if res.scalars().first() is None:
            await upsert_system_config_row(
                db,
                config_key=key,
                config_value=value,
                config_group="ai",
                description=desc,
            )
