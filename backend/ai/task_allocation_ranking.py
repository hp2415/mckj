"""
任务分配规则层：主线优先级（评分 / 30·40·20 标签分层）与破冰轮询公平排序。
LLM 仍只处理 Top-N 候选；全量客户先经规则打分/排序，避免仅按 profiled_at 截断导致沉积。
"""
from __future__ import annotations

import os
import re
from copy import deepcopy
from datetime import date, timedelta
from typing import Any

from sqlalchemy.future import select

from models import ContactTask

# 标签名中的档位数字（如「40」「30标签」「20」）；匹配时避免误伤 120 等
_TAG_TIER_RE = re.compile(r"(?:^|[^\d])(40|30|20)(?:[^\d]|$|标签|级)")
_ABC_RE = [
    (re.compile(r"A\s*级|A类客户|【A】|意向[^\n]{0,8}A\b|分级[：:]\s*A", re.I), 90.0),
    (re.compile(r"B\s*级|B类客户|【B】|分级[：:]\s*B", re.I), 62.0),
    (re.compile(r"C\s*级|C类客户|【C】|分级[：:]\s*C", re.I), 35.0),
]

TIER_POINTS: dict[int, float] = {40: 100.0, 30: 72.0, 20: 38.0}
ABC_GRADE_POINTS: dict[str, float] = {"A": 90.0, "B": 62.0, "C": 35.0}

MAIN_SCORE_POOL_MAX = int(os.getenv("TASK_MAIN_SCORE_POOL_MAX") or "800")
MAIN_STALE_BOOST_DAYS = int(os.getenv("TASK_MAIN_STALE_BOOST_DAYS") or "14")
MAIN_STALE_BOOST_CAP = float(os.getenv("TASK_MAIN_STALE_BOOST_CAP") or "22")
ICEBREAKER_SCORE_POOL_MAX = int(os.getenv("TASK_ICEBREAKER_SCORE_POOL_MAX") or "400")
ICEBREAKER_NEW_BOOST_DAYS = int(os.getenv("TASK_ICEBREAKER_NEW_BOOST_DAYS") or "5")

DEFAULT_SCORING_WEIGHTS: dict[str, float] = {
    "abc_multiplier": 0.85,
    "budget_divisor": 8000.0,
    "budget_cap": 12.0,
    "followup_due_boost": 18.0,
    "pending_task_boost": 22.0,
    "repeat_contact_penalty": 15.0,
    "stale_never_boost": 18.7,  # MAIN_STALE_BOOST_CAP * 0.85
    "stale_daily_rate": 0.65,
    "voice_recent_connected_boost": 8.0,
    "voice_prefers_voice_boost": 5.0,
    "voice_unreachable_penalty": 10.0,
    "phone_channel_fit_boost": 6.0,
    "rule_llm_blend_rule": 0.45,
    "rule_llm_blend_llm": 0.55,
    "rule_llm_deviation_threshold": 25.0,
}

DEFAULT_CONTACT_INTERVAL: dict[str, int] = {
    "min_days": 1,
    "max_days": 14,
    "default_days": 2,
    "responsive_days": 1,
    "cold_days": 7,
}


def _clamp_float(val: Any, default: float, lo: float, hi: float) -> float:
    try:
        n = float(val)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def resolve_scoring_weights(limits: dict[str, Any] | None) -> dict[str, float]:
    """合并默认打分权重与 system_configs 中的 scoring_weights。"""
    out = deepcopy(DEFAULT_SCORING_WEIGHTS)
    if not limits or not isinstance(limits, dict):
        return out
    raw = limits.get("scoring_weights")
    if isinstance(raw, dict):
        for k, default in DEFAULT_SCORING_WEIGHTS.items():
            if k in raw:
                out[k] = _clamp_float(raw[k], default, 0.0, 200.0)
    if limits.get("structured_field_authority"):
        out["__structured_authority"] = 1.0
    return out


def resolve_contact_interval(limits: dict[str, Any] | None) -> dict[str, int]:
    out = deepcopy(DEFAULT_CONTACT_INTERVAL)
    if not limits or not isinstance(limits, dict):
        return out
    raw = limits.get("contact_interval")
    if not isinstance(raw, dict):
        return out
    for k, default in DEFAULT_CONTACT_INTERVAL.items():
        if k in raw:
            try:
                out[k] = max(1, min(60, int(raw[k])))
            except (TypeError, ValueError):
                out[k] = default
    return out


