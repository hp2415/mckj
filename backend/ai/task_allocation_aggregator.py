"""
Phase C 之后：全局聚合器 — 去重、配额、时间窗排程、公平性裁剪。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any


def _task_dedupe_key(item: dict[str, Any]) -> str:
    dk = str(item.get("dedupe_key") or "").strip()
    if dk:
        return dk
    rid = str(item.get("raw_customer_id") or "").strip()
    kind = str(item.get("task_kind") or "contact").strip()
    topic = str(item.get("title") or "")[:40]
    return f"{rid}|{kind}|{topic}"


def _bucket_for_customer(feat_map: dict[str, dict], rid: str) -> str:
    f = feat_map.get(rid) or {}
    tags = f.get("stage_tags") or []
    if tags:
        return str(tags[0])
    return str(f.get("priority_band") or "default")


def schedule_due_dates(
    rows: list[dict[str, Any]],
    *,
    period_start: date,
    period_end: date,
    period_type: str,
) -> None:
    """为每条任务写入 due_date（日任务同一天；周/月按 time_window_bucket 摊开）。"""
    if period_type == "daily":
        for r in rows:
            r["due_date"] = period_start
        return

    span = max(1, (period_end - period_start).days + 1)
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        tb = str(r.get("time_window_bucket") or "D0").strip().upper()
        if not tb.startswith("D"):
            tb = "D0"
        try:
            off = int(tb[1:])
        except ValueError:
            off = 0
        off = max(0, min(off, span - 1))
        buckets[str(off)].append(r)

    day_load: dict[date, int] = defaultdict(int)
    max_per_day = max(3, len(rows) // span + 1)

    for r in sorted(rows, key=lambda x: (-float(x.get("priority_score") or 0), x.get("raw_customer_id", ""))):
        tb = str(r.get("time_window_bucket") or "D0").strip().upper()
        try:
            off = int(tb[1:]) if tb.startswith("D") else 0
        except ValueError:
            off = 0
        off = max(0, min(off, span - 1))
        d = period_start + timedelta(days=off)
        # 若当日已满，向后找空位
        while day_load[d] >= max_per_day and d < period_end:
            d += timedelta(days=1)
        if d > period_end:
            d = period_end
        r["due_date"] = d
        day_load[d] += 1


def aggregate_candidate_tasks(
    candidates: list[dict[str, Any]],
    *,
    task_cap: int,
    quota_plan: dict[str, Any],
    feature_by_id: dict[str, dict[str, Any]],
    period_start: date,
    period_end: date,
    period_type: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    输入多批 LLM 候选，输出最终 <= task_cap 条任务及评估指标。
    """
    cap = max(0, int(task_cap))
    metrics: dict[str, Any] = {
        "candidates_in": len(candidates),
        "duplicates_removed": 0,
        "discarded": [],
    }

    # 去重：同 dedupe_key 保留 priority_score 最高
    best: dict[str, dict[str, Any]] = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("raw_customer_id") or "").strip()
        if not rid:
            continue
        key = _task_dedupe_key(item)
        ps = float(item.get("priority_score") or 0)
        prev = best.get(key)
        if prev is None or ps > float(prev.get("priority_score") or 0):
            if prev is not None:
                metrics["duplicates_removed"] += 1
            best[key] = {**item, "raw_customer_id": rid}
        else:
            metrics["duplicates_removed"] += 1

    unique = list(best.values())
    unique.sort(
        key=lambda x: (
            -float(x.get("priority_score") or 0),
            str(x.get("raw_customer_id") or ""),
        )
    )

    # 桶配额裁剪
    targets = (quota_plan or {}).get("by_stage_tag") or {}
    bucket_counts: dict[str, int] = defaultdict(int)
    picked: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []

    for item in unique:
        rid = item["raw_customer_id"]
        bucket = _bucket_for_customer(feature_by_id, rid)
        target = int(targets.get(bucket, cap))
        if bucket_counts[bucket] < target or len(picked) < cap:
            if len(picked) < cap:
                picked.append(item)
                bucket_counts[bucket] += 1
            else:
                overflow.append(item)
                metrics["discarded"].append({"raw_customer_id": rid, "reason": "task_cap"})
        else:
            overflow.append(item)
            metrics["discarded"].append({"raw_customer_id": rid, "reason": "bucket_quota"})

    if len(picked) < cap:
        for item in overflow:
            if len(picked) >= cap:
                break
            if item not in picked:
                picked.append(item)

    schedule_due_dates(
        picked,
        period_start=period_start,
        period_end=period_end,
        period_type=period_type,
    )

    for i, row in enumerate(picked, start=1):
        row["priority_rank"] = i

    metrics["tasks_out"] = len(picked)
    metrics["bucket_counts"] = dict(bucket_counts)
    kind_counts: dict[str, int] = defaultdict(int)
    for r in picked:
        kind_counts[str(r.get("task_kind") or "contact")] += 1
    metrics["task_kind_distribution"] = dict(kind_counts)

    return picked[:cap], metrics
