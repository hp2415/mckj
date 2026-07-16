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
from ai.task_allocation_features import materialize_features, apply_profile_channel_authority, apply_profile_strategy_fallback
from ai.task_allocation_llm import (
    normalize_llm_tasks,
    run_task_allocation_llm_batch,
)
from ai.task_allocation_limits import channel_caps_for_period, main_channel_floor_caps, scale_channel_caps_to_task_cap
from ai.task_allocation_eval import build_evaluation_metrics
from ai.task_allocation_selection import select_customers_for_allocation
from ai.task_allocation_ranking import build_alloc_feature_snapshot, resolve_scoring_weights
from ai.task_allocation_reserve import (
    build_reserve_candidates_from_features,
    effective_reserve_cap,
    finalize_reserve_rows,
    merge_reserve_candidates,
    top_up_main_rows_to_channel_floors,
)
from core.logger import logger


def _normalize_aggregated_rows(
    aggregated_rows: list[dict[str, Any]],
    *,
    feature_by_id: dict[str, dict[str, Any]],
    exploration_ids: set[str],
    lookup: dict[str, tuple[Any, Any]],
    task_cap: int,
    wechat_cap: int,
    phone_cap: int,
    limits: dict[str, Any] | None = None,
    pool_tier: str | None = None,
) -> list[dict[str, Any]]:
    limits = limits or {}
    cap_for_phone = len(aggregated_rows) if pool_tier == "reserve" else phone_cap
    rows_for_norm, authority_meta = apply_profile_channel_authority(
        aggregated_rows,
        limits,
        phone_cap=cap_for_phone,
        feature_by_id=feature_by_id,
    )
    llm_rows: list[dict[str, Any]] = []
    for row in rows_for_norm:
        rid = str(row.get("raw_customer_id") or "")
        feat = feature_by_id.get(rid) or {}
        breakdown = feat.get("_score_breakdown") or {}
        row["_alloc_feature"] = build_alloc_feature_snapshot(
            raw_customer_id=rid,
            breakdown=breakdown,
            extra={
                "exploration": rid in exploration_ids,
                "blended_priority_score": row.get("priority_score"),
                "llm_priority_score": row.get("_llm_priority_score"),
            },
        )
        llm_rows.append(
            {
                "raw_customer_id": row["raw_customer_id"],
                "title": row.get("title"),
                "instruction": row.get("instruction"),
                "task_kind": row.get("task_kind") or "contact",
                "contact_channel": row.get("contact_channel") or "wechat",
                "priority_score": row.get("priority_score"),
                "priority_rank": row.get("priority_rank"),
                "_due_date": row.get("due_date"),
            }
        )

    cap = len(llm_rows) if pool_tier == "reserve" else task_cap
    w_cap = len(llm_rows) if pool_tier == "reserve" else wechat_cap
    p_cap = len(llm_rows) if pool_tier == "reserve" else phone_cap
    normalized = normalize_llm_tasks(
        llm_rows,
        lookup,
        task_cap=max(1, cap) if cap > 0 else 0,
        wechat_cap=w_cap,
        phone_cap=p_cap,
    )
    due_by_rid = {
        str(r["raw_customer_id"]): r.get("due_date")
        for r in aggregated_rows
        if r.get("due_date")
    }
    alloc_by_rid = {
        str(r["raw_customer_id"]): r.get("_alloc_feature")
        for r in rows_for_norm
        if r.get("_alloc_feature")
    }
    rank_by_rid = {
        str(r["raw_customer_id"]): int(r.get("priority_rank") or 0)
        for r in aggregated_rows
        if r.get("raw_customer_id")
    }
    for n in normalized:
        rid = str(n.get("raw_customer_id") or "")
        feat = feature_by_id.get(rid) or {}
        if rid in due_by_rid:
            n["_due_date"] = due_by_rid[rid]
        if rid in alloc_by_rid:
            n["_alloc_feature"] = alloc_by_rid[rid]
        n["_profile_followup_channel"] = feat.get("_profile_followup_channel")
        n["_profile_followup_strategy"] = feat.get("_profile_followup_strategy")
        n["_profile_followup_date"] = feat.get("_profile_followup_date")
        if pool_tier == "reserve":
            n["_pool_tier"] = "reserve"
            if rid in rank_by_rid and rank_by_rid[rid] > 0:
                n["priority_rank"] = rank_by_rid[rid]
        if rid in authority_meta.get("overridden_rids", []):
            n["_channel_authority_applied"] = True
    if pool_tier != "reserve":
        normalized, _strategy_meta = apply_profile_strategy_fallback(
            normalized,
            limits,
            feature_by_id=feature_by_id,
            skip_reserve=True,
        )
    return normalized


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
    wechat_cap: int | None = None,
    phone_cap: int | None = None,
    base_wechat_cap: int | None = None,
    base_phone_cap: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    返回 (normalize 前的 enriched rows 供写库, pipeline_meta)。
    wechat_cap/phone_cap 为有效目标（含自适应）；未传则回退到配置上限。
    """
    cfg_wechat, cfg_phone = channel_caps_for_period(period_type, limits)
    if wechat_cap is None:
        wechat_cap = cfg_wechat
    if phone_cap is None:
        phone_cap = cfg_phone
    wechat_cap = int(wechat_cap)
    phone_cap = int(phone_cap)
    base_w = int(base_wechat_cap if base_wechat_cap is not None else cfg_wechat)
    base_p = int(base_phone_cap if base_phone_cap is not None else cfg_phone)
    meta: dict[str, Any] = {
        "pipeline": "scalable",
        "phase_a_count": len(customer_payloads),
        "wechat_cap": wechat_cap,
        "phone_cap": phone_cap,
        "base_wechat_cap": base_w,
        "base_phone_cap": base_p,
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
        wechat_cap=wechat_cap,
        phone_cap=phone_cap,
        ref_today=ref_today,
        limits=limits,
    )
    meta["quota_plan"] = quota_plan
    meta["selected_count"] = len(selected_ids)
    reserve_cap = effective_reserve_cap(task_cap, limits)
    quota_plan["reserve_cap"] = reserve_cap
    meta["reserve_cap"] = reserve_cap
    meta["surplus_enabled"] = bool(limits.get("surplus_enabled"))

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
        w_this, p_this = scale_channel_caps_to_task_cap(cap_this, wechat_cap, phone_cap)
        await _prog(
            phase=f"Phase C：LLM 分批 {bi + 1}/{len(batches)}",
            detail=f"{len(feat_batch)} 客，本批 cap≤{cap_this}（wx≤{w_this} ph≤{p_this}）",
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
                    wechat_cap=w_this,
                    phone_cap=p_this,
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
                    "contact_channel": item.get("contact_channel") or "wechat",
                    "priority_score": item.get("priority_score"),
                    "priority_rank": item.get("priority_rank"),
                    "time_window_bucket": str(tb).upper()[:8],
                    "dedupe_key": item.get("dedupe_key")
                    or f"{rid}|{item.get('task_kind') or 'contact'}|{item.get('contact_channel') or 'wechat'}",
                    "reason_short": str(item.get("reason_short") or "")[:80],
                }
            )

    meta["llm_batch_meta"] = batch_meta_list
    meta["candidates_before_aggregate"] = len(all_candidates)

    scoring_weights = resolve_scoring_weights(limits)
    exploration_ids = set(quota_plan.get("exploration_ids") or [])

    await _prog(phase="全局聚合与排程", pct=0.82)
    aggregated, llm_reserved, agg_metrics = aggregate_candidate_tasks(
        all_candidates,
        task_cap=task_cap,
        quota_plan=quota_plan,
        feature_by_id=feature_by_id,
        period_start=period_start,
        period_end=period_end,
        period_type=period_type,
        scoring_weights=scoring_weights,
    )
    picked_rids = {str(r.get("raw_customer_id") or "").strip() for r in aggregated if r.get("raw_customer_id")}
    pool_reserved = build_reserve_candidates_from_features(
        feature_by_id,
        selected_ids,
        picked_rids,
        reserve_cap=reserve_cap,
    )
    reserved = finalize_reserve_rows(
        merge_reserve_candidates(llm_reserved, pool_reserved, picked_rids, reserve_cap=reserve_cap),
        picked_count=len(aggregated),
        period_start=period_start,
        period_end=period_end,
        period_type=period_type,
        reserve_cap=reserve_cap,
    )
    agg_metrics["reserve_from_llm"] = len(llm_reserved)
    agg_metrics["reserve_from_pool"] = len(pool_reserved)
    agg_metrics["reserve_out"] = len(reserved)
    meta["aggregator"] = agg_metrics
    if reserve_cap > 0 and not reserved:
        logger.info(
            "储备任务为空 sw={} cap={} selected={} llm_candidates={} picked={}",
            sales_wechat_id,
            reserve_cap,
            len(selected_ids),
            len(all_candidates),
            len(aggregated),
        )

    normalized = _normalize_aggregated_rows(
        aggregated,
        feature_by_id=feature_by_id,
        exploration_ids=exploration_ids,
        lookup=lookup,
        task_cap=task_cap,
        wechat_cap=wechat_cap,
        phone_cap=phone_cap,
        limits=limits,
    )
    normalized_reserve: list[dict[str, Any]] = []
    if reserved:
        normalized_reserve = _normalize_aggregated_rows(
            reserved,
            feature_by_id=feature_by_id,
            exploration_ids=exploration_ids,
            lookup=lookup,
            task_cap=len(reserved),
            wechat_cap=len(reserved),
            phone_cap=len(reserved),
            limits=limits,
            pool_tier="reserve",
        )

    meta["tasks_after_normalize"] = len(normalized)
    meta["reserve_after_normalize"] = len(normalized_reserve)
    meta["selected_ids"] = list(selected_ids)

    w_floor, p_floor = main_channel_floor_caps(base_w, base_p, limits)
    # 目标取有效 cap 与下限的较大者，确保不足时规则补齐到至少 60% 上限
    normalized, normalized_reserve, floor_meta = top_up_main_rows_to_channel_floors(
        normalized,
        wechat_target=wechat_cap,
        phone_target=phone_cap,
        wechat_floor=w_floor,
        phone_floor=p_floor,
        lookup=lookup,
        feature_by_id=feature_by_id,
        selected_ids=selected_ids,
        payloads=customer_payloads,
        reserve_rows=normalized_reserve,
    )
    meta["channel_floor_topup"] = floor_meta
    if floor_meta.get("applied"):
        logger.info(
            "主线渠道下限补齐 sw={} +wx={} +ph={} final={}/{} floor={}/{} met={}",
            sales_wechat_id,
            floor_meta.get("added_wechat"),
            floor_meta.get("added_phone"),
            floor_meta.get("wechat_final"),
            floor_meta.get("phone_final"),
            w_floor,
            p_floor,
            floor_meta.get("floor_met"),
        )
        # 补齐后重排储备 rank
        if normalized_reserve:
            normalized_reserve = finalize_reserve_rows(
                normalized_reserve,
                picked_count=len(normalized),
                period_start=period_start,
                period_end=period_end,
                period_type=period_type,
                reserve_cap=reserve_cap,
            )
    meta["tasks_after_normalize"] = len(normalized)
    meta["reserve_after_normalize"] = len(normalized_reserve)

    meta["evaluation"] = build_evaluation_metrics(
        features=features,
        selected_ids=selected_ids,
        final_tasks=normalized,
        aggregator_metrics=agg_metrics,
        quota_plan=quota_plan,
        exploration_ids=list(exploration_ids),
    )
    return normalized + normalized_reserve, meta
