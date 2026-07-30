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
from ai.task_allocation_ranking import DEFAULT_SCORING_WEIGHTS, resolve_scoring_weights
from ai.task_scheduler import is_claimed_from_reserve
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


def _parse_iso_date(val: Any) -> date | None:
    s = str(val or "")[:10]
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _task_due_as_date(due: Any) -> date | None:
    if due is None:
        return None
    if isinstance(due, date):
        return due
    if hasattr(due, "date"):
        try:
            return due.date()
        except (TypeError, ValueError):
            return None
    return _parse_iso_date(due)


def compute_accuracy_metrics(
    rows: list[tuple[Any, Any, Any, Any]],
) -> dict[str, Any]:
    """
    从 (status, alloc_feature_json, contact_channel, due_date) 计算准确性基线指标。
    - channel_fit_rate: 画像建议渠道与实际渠道一致
    - strategy_adopt_rate: 最终 instruction 来自画像兜底（instruction_source=profile_fallback）
    - strategy_profile_available_rate: 分配时画像是否含跟进策略
    - due_hit_rate: 任务 due_date 不早于画像建议跟进日
    """
    channel_denom = 0
    channel_hit = 0
    strategy_denom = 0
    strategy_hit = 0
    strategy_available = 0
    due_denom = 0
    due_hit = 0

    for _status, alloc_json, channel, due_date in rows:
        alloc = alloc_json if isinstance(alloc_json, dict) else {}
        ps = alloc.get("profile_suggest")
        if not isinstance(ps, dict):
            continue

        strategy_denom += 1
        if ps.get("has_strategy"):
            strategy_available += 1
        if alloc.get("instruction_source") == "profile_fallback":
            strategy_hit += 1

        actual_ch = str(channel or "wechat").strip().lower()
        sug_ch = str(ps.get("channel") or "").strip().lower()
        if sug_ch in ("wechat", "phone"):
            channel_denom += 1
            if sug_ch == actual_ch:
                channel_hit += 1

        followup_d = _parse_iso_date(ps.get("followup_date"))
        task_due = _task_due_as_date(due_date)
        if followup_d and task_due:
            due_denom += 1
            if followup_d <= task_due:
                due_hit += 1

    def _rate(hit: int, denom: int) -> float | None:
        if denom <= 0:
            return None
        return round(hit / denom, 4)

    return {
        "channel_fit_rate": _rate(channel_hit, channel_denom),
        "channel_fit_samples": channel_denom,
        "strategy_adopt_rate": _rate(strategy_hit, strategy_denom),
        "strategy_adopt_samples": strategy_denom,
        "strategy_profile_available_rate": _rate(strategy_available, strategy_denom),
        "strategy_profile_available_samples": strategy_denom,
        "due_hit_rate": _rate(due_hit, due_denom),
        "due_hit_samples": due_denom,
        "tasks_with_profile_suggest": strategy_denom,
    }


