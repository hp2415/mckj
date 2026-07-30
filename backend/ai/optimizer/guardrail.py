"""
优化器护栏：每日监测已应用提案，击穿则回滚。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.future import select

from ai.optimizer.applier import rollback_limits_proposal
from ai.optimizer.config import STATUS_APPLIED, TRACK_LIMITS
from ai.optimizer.metrics import accuracy_guardrail_breaches, collect_limits_metrics
from core.logger import logger
from models import AiOptimizationProposal


async def run_limits_guardrail(db) -> dict[str, Any]:
    """
    扫描 status=applied 且仍在观察期（或观察期刚结束）的 limits 提案。
    - 护栏击穿 → 回滚
    - 观察期满且未击穿 → 回填 effect_json.observation_result=pass
    """
    now = datetime.now()
    metrics = await collect_limits_metrics(db)
    current_snap = {
        "accuracy": metrics.get("accuracy"),
        "generated_at": metrics.get("generated_at"),
        "lookback_days": metrics.get("lookback_days"),
    }

    res = await db.execute(
        select(AiOptimizationProposal).where(
            AiOptimizationProposal.track == TRACK_LIMITS,
            AiOptimizationProposal.status == STATUS_APPLIED,
        )
    )
    rows = list(res.scalars().all())
    rolled: list[int] = []
    passed: list[int] = []
    watching: list[int] = []

    for prop in rows:
        effect = dict(prop.effect_json) if isinstance(prop.effect_json, dict) else {}
        baseline = effect.get("baseline_at_apply")
        reasons = accuracy_guardrail_breaches(baseline, current_snap)
        if reasons:
            reason = "; ".join(reasons)[:255]
            out = await rollback_limits_proposal(
                db, int(prop.id), reason=reason, by="optimizer_guardrail"
            )
            if out.get("ok"):
                rolled.append(int(prop.id))
            continue

        until = prop.observation_until
        if until is not None and now >= until:
            effect["observation_result"] = "pass"
            effect["observation_closed_at"] = now.isoformat(timespec="seconds")
            effect["metrics_at_close"] = current_snap
            prop.effect_json = effect
            await db.commit()
            passed.append(int(prop.id))
        else:
            watching.append(int(prop.id))

    logger.info(
        "优化器护栏完成 rolled={} passed={} watching={}",
        rolled,
        passed,
        watching,
    )
    return {
        "ok": True,
        "rolled_back": rolled,
        "observation_passed": passed,
        "still_watching": watching,
        "current_accuracy": current_snap.get("accuracy"),
    }
