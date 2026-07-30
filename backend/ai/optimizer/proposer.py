"""
优化器提案生成（只读）：包装 suggest_limits_from_accuracy，过滤 P0.5 白名单。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy.future import select

from ai.optimizer.config import (
    P05_WHITELIST_KEYS,
    STATUS_PENDING,
    TRACK_LIMITS,
)
from ai.optimizer.metrics import collect_limits_metrics
from ai.task_allocation_feedback import suggest_limits_from_accuracy
from core.logger import logger
from models import AiOptimizationProposal


def _filter_whitelist_patch(suggested: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (suggested or {}).items():
        if k in P05_WHITELIST_KEYS:
            out[k] = v
    return out


def _notes_for_keys(notes: list[str], keys: set[str]) -> list[str]:
    """粗匹配：note 文本里出现 key 名则保留。"""
    kept: list[str] = []
    for n in notes or []:
        if any(k in n for k in keys):
            kept.append(n)
    return kept or list(notes or [])


async def propose_limits_changes(
    db,
    *,
    ref_date: date | None = None,
    expire_stale_pending: bool = True,
) -> dict[str, Any]:
    """
    生成 pending 提案（默认不自动应用）。
    同一 key 若已有 pending，跳过避免堆叠。
    """
    metrics = await collect_limits_metrics(db, ref_date=ref_date)
    limits = metrics.get("full_limits") or {}
    report = metrics.get("raw_report") or {}
    accuracy = metrics.get("accuracy") or {}
    assignment_stats = report.get("by_assignment_source") or {}

    suggested, notes = suggest_limits_from_accuracy(
        accuracy,
        assignment_stats,
        current_limits=limits,
    )
    patch = _filter_whitelist_patch(suggested)
    result: dict[str, Any] = {
        "suggested_all": suggested,
        "notes_all": notes,
        "whitelist_patch": patch,
        "created_ids": [],
        "skipped_keys": [],
        "metrics": {
            "generated_at": metrics.get("generated_at"),
            "accuracy": accuracy,
            "lookback_days": metrics.get("lookback_days"),
            "task_count_with_snapshot": metrics.get("task_count_with_snapshot"),
        },
    }

    if expire_stale_pending:
        # 可选：不强制过期；仅记录
        pass

    if not patch:
        logger.info("优化器提案：白名单无新增建议 suggested={}", suggested)
        return result

    # 已有 pending 的 key 跳过
    pending_res = await db.execute(
        select(AiOptimizationProposal).where(
            AiOptimizationProposal.track == TRACK_LIMITS,
            AiOptimizationProposal.status == STATUS_PENDING,
        )
    )
    pending_keys: set[str] = set()
    for row in pending_res.scalars().all():
        ch = row.change_json if isinstance(row.change_json, dict) else {}
        pending_keys.update(ch.keys())

    # 按 key 拆成独立提案，便于逐条批准
    for key, value in patch.items():
        if key in pending_keys:
            result["skipped_keys"].append(key)
            continue
        single = {key: value}
        key_notes = _notes_for_keys(notes, {key})
        before = {k: limits.get(k) for k in P05_WHITELIST_KEYS}
        trigger = {
            "accuracy": accuracy,
            "lookback_days": metrics.get("lookback_days"),
            "task_count_with_snapshot": metrics.get("task_count_with_snapshot"),
            "notes": key_notes,
            "generated_at": metrics.get("generated_at") or date.today().isoformat(),
        }
        prop = AiOptimizationProposal(
            track=TRACK_LIMITS,
            scenario_key=None,
            status=STATUS_PENDING,
            trigger_metric_json=trigger,
            change_json=single,
            before_snapshot_json=before,
            auto_applied=False,
            created_at=datetime.now(),
        )
        db.add(prop)
        await db.flush()
        result["created_ids"].append(int(prop.id))
        logger.info(
            "优化器提案已创建 id={} change={} notes={}",
            prop.id,
            single,
            key_notes,
        )

    await db.commit()
    return result
