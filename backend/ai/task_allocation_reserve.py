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


def _count_by_channel(rows: list[dict[str, Any]]) -> tuple[int, int]:
    w = sum(1 for r in rows if (r.get("contact_channel") or "wechat") != "phone")
    p = sum(1 for r in rows if (r.get("contact_channel") or "") == "phone")
    return w, p


def _rule_main_row_from_feature(feat: dict[str, Any], *, channel: str) -> dict[str, Any]:
    rid = str(feat.get("raw_customer_id") or "").strip()
    name = _display_name_from_feature(feat)
    instruction, preferred = _instruction_from_feature(feat)
    ch = channel if channel in ("wechat", "phone") else preferred
    if ch == "phone":
        title = f"电话跟进 · {name}"[:200]
        if "电话" not in instruction:
            instruction = f"电话触达：{instruction}"[:2000]
    else:
        title = f"微信跟进 · {name}"[:200]
    return {
        "raw_customer_id": rid,
        "title": title,
        "instruction": instruction,
        "task_kind": "contact",
        "contact_channel": ch,
        "priority_score": float(feat.get("rule_priority_score") or 0),
        "time_window_bucket": "D0",
        "dedupe_key": f"{rid}|contact|{ch}|floor_topup",
        "_floor_topup": True,
        "_profile_followup_channel": feat.get("_profile_followup_channel"),
        "_profile_followup_strategy": feat.get("_profile_followup_strategy"),
        "_profile_followup_date": feat.get("_profile_followup_date"),
    }


def _rule_main_row_from_payload(payload: dict[str, Any], *, channel: str) -> dict[str, Any]:
    rid = str(payload.get("raw_customer_id") or "").strip()
    name = _display_name_from_payload(payload)
    instruction, preferred = _instruction_from_payload(payload)
    ch = channel if channel in ("wechat", "phone") else preferred
    if ch == "phone":
        title = f"电话跟进 · {name}"[:200]
        if "电话" not in instruction:
            instruction = f"电话触达：{instruction}"[:2000]
    else:
        title = f"微信跟进 · {name}"[:200]
    return {
        "raw_customer_id": rid,
        "title": title,
        "instruction": instruction,
        "task_kind": "contact",
        "contact_channel": ch,
        "priority_score": float(payload.get("rule_priority_score") or 0),
        "time_window_bucket": "D0",
        "dedupe_key": f"{rid}|contact|{ch}|floor_topup",
        "_floor_topup": True,
    }


