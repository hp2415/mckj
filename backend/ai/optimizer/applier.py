"""
优化器唯一写入口：应用 / 驳回 / 回滚参数轨提案。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.future import select

from ai.optimizer.config import (
    P05_WHITELIST_KEYS,
    STATUS_APPLIED,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_ROLLED_BACK,
    TRACK_LIMITS,
    get_observation_days,
    is_limits_auto_apply_enabled,
    is_limits_circuit_open,
    open_limits_circuit,
    set_rollback_streak,
    get_rollback_streak,
)
from ai.optimizer.metrics import collect_limits_metrics
from ai.task_allocation_limits import get_task_allocation_limits, set_task_allocation_limits
from core.logger import logger
from models import AiOptimizationProposal


async def _load_proposal(db, proposal_id: int) -> AiOptimizationProposal | None:
    res = await db.execute(
        select(AiOptimizationProposal).where(AiOptimizationProposal.id == int(proposal_id))
    )
    return res.scalars().first()


def _whitelist_patch(change: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (change or {}).items():
        if k in P05_WHITELIST_KEYS:
            out[k] = v
    return out


async def apply_limits_proposal(
    db,
    proposal_id: int,
    *,
    applied_by: str,
    auto: bool = False,
) -> dict[str, Any]:
    """
    应用提案：校验 → 刷新 before_snapshot → set_task_allocation_limits → status=applied。
    """
    prop = await _load_proposal(db, proposal_id)
    if prop is None:
        return {"ok": False, "error": "proposal_not_found"}
    if prop.track != TRACK_LIMITS:
        return {"ok": False, "error": "track_not_limits"}
    if prop.status not in (STATUS_PENDING, STATUS_APPROVED):
        return {"ok": False, "error": f"invalid_status:{prop.status}"}

    if auto:
        if await is_limits_circuit_open(db):
            return {"ok": False, "error": "circuit_open"}
        if not await is_limits_auto_apply_enabled(db):
            return {"ok": False, "error": "auto_apply_disabled"}

    patch = _whitelist_patch(prop.change_json if isinstance(prop.change_json, dict) else {})
    if not patch:
        return {"ok": False, "error": "empty_whitelist_patch"}

    # 应用前刷新前值快照（回滚依据）
    current = await get_task_allocation_limits(db)
    before = {k: current.get(k) for k in P05_WHITELIST_KEYS}
    prop.before_snapshot_json = before

    # 基线指标写入 effect 预留字段的一部分，供护栏对比
    metrics = await collect_limits_metrics(db)
    baseline = {
        "accuracy": metrics.get("accuracy"),
        "generated_at": metrics.get("generated_at"),
        "lookback_days": metrics.get("lookback_days"),
    }

    final = await set_task_allocation_limits(db, patch)
    # set_task_allocation_limits 已 commit；重新 bind 状态需在新事务写提案
    # 这里同一 session 可能已过期，重新查询
    prop = await _load_proposal(db, proposal_id)
    if prop is None:
        return {"ok": False, "error": "proposal_missing_after_limits_write"}

    obs_days = await get_observation_days(db)
    now = datetime.now()
    prop.status = STATUS_APPLIED
    prop.auto_applied = bool(auto)
    prop.applied_at = now
    prop.applied_by = (applied_by or ("optimizer" if auto else "unknown"))[:50]
    prop.observation_until = now + timedelta(days=obs_days)
    prop.decided_at = now
    prop.effect_json = {
        "baseline_at_apply": baseline,
        "applied_patch": patch,
        "limits_after": {k: final.get(k) for k in P05_WHITELIST_KEYS},
    }
    # before 可能在上面 set 过，再确认一次
    if not prop.before_snapshot_json:
        prop.before_snapshot_json = before
    await db.commit()

    logger.info(
        "优化器已应用提案 id={} auto={} by={} patch={}",
        proposal_id,
        auto,
        applied_by,
        patch,
    )
    return {"ok": True, "proposal_id": proposal_id, "patch": patch, "status": STATUS_APPLIED}


async def reject_limits_proposal(
    db,
    proposal_id: int,
    *,
    rejected_by: str,
    reason: str | None = None,
) -> dict[str, Any]:
    prop = await _load_proposal(db, proposal_id)
    if prop is None:
        return {"ok": False, "error": "proposal_not_found"}
    if prop.status != STATUS_PENDING:
        return {"ok": False, "error": f"invalid_status:{prop.status}"}
    prop.status = STATUS_REJECTED
    prop.decided_at = datetime.now()
    prop.applied_by = (rejected_by or "unknown")[:50]
    if reason:
        prop.rollback_reason = f"rejected: {reason}"[:255]
    await db.commit()
    return {"ok": True, "proposal_id": proposal_id, "status": STATUS_REJECTED}


async def approve_limits_proposal(
    db,
    proposal_id: int,
    *,
    approved_by: str,
) -> dict[str, Any]:
    """人工批准 = 立即应用（P0.5 无灰度，limits 全局生效）。"""
    prop = await _load_proposal(db, proposal_id)
    if prop is None:
        return {"ok": False, "error": "proposal_not_found"}
    if prop.status != STATUS_PENDING:
        return {"ok": False, "error": f"invalid_status:{prop.status}"}
    prop.status = STATUS_APPROVED
    prop.decided_at = datetime.now()
    await db.flush()
    return await apply_limits_proposal(
        db, proposal_id, applied_by=approved_by, auto=False
    )


async def rollback_limits_proposal(
    db,
    proposal_id: int,
    *,
    reason: str,
    by: str = "optimizer",
) -> dict[str, Any]:
    """从 before_snapshot_json 反向 patch 回滚。"""
    prop = await _load_proposal(db, proposal_id)
    if prop is None:
        return {"ok": False, "error": "proposal_not_found"}
    if prop.status != STATUS_APPLIED:
        return {"ok": False, "error": f"invalid_status:{prop.status}"}

    before = prop.before_snapshot_json if isinstance(prop.before_snapshot_json, dict) else {}
    change = prop.change_json if isinstance(prop.change_json, dict) else {}
    # 只回滚本提案改过的 key
    revert: dict[str, Any] = {}
    for k in change.keys():
        if k in P05_WHITELIST_KEYS and k in before:
            revert[k] = before[k]
    if not revert:
        return {"ok": False, "error": "empty_revert_snapshot"}

    await set_task_allocation_limits(db, revert)
    prop = await _load_proposal(db, proposal_id)
    if prop is None:
        return {"ok": False, "error": "proposal_missing_after_rollback"}

    prop.status = STATUS_ROLLED_BACK
    prop.rollback_reason = (reason or "guardrail")[:255]
    prop.decided_at = datetime.now()
    effect = dict(prop.effect_json) if isinstance(prop.effect_json, dict) else {}
    effect["rolled_back_at"] = datetime.now().isoformat(timespec="seconds")
    effect["rollback_by"] = by
    effect["revert_patch"] = revert
    prop.effect_json = effect
    await db.commit()

    streak = await get_rollback_streak(db) + 1
    await set_rollback_streak(db, streak)
    await db.commit()
    if streak >= 2:
        await open_limits_circuit(db, reason=f"连续回滚 {streak} 次")
        await db.commit()
        logger.warning("参数轨熔断打开：连续回滚 {} 次", streak)
    else:
        # streak 写入后需要 commit — set_rollback_streak 用 upsert 不 commit
        pass

    logger.info(
        "优化器已回滚提案 id={} reason={} revert={}",
        proposal_id,
        reason,
        revert,
    )
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "status": STATUS_ROLLED_BACK,
        "revert": revert,
        "rollback_streak": streak,
        "circuit_open": streak >= 2,
    }


async def try_auto_apply_pending(db) -> dict[str, Any]:
    """若开启自动应用，将 pending 提案逐条应用。"""
    if not await is_limits_auto_apply_enabled(db):
        return {"ok": True, "applied": [], "skipped": "auto_disabled"}
    res = await db.execute(
        select(AiOptimizationProposal)
        .where(
            AiOptimizationProposal.track == TRACK_LIMITS,
            AiOptimizationProposal.status == STATUS_PENDING,
        )
        .order_by(AiOptimizationProposal.id.asc())
    )
    rows = list(res.scalars().all())
    applied: list[int] = []
    errors: list[dict[str, Any]] = []
    for prop in rows:
        out = await apply_limits_proposal(
            db, int(prop.id), applied_by="optimizer", auto=True
        )
        if out.get("ok"):
            applied.append(int(prop.id))
        else:
            errors.append({"id": int(prop.id), "error": out.get("error")})
    return {"ok": True, "applied": applied, "errors": errors}