def tag_tier_from_tags(tags: list[dict]) -> tuple[int | None, float]:
    """从客户已打标签解析最高档位 40>30>20；返回 (档位数字, 分数)。"""
    best_tier: int | None = None
    best_pts = 0.0
    for t in tags:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        m = _TAG_TIER_RE.search(name)
        if m:
            tier = int(m.group(1))
            pts = TIER_POINTS.get(tier, 0.0)
            if best_tier is None or tier > best_tier:
                best_tier = tier
                best_pts = pts
            continue
        so = t.get("sort_order")
        if so is not None:
            try:
                so_i = int(so)
            except (TypeError, ValueError):
                so_i = 999
            pts = max(15.0, 50.0 - min(so_i, 35) * 1.0)
            if pts > best_pts and best_tier is None:
                best_pts = pts
    return best_tier, best_pts


def abc_score_from_profile(ai_profile: str) -> float:
    text = (ai_profile or "")[:3000]
    if not text.strip():
        return 0.0
    for pat, pts in _ABC_RE:
        if pat.search(text):
            return pts
    return 0.0


def normalize_abc_grade(value: Any) -> str | None:
    s = str(value or "").strip().upper()
    if s in ("A", "B", "C"):
        return s
    if s and s[0] in ("A", "B", "C"):
        return s[0]
    return None


def extract_abc_grade_from_profile(ai_profile: str) -> str | None:
    text = (ai_profile or "")[:3000]
    if not text.strip():
        return None
    for pat, _ in _ABC_RE:
        m = pat.search(text)
        if m:
            chunk = m.group(0).upper()
            for g in ("A", "B", "C"):
                if g in chunk:
                    return g
    return None


def resolve_abc_score(
    *,
    abc_grade: str | None = None,
    intent_score: float | None = None,
    ai_profile: str = "",
) -> tuple[float, str | None, str]:
    """
    返回 (abc_pts, resolved_grade, source)。
    优先结构化字段，正则兜底。
    """
    grade = normalize_abc_grade(abc_grade)
    if grade:
        pts = float(intent_score) if intent_score is not None else ABC_GRADE_POINTS.get(grade, 0.0)
        return pts, grade, "structured"
    if intent_score is not None:
        try:
            pts = float(intent_score)
        except (TypeError, ValueError):
            pts = 0.0
        if pts >= 75:
            grade = "A"
        elif pts >= 50:
            grade = "B"
        elif pts > 0:
            grade = "C"
        return pts, grade, "intent_score"
    fallback_grade = extract_abc_grade_from_profile(ai_profile)
    if fallback_grade:
        return ABC_GRADE_POINTS.get(fallback_grade, 0.0), fallback_grade, "regex"
    return abc_score_from_profile(ai_profile), fallback_grade, "regex"


def voice_signal_adjustments(
    contact_voice_summary: dict[str, Any] | None,
    *,
    weights: dict[str, float],
) -> tuple[float, dict[str, Any]]:
    """从 contact_voice_summary 派生规则分调整与电话适配度。"""
    summary = contact_voice_summary or {}
    adj = 0.0
    meta: dict[str, Any] = {
        "mobile_call_available": bool(summary.get("mobile_call_available")),
        "prefers_voice": bool(summary.get("prefers_voice")),
    }
    days_since = summary.get("days_since_connected")
    connected_count = int(summary.get("connected_count_90d") or 0)
    connected_sec = int(summary.get("connected_sec_90d") or 0)

    if days_since is not None:
        try:
            dsi = int(days_since)
        except (TypeError, ValueError):
            dsi = None
        else:
            meta["days_since_connected"] = dsi
            if dsi <= 21 and connected_count > 0 and connected_sec >= 30:
                adj += weights.get("voice_recent_connected_boost", 8.0)
                meta["recent_connected"] = True
            elif dsi > 60 and connected_count == 0 and summary.get("mobile_call_available"):
                adj -= weights.get("voice_unreachable_penalty", 10.0)
                meta["long_unreachable"] = True

    if summary.get("prefers_voice"):
        adj += weights.get("voice_prefers_voice_boost", 5.0)

    phone_fit = 0.0
    if connected_count > 0 or summary.get("prefers_voice"):
        phone_fit = weights.get("phone_channel_fit_boost", 6.0)
    meta["phone_channel_fit"] = round(phone_fit, 2)
    return round(adj, 2), meta


