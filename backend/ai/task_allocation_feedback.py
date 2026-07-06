"""
任务分配结果反馈：按分配时特征分桶统计完成/跳过率，产出校准报表。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select

from ai.task_allocation_limits import get_task_allocation_limits
from ai.task_allocation_ranking import DEFAULT_SCORING_WEIGHTS
from core.system_config_store import upsert_system_config_row
from models import ContactTask

TASK_ALLOCATION_FEEDBACK_KEY = "task_allocation_feedback_json"


def _stats_from_counts(counts: dict[str, int]) -> dict[str, Any]:
    total = sum(counts.values())
    done = counts.get("done", 0)
    skipped = counts.get("skipped", 0)
    denom = max(1, total - skipped)
    return {
        "total": total,
        "done": done,
        "skipped": skipped,
        "pending": counts.get("pending", 0),
        "in_progress": counts.get("in_progress", 0),
        "overdue": counts.get("overdue", 0),
        "completion_rate": round(done / denom, 4),
        "skip_rate": round(skipped / max(1, total), 4),
    }


def _bucket_key(alloc: dict[str, Any], field: str) -> str:
    val = alloc.get(field)
    if val is None or val == "":
        return "unknown"
    return str(val)


async def build_task_allocation_feedback_report(
    db,
    *,
    lookback_days: int = 30,
    ref_date: date | None = None,
) -> dict[str, Any]:
    """扫描近 lookback_days 内已到期任务，按 alloc_feature_json 分桶汇总。"""
    ref = ref_date or date.today()
    since = ref - timedelta(days=max(1, lookback_days))

    res = await db.execute(
        select(ContactTask.status, ContactTask.alloc_feature_json, ContactTask.contact_channel)
        .where(ContactTask.due_date >= since)
        .where(ContactTask.due_date <= ref)
        .where(ContactTask.task_kind != "icebreaker")
        .where(ContactTask.alloc_feature_json.isnot(None))
    )
    rows = res.all()

    by_band: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_tier: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_channel: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    exploration: dict[str, int] = defaultdict(int)
    overall: dict[str, int] = defaultdict(int)

    for status, alloc_json, channel in rows:
        st = str(status or "pending")
        overall[st] += 1
        alloc = alloc_json if isinstance(alloc_json, dict) else {}
        band = _bucket_key(alloc, "priority_band")
        tier = _bucket_key(alloc, "tag_tier")
        ch = str(channel or "wechat")
        by_band[band][st] += 1
        by_tier[tier][st] += 1
        by_channel[ch][st] += 1
        if alloc.get("exploration"):
            exploration[st] += 1

    band_stats = {k: _stats_from_counts(dict(v)) for k, v in sorted(by_band.items())}
    tier_stats = {k: _stats_from_counts(dict(v)) for k, v in sorted(by_tier.items())}
    channel_stats = {k: _stats_from_counts(dict(v)) for k, v in sorted(by_channel.items())}

    suggested_weights = suggest_scoring_weights_from_feedback(
        band_stats,
        current_weights=DEFAULT_SCORING_WEIGHTS,
    )

    return {
        "generated_at": ref.isoformat(),
        "lookback_days": lookback_days,
        "task_count_with_snapshot": sum(overall.values()),
        "overall": _stats_from_counts(dict(overall)),
        "by_priority_band": band_stats,
        "by_tag_tier": tier_stats,
        "by_contact_channel": channel_stats,
        "exploration": _stats_from_counts(dict(exploration)) if exploration else None,
        "suggested_scoring_weights": suggested_weights,
    }


def suggest_scoring_weights_from_feedback(
    band_stats: dict[str, dict[str, Any]],
    *,
    current_weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    根据各 priority_band 完成率给出简单权重建议（非严格拟合，供人工/自动微调参考）。
    """
    current = dict(current_weights or DEFAULT_SCORING_WEIGHTS)
    high = band_stats.get("high", {})
    mid = band_stats.get("mid", {})
    low = band_stats.get("low", {})
    high_cr = float(high.get("completion_rate") or 0)
    low_cr = float(low.get("completion_rate") or 0)

    suggested = dict(current)
    notes: list[str] = []
    if high_cr > 0 and low_cr > 0 and high_cr < low_cr:
        suggested["followup_due_boost"] = round(min(25.0, current.get("followup_due_boost", 18.0) * 1.1), 2)
        notes.append("低分档完成率反而更高，建议略增 followup_due_boost")
    if high_cr >= 0.8:
        suggested["pending_task_boost"] = round(min(30.0, current.get("pending_task_boost", 22.0) * 1.05), 2)
        notes.append("高分档完成率高，可略增 pending_task_boost")

    return {
        "current": current,
        "suggested": suggested,
        "notes": notes,
    }


async def persist_task_allocation_feedback(
    db,
    report: dict[str, Any],
) -> None:
    await upsert_system_config_row(
        db,
        config_key=TASK_ALLOCATION_FEEDBACK_KEY,
        config_value=json.dumps(report, ensure_ascii=False),
        config_group="task",
        description="任务分配反馈：按特征分桶的完成/跳过率与权重建议",
        update_description=False,
    )


async def get_task_allocation_feedback(db) -> dict[str, Any] | None:
    from models import SystemConfig

    res = await db.execute(
        select(SystemConfig).where(SystemConfig.config_key == TASK_ALLOCATION_FEEDBACK_KEY)
    )
    cfg = res.scalars().first()
    if not cfg or not (cfg.config_value or "").strip():
        return None
    try:
        data = json.loads(cfg.config_value)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def run_task_allocation_feedback_job(
    db,
    *,
    ref_date: date | None = None,
) -> dict[str, Any]:
    limits = await get_task_allocation_limits(db)
    lookback = int(limits.get("feedback_lookback_days") or 30)
    report = await build_task_allocation_feedback_report(
        db,
        lookback_days=lookback,
        ref_date=ref_date,
    )
    await persist_task_allocation_feedback(db, report)
    await db.commit()
    return report


async def sales_completion_stats(
    db,
    sales_wechat_id: str,
    *,
    lookback_days: int = 14,
    ref_date: date | None = None,
) -> dict[str, Any]:
    """单销售近期任务完成率，供 adaptive cap 使用。"""
    sw = (sales_wechat_id or "").strip()
    ref = ref_date or date.today()
    since = ref - timedelta(days=max(1, lookback_days))
    if not sw:
        return {"completion_rate": None, "pending_overdue": 0, "total": 0}

    res = await db.execute(
        select(ContactTask.status, func.count(ContactTask.id))
        .where(ContactTask.sales_wechat_id == sw)
        .where(ContactTask.due_date >= since)
        .where(ContactTask.due_date <= ref)
        .where(ContactTask.task_kind != "icebreaker")
        .group_by(ContactTask.status)
    )
    counts = {str(k): int(v) for k, v in res.all()}
    stats = _stats_from_counts(counts)
    stats["pending_overdue"] = stats.get("pending", 0) + stats.get("overdue", 0)
    return stats
