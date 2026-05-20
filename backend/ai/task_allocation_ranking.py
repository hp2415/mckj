"""
任务分配规则层：主线优先级（评分 / 30·40·20 标签分层）与破冰轮询公平排序。
LLM 仍只处理 Top-N 候选；全量客户先经规则打分/排序，避免仅按 profiled_at 截断导致沉积。
"""
from __future__ import annotations

import os
import re
from datetime import date
from typing import Any

from sqlalchemy import func
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

MAIN_SCORE_POOL_MAX = int(os.getenv("TASK_MAIN_SCORE_POOL_MAX") or "800")
MAIN_STALE_BOOST_DAYS = int(os.getenv("TASK_MAIN_STALE_BOOST_DAYS") or "14")
MAIN_STALE_BOOST_CAP = float(os.getenv("TASK_MAIN_STALE_BOOST_CAP") or "22")
ICEBREAKER_SCORE_POOL_MAX = int(os.getenv("TASK_ICEBREAKER_SCORE_POOL_MAX") or "400")
ICEBREAKER_NEW_BOOST_DAYS = int(os.getenv("TASK_ICEBREAKER_NEW_BOOST_DAYS") or "5")


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
            # sort_order 越小越优先，映射到 20~50 分
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


def priority_band(score: float, tag_tier: int | None) -> str:
    if tag_tier in (40, 30) or score >= 68:
        return "high"
    if tag_tier == 20 or score >= 42:
        return "mid"
    return "low"


def compute_main_rule_score(
    *,
    ref_date: date,
    tags: list[dict],
    ai_profile: str,
    budget_amount: float,
    suggested_followup_date: date | None,
    recent_tasks: list[dict],
    last_main_task_due: date | None,
) -> tuple[float, int | None, str, int | None]:
    """
    返回 (rule_priority_score, tag_tier, priority_band, days_since_last_main_task)。
    """
    tag_tier, tier_pts = tag_tier_from_tags(tags)
    abc_pts = abc_score_from_profile(ai_profile)

    score = max(tier_pts, abc_pts * 0.85)

    if budget_amount and budget_amount > 0:
        score += min(12.0, float(budget_amount) / 8000.0)

    if suggested_followup_date and suggested_followup_date <= ref_date:
        score += 18.0

    days_since_main: int | None = None
    if last_main_task_due is None:
        days_since_main = None
        score += MAIN_STALE_BOOST_CAP * 0.85
    else:
        days_since_main = max(0, (ref_date - last_main_task_due).days)
        if days_since_main >= MAIN_STALE_BOOST_DAYS:
            score += min(
                MAIN_STALE_BOOST_CAP,
                (days_since_main - MAIN_STALE_BOOST_DAYS + 1) * 0.65,
            )

    for rt in recent_tasks or []:
        st = (rt.get("status") or "").strip().lower()
        if st in ("pending", "overdue", "in_progress"):
            score += 22.0
            break
        if rt.get("was_yesterday") and st == "done":
            score -= 15.0
            break

    score = round(min(100.0, max(0.0, score)), 2)
    band = priority_band(score, tag_tier)
    return score, tag_tier, band, days_since_main


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