def estimate_contact_interval_days(
    *,
    contact_voice_summary: dict[str, Any] | None = None,
    recent_tasks: list[dict] | None = None,
    limits: dict[str, Any] | None = None,
) -> int:
    """按客户响应习惯估计最短联系间隔（天）。"""
    cfg = resolve_contact_interval(limits)
    interval = cfg["default_days"]
    summary = contact_voice_summary or {}
    days_since = summary.get("days_since_connected")
    connected_count = int(summary.get("connected_count_90d") or 0)

    if summary.get("prefers_voice") or (days_since is not None and int(days_since) <= 7):
        interval = cfg["responsive_days"]
    elif connected_count == 0 and days_since is None:
        interval = cfg["cold_days"]

    for rt in recent_tasks or []:
        st = (rt.get("status") or "").strip().lower()
        if st == "skipped":
            interval = max(interval, cfg["cold_days"])
            break

    return max(cfg["min_days"], min(cfg["max_days"], interval))


def _parse_task_due_date(rt: dict) -> date | None:
    due_s = str(rt.get("due_date") or "")[:10]
    if not due_s:
        return None
    try:
        return date.fromisoformat(due_s)
    except ValueError:
        return None


def should_skip_repeat_contact_today(
    recent_tasks: list[dict] | None,
    ref_date: date,
    *,
    last_sales_outbound: date | None = None,
    contact_voice_summary: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
) -> bool:
    """
    今日不宜再排触达：结合自适应间隔、昨日完成、近期 outbound。
    """
    interval_days = estimate_contact_interval_days(
        contact_voice_summary=contact_voice_summary,
        recent_tasks=recent_tasks,
        limits=limits,
    )
    cutoff = ref_date - timedelta(days=interval_days)

    for rt in recent_tasks or []:
        st = (rt.get("status") or "").strip().lower()
        if st not in ("done", "skipped"):
            continue
        due_d = _parse_task_due_date(rt)
        if due_d is None:
            continue
        if due_d >= cutoff:
            return True
        if rt.get("was_yesterday") and interval_days <= 1:
            return True

    if last_sales_outbound is not None and last_sales_outbound >= cutoff:
        return True
    return False


def should_skip_icebreaker_repeat_today(
    recent_tasks: list[dict] | None,
    ref_date: date,
    *,
    last_sales_outbound: date | None = None,
    cooldown_days: int = 1,
) -> bool:
    """
    激活候选池专用：仅排除近 1~2 日已触达/已排任务的客户。
    主线用的自适应间隔（可长达 7 天）会过度滤掉大好友池，导致激活候选过少。
    """
    cd = max(0, int(cooldown_days))
    if cd <= 0:
        return False
    earliest = ref_date - timedelta(days=cd)
    for rt in recent_tasks or []:
        st = (rt.get("status") or "").strip().lower()
        if st not in ("pending", "in_progress", "done"):
            continue
        due_d = _parse_task_due_date(rt)
        if due_d is None:
            continue
        if due_d >= earliest:
            return True
    if last_sales_outbound is not None and last_sales_outbound >= earliest:
        return True
    return False


def priority_band(score: float, tag_tier: int | None) -> str:
    if tag_tier in (40, 30) or score >= 68:
        return "high"
    if tag_tier == 20 or score >= 42:
        return "mid"
    return "low"


def followup_date_score_adjustment(
    suggested_followup_date: date | None,
    ref_date: date,
    *,
    limits: dict[str, Any] | None = None,
    scoring_weights: dict[str, float] | None = None,
) -> tuple[float, dict[str, float]]:
    """
    跟进日期分级加成：过期 > 今日到期 > 临期。
    开关关闭时保持旧行为（仅 followup_date <= ref_date 时 +followup_due_boost）。
    """
    if not suggested_followup_date:
        return 0.0, {}

    w = scoring_weights or resolve_scoring_weights(limits)
    limits = limits or {}

    if bool(limits.get("followup_due_signal_enabled")):
        upcoming_days = int(limits.get("followup_upcoming_days") or 2)
        delta = (suggested_followup_date - ref_date).days
        if delta < 0:
            boost = float(limits.get("followup_overdue_boost") or 28.0)
            return boost, {"followup_overdue": boost}
        if delta == 0:
            boost = float(limits.get("followup_dueday_boost") or 18.0)
            return boost, {"followup_dueday": boost}
        if delta <= upcoming_days:
            boost = float(limits.get("followup_upcoming_boost") or 8.0)
            return boost, {"followup_upcoming": boost}
        return 0.0, {}

    if suggested_followup_date <= ref_date:
        boost = float(w.get("followup_due_boost", 18.0))
        return boost, {"followup_due": boost}
    return 0.0, {}


