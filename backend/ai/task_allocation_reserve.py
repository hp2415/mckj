"""储备任务：从选人池超额客户生成规则任务（不依赖 LLM 溢出）。"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

from ai.task_allocation_aggregator import schedule_due_dates


def effective_reserve_cap(task_cap: int, limits: dict[str, Any]) -> int:
    if not limits.get("surplus_enabled"):
        return 0
    hard_cap = int(limits.get("reserve_cap") or 0)
    if hard_cap <= 0:
        return 0
    ratio = float(limits.get("surplus_ratio") or 0.5)
    soft_cap = max(0, int(math.ceil(max(0, int(task_cap)) * ratio)))
    return min(hard_cap, soft_cap)


def _display_name_from_feature(feat: dict[str, Any]) -> str:
    for key in ("customer_name", "unit_name"):
        name = str(feat.get(key) or "").strip()
        if name:
            return name
    return "客户"


def _display_name_from_payload(payload: dict[str, Any]) -> str:
    for key in ("customer_name", "wechat_remark", "unit_name"):
        name = str(payload.get(key) or "").strip()
        if name:
            return name
    return "客户"


def _instruction_from_feature(feat: dict[str, Any]) -> tuple[str, str]:
    recency = feat.get("recency") if isinstance(feat.get("recency"), dict) else {}
    strategy = str(recency.get("followup_strategy") or feat.get("next_best_action_hint") or "").strip()
    channel = str(recency.get("followup_channel") or "wechat").strip().lower()
    if channel not in ("wechat", "phone"):
        channel = "wechat"
    if not strategy:
        strategy = (
            "电话深沟通，确认需求并推进合作"
            if channel == "phone"
            else "微信轻触达，确认需求与跟进计划"
        )
    return strategy[:2000], channel


def _instruction_from_payload(payload: dict[str, Any]) -> tuple[str, str]:
    strategy = str(payload.get("followup_strategy") or "").strip()
    channel = str(payload.get("followup_channel") or "wechat").strip().lower()
    if channel not in ("wechat", "phone"):
        channel = "wechat"
    if not strategy:
        strategy = (
            "电话深沟通，确认需求并推进合作"
            if channel == "phone"
            else "微信轻触达，确认需求与跟进计划"
        )
    return strategy[:2000], channel


def build_reserve_candidates_from_features(
    feature_by_id: dict[str, dict[str, Any]],
    selected_ids: list[str],
    picked_rids: set[str],
    *,
    reserve_cap: int,
) -> list[dict[str, Any]]:
    """从 Phase B 选人池取未入主派的客户，生成规则储备任务。"""
    cap = max(0, int(reserve_cap))
    if cap <= 0:
        return []
    pool: list[dict[str, Any]] = []
    for rid in selected_ids:
        rid_s = str(rid or "").strip()
        if not rid_s or rid_s in picked_rids:
            continue
        feat = feature_by_id.get(rid_s)
        if feat:
            pool.append(feat)
    pool.sort(
        key=lambda f: (
            -float(f.get("rule_priority_score") or 0),
            str(f.get("raw_customer_id") or ""),
        )
    )
    rows: list[dict[str, Any]] = []
    for feat in pool:
        if len(rows) >= cap:
            break
        rid = str(feat.get("raw_customer_id") or "").strip()
        if not rid:
            continue
        name = _display_name_from_feature(feat)
        instruction, channel = _instruction_from_feature(feat)
        rows.append(
            {
                "raw_customer_id": rid,
                "title": f"储备跟进 · {name}"[:200],
                "instruction": instruction,
                "task_kind": "contact",
                "contact_channel": channel,
                "priority_score": float(feat.get("rule_priority_score") or 0),
                "time_window_bucket": "D0",
                "dedupe_key": f"{rid}|contact|{channel}|reserve",
                "_reserve_source": "selection_pool",
            }
        )
    return rows


def build_reserve_candidates_from_payloads(
    payloads: list[dict[str, Any]],
    picked_rids: set[str],
    *,
    reserve_cap: int,
) -> list[dict[str, Any]]:
    """非可扩展管线：从已排序 payload 池补储备任务。"""
    cap = max(0, int(reserve_cap))
    if cap <= 0 or not payloads:
        return []
    sorted_payloads = sorted(
        payloads,
        key=lambda p: (
            -float(p.get("rule_priority_score") or 0),
            str(p.get("raw_customer_id") or ""),
        ),
    )
    rows: list[dict[str, Any]] = []
    for p in sorted_payloads:
        if len(rows) >= cap:
            break
        rid = str(p.get("raw_customer_id") or "").strip()
        if not rid or rid in picked_rids:
            continue
        name = _display_name_from_payload(p)
        instruction, channel = _instruction_from_payload(p)
        rows.append(
            {
                "raw_customer_id": rid,
                "title": f"储备跟进 · {name}"[:200],
                "instruction": instruction,
                "task_kind": "contact",
                "contact_channel": channel,
                "priority_score": float(p.get("rule_priority_score") or 0),
                "time_window_bucket": "D0",
                "dedupe_key": f"{rid}|contact|{channel}|reserve",
                "_reserve_source": "payload_pool",
            }
        )
    return rows


def finalize_reserve_rows(
    reserved: list[dict[str, Any]],
    *,
    picked_count: int,
    period_start: date,
    period_end: date,
    period_type: str,
    reserve_cap: int,
) -> list[dict[str, Any]]:
    """截断、排 due_date、设置 priority_rank（接在主派之后）。"""
    cap = max(0, int(reserve_cap))
    if cap <= 0 or not reserved:
        return []
    out = reserved[:cap]
    schedule_due_dates(
        out,
        period_start=period_start,
        period_end=period_end,
        period_type=period_type,
    )
    rank_base = max(0, int(picked_count))
    for i, row in enumerate(out, start=1):
        row["priority_rank"] = rank_base + i
    return out


def merge_reserve_candidates(
    llm_reserved: list[dict[str, Any]],
    pool_reserved: list[dict[str, Any]],
    picked_rids: set[str],
    *,
    reserve_cap: int,
) -> list[dict[str, Any]]:
    """合并 LLM 溢出与选人池储备，按分数去重截断。"""
    cap = max(0, int(reserve_cap))
    if cap <= 0:
        return []
    merged: list[dict[str, Any]] = []
    seen: set[str] = set(picked_rids)
    for item in sorted(
        list(llm_reserved or []) + list(pool_reserved or []),
        key=lambda x: (-float(x.get("priority_score") or 0), str(x.get("raw_customer_id") or "")),
    ):
        rid = str(item.get("raw_customer_id") or "").strip()
        if not rid or rid in seen:
            continue
        merged.append(item)
        seen.add(rid)
        if len(merged) >= cap:
            break
    return merged
