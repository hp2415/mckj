"""
Phase C 之后：全局聚合器 — 去重、配额、时间窗排程、公平性裁剪。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from math import ceil
from typing import Any

from ai.task_allocation_limits import CONTACT_CHANNEL_PHONE, CONTACT_CHANNEL_WECHAT
from ai.task_allocation_ranking import (
    blend_priority_scores,
    normalize_abc_grade,
    resolve_scoring_weights,
)


def _exploration_final_seats(cap: int, quota_plan: dict[str, Any] | None) -> tuple[int, set[str], float]:
    """
    计算最终任务中为探索客户预留的席位数。
    返回 (seats, exploration_ids, ratio)。
    """
    qp = quota_plan or {}
    exploration_ids = {
        str(x).strip() for x in (qp.get("exploration_ids") or []) if str(x).strip()
    }
    try:
        ratio = float(qp.get("exploration_ratio") or 0.0)
    except (TypeError, ValueError):
        ratio = 0.0
    ratio = max(0.0, min(0.3, ratio))
    if cap <= 0 or not exploration_ids or ratio <= 0:
        return 0, exploration_ids, ratio
    seats = int(ceil(cap * ratio))
    seats = max(0, min(seats, cap, len(exploration_ids)))
    return seats, exploration_ids, ratio


def _effective_priority_score(
    item: dict[str, Any],
    feat_map: dict[str, dict],
    scoring_weights: dict[str, float] | None,
) -> float:
    rid = str(item.get("raw_customer_id") or "")
    feat = feat_map.get(rid) or {}
    rule_score = feat.get("rule_priority_score")
    llm_score = item.get("priority_score")
    llm_raw = llm_score
    threshold = float((scoring_weights or {}).get("rule_llm_deviation_threshold", 25.0))
    deviation = False
    if rule_score is not None and llm_score is not None:
        try:
            if abs(float(rule_score) - float(llm_score)) > threshold:
                deviation = True
                item["_score_deviation"] = True
        except (TypeError, ValueError):
            pass

    blend_weights = scoring_weights
    abc_grade = normalize_abc_grade(feat.get("abc_grade"))
    if (
        deviation
        and abc_grade
        and (scoring_weights or {}).get("__structured_authority")
    ):
        blend_weights = {
            **(scoring_weights or {}),
            "rule_llm_blend_rule": 0.7,
            "rule_llm_blend_llm": 0.3,
        }
        item["_structured_authority_blend"] = True

    blended = blend_priority_scores(
        rule_score=rule_score,
        llm_score=llm_score,
        weights=blend_weights,
    )
    item["_blended_priority_score"] = blended
    item["_rule_priority_score"] = rule_score
    item["_llm_priority_score"] = llm_raw
    return blended


def _normalize_contact_channel(item: dict[str, Any]) -> str:
    ch = str(item.get("contact_channel") or CONTACT_CHANNEL_WECHAT).strip().lower()
    return ch if ch in (CONTACT_CHANNEL_WECHAT, CONTACT_CHANNEL_PHONE) else CONTACT_CHANNEL_WECHAT


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
    scoring_weights: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """
    输入多批 LLM 候选，输出 (主派任务 <= task_cap, 储备任务, 评估指标)。
    储备条数由 quota_plan.reserve_cap 控制；为 0 时不产出储备。
    """
    cap = max(0, int(task_cap))
    metrics: dict[str, Any] = {
        "candidates_in": len(candidates),
        "duplicates_removed": 0,
        "discarded": [],
    }

    # 去重：同 dedupe_key 保留融合分最高
    best: dict[str, dict[str, Any]] = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("raw_customer_id") or "").strip()
        if not rid:
            continue
        key = _task_dedupe_key(item)
        ps = _effective_priority_score(item, feature_by_id, scoring_weights)
        prev = best.get(key)
        if prev is None or ps > float(prev.get("_blended_priority_score") or 0):
            if prev is not None:
                metrics["duplicates_removed"] += 1
            best[key] = {**item, "raw_customer_id": rid, "priority_score": ps}
        else:
            metrics["duplicates_removed"] += 1

    unique = list(best.values())
    phone_preferred = set((quota_plan or {}).get("phone_preferred_ids") or [])
    unique.sort(
        key=lambda x: (
            -float(x.get("priority_score") or 0),
            0 if str(x.get("raw_customer_id")) in phone_preferred else 1,
            str(x.get("raw_customer_id") or ""),
        )
    )

    exploration_seats, exploration_ids, exploration_ratio = _exploration_final_seats(
        cap, quota_plan
    )
    main_cap = max(0, cap - exploration_seats)
    metrics["exploration_ratio"] = exploration_ratio
    metrics["exploration_seats_reserved"] = exploration_seats
    metrics["exploration_ids_count"] = len(exploration_ids)

    # 渠道配额裁剪；探索席位预留给 exploration_ids，避免被高分精英占满
    channel_targets = (quota_plan or {}).get("by_contact_channel") or {}
    bucket_counts: dict[str, int] = defaultdict(int)
    channel_counts: dict[str, int] = defaultdict(int)
    picked: list[dict[str, Any]] = []
    picked_keys: set[str] = set()

    def _channel_ok(item: dict[str, Any]) -> bool:
        ch = _normalize_contact_channel(item)
        target = int(channel_targets.get(ch, cap))
        return channel_counts[ch] < target

    def _try_pick(item: dict[str, Any]) -> bool:
        if len(picked) >= cap:
            return False
        rid = str(item.get("raw_customer_id") or "").strip()
        key = _task_dedupe_key(item)
        if not rid or key in picked_keys:
            return False
        if not _channel_ok(item):
            return False
        ch = _normalize_contact_channel(item)
        bucket = _bucket_for_customer(feature_by_id, rid)
        picked.append({**item, "contact_channel": ch, "_exploration": rid in exploration_ids})
        picked_keys.add(key)
        bucket_counts[bucket] += 1
        channel_counts[ch] += 1
        return True

    # Phase 1：主席位填非探索客户（按 blended 分）
    main_picked = 0
    for item in unique:
        if main_picked >= main_cap:
            break
        rid = str(item.get("raw_customer_id") or "").strip()
        if rid in exploration_ids:
            continue
        if _try_pick(item):
            main_picked += 1
        else:
            metrics["discarded"].append({"raw_customer_id": rid, "reason": "channel_quota"})

    # Phase 2：探索保底席（仅 exploration_ids）
    exploration_filled = 0
    for item in unique:
        if exploration_filled >= exploration_seats or len(picked) >= cap:
            break
        rid = str(item.get("raw_customer_id") or "").strip()
        if rid not in exploration_ids:
            continue
        if _try_pick(item):
            exploration_filled += 1

    # Phase 3：席位未满则按分回填（含未入选的探索/主线）
    for item in unique:
        if len(picked) >= cap:
            break
        if _task_dedupe_key(item) in picked_keys:
            continue
        if not _try_pick(item):
            rid = str(item.get("raw_customer_id") or "").strip()
            metrics["discarded"].append({"raw_customer_id": rid, "reason": "backfill_skip"})

    metrics["exploration_seats_filled"] = sum(1 for p in picked if p.get("_exploration"))
    metrics["exploration_ids_in_final"] = [
        str(p.get("raw_customer_id")) for p in picked if p.get("_exploration")
    ][:20]
    for row in picked:
        row.pop("_exploration", None)

    schedule_due_dates(
        picked,
        period_start=period_start,
        period_end=period_end,
        period_type=period_type,
    )

    for i, row in enumerate(picked, start=1):
        row["priority_rank"] = i

    reserve_cap = int((quota_plan or {}).get("reserve_cap") or 0)
    reserve: list[dict[str, Any]] = []
    picked_rids = {str(r.get("raw_customer_id") or "").strip() for r in picked}
    if reserve_cap > 0:
        for item in unique:
            if len(reserve) >= reserve_cap:
                break
            rid = str(item.get("raw_customer_id") or "").strip()
            if not rid or rid in picked_rids:
                continue
            ch = _normalize_contact_channel(item)
            reserve.append({**item, "contact_channel": ch})
            picked_rids.add(rid)
        if reserve:
            schedule_due_dates(
                reserve,
                period_start=period_start,
                period_end=period_end,
                period_type=period_type,
            )
            rank_base = len(picked)
            for i, row in enumerate(reserve, start=1):
                row["priority_rank"] = rank_base + i

    metrics["tasks_out"] = len(picked)
    metrics["reserve_out"] = len(reserve)
    metrics["bucket_counts"] = dict(bucket_counts)
    metrics["channel_counts"] = dict(channel_counts)
    kind_counts: dict[str, int] = defaultdict(int)
    for r in picked:
        kind_counts[str(r.get("task_kind") or "contact")] += 1
    metrics["task_kind_distribution"] = dict(kind_counts)
    channel_dist: dict[str, int] = defaultdict(int)
    for r in picked:
        channel_dist[_normalize_contact_channel(r)] += 1
    metrics["contact_channel_distribution"] = dict(channel_dist)
    deviation_count = sum(1 for r in picked if r.get("_score_deviation"))
    metrics["score_deviation_count"] = deviation_count
    metrics["score_deviation_tasks"] = [
        {
            "raw_customer_id": r.get("raw_customer_id"),
            "rule": r.get("_rule_priority_score"),
            "blended": r.get("priority_score"),
        }
        for r in picked
        if r.get("_score_deviation")
    ][:20]

    return picked[:cap], reserve, metrics
