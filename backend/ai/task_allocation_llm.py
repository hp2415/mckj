"""
任务分配 LLM：读取管理平台已发布的 task_allocation / task_allocation_icebreaker 场景，
流式调用与画像相同的 profile LLM 配置，解析 JSON 得到联系任务列表。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Date, and_, cast, desc, or_
from sqlalchemy.future import select

from ai.llm_client import LLMClient
from ai.prompt_models import DocInjectSpec, PromptTemplate
from ai.prompt_renderer import render_system
from ai.prompt_seed import (
    TASK_ALLOCATION_SYSTEM,
    TASK_ALLOCATION_USER,
    TASK_ICEBREAKER_SYSTEM,
    TASK_ICEBREAKER_USER,
)
from ai.prompt_store import get_prompt_store
from ai.raw_profiling import (
    _extract_first_json_object,
    _use_db_prompts,
    load_profile_tags_catalog_text,
)
from core.logger import logger
from ai.task_allocation_ranking import (
    MAIN_SCORE_POOL_MAX,
    ICEBREAKER_SCORE_POOL_MAX,
    compute_main_rule_score,
    icebreaker_fair_sort_key,
    load_last_task_due_by_customer,
)
from crud import profile_tags_by_relation_ids
from models import (
    ContactTask,
    RawCustomer,
    RawCustomerSalesWechat,
    SalesCustomerProfile,
)

SCENARIO_KEY = "task_allocation"
SCENARIO_ICEBREAKER_KEY = "task_allocation_icebreaker"

SHANGHAI_TZ = timezone(timedelta(hours=8))

MAX_CUSTOMERS = int(os.getenv("TASK_ALLOCATION_MAX_CUSTOMERS") or "120")
AI_PROFILE_MAX_CHARS = int(os.getenv("TASK_ALLOCATION_AI_PROFILE_MAX_CHARS") or "1500")
TEMPERATURE = float(os.getenv("TASK_ALLOCATION_TEMPERATURE") or "0.25")
MAX_TOKENS = int(os.getenv("TASK_ALLOCATION_MAX_TOKENS") or "4096")

ICEBREAKER_ENABLED = str(os.getenv("TASK_ICEBREAKER_ENABLED") or "1").strip().lower() not in (
    "0",
    "false",
    "off",
)
ICEBREAKER_NEW_DAYS = int(os.getenv("TASK_ICEBREAKER_NEW_DAYS") or "3")
ICEBREAKER_STALE_DAYS = int(os.getenv("TASK_ICEBREAKER_STALE_DAYS") or "60")
# 实际条数由 task_allocation.resolve_icebreaker_task_cap() 决定；此处仅作模块默认参考
ICEBREAKER_CAP = int(os.getenv("TASK_ICEBREAKER_CAP") or "25")
ICEBREAKER_MAX_FETCH = int(os.getenv("TASK_ICEBREAKER_MAX_CANDIDATES") or "200")

TASK_HISTORY_LOOKBACK_DAYS = int(os.getenv("TASK_ALLOCATION_HISTORY_DAYS") or "14")
TASK_HISTORY_PER_CUSTOMER = int(os.getenv("TASK_ALLOCATION_HISTORY_PER_CUSTOMER") or "5")

_DEBUG_PROMPT = str(os.getenv("TASK_ALLOCATION_DEBUG_PROMPT") or "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_DEBUG_CHUNK = int(os.getenv("TASK_ALLOCATION_DEBUG_MAX_CHARS") or "8000")

_LOCAL_DOC_SPECS: dict[str, DocInjectSpec] = {
    "opening": DocInjectSpec(
        doc_key="opening",
        title="开场破冰话术参考",
        required=False,
        max_chars=8000,
    ),
    "scoring_criteria": DocInjectSpec(
        doc_key="scoring_criteria",
        title="高意向客户行为特征与ABC分级判定框架（key=scoring_criteria）",
        required=False,
        max_chars=16000,
    ),
    "strategy": DocInjectSpec(
        doc_key="strategy",
        title="客户分层话术参考（补充）",
        required=False,
        max_chars=12000,
    ),
    "profile_tags_detail": DocInjectSpec(
        doc_key="profile_tags_detail",
        title="客户动态标签及跟进策略（profile_tags_detail）",
        required=False,
        max_chars=12000,
    ),
}


def compose_profile_tags_detail(tags: list[dict] | None) -> str:
    """与画像 context 一致：格式化客户已打标签的特征与策略。"""
    if not tags:
        return "暂无动态标签"
    lines: list[str] = []
    for t in tags:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        feat = (t.get("feature_note") or "").strip()
        strat = (t.get("strategy_note") or "").strip()
        line = f"- 【{name}】"
        if feat:
            line += f"\n  特征：{feat}"
        if strat:
            line += f"\n  跟进策略/节奏：{strat}"
        lines.append(line)
    return "\n".join(lines) if lines else "暂无动态标签"

_ICEBREAKER_REASON_ORDER = {"new_friend": 0, "long_no_chat": 1, "added_old_never_chat": 2}


def _log_allocation_io(
    *,
    log_tag: str,
    sales_wechat_id: str,
    messages: list[dict[str, str]],
    meta: dict[str, Any],
    customer_payloads: list[dict[str, Any]],
) -> None:
    """测试阶段：在控制台输出发给模型的数据（需 TASK_ALLOCATION_DEBUG_PROMPT=1）。"""
    if not _DEBUG_PROMPT:
        return
    ids = [str(p.get("raw_customer_id") or "") for p in customer_payloads[:80]]
    sys_c = ""
    usr_c = ""
    for m in messages or []:
        if m.get("role") == "system":
            sys_c = str(m.get("content") or "")
        elif m.get("role") == "user":
            usr_c = str(m.get("content") or "")
    cap = max(500, _DEBUG_CHUNK)
    slim_meta = {
        k: meta.get(k)
        for k in ("prompt_source", "scenario_key", "prompt_version_id", "prompt_version", "rationale")
        if k in meta
    }
    logger.info(
        "{} sw={} meta={} candidate_count={} raw_customer_ids_sample={}",
        log_tag,
        sales_wechat_id,
        json.dumps(slim_meta, ensure_ascii=False),
        len(customer_payloads),
        ids[:20],
    )
    logger.info(
        "{} ---SYSTEM len={}---\n{}",
        log_tag,
        len(sys_c),
        sys_c[:cap] + ("…<truncated>" if len(sys_c) > cap else ""),
    )
    logger.info(
        "{} ---USER len={}---\n{}",
        log_tag,
        len(usr_c),
        usr_c[:cap] + ("…<truncated>" if len(usr_c) > cap else ""),
    )


def _period_type_label(period_type: str) -> str:
    return {"daily": "日联系", "weekly": "周联系", "monthly": "月联系"}.get(period_type, period_type)


def _dt_to_date(dt: datetime | date | None) -> date | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.date()
    if isinstance(dt, date):
        return dt
    return None


def _icebreaker_eligibility(
    rcsw: RawCustomerSalesWechat,
    ref_date: date,
    *,
    new_days: int,
    stale_days: int,
) -> tuple[bool, str]:
    """判定是否属于破冰池：新加 / 长期未聊 / 加好友较早从未私聊。"""
    rid = (rcsw.raw_customer_id or "").strip()
    if not rid or rid.endswith("@chatroom"):
        return False, ""
    add_d = _dt_to_date(rcsw.add_time)
    last_d = _dt_to_date(rcsw.last_chat_time)
    new_from = ref_date - timedelta(days=max(1, new_days) - 1)
    stale_before = ref_date - timedelta(days=max(1, stale_days))

    is_new = add_d is not None and add_d >= new_from
    is_stale = last_d is not None and last_d <= stale_before
    is_cold_never = last_d is None and add_d is not None and add_d < new_from

    if is_new:
        return True, "new_friend"
    if is_stale:
        return True, "long_no_chat"
    if is_cold_never:
        return True, "added_old_never_chat"
    return False, ""


def _icebreaker_sort_key(item: tuple[RawCustomerSalesWechat, RawCustomer, SalesCustomerProfile | None, str]) -> tuple:
    rcsw, _rc, _scp, reason = item
    add_d = _dt_to_date(rcsw.add_time) or date.min
    last_d = _dt_to_date(rcsw.last_chat_time)
    ro = _ICEBREAKER_REASON_ORDER.get(reason, 9)
    if reason == "new_friend":
        return (ro, -add_d.toordinal())
    if reason == "long_no_chat":
        ld = last_d or date.min
        return (ro, ld.toordinal())
    return (ro, -add_d.toordinal())


async def load_recent_contact_tasks_by_customer(
    db,
    sales_wechat_id: str,
    ref_date: date,
    *,
    lookback_days: int = TASK_HISTORY_LOOKBACK_DAYS,
    per_customer: int = TASK_HISTORY_PER_CUSTOMER,
) -> dict[str, list[dict[str, Any]]]:
    """近 N 日联系任务（含昨日），按 raw_customer_id 聚合，供模型判断节奏与重复触达。"""
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return {}
    start = ref_date - timedelta(days=max(1, lookback_days))
    res = await db.execute(
        select(ContactTask)
        .where(ContactTask.sales_wechat_id == sw)
        .where(ContactTask.due_date >= start)
        .where(ContactTask.due_date <= ref_date)
        .order_by(desc(ContactTask.due_date), desc(ContactTask.id))
        .limit(2000)
    )
    by_customer: dict[str, list[dict[str, Any]]] = {}
    yesterday = ref_date - timedelta(days=1)
    for t in res.scalars().all():
        rid = (t.raw_customer_id or "").strip()
        if not rid:
            continue
        bucket = by_customer.setdefault(rid, [])
        if len(bucket) >= per_customer:
            continue
        due = t.due_date
        bucket.append(
            {
                "due_date": due.isoformat() if due else "",
                "status": (t.status or "").strip(),
                "title": (t.title or "").strip()[:120],
                "task_kind": (t.task_kind or "").strip(),
                "was_yesterday": bool(due and due == yesterday),
                "completed_at": t.completed_at.isoformat() if t.completed_at else "",
            }
        )
    return by_customer


async def load_allocation_customer_payloads(
    db,
    sales_wechat_id: str,
    *,
    limit: int = MAX_CUSTOMERS,
    ref_date: date | None = None,
) -> tuple[list[dict[str, Any]], dict[str, tuple[SalesCustomerProfile, RawCustomer]]]:
    """
    返回 (发给模型的客户 JSON 列表, raw_customer_id -> (scp, rc) 校验用映射)。
    """
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return [], {}

    pool_cap = max(limit, min(MAIN_SCORE_POOL_MAX, 2500))
    stmt = (
        select(SalesCustomerProfile, RawCustomer)
        .join(RawCustomer, RawCustomer.id == SalesCustomerProfile.raw_customer_id)
        .join(
            RawCustomerSalesWechat,
            and_(
                RawCustomerSalesWechat.raw_customer_id == SalesCustomerProfile.raw_customer_id,
                RawCustomerSalesWechat.sales_wechat_id == SalesCustomerProfile.sales_wechat_id,
            ),
        )
        .where(SalesCustomerProfile.sales_wechat_id == sw)
        .where(SalesCustomerProfile.profile_status == 1)
        .where(
            RawCustomerSalesWechat.is_deleted.is_(False)
            | RawCustomerSalesWechat.is_deleted.is_(None)
        )
    )
    rows = (await db.execute(stmt)).all()
    scp_ids = [rel.id for rel, _ in rows if rel and rel.id]
    tag_detail_map = await profile_tags_by_relation_ids(db, scp_ids)
    ref_date = ref_date or date.today()
    recent_tasks_map = await load_recent_contact_tasks_by_customer(db, sw, ref_date)
    last_main_due, _last_ice = await load_last_task_due_by_customer(db, sw)

    scored: list[tuple[float, int | None, str, int | None, SalesCustomerProfile, RawCustomer]] = []
    for scp, rc in rows:
        if not scp or not rc:
            continue
        rid = (scp.raw_customer_id or "").strip()
        if not rid:
            continue
        tags = tag_detail_map.get(scp.id, [])
        try:
            budget = float(scp.budget_amount or 0)
        except (TypeError, ValueError):
            budget = 0.0
        rule_score, tag_tier, band, days_since_main = compute_main_rule_score(
            ref_date=ref_date,
            tags=tags,
            ai_profile=(scp.ai_profile or ""),
            budget_amount=budget,
            suggested_followup_date=scp.suggested_followup_date,
            recent_tasks=recent_tasks_map.get(rid, []),
            last_main_task_due=last_main_due.get(rid),
        )
        scored.append((rule_score, tag_tier, band, days_since_main, scp, rc))

    scored.sort(key=lambda x: (-x[0], x[4].id or 0))
    rows_for_llm = scored[: max(1, min(limit, 200))]

    payloads: list[dict[str, Any]] = []
    lookup: dict[str, tuple[SalesCustomerProfile, RawCustomer]] = {}
    for rule_score, tag_tier, band, days_since_main, scp, rc in rows_for_llm:
        if not scp or not rc:
            continue
        rid = (scp.raw_customer_id or "").strip()
        if not rid:
            continue
        lookup[rid] = (scp, rc)
        ap = (scp.ai_profile or "").strip()
        if len(ap) > AI_PROFILE_MAX_CHARS:
            ap = ap[: AI_PROFILE_MAX_CHARS - 1].rstrip() + "…"
        try:
            budget = float(scp.budget_amount or 0)
        except (TypeError, ValueError):
            budget = 0.0
        tags = tag_detail_map.get(scp.id, [])
        payloads.append(
            {
                "raw_customer_id": rid,
                "scp_id": scp.id,
                "customer_name": (rc.customer_name or "").strip(),
                "unit_name": (rc.unit_name or "").strip(),
                "wechat_remark": (scp.wechat_remark or "").strip(),
                "suggested_followup_date": scp.suggested_followup_date.isoformat()
                if scp.suggested_followup_date
                else "",
                "budget_amount": budget,
                "purchase_type": (scp.purchase_type or "").strip(),
                "profile_tags": [str(t.get("name") or "") for t in tags if t.get("name")],
                "profile_tags_detail": compose_profile_tags_detail(tags),
                "recent_tasks": recent_tasks_map.get(rid, []),
                "rule_priority_score": rule_score,
                "tag_tier": tag_tier,
                "priority_band": band,
                "days_since_last_main_task": days_since_main,
                "ai_profile": ap,
            }
        )
    return payloads, lookup


async def load_icebreaker_customer_payloads(
    db,
    sales_wechat_id: str,
    ref_date: date,
    *,
    exclude_raw_ids: set[str],
    cap_for_llm: int,
    new_days: int = ICEBREAKER_NEW_DAYS,
    stale_days: int = ICEBREAKER_STALE_DAYS,
    per_query_limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, tuple[SalesCustomerProfile | None, RawCustomer | None]], dict[str, Any]]:
    """
    从 raw_customer_sales_wechats 筛「新加 / 长期未聊 / 从未私聊」好友，排除已在主线任务中的 raw_customer_id。
    返回 (LLM 快照列表, raw_customer_id -> (scp|None, rc), 统计信息)。
    """
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return [], {}, {"skipped": "empty_sw"}

    per_query_limit = per_query_limit or max(100, ICEBREAKER_SCORE_POOL_MAX // 2)
    _last_main, last_ice_due = await load_last_task_due_by_customer(db, sw)

    active = (RawCustomerSalesWechat.is_deleted.is_(False)) | (RawCustomerSalesWechat.is_deleted.is_(None))
    new_from = ref_date - timedelta(days=max(1, new_days) - 1)
    stale_before = ref_date - timedelta(days=max(1, stale_days))

    join_scp = and_(
        SalesCustomerProfile.raw_customer_id == RawCustomerSalesWechat.raw_customer_id,
        SalesCustomerProfile.sales_wechat_id == RawCustomerSalesWechat.sales_wechat_id,
    )

    base = (
        select(RawCustomerSalesWechat, RawCustomer, SalesCustomerProfile)
        .join(RawCustomer, RawCustomer.id == RawCustomerSalesWechat.raw_customer_id)
        .outerjoin(SalesCustomerProfile, join_scp)
        .where(RawCustomerSalesWechat.sales_wechat_id == sw)
        .where(active)
    )

    stmt_new = (
        base.where(RawCustomerSalesWechat.add_time.isnot(None))
        .where(cast(RawCustomerSalesWechat.add_time, Date) >= new_from)
        .order_by(desc(RawCustomerSalesWechat.add_time))
        .limit(per_query_limit)
    )
    stmt_stale = (
        base.where(
            or_(
                and_(
                    RawCustomerSalesWechat.last_chat_time.isnot(None),
                    cast(RawCustomerSalesWechat.last_chat_time, Date) <= stale_before,
                ),
                and_(
                    RawCustomerSalesWechat.last_chat_time.is_(None),
                    RawCustomerSalesWechat.add_time.isnot(None),
                    cast(RawCustomerSalesWechat.add_time, Date) < new_from,
                ),
            )
        )
        .order_by(RawCustomerSalesWechat.last_chat_time.asc(), RawCustomerSalesWechat.add_time.asc())
        .limit(per_query_limit)
    )

    merged: dict[str, tuple[RawCustomerSalesWechat, RawCustomer, SalesCustomerProfile | None, str]] = {}
    for stmt in (stmt_new, stmt_stale):
        rows = (await db.execute(stmt)).all()
        for rcsw, rc, scp in rows:
            if not rcsw or not rc:
                continue
            rid = (rcsw.raw_customer_id or "").strip()
            if not rid or rid.endswith("@chatroom"):
                continue
            ok, reason = _icebreaker_eligibility(rcsw, ref_date, new_days=new_days, stale_days=stale_days)
            if not ok:
                continue
            if rid in exclude_raw_ids:
                continue
            prev = merged.get(rid)
            if prev is None:
                merged[rid] = (rcsw, rc, scp, reason)
                continue
            prev_reason = prev[3]
            if _ICEBREAKER_REASON_ORDER.get(reason, 9) < _ICEBREAKER_REASON_ORDER.get(prev_reason, 9):
                merged[rid] = (rcsw, rc, scp, reason)

    ordered = sorted(
        merged.values(),
        key=lambda it: icebreaker_fair_sort_key(
            it,
            ref_date=ref_date,
            last_ice_due=last_ice_due,
            reason_order=_ICEBREAKER_REASON_ORDER,
        ),
    )
    pool_take = min(len(ordered), ICEBREAKER_SCORE_POOL_MAX, ICEBREAKER_MAX_FETCH)
    take = min(max(1, cap_for_llm), pool_take)
    picked = ordered[:take]

    scp_ids = [int(scp.id) for _a, _b, scp, _r in picked if scp and scp.id]
    tag_detail_map = await profile_tags_by_relation_ids(db, scp_ids)
    recent_tasks_map = await load_recent_contact_tasks_by_customer(db, sw, ref_date)

    payloads: list[dict[str, Any]] = []
    lookup: dict[str, tuple[SalesCustomerProfile | None, RawCustomer | None]] = {}
    for rcsw, rc, scp, reason in picked:
        rid = (rcsw.raw_customer_id or "").strip()
        ap = ""
        if scp:
            ap = (scp.ai_profile or "").strip()
            if len(ap) > AI_PROFILE_MAX_CHARS:
                ap = ap[: AI_PROFILE_MAX_CHARS - 1].rstrip() + "…"
        remark = (scp.wechat_remark if scp else "") or (rcsw.remark or "") or (rcsw.alias or "")
        tags = tag_detail_map.get(scp.id, []) if scp else []
        last_ice = last_ice_due.get(rid)
        days_since_ice = None
        if last_ice is not None:
            days_since_ice = max(0, (ref_date - last_ice).days)
        payloads.append(
            {
                "raw_customer_id": rid,
                "scp_id": scp.id if scp else None,
                "customer_name": (rc.customer_name or "").strip(),
                "unit_name": (rc.unit_name or "").strip(),
                "wechat_remark": remark.strip(),
                "add_time": rcsw.add_time.isoformat() if rcsw.add_time else "",
                "last_chat_time": rcsw.last_chat_time.isoformat() if rcsw.last_chat_time else "",
                "icebreaker_reason": reason,
                "profile_tags": [str(t.get("name") or "") for t in tags if t.get("name")],
                "profile_tags_detail": compose_profile_tags_detail(tags),
                "recent_tasks": recent_tasks_map.get(rid, []),
                "days_since_last_icebreaker": days_since_ice,
                "ai_profile": ap,
            }
        )
        lookup[rid] = (scp, rc)

    stats = {
        "merged_candidates": len(merged),
        "sent_to_llm": len(payloads),
        "new_days": new_days,
        "stale_days": stale_days,
        "rotation": "last_icebreaker_due_asc",
    }
    return payloads, lookup, stats


async def build_scenario_task_messages(
    db,
    *,
    scenario_key: str,
    fallback_system: str,
    fallback_user: str,
    ctx: dict[str, Any],
    local_doc_keys: tuple[str, ...],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if await _use_db_prompts(db):
        store = get_prompt_store()
        version = await store.get_published_version(scenario_key)
        if version:
            docs_map: dict[str, tuple[str, int | None]] = {}
            for spec in version.doc_refs or []:
                c, vid = await store.get_doc_text(spec.doc_key, spec.doc_version_id)
                docs_map[spec.doc_key] = (c, vid)
            system_text = render_system(version.template, ctx, docs_map, version.doc_refs or [])
            user_src = (version.template.user or "").strip() or fallback_user
            user_text = render_system(PromptTemplate(system=user_src), ctx, {}, ())
            messages = [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ]
            meta = {
                "prompt_source": "db",
                "scenario_key": scenario_key,
                "prompt_version_id": getattr(version, "id", None),
                "prompt_version": getattr(version, "version", None),
            }
            return messages, meta

    store = get_prompt_store()
    docs_map: dict[str, tuple[str, int | None]] = {}
    refs: list[DocInjectSpec] = []
    for key in local_doc_keys:
        spec = _LOCAL_DOC_SPECS.get(key)
        if not spec:
            continue
        c, vid = await store.get_doc_text(key, None)
        docs_map[key] = (c or "", vid)
        refs.append(spec)
    system_text = render_system(PromptTemplate(system=fallback_system), ctx, docs_map, refs)
    user_text = render_system(PromptTemplate(system=fallback_user), ctx, {}, ())
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]
    meta = {"prompt_source": "local", "scenario_key": scenario_key}
    return messages, meta


async def build_task_allocation_messages(
    db,
    *,
    sales_wechat_id: str,
    period_type: str,
    period_start: date,
    period_end: date,
    ref_today: date,
    task_cap: int,
    customer_payloads: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    customers_json = json.dumps(customer_payloads, ensure_ascii=False, indent=2)
    tags_catalog = await load_profile_tags_catalog_text(db)
    ctx: dict[str, Any] = {
        "current_date": ref_today.isoformat(),
        "sales_wechat_id": sales_wechat_id,
        "period_type": period_type,
        "period_type_label": _period_type_label(period_type),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "ref_today": ref_today.isoformat(),
        "task_cap": str(int(task_cap)),
        "profile_tags_catalog": tags_catalog,
        "customers_json": customers_json,
    }
    return await build_scenario_task_messages(
        db,
        scenario_key=SCENARIO_KEY,
        fallback_system=TASK_ALLOCATION_SYSTEM,
        fallback_user=TASK_ALLOCATION_USER.strip(),
        ctx=ctx,
        local_doc_keys=("scoring_criteria", "profile_tags_detail", "strategy"),
    )


async def build_icebreaker_task_messages(
    db,
    *,
    sales_wechat_id: str,
    ref_today: date,
    task_cap: int,
    customer_payloads: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    customers_json = json.dumps(customer_payloads, ensure_ascii=False, indent=2)
    ctx: dict[str, Any] = {
        "current_date": ref_today.isoformat(),
        "sales_wechat_id": sales_wechat_id,
        "ref_today": ref_today.isoformat(),
        "task_cap": str(int(task_cap)),
        "ice_new_days": str(ICEBREAKER_NEW_DAYS),
        "ice_stale_days": str(ICEBREAKER_STALE_DAYS),
        "customers_json": customers_json,
    }
    return await build_scenario_task_messages(
        db,
        scenario_key=SCENARIO_ICEBREAKER_KEY,
        fallback_system=TASK_ICEBREAKER_SYSTEM,
        fallback_user=TASK_ICEBREAKER_USER.strip(),
        ctx=ctx,
        local_doc_keys=("opening", "scoring_criteria", "strategy"),
    )


async def run_task_allocation_llm(
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
    scenario_key: str = SCENARIO_KEY,
    log_tag: str = "TASK_ALLOCATION_DEBUG",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    调用 LLM，返回 (tasks 数组元素 dict 列表, 审计 meta)。
    tasks 元素含 raw_customer_id, priority_rank, title, instruction, task_kind, priority_score(可选)
    """
    if scenario_key == SCENARIO_ICEBREAKER_KEY:
        messages, meta = await build_icebreaker_task_messages(
            db,
            sales_wechat_id=sales_wechat_id,
            ref_today=ref_today,
            task_cap=task_cap,
            customer_payloads=customer_payloads,
        )
    else:
        messages, meta = await build_task_allocation_messages(
            db,
            sales_wechat_id=sales_wechat_id,
            period_type=period_type,
            period_start=period_start,
            period_end=period_end,
            ref_today=ref_today,
            task_cap=task_cap,
            customer_payloads=customer_payloads,
        )
    _log_allocation_io(
        log_tag=log_tag,
        sales_wechat_id=sales_wechat_id,
        messages=messages,
        meta=meta,
        customer_payloads=customer_payloads,
    )
    full = ""
    try:
        async for chunk in llm.stream_chat(messages, temperature=TEMPERATURE, max_tokens=MAX_TOKENS):
            if chunk.startswith("__TOOL_CALL__:") or chunk.startswith("__REASONING_CONTENT__:"):
                continue
            full += chunk
    except Exception as e:
        logger.exception("任务分配 LLM 调用失败 sw={} scenario={}: {}", sales_wechat_id, scenario_key, e)
        meta["llm_error"] = str(e)
        return [], meta

    meta["llm_response_preview"] = (full[:800] + ("…" if len(full) > 800 else ""))
    data = _extract_first_json_object(full)
    if not data:
        logger.warning(
            "任务分配 LLM 未解析到 JSON sw={} scenario={} preview={}",
            sales_wechat_id,
            scenario_key,
            (full[:400] + ("…" if len(full) > 400 else "")),
        )
        meta["parse_error"] = "no_json"
        return [], meta

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list):
        meta["parse_error"] = "tasks_not_list"
        return [], meta

    out: list[dict[str, Any]] = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("raw_customer_id") or "").strip()
        if not rid:
            continue
        out.append(item)
    meta["rationale"] = data.get("rationale")
    return out, meta