def compute_main_rule_score(
    *,
    ref_date: date,
    tags: list[dict],
    ai_profile: str,
    budget_amount: float,
    suggested_followup_date: date | None,
    recent_tasks: list[dict],
    last_main_task_due: date | None,
    abc_grade: str | None = None,
    intent_score: float | None = None,
    contact_voice_summary: dict[str, Any] | None = None,
    scoring_weights: dict[str, float] | None = None,
    limits: dict[str, Any] | None = None,
) -> tuple[float, int | None, str, int | None, dict[str, Any]]:
    """
    返回 (rule_priority_score, tag_tier, priority_band, days_since_last_main_task, breakdown)。
    """
    w = scoring_weights or resolve_scoring_weights(limits)
    tag_tier, tier_pts = tag_tier_from_tags(tags)
    abc_pts, resolved_grade, abc_source = resolve_abc_score(
        abc_grade=abc_grade,
        intent_score=intent_score,
        ai_profile=ai_profile,
    )

    breakdown: dict[str, Any] = {
        "tier_pts": tier_pts,
        "abc_pts": abc_pts,
        "abc_grade": resolved_grade,
        "abc_source": abc_source,
        "adjustments": {},
    }

    score = max(tier_pts, abc_pts * w.get("abc_multiplier", 0.85))

    if budget_amount and budget_amount > 0:
        b_adj = min(w.get("budget_cap", 12.0), float(budget_amount) / max(1.0, w.get("budget_divisor", 8000.0)))
        score += b_adj
        breakdown["adjustments"]["budget"] = round(b_adj, 2)

    if suggested_followup_date:
        followup_adj, followup_breakdown = followup_date_score_adjustment(
            suggested_followup_date,
            ref_date,
            limits=limits,
            scoring_weights=w,
        )
        if followup_adj:
            score += followup_adj
            breakdown["adjustments"].update(followup_breakdown)

    days_since_main: int | None = None
    stale_boost_days = int(limits.get("stale_boost_days") if limits else MAIN_STALE_BOOST_DAYS) or MAIN_STALE_BOOST_DAYS
    stale_boost_cap = float(limits.get("stale_boost_cap") if limits else MAIN_STALE_BOOST_CAP) or MAIN_STALE_BOOST_CAP

    if last_main_task_due is None:
        days_since_main = None
        stale_adj = w.get("stale_never_boost", stale_boost_cap * 0.85)
        score += stale_adj
        breakdown["adjustments"]["stale_never"] = stale_adj
    else:
        days_since_main = max(0, (ref_date - last_main_task_due).days)
        if days_since_main >= stale_boost_days:
            stale_adj = min(
                stale_boost_cap,
                (days_since_main - stale_boost_days + 1) * w.get("stale_daily_rate", 0.65),
            )
            score += stale_adj
            breakdown["adjustments"]["stale"] = round(stale_adj, 2)

    for rt in recent_tasks or []:
        st = (rt.get("status") or "").strip().lower()
        if st in ("pending", "overdue", "in_progress"):
            score += w.get("pending_task_boost", 22.0)
            breakdown["adjustments"]["pending_task"] = w.get("pending_task_boost", 22.0)
            break

    if should_skip_repeat_contact_today(
        recent_tasks,
        ref_date,
        contact_voice_summary=contact_voice_summary,
        limits=limits,
    ):
        penalty = w.get("repeat_contact_penalty", 15.0)
        score -= penalty
        breakdown["adjustments"]["repeat_contact_penalty"] = -penalty

    voice_adj, voice_meta = voice_signal_adjustments(contact_voice_summary, weights=w)
    score += voice_adj
    breakdown["voice"] = voice_meta
    if voice_adj:
        breakdown["adjustments"]["voice"] = voice_adj

    score = round(min(100.0, max(0.0, score)), 2)
    band = priority_band(score, tag_tier)
    breakdown["rule_priority_score"] = score
    breakdown["tag_tier"] = tag_tier
    breakdown["priority_band"] = band
    breakdown["days_since_last_main_task"] = days_since_main
    breakdown["intent_level"] = intent_level_from_score(score)
    return score, tag_tier, band, days_since_main, breakdown


