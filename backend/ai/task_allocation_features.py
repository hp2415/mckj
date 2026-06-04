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
    }
    summary = payload.get("contact_voice_summary") or {}
    if isinstance(summary, dict) and summary:
        from ai.wechat_voice_stats import compact_contact_voice_for_feature

        compact = compact_contact_voice_for_feature(summary)
        if compact:
            recency["contact_voice"] = compact

    return {
        "raw_customer_id": rid,
        "scp_id": payload.get("scp_id"),
        "customer_name": str(payload.get("customer_name") or "")[:80],
        "unit_name": str(payload.get("unit_name") or "")[:80],
        "stage_tags": tag_names,
        "recency": recency,
        "intent_level": intent_level_from_score(rule_score),
        "rule_priority_score": round(rule_score, 2),
        "tag_tier": payload.get("tag_tier"),
        "priority_band": str(payload.get("priority_band") or ""),
        "next_best_action_hint": hint,
        "constraints": constraints,
        "recent_tasks": recent_compact,
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


def features_to_llm_json(features: list[dict[str, Any]]) -> str:
    """紧凑 JSON，不含 profile_tags_detail / 长 ai_profile。"""
    return json.dumps(features, ensure_ascii=False, separators=(",", ":"))
