"""
优化器配置开关（system_configs）。
"""
from __future__ import annotations

from sqlalchemy.future import select

from core.system_config_store import upsert_system_config_row
from models import SystemConfig

# 参数轨是否允许自动应用（P0.5 前两周默认关，仅人工批准）
CFG_LIMITS_AUTO_APPLY = "optimizer_limits_auto_apply"
# 熔断：连续回滚后关闭自动应用
CFG_LIMITS_CIRCUIT_OPEN = "optimizer_limits_circuit_open"
# 连续回滚计数（同 track）
CFG_LIMITS_ROLLBACK_STREAK = "optimizer_limits_rollback_streak"
# 观察期天数
CFG_OBSERVATION_DAYS = "optimizer_observation_days"

DEFAULT_OBSERVATION_DAYS = 7

# P0.5 白名单：仅准确性指标驱动的 limits（不碰 status/completion_rate）
P05_WHITELIST_KEYS = frozenset(
    {
        "followup_channel_authority",
        "followup_channel_authority_min_conf",
        "followup_overdue_boost",
        "followup_due_signal_enabled",
        "followup_strategy_fallback",
    }
)

TRACK_LIMITS = "limits"
TRACK_PROMPT = "prompt"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_EXPIRED = "expired"


async def _get_cfg(db, key: str, default: str = "") -> str:
    res = await db.execute(select(SystemConfig).where(SystemConfig.config_key == key))
    row = res.scalars().first()
    if not row or row.config_value is None:
        return default
    return str(row.config_value).strip()


async def is_limits_auto_apply_enabled(db) -> bool:
    if await is_limits_circuit_open(db):
        return False
    v = (await _get_cfg(db, CFG_LIMITS_AUTO_APPLY, "0")).lower()
    return v in ("1", "true", "yes", "on")


async def is_limits_circuit_open(db) -> bool:
    v = (await _get_cfg(db, CFG_LIMITS_CIRCUIT_OPEN, "0")).lower()
    return v in ("1", "true", "yes", "on")


async def get_observation_days(db) -> int:
    raw = await _get_cfg(db, CFG_OBSERVATION_DAYS, str(DEFAULT_OBSERVATION_DAYS))
    try:
        return max(1, min(60, int(raw)))
    except ValueError:
        return DEFAULT_OBSERVATION_DAYS


async def get_rollback_streak(db) -> int:
    raw = await _get_cfg(db, CFG_LIMITS_ROLLBACK_STREAK, "0")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


async def set_rollback_streak(db, n: int) -> None:
    await upsert_system_config_row(
        db,
        config_key=CFG_LIMITS_ROLLBACK_STREAK,
        config_value=str(max(0, int(n))),
        config_group="optimizer",
        description="参数轨连续回滚次数（达 2 触发熔断）",
        update_description=False,
    )


async def open_limits_circuit(db, *, reason: str = "") -> None:
    await upsert_system_config_row(
        db,
        config_key=CFG_LIMITS_CIRCUIT_OPEN,
        config_value="1",
        config_group="optimizer",
        description=f"参数轨熔断打开：{reason}"[:200] if reason else "参数轨熔断打开",
        update_description=True,
    )
    await upsert_system_config_row(
        db,
        config_key=CFG_LIMITS_AUTO_APPLY,
        config_value="0",
        config_group="optimizer",
        description="参数轨自动应用（熔断后强制关闭）",
        update_description=False,
    )


async def reset_limits_circuit(db) -> None:
    await upsert_system_config_row(
        db,
        config_key=CFG_LIMITS_CIRCUIT_OPEN,
        config_value="0",
        config_group="optimizer",
        description="参数轨熔断（0=关闭）",
        update_description=False,
    )
    await set_rollback_streak(db, 0)


async def set_limits_auto_apply(db, enabled: bool) -> None:
    await upsert_system_config_row(
        db,
        config_key=CFG_LIMITS_AUTO_APPLY,
        config_value="1" if enabled else "0",
        config_group="optimizer",
        description="参数轨是否自动应用提案（P0.5 默认 0，仅人工批准）",
        update_description=False,
    )


async def freeze_adaptive_cap(db, *, freeze: bool = True) -> dict:
    """实验期冻结 adaptive_channel_caps_for_sales（§2.3）。"""
    from ai.task_allocation_limits import set_task_allocation_limits

    return await set_task_allocation_limits(
        db, {"adaptive_cap_enabled": (not freeze)}
    )


async def ensure_optimizer_defaults(db) -> None:
    """写入 P0.5 默认开关（不覆盖已有值）。"""
    defaults = (
        (
            CFG_LIMITS_AUTO_APPLY,
            "0",
            "参数轨是否自动应用提案（P0.5 前两周默认 0，仅人工批准）",
        ),
        (CFG_LIMITS_CIRCUIT_OPEN, "0", "参数轨熔断（0=关闭）"),
        (CFG_LIMITS_ROLLBACK_STREAK, "0", "参数轨连续回滚次数"),
        (
            CFG_OBSERVATION_DAYS,
            str(DEFAULT_OBSERVATION_DAYS),
            "参数轨观察期天数",
        ),
    )
    for key, value, desc in defaults:
        existing = await _get_cfg(db, key, "")
        if existing != "":
            continue
        await upsert_system_config_row(
            db,
            config_key=key,
            config_value=value,
            config_group="optimizer",
            description=desc,
            update_description=False,
        )
    await db.commit()