async def build_task_allocation_feedback_report(
    db,
    *,
    lookback_days: int = 30,
    ref_date: date | None = None,
    include_accuracy: bool = True,
) -> dict[str, Any]:
    """扫描近 lookback_days 内已到期任务，按 alloc_feature_json 分桶汇总。"""
    ref = ref_date or date.today()
    since = ref - timedelta(days=max(1, lookback_days))

    res = await db.execute(
        select(
            ContactTask.status,
            ContactTask.alloc_feature_json,
            ContactTask.contact_channel,
            ContactTask.due_date,
        )
        .where(ContactTask.due_date >= since)
        .where(ContactTask.due_date <= ref)
        .where(ContactTask.task_kind != "icebreaker")
        .where(ContactTask.status != "reserve")
        .where(ContactTask.alloc_feature_json.isnot(None))
    )
    rows = res.all()

    by_band: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_tier: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_channel: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_assignment: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    exploration: dict[str, int] = defaultdict(int)
    overall: dict[str, int] = defaultdict(int)

    for status, alloc_json, channel, _due in rows:
        st = str(status or "pending")
        overall[st] += 1
        alloc = alloc_json if isinstance(alloc_json, dict) else {}
        band = _bucket_key(alloc, "priority_band")
        tier = _bucket_key(alloc, "tag_tier")
        ch = str(channel or "wechat")
        by_band[band][st] += 1
        by_tier[tier][st] += 1
        by_channel[ch][st] += 1
        assign_key = "claimed_reserve" if is_claimed_from_reserve(alloc) else "main_assigned"
        by_assignment[assign_key][st] += 1
        if alloc.get("exploration"):
            exploration[st] += 1

    band_stats = {k: _stats_from_counts(dict(v)) for k, v in sorted(by_band.items())}
    tier_stats = {k: _stats_from_counts(dict(v)) for k, v in sorted(by_tier.items())}
    channel_stats = {k: _stats_from_counts(dict(v)) for k, v in sorted(by_channel.items())}
    assignment_stats = {
        k: _stats_from_counts(dict(v)) for k, v in sorted(by_assignment.items())
    }

    limits = await get_task_allocation_limits(db)
    accuracy_metrics = compute_accuracy_metrics(rows) if include_accuracy else None
    suggested_weights = suggest_scoring_weights_from_feedback(
        band_stats,
        current_weights=resolve_scoring_weights(limits),
        accuracy=accuracy_metrics,
        assignment_stats=assignment_stats,
        current_limits=limits,
    )

    report: dict[str, Any] = {
        "generated_at": ref.isoformat(),
        "lookback_days": lookback_days,
        "task_count_with_snapshot": sum(overall.values()),
        "overall": _stats_from_counts(dict(overall)),
        "by_priority_band": band_stats,
        "by_tag_tier": tier_stats,
        "by_contact_channel": channel_stats,
        "by_assignment_source": assignment_stats,
        "exploration": _stats_from_counts(dict(exploration)) if exploration else None,
        "suggested_scoring_weights": suggested_weights,
    }
    if include_accuracy and accuracy_metrics is not None:
        report["accuracy"] = accuracy_metrics
    return report