def top_up_main_rows_to_channel_floors(
    main_rows: list[dict[str, Any]],
    *,
    wechat_target: int,
    phone_target: int,
    wechat_floor: int,
    phone_floor: int,
    lookup: dict[str, tuple[Any, Any]] | None = None,
    feature_by_id: dict[str, dict[str, Any]] | None = None,
    selected_ids: list[str] | None = None,
    payloads: list[dict[str, Any]] | None = None,
    reserve_rows: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """
    LLM/聚合产出不足时，用规则任务补齐到渠道下限（并尽量贴近有效目标）。
    目标取 max(floor, min(target, ...))：每个渠道至少到 floor，且不超过 target。
    补齐来源优先：储备池 → Phase B 选人池未入选 → 全量特征/payload。
    """
    rows = list(main_rows or [])
    reserved = list(reserve_rows or [])
    lookup = lookup or {}
    feature_by_id = feature_by_id or {}
    selected_ids = list(selected_ids or [])
    payloads = list(payloads or [])

    w_floor = max(0, int(wechat_floor))
    p_floor = max(0, int(phone_floor))
    w_target = max(w_floor, max(0, int(wechat_target)))
    p_target = max(p_floor, max(0, int(phone_target)))

    meta: dict[str, Any] = {
        "wechat_floor": w_floor,
        "phone_floor": p_floor,
        "wechat_target": w_target,
        "phone_target": p_target,
        "added_wechat": 0,
        "added_phone": 0,
        "promoted_from_reserve": 0,
        "from_features": 0,
        "from_payloads": 0,
    }

    def _need(channel: str) -> int:
        w, p = _count_by_channel(rows)
        if channel == "phone":
            return max(0, p_target - p)
        return max(0, w_target - w)

    if _need("wechat") <= 0 and _need("phone") <= 0:
        meta["applied"] = False
        return rows, reserved, meta

    picked = {str(r.get("raw_customer_id") or "").strip() for r in rows if r.get("raw_customer_id")}

    def _usable(rid: str) -> bool:
        rid = str(rid or "").strip()
        if not rid or rid in picked:
            return False
        if not lookup:
            return True
        pair = lookup.get(rid)
        if not pair:
            return False
        scp, rc = pair
        return rc is not None and scp is not None

    def _append(row: dict[str, Any], *, source: str) -> bool:
        rid = str(row.get("raw_customer_id") or "").strip()
        if not _usable(rid):
            return False
        ch = "phone" if (row.get("contact_channel") or "") == "phone" else "wechat"
        if _need(ch) <= 0:
            return False
        rows.append(row)
        picked.add(rid)
        if ch == "phone":
            meta["added_phone"] += 1
        else:
            meta["added_wechat"] += 1
        meta[source] = int(meta.get(source) or 0) + 1
        return True

    # 1) 从储备上提（改渠道标签以匹配缺口）
    still_reserve: list[dict[str, Any]] = []
    for item in reserved:
        w_need, p_need = _need("wechat"), _need("phone")
        if w_need <= 0 and p_need <= 0:
            still_reserve.append(item)
            continue
        rid = str(item.get("raw_customer_id") or "").strip()
        if not _usable(rid):
            still_reserve.append(item)
            continue
        prefer = "phone" if p_need > w_need else "wechat"
        if prefer == "phone" and p_need <= 0:
            prefer = "wechat"
        if prefer == "wechat" and w_need <= 0:
            prefer = "phone"
        promoted = {**item, "contact_channel": prefer, "_floor_topup": True, "_pool_tier": None}
        title = str(promoted.get("title") or "")
        if prefer == "phone" and "电话" not in title:
            promoted["title"] = f"电话跟进 · {title.replace('储备跟进 · ', '')}"[:200]
        elif prefer == "wechat" and title.startswith("储备跟进"):
            promoted["title"] = title.replace("储备跟进", "微信跟进", 1)[:200]
        if _append(promoted, source="promoted_from_reserve"):
            continue
        still_reserve.append(item)
    reserved = still_reserve

    # 2) 特征池：选人未入选 → 其余特征
    feat_pool: list[dict[str, Any]] = []
    seen_feat: set[str] = set()
    for rid in selected_ids:
        rid_s = str(rid or "").strip()
        if not rid_s or rid_s in picked or rid_s in seen_feat:
            continue
        feat = feature_by_id.get(rid_s)
        if feat:
            feat_pool.append(feat)
            seen_feat.add(rid_s)
    for rid_s, feat in feature_by_id.items():
        if rid_s in picked or rid_s in seen_feat:
            continue
        feat_pool.append(feat)
        seen_feat.add(rid_s)
    feat_pool.sort(
        key=lambda f: (-float(f.get("rule_priority_score") or 0), str(f.get("raw_customer_id") or ""))
    )
    for feat in feat_pool:
        if _need("wechat") <= 0 and _need("phone") <= 0:
            break
        prefer = "phone" if _need("phone") > _need("wechat") else "wechat"
        if prefer == "phone" and _need("phone") <= 0:
            prefer = "wechat"
        if prefer == "wechat" and _need("wechat") <= 0:
            prefer = "phone"
        _append(_rule_main_row_from_feature(feat, channel=prefer), source="from_features")

    # 3) payload 兜底（非可扩展 / 特征不足）
    if payloads and (_need("wechat") > 0 or _need("phone") > 0):
        sorted_payloads = sorted(
            payloads,
            key=lambda p: (-float(p.get("rule_priority_score") or 0), str(p.get("raw_customer_id") or "")),
        )
        for p in sorted_payloads:
            if _need("wechat") <= 0 and _need("phone") <= 0:
                break
            prefer = "phone" if _need("phone") > _need("wechat") else "wechat"
            if prefer == "phone" and _need("phone") <= 0:
                prefer = "wechat"
            if prefer == "wechat" and _need("wechat") <= 0:
                prefer = "phone"
            _append(_rule_main_row_from_payload(p, channel=prefer), source="from_payloads")

    # 重排主派 rank：原 LLM/聚合任务优先，规则补齐在后
    rows.sort(
        key=lambda r: (
            1 if r.get("_floor_topup") else 0,
            -float(r.get("priority_score") or 0),
            str(r.get("raw_customer_id") or ""),
        )
    )
    for i, row in enumerate(rows, start=1):
        row["priority_rank"] = i

    w_final, p_final = _count_by_channel(rows)
    meta["applied"] = (meta["added_wechat"] + meta["added_phone"]) > 0
    meta["wechat_final"] = w_final
    meta["phone_final"] = p_final
    meta["floor_met"] = w_final >= w_floor and p_final >= p_floor
    return rows, reserved, meta


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
