"""
优化器定时入口：提案生成 + 护栏。
"""
from __future__ import annotations

from ai.optimizer.applier import try_auto_apply_pending
from ai.optimizer.config import ensure_optimizer_defaults
from ai.optimizer.guardrail import run_limits_guardrail
from ai.optimizer.proposer import propose_limits_changes
from core.logger import logger
from database import AsyncSessionLocal


async def scheduled_weekly_optimizer_propose() -> None:
    """周一 08:00：产出 limits 白名单提案；若开启自动应用则尝试应用。"""
    async with AsyncSessionLocal() as db:
        try:
            await ensure_optimizer_defaults(db)
            result = await propose_limits_changes(db)
            logger.info(
                "周优化提案完成 created={} skipped={} patch={}",
                result.get("created_ids"),
                result.get("skipped_keys"),
                result.get("whitelist_patch"),
            )
        except Exception:
            logger.exception("周优化提案失败")
            return

    async with AsyncSessionLocal() as db:
        try:
            auto = await try_auto_apply_pending(db)
            logger.info("周优化自动应用结果 {}", auto)
        except Exception:
            logger.exception("周优化自动应用失败")


async def scheduled_daily_optimizer_guardrail() -> None:
    """每日 07:30：护栏监测，击穿回滚。"""
    async with AsyncSessionLocal() as db:
        try:
            result = await run_limits_guardrail(db)
            logger.info("日优化护栏结果 {}", result)
        except Exception:
            logger.exception("日优化护栏失败")