def _float_metric(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def suggest_limits_from_accuracy(
    accuracy: dict[str, Any] | None,
    assignment_stats: dict[str, dict[str, Any]] | None,
    *,
    current_limits: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """基于准确性指标与认领来源完成率，给出 limits 调参建议（仅建议，不自动写入）。"""
    limits = dict(current_limits or {})
    suggested: dict[str, Any] = {}
    notes: list[str] = []
    accuracy = accuracy or {}
    assignment_stats = assignment_stats or {}

    channel_rate = _float_metric(accuracy.get("channel_fit_rate"))
    channel_samples = int(accuracy.get("channel_fit_samples") or 0)
    if channel_rate is not None and channel_samples >= 5 and channel_rate < 0.8:
        if not limits.get("followup_channel_authority"):
            suggested["followup_channel_authority"] = True
            notes.append(
                f"渠道命中率 {channel_rate:.1%}（样本 {channel_samples}）偏低，建议开启 followup_channel_authority"
            )
        else:
            min_conf = str(limits.get("followup_channel_authority_min_conf") or "phone").strip().lower()
            if min_conf != "any":
                suggested["followup_channel_authority_min_conf"] = "any"
                notes.append(
                    f"渠道命中率 {channel_rate:.1%} 仍偏低，建议将 followup_channel_authority_min_conf 设为 any"
                )

    due_rate = _float_metric(accuracy.get("due_hit_rate"))
    due_samples = int(accuracy.get("due_hit_samples") or 0)
    if due_rate is not None and due_samples >= 5 and due_rate < 0.7:
        current_boost = float(limits.get("followup_overdue_boost") or 28.0)
        next_boost = round(min(40.0, max(current_boost + 2.0, current_boost * 1.1)), 2)
        if next_boost > current_boost:
            suggested["followup_overdue_boost"] = next_boost
            notes.append(
                f"到期命中率 {due_rate:.1%}（样本 {due_samples}）偏低，建议上调 followup_overdue_boost 至 {next_boost}"
            )
        if not limits.get("followup_due_signal_enabled"):
            suggested["followup_due_signal_enabled"] = True
            notes.append("到期命中率偏低，建议开启 followup_due_signal_enabled")

    main_stats = assignment_stats.get("main_assigned") or {}
    reserve_stats = assignment_stats.get("claimed_reserve") or {}
    main_cr = _float_metric(main_stats.get("completion_rate"))
    reserve_cr = _float_metric(reserve_stats.get("completion_rate"))
    reserve_total = int(reserve_stats.get("total") or 0)
    if (
        main_cr is not None
        and reserve_cr is not None
        and reserve_total >= 3
        and reserve_cr > main_cr
    ):
        current_cap = int(limits.get("reserve_cap") or 20)
        suggested_cap = min(200, current_cap + max(2, current_cap // 4))
        if suggested_cap > current_cap:
            suggested["reserve_cap"] = suggested_cap
            notes.append(
                f"认领储备任务完成率 {reserve_cr:.1%} 高于主派 {main_cr:.1%}，"
                f"建议上调 reserve_cap：{current_cap} → {suggested_cap}"
            )

    strategy_rate = _float_metric(accuracy.get("strategy_adopt_rate"))
    strategy_samples = int(accuracy.get("strategy_adopt_samples") or 0)
    if (
        strategy_rate is not None
        and strategy_samples >= 5
        and strategy_rate < 0.5
        and not limits.get("followup_strategy_fallback")
    ):
        suggested["followup_strategy_fallback"] = True
        notes.append(
            f"策略采纳率 {strategy_rate:.1%} 偏低，建议开启 followup_strategy_fallback"
        )

    return suggested, notes


def suggest_scoring_weights_from_feedback(
    band_stats: dict[str, dict[str, Any]],
    *,
    current_weights: dict[str, float] | None = None,
    accuracy: dict[str, Any] | None = None,
    assignment_stats: dict[str, dict[str, Any]] | None = None,
    current_limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    根据完成率分桶与准确性指标给出权重/limits 微调建议（非严格拟合，供人工确认后落库）。
    """
    current = dict(current_weights or DEFAULT_SCORING_WEIGHTS)
    high = band_stats.get("high", {})
    low = band_stats.get("low", {})
    high_cr = float(high.get("completion_rate") or 0)
    low_cr = float(low.get("completion_rate") or 0)

    suggested = dict(current)
    notes: list[str] = []
    if high_cr > 0 and low_cr > 0 and high_cr < low_cr:
        suggested["followup_due_boost"] = round(min(25.0, current.get("followup_due_boost", 18.0) * 1.1), 2)
        notes.append("低分档完成率反而更高，建议略增 followup_due_boost")
    if high_cr >= 0.8:
        # pending 加分会强化重复分配，默认不再建议上调
        notes.append("高分档完成率高；保持 pending_task_boost=0，避免未完成任务循环入选")

    suggested_limits, limit_notes = suggest_limits_from_accuracy(
        accuracy,
        assignment_stats,
        current_limits=current_limits,
    )
    notes.extend(limit_notes)

    return {
        "current": current,
        "suggested": suggested,
        "notes": notes,
        "suggested_limits": suggested_limits,
        "current_limits_snapshot": {
            k: current_limits.get(k)
            for k in (
                "followup_channel_authority",
                "followup_channel_authority_min_conf",
                "followup_overdue_boost",
                "followup_due_signal_enabled",
                "followup_strategy_fallback",
                "reserve_cap",
            )
            if current_limits and k in current_limits
        },
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
    include_accuracy = bool(limits.get("accuracy_metrics_enabled", True))
    report = await build_task_allocation_feedback_report(
        db,
        lookback_days=lookback,
        ref_date=ref_date,
        include_accuracy=include_accuracy,
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

    row_res = await db.execute(
        select(ContactTask.status, ContactTask.alloc_feature_json)
        .where(ContactTask.sales_wechat_id == sw)
        .where(ContactTask.due_date >= since)
        .where(ContactTask.due_date <= ref)
        .where(ContactTask.task_kind != "icebreaker")
        .where(ContactTask.status != "reserve")
    )
    main_counts: dict[str, int] = defaultdict(int)
    claimed_counts: dict[str, int] = defaultdict(int)
    for status, alloc_json in row_res.all():
        st = str(status or "pending")
        alloc = alloc_json if isinstance(alloc_json, dict) else None
        if is_claimed_from_reserve(alloc):
            claimed_counts[st] += 1
        else:
            main_counts[st] += 1
    stats = _stats_from_counts(dict(main_counts))
    stats["claimed_from_reserve"] = _stats_from_counts(dict(claimed_counts))
    stats["pending_overdue"] = stats.get("pending", 0) + stats.get("overdue", 0)
    return stats
