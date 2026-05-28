"""
任务分配质量评估指标（写入 batch.input_snapshot_json.evaluation）。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def build_evaluation_metrics(
    *,
    features: list[dict[str, Any]],
    selected_ids: list[str],
    final_tasks: list[dict[str, Any]],
    aggregator_metrics: dict[str, Any],
    quota_plan: dict[str, Any],
) -> dict[str, Any]:
    feat_by_id = {str(f.get("raw_customer_id")): f for f in features}
    selected_set = set(selected_ids)

    bucket_selected: dict[str, int] = defaultdict(int)
    bucket_total: dict[str, int] = defaultdict(int)
    for f in features:
        rid = str(f.get("raw_customer_id") or "")
        tags = f.get("stage_tags") or []
        bucket = str(tags[0]) if tags else "default"
        bucket_total[bucket] += 1
        if rid in selected_set:
            bucket_selected[bucket] += 1

    coverage = {}
    for b, total in bucket_total.items():
        coverage[b] = {
            "total": total,
            "selected": bucket_selected.get(b, 0),
            "rate": round(bucket_selected.get(b, 0) / max(1, total), 4),
        }

    not_selected_sample = [
        str(f.get("raw_customer_id"))
        for f in sorted(
            features,
            key=lambda x: float(x.get("rule_priority_score") or 0),
        )
        if str(f.get("raw_customer_id")) not in selected_set
    ][:20]

    return {
        "candidate_count": len(features),
        "selected_count": len(selected_ids),
        "final_task_count": len(final_tasks),
        "bucket_coverage": coverage,
        "aggregator": aggregator_metrics,
        "quota_plan_summary": {
            "task_cap": quota_plan.get("task_cap"),
            "pool_k": quota_plan.get("pool_k"),
        },
        "not_selected_low_pool_sample": not_selected_sample,
    }
