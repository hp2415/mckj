"""
Phase A：将完整客户快照压缩为固定宽度的 CustomerFeature，供选人/分批 LLM 使用。
默认规则派生，不调用 LLM。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

STAGE_TAGS_MAX = 12
HINT_MAX_CHARS = int(os.getenv("TASK_ALLOCATION_HINT_MAX_CHARS") or "200")
CONSTRAINTS_MAX_CHARS = int(os.getenv("TASK_ALLOCATION_CONSTRAINTS_MAX_CHARS") or "200")
AI_PROFILE_HINT_CHARS = int(os.getenv("TASK_ALLOCATION_AI_PROFILE_HINT_CHARS") or "200")
RECENT_TASKS_MAX = int(os.getenv("TASK_ALLOCATION_RECENT_TASKS_MAX") or "3")
STRATEGY_SNIPPET_MAX = int(os.getenv("TASK_ALLOCATION_STRATEGY_SNIPPET_MAX") or "120")

_CONSTRAINT_KEYWORDS = ("勿", "禁止", "不宜", "避免", "不要", "不可", "忌")


def _first_strategy_line(tags: list[dict] | None) -> str:
    if not tags:
        return ""
    for t in tags:
        strat = (t.get("strategy_note") or "").strip()
        if strat:
            first = strat.split("\n")[0].strip()
            if first:
                return first[:STRATEGY_SNIPPET_MAX]
    return ""


def _extract_constraints(tags: list[dict] | None) -> str:
    if not tags:
        return ""
    parts: list[str] = []
    for t in tags:
        feat = (t.get("feature_note") or "").strip()
        if not feat:
            continue
        for line in feat.replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            if any(k in line for k in _CONSTRAINT_KEYWORDS):
                parts.append(line[:80])
    text = "；".join(parts)[:CONSTRAINTS_MAX_CHARS]
    return text


def intent_level_from_score(rule_score: float) -> int:
    """0–5，与 rule_priority_score 对齐。"""
    s = float(rule_score or 0)
    if s >= 85:
        return 5
    if s >= 70:
        return 4
    if s >= 55:
        return 3
    if s >= 40:
        return 2
    if s >= 25:
        return 1
    return 0


def feature_version_hash(
    *,
    profiled_at: str | None,
    tag_names: list[str],
    ai_profile_len: int,
    days_since_main: int | None,
) -> str:
    payload = {
        "profiled_at": profiled_at or "",
        "tags": sorted(tag_names),
        "ai_len": ai_profile_len,
        "days_main": days_since_main,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def payload_to_customer_feature(payload: dict[str, Any]) -> dict[str, Any]:
    """从 load_allocation_customer_payloads 的单条 dict 生成 CustomerFeature。"""
    rid = str(payload.get("raw_customer_id") or "").strip()
    tags = payload.get("profile_tags") or []
    if isinstance(tags, str):
        tag_names = [tags]
    else:
        tag_names = [str(x).strip() for x in tags if str(x).strip()][:STAGE_TAGS_MAX]

    ap = str(payload.get("ai_profile") or "").strip()
    hint = ap[:AI_PROFILE_HINT_CHARS]
    if not hint:
        hint = _first_strategy_line(
            [{"strategy_note": payload.get("strategy_snippet")}]
            if payload.get("strategy_snippet")
            else None
        )
    detail_text = str(payload.get("profile_tags_detail") or "")
    if not hint and detail_text:
        for line in detail_text.split("\n"):
            if "跟进策略" in line or "节奏" in line:
                hint = re.sub(r"^\s*跟进策略/节奏：\s*", "", line.strip())[:HINT_MAX_CHARS]
                break
    hint = (hint or "")[:HINT_MAX_CHARS]

    constraints = ""
    if detail_text:
        for line in detail_text.replace("\r", "\n").split("\n"):
            line = line.strip()
            if line and any(k in line for k in _CONSTRAINT_KEYWORDS):
                constraints = (constraints + "；" + line[:80]).strip("；")
        constraints = constraints[:CONSTRAINTS_MAX_CHARS]

    recent = payload.get("recent_tasks") or []
    if isinstance(recent, list):
        recent_compact = recent[:RECENT_TASKS_MAX]
    else:
        recent_compact = []

    rule_score = float(payload.get("rule_priority_score") or 0)
    days_main = payload.get("days_since_last_main_task")
    try:
        days_main_i = int(days_main) if days_main is not None else None
    except (TypeError, ValueError):
        days_main_i = None

    recency: dict[str, Any] = {
        "days_since_last_main_task": days_main_i,
        "suggested_followup_date": str(payload.get("suggested_followup_date") or ""),
        "followup_strategy": str(payload.get("followup_strategy") or "")[:STRATEGY_SNIPPET_MAX],
        "followup_channel": str(payload.get("followup_channel") or "").strip().lower(),
    }
    summary = payload.get("contact_voice_summary") or {}
    if isinstance(summary, dict) and summary:
        from ai.wechat_voice_stats import compact_contact_voice_for_feature

        compact = compact_contact_voice_for_feature(summary)
        if compact:
            recency["contact_voice"] = compact

    months = payload.get("purchase_months") or []
    if isinstance(months, str):
        purchase_months = [m.strip() for m in months.replace("，", ",").split(",") if m.strip()][:12]
    elif isinstance(months, list):
        purchase_months = [str(m).strip() for m in months if str(m).strip()][:12]
    else:
        purchase_months = []
    unit_type = str(payload.get("unit_type") or "")[:50]
    unit_name = str(payload.get("unit_name") or "")[:80]
    unit_segment = str(payload.get("unit_segment") or "").strip()
    if not unit_segment:
        from ai.time_context import resolve_unit_segment

        unit_segment = resolve_unit_segment(unit_type, unit_name)

    return {
        "raw_customer_id": rid,
        "scp_id": payload.get("scp_id"),
        "customer_name": str(payload.get("customer_name") or "")[:80],
        "unit_name": unit_name,
        "unit_type": unit_type,
        "unit_segment": unit_segment,
        "purchase_months": purchase_months,
        "phone": str(payload.get("phone") or "")[:40],
        "phone_normalized": str(payload.get("phone_normalized") or "")[:40] or None,
        "has_phone": bool(payload.get("has_phone") if "has_phone" in payload else payload.get("phone")),
        "stage_tags": tag_names,
        "recency": recency,
        "intent_level": intent_level_from_score(rule_score),
        "rule_priority_score": round(rule_score, 2),
        "tag_tier": payload.get("tag_tier"),
        "priority_band": str(payload.get("priority_band") or ""),
        "abc_grade": payload.get("abc_grade"),
        "_score_breakdown": payload.get("_score_breakdown"),
        "next_best_action_hint": hint,
        "constraints": constraints,
        "recent_tasks": recent_compact,
        "_profile_followup_channel": recency["followup_channel"],
        "_profile_followup_strategy": recency["followup_strategy"],
        "_profile_followup_date": recency["suggested_followup_date"],
        "feature_version": feature_version_hash(
            profiled_at=None,
            tag_names=tag_names,
            ai_profile_len=len(ap),
            days_since_main=days_main_i,
        ),
    }


def materialize_features(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in payloads:
        rid = str(p.get("raw_customer_id") or "").strip()
        if not rid:
            continue
        out.append(payload_to_customer_feature(p))
    return out


def features_for_llm(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """投喂模型前去掉内部字段（_ 前缀），保留 phone / has_phone 等决策字段。"""
    cleaned: list[dict[str, Any]] = []
    for f in features:
        if not isinstance(f, dict):
            continue
        cleaned.append({k: v for k, v in f.items() if not str(k).startswith("_")})
    return cleaned


def features_to_llm_json(features: list[dict[str, Any]]) -> str:
    """紧凑 JSON，不含 profile_tags_detail / 长 ai_profile / 内部字段。"""
    return json.dumps(features_for_llm(features), ensure_ascii=False, separators=(",", ":"))


_VALID_FOLLOWUP_CHANNELS = frozenset({"wechat", "phone"})


def _profile_channel_for_row(
    row: dict[str, Any],
    feature_by_id: dict[str, dict[str, Any]] | None = None,
) -> str:
    ch = str(row.get("_profile_followup_channel") or "").strip().lower()
    if ch in _VALID_FOLLOWUP_CHANNELS:
        return ch
    if feature_by_id is not None:
        rid = str(row.get("raw_customer_id") or "").strip()
        ch = str((feature_by_id.get(rid) or {}).get("_profile_followup_channel") or "").strip().lower()
        if ch in _VALID_FOLLOWUP_CHANNELS:
            return ch
    return ""


def apply_profile_channel_authority(
    rows: list[dict[str, Any]],
    limits: dict[str, Any] | None,
    *,
    phone_cap: int = 0,
    feature_by_id: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    按画像 followup_channel 覆盖 contact_channel。
    默认只「升」为电话（画像要求电话且未超 phone_cap）；
    禁止把模型已选的 phone 降成 wechat（否则随后 wechat_cap 会把任务截掉）。
    仅当 min_conf 显式为 wechat 时才允许 phone→wechat。
    """
    limits = limits or {}
    meta: dict[str, Any] = {
        "enabled": bool(limits.get("followup_channel_authority")),
        "applied": 0,
        "phone_forced": 0,
        "skipped_phone_cap": 0,
        "skipped_phone_downgrade": 0,
    }
    if not meta["enabled"] or not rows:
        return rows, meta

    force_ch = str(limits.get("followup_channel_authority_min_conf") or "phone").strip().lower()
    if force_ch not in _VALID_FOLLOWUP_CHANNELS | {"any"}:
        force_ch = "phone"

    phone_limit = max(0, int(phone_cap))
    indexed = list(enumerate(rows))
    sortable = sorted(indexed, key=lambda t: (int(t[1].get("priority_rank") or 99999), t[0]))

    phone_forced = 0
    overrides: list[tuple[int, str]] = []
    overridden_rids: set[str] = set()

    for idx, row in sortable:
        prof_ch = _profile_channel_for_row(row, feature_by_id)
        if not prof_ch:
            continue
        should_force = force_ch == "any" or prof_ch == force_ch
        if not should_force:
            continue
        current = str(row.get("contact_channel") or "wechat").strip().lower()
        if current not in _VALID_FOLLOWUP_CHANNELS:
            current = "wechat"
        # 保护模型电话选择：min_conf=any/phone 时不把 phone 降成 wechat
        if current == "phone" and prof_ch == "wechat" and force_ch != "wechat":
            meta["skipped_phone_downgrade"] += 1
            continue
        if prof_ch == "phone" and phone_forced >= phone_limit:
            meta["skipped_phone_cap"] += 1
            continue
        if current != prof_ch:
            meta["applied"] += 1
            rid = str(row.get("raw_customer_id") or "").strip()
            if rid:
                overridden_rids.add(rid)
        if prof_ch == "phone":
            phone_forced += 1
        overrides.append((idx, prof_ch))

    meta["phone_forced"] = phone_forced
    meta["overridden_rids"] = sorted(overridden_rids)
    if not overrides:
        return rows, meta

    out = [dict(r) for r in rows]
    for idx, ch in overrides:
        out[idx]["contact_channel"] = ch
    return out, meta