def intent_level_from_score(rule_score: float) -> int:
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


def build_alloc_feature_snapshot(
    *,
    raw_customer_id: str,
    breakdown: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """写入 ContactTask.alloc_feature_json 的快照。"""
    b = breakdown or {}
    snap: dict[str, Any] = {
        "raw_customer_id": raw_customer_id,
        "rule_priority_score": b.get("rule_priority_score"),
        "tag_tier": b.get("tag_tier"),
        "priority_band": b.get("priority_band"),
        "intent_level": b.get("intent_level"),
        "days_since_last_main_task": b.get("days_since_last_main_task"),
        "abc_grade": b.get("abc_grade"),
        "abc_source": b.get("abc_source"),
        "adjustments": b.get("adjustments"),
        "voice": b.get("voice"),
    }
    if extra:
        snap.update(extra)
    return {k: v for k, v in snap.items() if v is not None}


def blend_priority_scores(
    *,
    rule_score: float | None,
    llm_score: float | None,
    weights: dict[str, float] | None = None,
) -> float:
    w = weights or DEFAULT_SCORING_WEIGHTS
    rs = float(rule_score or 0)
    ls = float(llm_score or 0)
    wr = w.get("rule_llm_blend_rule", 0.45)
    wl = w.get("rule_llm_blend_llm", 0.55)
    total = wr + wl
    if total <= 0:
        return max(rs, ls)
    return round((rs * wr + ls * wl) / total, 2)


def phone_channel_sort_key(feat: dict[str, Any]) -> float:
    """电话渠道选人偏好分（越高越适合分配电话任务）。"""
    score = float(feat.get("rule_priority_score") or 0)
    voice = (feat.get("recency") or {}).get("contact_voice") or {}
    voice_br = (feat.get("_score_breakdown") or {}).get("voice") or {}
    phone_fit = float(voice_br.get("phone_channel_fit") or 0)
    grade = normalize_abc_grade(feat.get("abc_grade"))
    if grade == "A":
        score += 12.0
    elif grade == "B":
        score += 6.0
    if voice.get("prefers_voice") or voice_br.get("prefers_voice"):
        score += 8.0
    recent = voice_br.get("recent_connected") or (
        voice.get("days_since_connected") is not None
        and int(voice.get("days_since_connected")) <= 21
    )
    if recent:
        score += 5.0
    return score + phone_fit


async def load_last_task_due_by_customer(
    db,
    sales_wechat_id: str,
) -> tuple[dict[str, date], dict[str, date]]:
    """返回 (last_main_task_due, last_icebreaker_due) 按 raw_customer_id。"""
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return {}, {}
    res = await db.execute(
        select(ContactTask.raw_customer_id, ContactTask.task_kind, ContactTask.due_date)
        .where(ContactTask.sales_wechat_id == sw)
        .where(ContactTask.due_date.isnot(None))
    )
    last_main: dict[str, date] = {}
    last_ice: dict[str, date] = {}
    for rid, kind, due in res.all():
        if not rid or not due:
            continue
        rid_s = str(rid).strip()
        d = due if isinstance(due, date) else due.date()
        k = (kind or "").strip().lower()
        if k == "icebreaker":
            prev = last_ice.get(rid_s)
            if prev is None or d > prev:
                last_ice[rid_s] = d
        else:
            prev = last_main.get(rid_s)
            if prev is None or d > prev:
                last_main[rid_s] = d
    return last_main, last_ice


def icebreaker_fair_sort_key(
    item: tuple[Any, Any, Any, str],
    *,
    ref_date: date,
    last_ice_due: dict[str, date],
    reason_order: dict[str, int],
) -> tuple:
    """越久未安排破冰越靠前；新加好友适度提前。"""
    rcsw, _rc, _scp, reason = item
    rid = (getattr(rcsw, "raw_customer_id", None) or "").strip()
    last = last_ice_due.get(rid)
    if last is None:
        days_since = 9999
    else:
        days_since = max(0, (ref_date - last).days)
    if reason == "new_friend":
        days_since += min(ICEBREAKER_NEW_BOOST_DAYS * 40, 200)
    ro = reason_order.get(reason, 9)
    return (-days_since, ro, rid)
