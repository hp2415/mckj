"""
Phase B：全局选人 + 配额计划（规则为主，不调用 LLM）。
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any

SELECTION_POOL_MULTIPLIER = float(os.getenv("TASK_SELECTION_POOL_MULTIPLIER") or "3.0")
MIN_PER_BUCKET = int(os.getenv("TASK_SELECTION_MIN_PER_BUCKET") or "1")


def _primary_bucket(feat: dict[str, Any]) -> str:
    tags = feat.get("stage_tags") or []
    if tags:
        return str(tags[0])
    band = str(feat.get("priority_band") or "").strip()
    if band:
        return f"band:{band}"
    return "default"


def build_quota_plan(
    features: list[dict[str, Any]],
    *,
    task_cap: int,
    period_type: str,
) -> dict[str, Any]:
    """按主标签分桶的目标配额（阶段/标签）。"""
    cap = max(1, int(task_cap))
    buckets: dict[str, int] = defaultdict(int)
    for f in features:
        buckets[_primary_bucket(f)] += 1
    if not buckets:
        return {"by_stage_tag": {}, "task_cap": cap, "period_type": period_type}

    n_buckets = len(buckets)
    base = max(MIN_PER_BUCKET, cap // max(n_buckets, 1))
    by_tag: dict[str, int] = {}
    remaining = cap
    sorted_keys = sorted(buckets.keys(), key=lambda k: -buckets[k])
    for i, key in enumerate(sorted_keys):
        if i == len(sorted_keys) - 1:
            by_tag[key] = max(0, remaining)
        else:
            take = min(base, remaining)
            by_tag[key] = take
            remaining -= take
    return {
        "by_stage_tag": by_tag,
        "by_task_kind": {
            "contact": max(1, int(cap * 0.5)),
            "follow_up": max(0, int(cap * 0.25)),
            "close_deal": max(0, int(cap * 0.1)),
            "revisit": max(0, int(cap * 0.1)),
        },
        "task_cap": cap,
        "period_type": period_type,
    }


def select_customers_for_allocation(
    features: list[dict[str, Any]],
    *,
    task_cap: int,
    max_pool: int | None = None,
    period_type: str = "daily",
) -> tuple[list[str], dict[str, Any]]:
    """
    从全量特征中选出进入 Phase C 的 raw_customer_id 列表（TopK）。
    返回 (selected_ids, quota_plan)。
    """
    cap = max(1, int(task_cap))
    pool_k = max_pool if max_pool is not None else max(cap, int(cap * SELECTION_POOL_MULTIPLIER))
    pool_k = min(pool_k, len(features))

    sorted_feats = sorted(
        features,
        key=lambda f: (
            -float(f.get("rule_priority_score") or 0),
            str(f.get("raw_customer_id") or ""),
        ),
    )
    quota_plan = build_quota_plan(sorted_feats[:pool_k], task_cap=cap, period_type=period_type)

    # 分桶配额填充
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in sorted_feats:
        by_bucket[_primary_bucket(f)].append(f)

    targets = quota_plan.get("by_stage_tag") or {}
    selected: list[str] = []
    selected_set: set[str] = set()

    for bucket, target in sorted(targets.items(), key=lambda x: -x[1]):
        need = int(target)
        pool = by_bucket.get(bucket, [])
        for f in pool:
            if len(selected) >= pool_k:
                break
            rid = str(f.get("raw_customer_id") or "").strip()
            if not rid or rid in selected_set:
                continue
            selected.append(rid)
            selected_set.add(rid)
            need -= 1
            if need <= 0:
                break

    # 按分数补齐至 pool_k
    for f in sorted_feats:
        if len(selected) >= pool_k:
            break
        rid = str(f.get("raw_customer_id") or "").strip()
        if rid and rid not in selected_set:
            selected.append(rid)
            selected_set.add(rid)

    quota_plan["selected_count"] = len(selected)
    quota_plan["pool_k"] = pool_k
    return selected[:pool_k], quota_plan
