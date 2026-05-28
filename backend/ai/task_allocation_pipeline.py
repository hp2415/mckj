"""
可扩展任务分配管线：Phase A 特征化 → Phase B 选人 → Phase C 分批 LLM → 全局聚合。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from ai.llm_client import LLMClient
from ai.task_allocation_aggregator import aggregate_candidate_tasks
from ai.task_allocation_budget import (
    DEFAULT_LLM_BATCH_SIZE,
    PROMPT_CHAR_BUDGET,
    batch_task_cap,
    shrink_batch_params,
    split_feature_batches,
)
from ai.task_allocation_features import materialize_features
from ai.task_allocation_llm import (
    normalize_llm_tasks,
    run_task_allocation_llm_batch,
)
from ai.task_allocation_eval import build_evaluation_metrics
from ai.task_allocation_selection import select_customers_for_allocation
from core.logger import logger


async def run_scalable_main_allocation(
    db,
    llm: LLMClient,
    *,
    sales_wechat_id: str,
    period_type: str,
    period_start: date,
    period_end: date,
    ref_today: date,
    task_cap: int,
    customer_payloads: list[dict[str, Any]],
    lookup: dict[str, tuple[Any, Any]],
    limits: dict[str, Any],
    on_progress=None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    返回 (normalize 前的 enriched rows 供写库, pipeline_meta)。
    """
    meta: dict[str, Any] = {
        "pipeline": "scalable",
        "phase_a_count": len(customer_payloads),
    }

    async def _prog(**kw):
        if on_progress:
            await on_progress(**kw)

    await _prog(phase="Phase A：客户特征化", pct=0.3)
    features = materialize_features(customer_payloads)
    feature_by_id = {str(f["raw_customer_id"]): f for f in features if f.get("raw_customer_id")}
    meta["features_count"] = len(features)

    pool_mult = float(limits.get("selection_pool_multiplier") or 3.0)
    max_pool = max(task_cap, int(task_cap * pool_mult))
    max_pool = min(max_pool, len(features))

    await _prog(phase="Phase B：全局选人", detail=f"pool≤{max_pool}", pct=0.38)
    selected_ids, quota_plan = select_customers_for_allocation(
        features,
        task_cap=task_cap,
        max_pool=max_pool,
        period_type=period_type,
    )
    meta["quota_plan"] = quota_plan
    meta["selected_count"] = len(selected_ids)

    selected_features = [feature_by_id[rid] for rid in selected_ids if rid in feature_by_id]
    batch_size = int(limits.get("llm_batch_size") or DEFAULT_LLM_BATCH_SIZE)
    char_budget = int(limits.get("prompt_char_budget") or PROMPT_CHAR_BUDGET)

    batches = split_feature_batches(
        selected_features,
        batch_size=batch_size,
        char_budget=char_budget,
    )
    meta["llm_batches"] = len(batches)

    all_candidates: list[dict[str, Any]] = []
    batch_meta_list: list[dict[str, Any]] = []

    for bi, feat_batch in enumerate(batches):
        cap_this = batch_task_cap(task_cap, bi, len(batches))
        await _prog(
            phase=f"Phase C：LLM 分批 {bi + 1}/{len(batches)}",
            detail=f"{len(feat_batch)} 客，本批 cap≤{cap_this}",
            pct=0.4 + 0.35 * (bi / max(1, len(batches))),
        )
        attempt = 0
        bs = len(feat_batch)
        raw_batch: list[dict[str, Any]] = []
        snap: dict[str, Any] = {}
        while attempt < 3:
            try:
                raw_batch, snap = await run_task_allocation_llm_batch(
                    db,
                    llm,
                    sales_wechat_id=sales_wechat_id,
                    period_type=period_type,
                    period_start=period_start,
                    period_end=period_end,
                    ref_today=ref_today,
                    task_cap=cap_this,
                    customer_features=feat_batch,
                )
                if raw_batch or not feat_batch:
                    break
            except Exception as e:
                snap["batch_error"] = str(e)
                logger.warning(
                    "任务分配分批 LLM 失败 sw={} batch={}/{} attempt={}: {}",
                    sales_wechat_id,
                    bi + 1,
                    len(batches),
                    attempt,
                    e,
                )
            attempt += 1
            new_bs = shrink_batch_params(bs, attempt)
            if new_bs >= bs:
                break
            feat_batch = feat_batch[:new_bs]
            bs = new_bs

        batch_meta_list.append(snap)
        for item in raw_batch:
            rid = str(item.get("raw_customer_id") or "").strip()
            if not rid:
                continue
            tb = str(item.get("time_window_bucket") or item.get("suggested_day") or "D0")
            if not str(tb).upper().startswith("D"):
                tb = "D0"
            all_candidates.append(
                {
                    "raw_customer_id": rid,
                    "title": item.get("title"),
                    "instruction": item.get("instruction"),
                    "task_kind": item.get("task_kind") or "contact",
                    "priority_score": item.get("priority_score"),
                    "priority_rank": item.get("priority_rank"),
                    "time_window_bucket": str(tb).upper()[:8],
                    "dedupe_key": item.get("dedupe_key")
                    or f"{rid}|{item.get('task_kind') or 'contact'}",
                    "reason_short": str(item.get("reason_short") or "")[:80],
                }
            )

    meta["llm_batch_meta"] = batch_meta_list
    meta["candidates_before_aggregate"] = len(all_candidates)

    await _prog(phase="全局聚合与排程", pct=0.82)
    aggregated, agg_metrics = aggregate_candidate_tasks(
        all_candidates,
        task_cap=task_cap,
        quota_plan=quota_plan,
        feature_by_id=feature_by_id,
        period_start=period_start,
        period_end=period_end,
        period_type=period_type,
    )
    meta["aggregator"] = agg_metrics

    # 对齐 normalize_llm_tasks 输入
    llm_rows = []
    for row in aggregated:
        llm_rows.append(
            {
                "raw_customer_id": row["raw_customer_id"],
                "title": row.get("title"),
                "instruction": row.get("instruction"),
                "task_kind": row.get("task_kind") or "contact",
                "priority_score": row.get("priority_score"),
                "priority_rank": row.get("priority_rank"),
                "_due_date": row.get("due_date"),
            }
        )

    normalized = normalize_llm_tasks(llm_rows, lookup, task_cap=task_cap)
    due_by_rid = {
        str(r["raw_customer_id"]): r.get("due_date")
        for r in aggregated
        if r.get("due_date")
    }
    for n in normalized:
        rid = str(n.get("raw_customer_id") or "")
        if rid in due_by_rid:
            n["_due_date"] = due_by_rid[rid]

    meta["tasks_after_normalize"] = len(normalized)
    meta["evaluation"] = build_evaluation_metrics(
        features=features,
        selected_ids=selected_ids,
        final_tasks=normalized,
        aggregator_metrics=agg_metrics,
        quota_plan=quota_plan,
    )
    return normalized, meta