def normalize_llm_tasks(
    llm_rows: list[dict[str, Any]],
    lookup: dict[str, tuple[SalesCustomerProfile | None, RawCustomer | None]],
    *,
    task_cap: int,
    kind_default: str = "contact",
    allow_missing_scp: bool = False,
) -> list[dict[str, Any]]:
    """校验 raw_customer_id、去重、截断条数，输出稳定结构供写库。"""
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    allowed_kinds = frozenset({"contact", "follow_up", "close_deal", "revisit", "icebreaker"})
    for item in llm_rows:
        rid = str(item.get("raw_customer_id") or "").strip()
        if rid in seen:
            continue
        pair = lookup.get(rid)
        if not pair:
            continue
        scp, rc = pair
        if rc is None:
            continue
        if not allow_missing_scp and scp is None:
            continue
        seen.add(rid)
        title = str(item.get("title") or "联系客户").strip()[:200]
        instruction = str(item.get("instruction") or "查看画像并主动跟进").strip()[:2000]
        kind = str(item.get("task_kind") or kind_default).strip()[:30]
        if kind not in allowed_kinds:
            kind = kind_default
        pr = item.get("priority_rank")
        try:
            priority_rank = int(pr) if pr is not None else len(normalized) + 1
        except (TypeError, ValueError):
            priority_rank = len(normalized) + 1
        ps = item.get("priority_score")
        priority_score = None
        if ps is not None:
            try:
                priority_score = float(ps)
            except (TypeError, ValueError):
                priority_score = None
        normalized.append(
            {
                "raw_customer_id": rid,
                "title": title,
                "instruction": instruction,
                "task_kind": kind,
                "priority_rank": priority_rank,
                "priority_score": priority_score,
            }
        )
        if len(normalized) >= task_cap:
            break
    normalized.sort(key=lambda x: (x["priority_rank"], x["raw_customer_id"]))
    for i, row in enumerate(normalized, start=1):
        row["priority_rank"] = i
    return normalized
