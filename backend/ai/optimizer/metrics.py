"""
优化器指标汇总：统一准确性口径（P0.5）。

计算源复用 task_allocation_feedback，避免两套口径分叉。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from ai.task_allocation_feedback import build_task_allocation_feedback_report
from ai.task_allocation_limits import get_task_allocation_limits


async def collect_limits_metrics(
    db,
    *,
    ref_date: date | None = None,
    lookback_days: int | None = None,
) -> dict[str, Any]:
    """
    产出参数轨可用的指标快照。
    重点暴露准确性三项（不碰 status）：channel_fit / due_hit / strategy_adopt。
    """
    limits = await get_task_allocation_limits(db)
    lb = int(lookback_days if lookback_days is not None else limits.get("feedback_lookback_days") or 30)
    report = await build_task_allocation_feedback_report(
        db,
        lookback_days=lb,
        ref_date=ref_date,
        include_accuracy=True,
    )
    accuracy = report.get("accuracy") if isinstance(report.get("accuracy"), dict) else {}
    return {
        "generated_at": report.get("generated_at"),
        "lookback_days": report.get("lookback_days"),
        "task_count_with_snapshot": report.get("task_count_with_snapshot"),
        "overall": report.get("overall"),
        "accuracy": {
            "channel_fit_rate": accuracy.get("channel_fit_rate"),
            "channel_fit_samples": accuracy.get("channel_fit_samples"),
            "due_hit_rate": accuracy.get("due_hit_rate"),
            "due_hit_samples": accuracy.get("due_hit_samples"),
            "strategy_adopt_rate": accuracy.get("strategy_adopt_rate"),
            "strategy_adopt_samples": accuracy.get("strategy_adopt_samples"),
            "strategy_profile_available_rate": accuracy.get("strategy_profile_available_rate"),
        },
        "by_assignment_source": report.get("by_assignment_source"),
        "current_limits_snapshot": {
            k: limits.get(k)
            for k in (
                "followup_channel_authority",
                "followup_channel_authority_min_conf",
                "followup_overdue_boost",
                "followup_due_signal_enabled",
                "followup_strategy_fallback",
                "adaptive_cap_enabled",
                "reserve_cap",
            )
        },
        "full_limits": limits,
        "raw_report": report,
    }


def accuracy_guardrail_breaches(
    baseline: dict[str, Any] | None,
    current: dict[str, Any] | None,
    *,
    abs_drop_threshold: float = 0.10,
    min_samples: int = 10,
) -> list[str]:
    """
    比较准确性指标：相对基线绝对下降超过阈值且样本足够 → 记为击穿。
    返回原因字符串列表（空=未击穿）。
    """
    reasons: list[str] = []
    base_acc = (baseline or {}).get("accuracy") if isinstance(baseline, dict) else None
    cur_acc = (current or {}).get("accuracy") if isinstance(current, dict) else None
    if not isinstance(base_acc, dict) or not isinstance(cur_acc, dict):
        return reasons

    checks = (
        ("channel_fit_rate", "channel_fit_samples", "渠道命中率"),
        ("due_hit_rate", "due_hit_samples", "到期命中率"),
        ("strategy_adopt_rate", "strategy_adopt_samples", "策略采纳率"),
    )
    for rate_key, sample_key, label in checks:
        try:
            b = base_acc.get(rate_key)
            c = cur_acc.get(rate_key)
            bs = int(base_acc.get(sample_key) or 0)
            cs = int(cur_acc.get(sample_key) or 0)
            if b is None or c is None:
                continue
            if bs < min_samples or cs < min_samples:
                continue
            bf = float(b)
            cf = float(c)
            if bf - cf >= abs_drop_threshold:
                reasons.append(
                    f"{label} 从 {bf:.1%} 降至 {cf:.1%}（降幅≥{abs_drop_threshold:.0%}，样本 {cs}）"
                )
        except (TypeError, ValueError):
            continue
    return reasons
