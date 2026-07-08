"""
Phase B：全局选人 + 配额计划（规则为主，不调用 LLM）。
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any

from datetime import date

from ai.task_allocation_limits import CONTACT_CHANNEL_PHONE, CONTACT_CHANNEL_WECHAT
from ai.task_allocation_ranking import phone_channel_sort_key, should_skip_repeat_contact_today

SELECTION_POOL_MULTIPLIER = float(os.getenv("TASK_SELECTION_POOL_MULTIPLIER") or "3.0")
MIN_PER_BUCKET = int(os.getenv("TASK_SELECTION_MIN_PER_BUCKET") or "1")


def _parse_followup_date_from_feature(feat: dict[str, Any]) -> date | None:
    recency = feat.get("recency") if isinstance(feat.get("recency"), dict) else {}
    raw = recency.get("suggested_followup_date") or feat.get("_profile_followup_date")
    if hasattr(raw, "isoformat"):
        try:
            return raw if isinstance(raw, date) else raw.date()
        except (TypeError, ValueError, AttributeError):
            return None
    s = str(raw or "")[:10]
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _pick_due_guaranteed_ids(
    features: list[dict[str, Any]],
    *,
    ref: date,
    task_cap: int,
    selected_set: set[str],
) -> list[str]:
    """到期/过期客户保底进池（在 max_pool 之外额外追加）。"""
    cap = max(1, int(task_cap))
    guarantee_cap = min(max(1, cap // 2), len(features))
    due_feats: list[dict[str, Any]] = []
    for f in features:
        fd = _parse_followup_date_from_feature(f)
        if fd is not None and fd <= ref:
            due_feats.append(f)
    due_feats.sort(
        key=lambda f: (
            -float(f.get("rule_priority_score") or 0),
            str(f.get("raw_customer_id") or ""),
        )
    )
    out: list[str] = []
    for f in due_feats:
        if len(out) >= guarantee_cap:
            break
        rid = str(f.get("raw_customer_id") or "").strip()
        if not rid or rid in selected_set:
            continue
        out.append(rid)
        selected_set.add(rid)
    return out


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
    wechat_cap: int | None = None,
    phone_cap: int | None = None,
) -> dict[str, Any]:
    """按主标签分桶的目标配额（阶段/标签）+ 渠道配额。"""
    cap = max(1, int(task_cap))
    w_cap = int(wechat_cap) if wechat_cap is not None else cap
    p_cap = int(phone_cap) if phone_cap is not None else 0
    from ai.task_allocation_limits import scale_channel_caps_to_task_cap

    if w_cap + p_cap > cap:
        w_cap, p_cap = scale_channel_caps_to_task_cap(cap, w_cap, p_cap)
    buckets: dict[str, int] = defaultdict(int)
    for f in features:
        buckets[_primary_bucket(f)] += 1
    if not buckets:
        return {
            "by_stage_tag": {},
            "by_contact_channel": {CONTACT_CHANNEL_WECHAT: w_cap, CONTACT_CHANNEL_PHONE: p_cap},
            "task_cap": cap,
            "period_type": period_type,
        }

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
        "by_contact_channel": {
            CONTACT_CHANNEL_WECHAT: w_cap,
            CONTACT_CHANNEL_PHONE: p_cap,
        },
        "task_cap": cap,
        "period_type": period_type,
    }


def _eligible_for_daily(f: dict[str, Any], ref: date, limits: dict[str, Any] | None) -> bool:
    recency = f.get("recency") or {}
    voice = recency.get("contact_voice") or {}
    return not should_skip_repeat_contact_today(
        f.get("recent_tasks"),
        ref,
        contact_voice_summary=voice if voice else None,
        limits=limits,
    )


def _pick_exploration_ids(
    sorted_feats: list[dict[str, Any]],
    *,
    exploration_count: int,
    selected_set: set[str],
) -> list[str]:
    """从中低分且久未触达客户中选探索位。"""
    if exploration_count <= 0:
        return []
    candidates: list[dict[str, Any]] = []
    for f in sorted_feats:
        score = float(f.get("rule_priority_score") or 0)
        days = (f.get("recency") or {}).get("days_since_last_main_task")
        if days is None:
            days = (f.get("_score_breakdown") or {}).get("days_since_last_main_task")
        try:
            days_i = int(days) if days is not None else 999
        except (TypeError, ValueError):
            days_i = 999
        if score < 55 or days_i >= 14:
            candidates.append(f)
    candidates.sort(
        key=lambda x: (
            float(x.get("rule_priority_score") or 0),
            -int((x.get("recency") or {}).get("days_since_last_main_task") or 0),
        )
    )
    out: list[str] = []
    for f in candidates:
        rid = str(f.get("raw_customer_id") or "").strip()
        if rid and rid not in selected_set:
            out.append(rid)
            selected_set.add(rid)
        if len(out) >= exploration_count:
            break
    return out


def select_customers_for_allocation(
    features: list[dict[str, Any]],
    *,
    task_cap: int,
    max_pool: int | None = None,
    period_type: str = "daily",
    wechat_cap: int | None = None,
    phone_cap: int | None = None,
    ref_today: date | None = None,
    limits: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """
    从全量特征中选出进入 Phase C 的 raw_customer_id 列表（TopK）。
    返回 (selected_ids, quota_plan)。
    """
    cap = max(1, int(task_cap))
    pool_k = max_pool if max_pool is not None else max(cap, int(cap * SELECTION_POOL_MULTIPLIER))
    pool_k = min(pool_k, len(features))

    ref = ref_today or date.today()
    exploration_ratio = float((limits or {}).get("exploration_ratio") or 0.0)
    exploration_count = int(round(pool_k * exploration_ratio)) if exploration_ratio > 0 else 0

    def _eligible(f: dict[str, Any]) -> bool:
        if period_type != "daily":
            return True
        return _eligible_for_daily(f, ref, limits)

    eligible_feats = [f for f in features if _eligible(f)]
    sorted_feats = sorted(
        eligible_feats,
        key=lambda f: (
            -float(f.get("rule_priority_score") or 0),
            str(f.get("raw_customer_id") or ""),
        ),
    )
    quota_plan = build_quota_plan(
        sorted_feats[:pool_k],
        task_cap=cap,
        period_type=period_type,
        wechat_cap=wechat_cap,
        phone_cap=phone_cap,
    )

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

    # 按分数补齐至 pool_k（扣除探索位）
    fill_target = max(0, pool_k - exploration_count)
    for f in sorted_feats:
        if len(selected) >= fill_target:
            break
        rid = str(f.get("raw_customer_id") or "").strip()
        if rid and rid not in selected_set:
            selected.append(rid)
            selected_set.add(rid)

    # 探索位
    exploration_ids = _pick_exploration_ids(
        sorted_feats,
        exploration_count=exploration_count,
        selected_set=selected_set,
    )
    for rid in exploration_ids:
        if len(selected) < pool_k:
            selected.append(rid)

    # 电话优选客户 id（供聚合器参考）
    phone_pref_sorted = sorted(
        [f for f in sorted_feats if str(f.get("raw_customer_id") or "") in selected_set],
        key=lambda x: -phone_channel_sort_key(x),
    )
    quota_plan["phone_preferred_ids"] = [
        str(f.get("raw_customer_id"))
        for f in phone_pref_sorted[: max(0, int(phone_cap or 0) * 2)]
        if f.get("raw_customer_id")
    ]
    quota_plan["exploration_ids"] = exploration_ids

    due_guaranteed_ids: list[str] = []
    if (
        period_type == "daily"
        and limits
        and limits.get("followup_due_guaranteed_daily")
    ):
        due_guaranteed_ids = _pick_due_guaranteed_ids(
            features,
            ref=ref,
            task_cap=cap,
            selected_set=selected_set,
        )
        if due_guaranteed_ids:
            selected = due_guaranteed_ids + [rid for rid in selected if rid not in set(due_guaranteed_ids)]
            quota_plan["due_guaranteed_ids"] = due_guaranteed_ids
            quota_plan["due_guaranteed_count"] = len(due_guaranteed_ids)

    quota_plan["selected_count"] = len(selected)
    quota_plan["pool_k"] = pool_k
    quota_plan["excluded_contact_cooldown"] = len(features) - len(eligible_feats)
    extra = len(due_guaranteed_ids)
    return selected[: pool_k + extra], quota_plan