def _profile_strategy_for_row(
    row: dict[str, Any],
    feature_by_id: dict[str, dict[str, Any]] | None = None,
) -> str:
    strategy = str(row.get("_profile_followup_strategy") or "").strip()
    if strategy:
        return strategy
    if feature_by_id is not None:
        rid = str(row.get("raw_customer_id") or "").strip()
        strategy = str((feature_by_id.get(rid) or {}).get("_profile_followup_strategy") or "").strip()
    return strategy


def apply_profile_strategy_fallback(
    rows: list[dict[str, Any]],
    limits: dict[str, Any] | None,
    *,
    feature_by_id: dict[str, dict[str, Any]] | None = None,
    skip_reserve: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    LLM instruction 缺失或过短时，用画像 followup_strategy 兜底。
    不覆盖已有足够长度的 LLM 文案。
    """
    limits = limits or {}
    meta: dict[str, Any] = {
        "enabled": bool(limits.get("followup_strategy_fallback")),
        "applied": 0,
        "min_chars": int(limits.get("followup_strategy_min_chars") or 8),
    }
    if not meta["enabled"] or not rows:
        return rows, meta

    min_chars = max(1, min(int(meta["min_chars"]), 500))
    meta["min_chars"] = min_chars
    out: list[dict[str, Any]] = []

    for row in rows:
        item = dict(row)
        if skip_reserve and item.get("_pool_tier") == "reserve":
            out.append(item)
            continue
        instr = str(item.get("instruction") or "").strip()
        prof_strat = _profile_strategy_for_row(item, feature_by_id)
        if len(instr) >= min_chars:
            item.setdefault("_instruction_source", "llm")
        elif prof_strat:
            item["instruction"] = prof_strat[:2000]
            item["_instruction_source"] = "profile_fallback"
            meta["applied"] += 1
        out.append(item)

    return out, meta


def build_profile_suggest_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    """从任务行上的画像跟进字段生成 alloc_feature_json.profile_suggest 快照。"""
    ch = str(row.get("_profile_followup_channel") or "").strip().lower()
    fd = row.get("_profile_followup_date")
    if hasattr(fd, "isoformat"):
        fd_s = fd.isoformat()
    else:
        fd_s = str(fd or "").strip()[:10] or None
    return {
        "channel": ch if ch in ("wechat", "phone") else None,
        "has_strategy": bool(str(row.get("_profile_followup_strategy") or "").strip()),
        "followup_date": fd_s,
    }


def attach_profile_followup_from_payload(row: dict[str, Any], payload: dict[str, Any]) -> None:
    """非可扩展管线：从 payload 补齐画像跟进字段（可扩展管线已在 feature 中设置）。"""
    if "_profile_followup_channel" in row:
        return
    row["_profile_followup_channel"] = str(payload.get("followup_channel") or "").strip().lower()
    row["_profile_followup_strategy"] = str(payload.get("followup_strategy") or "")
    fd = payload.get("suggested_followup_date")
    if hasattr(fd, "isoformat"):
        row["_profile_followup_date"] = fd.isoformat()
    else:
        row["_profile_followup_date"] = str(fd or "")
