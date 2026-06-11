"""
任务分配 LLM：读取管理平台已发布的 task_allocation / task_allocation_icebreaker 场景，
使用独立的 task_allocation_llm_* 配置（可经环境变量覆盖），解析 JSON 得到联系任务列表。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import Date, and_, cast, desc, func, or_
from sqlalchemy.future import select

from ai.chat_log_filter import raw_chat_log_meaningful_clause
from ai.context import ContextAssembler
from ai.llm_client import LLMClient
from ai.llm_usage import LLMUsageContext
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
    _fetch_ai_system_configs,
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
    should_skip_repeat_contact_today,
)
from ai.profile_staff_tag import has_staff_profile_tag
from crud import profile_tags_by_relation_ids
from models import (
    ContactTask,
    RawChatLog,
    RawCustomer,
    RawCustomerSalesWechat,
    SalesCustomerProfile,
    SystemConfig,
)

SCENARIO_KEY = "task_allocation"
SCENARIO_ICEBREAKER_KEY = "task_allocation_icebreaker"

SHANGHAI_TZ = timezone(timedelta(hours=8))

MAX_CUSTOMERS = int(os.getenv("TASK_ALLOCATION_MAX_CUSTOMERS") or "120")
AI_PROFILE_MAX_CHARS = int(os.getenv("TASK_ALLOCATION_AI_PROFILE_MAX_CHARS") or "1500")
TEMPERATURE = float(os.getenv("TASK_ALLOCATION_TEMPERATURE") or "0.25")
MAX_TOKENS = int(os.getenv("TASK_ALLOCATION_MAX_TOKENS") or "8192")
USE_STREAM_FOR_ALLOCATION = str(os.getenv("TASK_ALLOCATION_USE_STREAM") or "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

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
# 可选单次送入破冰 LLM 的客户条数硬上限；0=不限制（仅用 fetch/dynamic/max_fetch）
ICEBREAKER_LLM_INPUT_CAP = int(os.getenv("TASK_ICEBREAKER_LLM_INPUT_CAP") or "60")
ICEBREAKER_AI_PROFILE_MAX_CHARS = int(os.getenv("TASK_ICEBREAKER_AI_PROFILE_MAX_CHARS") or "280")
ICEBREAKER_MAX_TOKENS = int(os.getenv("TASK_ICEBREAKER_MAX_TOKENS") or "8192")

TASK_HISTORY_LOOKBACK_DAYS = int(os.getenv("TASK_ALLOCATION_HISTORY_DAYS") or "14")
TASK_HISTORY_PER_CUSTOMER = int(os.getenv("TASK_ALLOCATION_HISTORY_PER_CUSTOMER") or "5")

_DEBUG_PROMPT = str(os.getenv("TASK_ALLOCATION_DEBUG_PROMPT") or "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_DEBUG_CHUNK = int(os.getenv("TASK_ALLOCATION_DEBUG_MAX_CHARS") or "8000")

_DEFAULT_LLM_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DEFAULT_LLM_MODEL = "qwen-max"

_TASK_ALLOCATION_LLM_CONFIG_KEYS = (
    "task_allocation_llm_api_url",
    "task_allocation_llm_api_key",
    "task_allocation_llm_model",
)


async def _fetch_task_allocation_llm_configs(db) -> dict[str, str]:
    """
    任务分配 LLM 配置：专属键按 config_key 读取（不限 config_group），
    回退项（profile_llm_* / llm_*）仍从 ai 组读取。

    说明：管理后台 on_model_change 曾将 task_allocation_llm_* 误归入 task 组，
    若仅查 ai 组会导致用户配置被忽略。
    """
    configs = await _fetch_ai_system_configs(db)
    stmt = select(SystemConfig).where(SystemConfig.config_key.in_(_TASK_ALLOCATION_LLM_CONFIG_KEYS))
    res = await db.execute(stmt)
    for row in res.scalars().all():
        configs[row.config_key] = row.config_value or ""
    return configs


def _resolve_task_allocation_llm_config(configs: dict[str, str]) -> tuple[str, str, str]:
    """
    任务分配 LLM 配置优先级：
    1) system_configs.task_allocation_llm_*
    2) 环境变量 TASK_ALLOCATION_LLM_*
    3) 画像 profile_llm_*（兼容未单独配置时的存量行为）
    4) 历史 llm_* / 默认值
    """
    api_url = (
        (configs.get("task_allocation_llm_api_url") or "").strip()
        or (os.getenv("TASK_ALLOCATION_LLM_API_URL") or "").strip()
        or (configs.get("profile_llm_api_url") or "").strip()
        or (configs.get("llm_api_url") or "").strip()
        or _DEFAULT_LLM_API_URL
    )
    api_key = (
        (configs.get("task_allocation_llm_api_key") or "").strip()
        or (os.getenv("TASK_ALLOCATION_LLM_API_KEY") or "").strip()
        or (configs.get("profile_llm_api_key") or "").strip()
        or (configs.get("llm_api_key") or "").strip()
    )
    model = (
        (configs.get("task_allocation_llm_model") or "").strip()
        or (os.getenv("TASK_ALLOCATION_LLM_MODEL") or "").strip()
        or (configs.get("profile_llm_model") or "").strip()
        or (configs.get("llm_model") or "").strip()
        or _DEFAULT_LLM_MODEL
    )
    return api_url, api_key, model


async def get_task_allocation_llm_display(db) -> dict[str, str]:
    """管理端展示用：当前生效的任务分配模型与 API 主机（不含密钥）。"""
    configs = await _fetch_task_allocation_llm_configs(db)
    api_url, _, model = _resolve_task_allocation_llm_config(configs)
    try:
        host = (urlparse(api_url).netloc or api_url)[:120]
    except Exception:
        host = "—"
    return {"model": model, "api_host": host}


async def get_task_allocation_llm_client(db) -> LLMClient:
    """任务分配专用 LLM（与画像分析 profile_llm_* 隔离，可独立配置）。"""
    configs = await _fetch_task_allocation_llm_configs(db)
    api_url, api_key, model = _resolve_task_allocation_llm_config(configs)
    try:
        api_host = (urlparse(api_url).netloc or api_url)[:120]
    except Exception:
        api_host = api_url[:120]
    logger.info("任务分配 LLM 配置生效 model={} api_host={}", model, api_host)
    return LLMClient(api_url=api_url, api_key=api_key, model=model)


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

_ICEBREAKER_FALLBACK_INSTRUCTION: dict[str, str] = {
    "new_friend": "新加好友：简短自我介绍，确认身份与单位，轻量寒暄，勿一上来推品压单。",
    "long_no_chat": "客户长期未回复：以关怀问候重新激活，可轻提上次话题或节日祝福，语气自然。",
    "added_old_never_chat": "加好友后客户从未回复：发送首触问候与自我介绍，确认是否方便简短沟通。",
}


def _icebreaker_llm_input_cap(task_output_cap: int, fetch_cap: int) -> int:
    out_cap = max(1, int(task_output_cap))
    fetch = max(1, int(fetch_cap))
    dynamic = max(out_cap + 15, min(out_cap * 2, 80))
    upper = min(fetch, dynamic, ICEBREAKER_MAX_FETCH)
    if ICEBREAKER_LLM_INPUT_CAP > 0:
        upper = min(upper, ICEBREAKER_LLM_INPUT_CAP)
    return upper


def fallback_icebreaker_tasks_from_payloads(
    payloads: list[dict[str, Any]],
    *,
    task_cap: int,
    ref_date: date | None = None,
) -> list[dict[str, Any]]:
    """LLM 无产出或解析失败时，按已排序候选生成规则兜底破冰任务。"""
    cap = max(0, int(task_cap))
    if cap <= 0 or not payloads:
        return []
    ref = ref_date or date.today()
    rows: list[dict[str, Any]] = []
    rank = 0
    for p in payloads:
        if len(rows) >= cap:
            break
        rid = str(p.get("raw_customer_id") or "").strip()
        if not rid:
            continue
        if should_skip_repeat_contact_today(p.get("recent_tasks"), ref):
            continue
        rank += 1
        reason = str(p.get("icebreaker_reason") or "long_no_chat").strip()
        name = (
            str(p.get("customer_name") or "").strip()
            or str(p.get("wechat_remark") or "").strip()
            or "客户"
        )
        base_instr = _ICEBREAKER_FALLBACK_INSTRUCTION.get(reason, _ICEBREAKER_FALLBACK_INSTRUCTION["long_no_chat"])
        rows.append(
            {
                "raw_customer_id": rid,
                "priority_rank": rank,
                "priority_score": 70.0 if reason == "new_friend" else 55.0,
                "title": f"破冰 · {name}"[:200],
                "instruction": base_instr[:2000],
                "task_kind": "icebreaker",
            }
        )
    return rows


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


def _ms_to_date(ms: int | None) -> date | None:
    if ms is None:
        return None
    try:
        n = int(ms)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    try:
        return datetime.fromtimestamp(n / 1000).date()
    except (OSError, OverflowError, ValueError):
        return None


async def load_last_customer_reply_date_by_customer(
    db,
    sales_wechat_id: str,
) -> dict[str, date]:
    """
    按 (raw_customer_id) 聚合客户最近一次「有效回复」日期。
    仅统计 raw_chat_logs 中 is_send=0（客户发送）且非群发助手噪音的消息。
    """
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return {}

    ts_expr = func.coalesce(RawChatLog.time_ms, RawChatLog.timestamp, 0)
    meaningful = raw_chat_log_meaningful_clause(RawChatLog.text)

    stmt_a = (
        select(
            RawChatLog.talker.label("rid"),
            func.max(ts_expr).label("latest_ms"),
        )
        .where(
            RawChatLog.wechat_id == sw,
            RawChatLog.is_send == 0,
            meaningful,
            ~RawChatLog.talker.like("%@chatroom%"),
        )
        .group_by(RawChatLog.talker)
    )
    stmt_b = (
        select(
            RawChatLog.wechat_id.label("rid"),
            func.max(ts_expr).label("latest_ms"),
        )
        .where(
            RawChatLog.talker == sw,
            RawChatLog.is_send == 0,
            meaningful,
            ~RawChatLog.wechat_id.like("%@chatroom%"),
        )
        .group_by(RawChatLog.wechat_id)
    )

    out: dict[str, date] = {}
    for stmt in (stmt_a, stmt_b):
        for rid, latest_ms in (await db.execute(stmt)).all():
            rid_s = (rid or "").strip()
            if not rid_s:
                continue
            d = _ms_to_date(latest_ms)
            if d is None:
                continue
            prev = out.get(rid_s)
            if prev is None or d > prev:
                out[rid_s] = d
    return out


async def load_last_sales_outbound_date_by_customer(
    db,
    sales_wechat_id: str,
) -> dict[str, date]:
    """
    按 raw_customer_id 聚合销售最近一次「有效 outbound」日期（is_send=1）。
    用于破冰池排除昨日/今日已主动触达的客户。
    """
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return {}

    ts_expr = func.coalesce(RawChatLog.time_ms, RawChatLog.timestamp, 0)
    meaningful = raw_chat_log_meaningful_clause(RawChatLog.text)

    stmt_a = (
        select(
            RawChatLog.talker.label("rid"),
            func.max(ts_expr).label("latest_ms"),
        )
        .where(
            RawChatLog.wechat_id == sw,
            RawChatLog.is_send == 1,
            meaningful,
            ~RawChatLog.talker.like("%@chatroom%"),
        )
        .group_by(RawChatLog.talker)
    )
    stmt_b = (
        select(
            RawChatLog.wechat_id.label("rid"),
            func.max(ts_expr).label("latest_ms"),
        )
        .where(
            RawChatLog.talker == sw,
            RawChatLog.is_send == 1,
            meaningful,
            ~RawChatLog.wechat_id.like("%@chatroom%"),
        )
        .group_by(RawChatLog.wechat_id)
    )

    out: dict[str, date] = {}
    for stmt in (stmt_a, stmt_b):
        for rid, latest_ms in (await db.execute(stmt)).all():
            rid_s = (rid or "").strip()
            if not rid_s:
                continue
            d = _ms_to_date(latest_ms)
            if d is None:
                continue
            prev = out.get(rid_s)
            if prev is None or d > prev:
                out[rid_s] = d
    return out


def _icebreaker_eligibility(
    rcsw: RawCustomerSalesWechat,
    ref_date: date,
    *,
    new_days: int,
    stale_days: int,
    last_customer_reply_d: date | None = None,
) -> tuple[bool, str]:
    """判定是否属于破冰池：新加 / 客户长期未回复 / 加好友较早但客户从未回复。"""
    rid = (rcsw.raw_customer_id or "").strip()
    if not rid or rid.endswith("@chatroom"):
        return False, ""
    add_d = _dt_to_date(rcsw.add_time)
    new_from = ref_date - timedelta(days=max(1, new_days) - 1)
    stale_before = ref_date - timedelta(days=max(1, stale_days))

    is_new = add_d is not None and add_d >= new_from
    is_stale = last_customer_reply_d is not None and last_customer_reply_d <= stale_before
    is_cold_never = last_customer_reply_d is None and add_d is not None and add_d < new_from

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
    from ai.wechat_voice_stats import (
        empty_contact_voice_summary,
        load_contact_voice_summary_by_customer,
    )

    voice_summary_map = await load_contact_voice_summary_by_customer(db, sw, ref_date=ref_date)

    scored: list[tuple[float, int | None, str, int | None, SalesCustomerProfile, RawCustomer]] = []
    for scp, rc in rows:
        if not scp or not rc:
            continue
        rid = (scp.raw_customer_id or "").strip()
        if not rid:
            continue
        tags = tag_detail_map.get(scp.id, [])
        if has_staff_profile_tag(tags):
            continue
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
    pool_take = max(1, min(limit, pool_cap, len(scored)))
    rows_for_llm = scored[:pool_take]

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
        voice_summary = voice_summary_map.get(rid) or empty_contact_voice_summary()
        phone_display = (rc.phone_normalized or rc.phone or "").strip()
        payloads.append(
            {
                "raw_customer_id": rid,
                "scp_id": scp.id,
                "customer_name": (rc.customer_name or "").strip(),
                "unit_name": (rc.unit_name or "").strip(),
                "phone": phone_display,
                "phone_raw": (rc.phone or "").strip() or None,
                "phone_normalized": (rc.phone_normalized or "").strip() or None,
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
                "contact_voice_summary": voice_summary,
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
    task_output_cap: int = ICEBREAKER_CAP,
    new_days: int = ICEBREAKER_NEW_DAYS,
    stale_days: int = ICEBREAKER_STALE_DAYS,
    per_query_limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, tuple[SalesCustomerProfile | None, RawCustomer | None]], dict[str, Any]]:
    """
    从 raw_customer_sales_wechats 筛「新加 / 客户长期未回复 / 客户从未回复」好友，排除已在主线任务中的 raw_customer_id。
    「有效聊天」以 raw_chat_logs 中客户发送消息（is_send=0）为准，不用云客 lastChatTime（含销售单向问候）。
    返回 (LLM 快照列表, raw_customer_id -> (scp|None, rc), 统计信息)。
    """
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return [], {}, {"skipped": "empty_sw"}

    per_query_limit = per_query_limit or max(100, ICEBREAKER_SCORE_POOL_MAX // 2)
    _last_main, last_ice_due = await load_last_task_due_by_customer(db, sw)
    last_customer_reply_map = await load_last_customer_reply_date_by_customer(db, sw)
    recent_tasks_map = await load_recent_contact_tasks_by_customer(db, sw, ref_date)
    sales_outbound_map = await load_last_sales_outbound_date_by_customer(db, sw)

    active = (RawCustomerSalesWechat.is_deleted.is_(False)) | (RawCustomerSalesWechat.is_deleted.is_(None))
    new_from = ref_date - timedelta(days=max(1, new_days) - 1)

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
    # 非「近期新加」的好友；是否长期未聊由客户有效回复日（chat log）在 Python 侧判定
    stmt_stale = (
        base.where(
            or_(
                RawCustomerSalesWechat.add_time.is_(None),
                cast(RawCustomerSalesWechat.add_time, Date) < new_from,
            )
        )
        .order_by(RawCustomerSalesWechat.add_time.asc())
        .limit(per_query_limit)
    )

    merged: dict[str, tuple[RawCustomerSalesWechat, RawCustomer, SalesCustomerProfile | None, str]] = {}
    skipped_cooldown = 0
    for stmt in (stmt_new, stmt_stale):
        rows = (await db.execute(stmt)).all()
        for rcsw, rc, scp in rows:
            if not rcsw or not rc:
                continue
            rid = (rcsw.raw_customer_id or "").strip()
            if not rid or rid.endswith("@chatroom"):
                continue
            last_reply_d = last_customer_reply_map.get(rid)
            ok, reason = _icebreaker_eligibility(
                rcsw,
                ref_date,
                new_days=new_days,
                stale_days=stale_days,
                last_customer_reply_d=last_reply_d,
            )
            if not ok:
                continue
            if should_skip_repeat_contact_today(
                recent_tasks_map.get(rid),
                ref_date,
                last_sales_outbound=sales_outbound_map.get(rid),
            ):
                skipped_cooldown += 1
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

    ice_scp_ids = [int(v[2].id) for v in merged.values() if v[2] and v[2].id]
    if ice_scp_ids:
        ice_tag_map = await profile_tags_by_relation_ids(db, ice_scp_ids)
        merged = {
            rid: row
            for rid, row in merged.items()
            if not (
                row[2]
                and row[2].id
                and has_staff_profile_tag(ice_tag_map.get(row[2].id, []))
            )
        }

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
    take = _icebreaker_llm_input_cap(task_output_cap, cap_for_llm)
    take = min(take, pool_take)
    picked = ordered[:take]

    scp_ids = [int(scp.id) for _a, _b, scp, _r in picked if scp and scp.id]
    tag_detail_map = await profile_tags_by_relation_ids(db, scp_ids)

    payloads: list[dict[str, Any]] = []
    lookup: dict[str, tuple[SalesCustomerProfile | None, RawCustomer | None]] = {}
    for rcsw, rc, scp, reason in picked:
        if scp and scp.id and has_staff_profile_tag(tag_detail_map.get(scp.id, [])):
            continue
        rid = (rcsw.raw_customer_id or "").strip()
        ap = ""
        if scp:
            ap = (scp.ai_profile or "").strip()
            if len(ap) > ICEBREAKER_AI_PROFILE_MAX_CHARS:
                ap = ap[: ICEBREAKER_AI_PROFILE_MAX_CHARS - 1].rstrip() + "…"
        remark = (scp.wechat_remark if scp else "") or (rcsw.remark or "") or (rcsw.alias or "")
        tags = tag_detail_map.get(scp.id, []) if scp else []
        tag_names = [str(t.get("name") or "") for t in tags if t.get("name")][:6]
        last_ice = last_ice_due.get(rid)
        days_since_ice = None
        if last_ice is not None:
            days_since_ice = max(0, (ref_date - last_ice).days)
        last_reply_d = last_customer_reply_map.get(rid)
        recent = recent_tasks_map.get(rid, [])[:TASK_HISTORY_PER_CUSTOMER]
        payloads.append(
            {
                "raw_customer_id": rid,
                "scp_id": scp.id if scp else None,
                "customer_name": (rc.customer_name or "").strip(),
                "unit_name": (rc.unit_name or "").strip(),
                "wechat_remark": remark.strip(),
                "add_time": rcsw.add_time.isoformat() if rcsw.add_time else "",
                "last_customer_reply_date": last_reply_d.isoformat() if last_reply_d else "",
                "icebreaker_reason": reason,
                "profile_tags": tag_names,
                "recent_tasks": recent,
                "days_since_last_icebreaker": days_since_ice,
                "ai_profile": ap,
            }
        )
        lookup[rid] = (scp, rc)

    stats = {
        "merged_candidates": len(merged),
        "skipped_contact_cooldown": skipped_cooldown,
        "pool_ranked": len(ordered),
        "sent_to_llm": len(payloads),
        "llm_input_cap": take,
        "task_output_cap": int(task_output_cap),
        "new_days": new_days,
        "stale_days": stale_days,
        "effective_chat": "raw_chat_logs.is_send=0",
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


async def _llm_complete_text(
    llm: LLMClient,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    usage: LLMUsageContext | None = None,
) -> str:
    """任务分配默认非流式，避免长输出流中断；超时/网络错误时重试一次。"""
    import httpx

    retry_types = (
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
        httpx.ReadError,
        httpx.ConnectError,
        httpx.RemoteProtocolError,
    )
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            if USE_STREAM_FOR_ALLOCATION:
                full = ""
                async for chunk in llm.stream_chat(
                    messages, temperature=TEMPERATURE, max_tokens=max_tokens, usage=usage
                ):
                    if chunk.startswith("__TOOL_CALL__:") or chunk.startswith("__REASONING_CONTENT__:"):
                        continue
                    full += chunk
                return full
            data = await llm.chat(
                messages, temperature=TEMPERATURE, max_tokens=max_tokens, usage=usage
            )
            choices = data.get("choices") or []
            if not choices:
                return ""
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("text"):
                        parts.append(str(p["text"]))
                return "".join(parts)
            return str(content or "")
        except retry_types as e:
            last_err = e
            if attempt == 0:
                logger.warning("任务分配 LLM 超时/网络错误，重试一次: {}", e)
                continue
            raise
    if last_err:
        raise last_err
    return ""


async def build_task_allocation_messages(
    db,
    *,
    sales_wechat_id: str,
    period_type: str,
    period_start: date,
    period_end: date,
    ref_today: date,
    task_cap: int,
    wechat_cap: int | None = None,
    phone_cap: int | None = None,
    customer_payloads: list[dict[str, Any]] | None = None,
    customer_features: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if customer_features is not None:
        customers_json = json.dumps(customer_features, ensure_ascii=False, separators=(",", ":"))
    else:
        customers_json = json.dumps(customer_payloads or [], ensure_ascii=False, separators=(",", ":"))
    tags_catalog = await load_profile_tags_catalog_text(db)
    cap = int(task_cap)
    w_cap = int(wechat_cap) if wechat_cap is not None else cap
    p_cap = int(phone_cap) if phone_cap is not None else 0
    from ai.task_allocation_limits import scale_channel_caps_to_task_cap
    from ai.wechat_voice_stats import DEFAULT_LOOKBACK_DAYS

    w_cap, p_cap = scale_channel_caps_to_task_cap(cap, w_cap, p_cap)

    ctx: dict[str, Any] = {
        "current_date": ref_today.isoformat(),
        "sales_wechat_id": sales_wechat_id,
        "period_type": period_type,
        "period_type_label": _period_type_label(period_type),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "ref_today": ref_today.isoformat(),
        "task_cap": str(cap),
        "wechat_cap": str(w_cap),
        "phone_cap": str(p_cap),
        "lookback_days": str(DEFAULT_LOOKBACK_DAYS),
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
    customers_json = json.dumps(customer_payloads, ensure_ascii=False, separators=(",", ":"))
    identity = await ContextAssembler(db).assemble_sales_identity_for_wechat(sales_wechat_id)
    ctx: dict[str, Any] = {
        "current_date": ref_today.isoformat(),
        "sales_wechat_id": sales_wechat_id,
        "ref_today": ref_today.isoformat(),
        "task_cap": str(int(task_cap)),
        "ice_new_days": str(ICEBREAKER_NEW_DAYS),
        "ice_stale_days": str(ICEBREAKER_STALE_DAYS),
        "customers_json": customers_json,
        "staff_identity": identity.get("staff_identity") or "未登记",
        "sales_wechat_persona": identity.get("sales_wechat_persona") or "",
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
    customer_features: list[dict[str, Any]] | None = None,
    wechat_cap: int | None = None,
    phone_cap: int | None = None,
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
            wechat_cap=wechat_cap,
            phone_cap=phone_cap,
            customer_payloads=customer_payloads if customer_features is None else None,
            customer_features=customer_features,
        )
    _log_allocation_io(
        log_tag=log_tag,
        sales_wechat_id=sales_wechat_id,
        messages=messages,
        meta=meta,
        customer_payloads=customer_payloads,
    )
    max_out_tokens = ICEBREAKER_MAX_TOKENS if scenario_key == SCENARIO_ICEBREAKER_KEY else MAX_TOKENS
    try:
        full = await _llm_complete_text(
            llm,
            messages,
            max_tokens=max_out_tokens,
            usage=LLMUsageContext(scenario_key=scenario_key),
        )
    except Exception as e:
        logger.exception("任务分配 LLM 调用失败 sw={} scenario={}: {}", sales_wechat_id, scenario_key, e)
        meta["llm_error"] = str(e)
        return [], meta
    meta["llm_non_stream"] = not USE_STREAM_FOR_ALLOCATION

    meta["llm_response_preview"] = (full[:800] + ("…" if len(full) > 800 else ""))
    meta["llm_response_len"] = len(full)
    data = _extract_first_json_object(full)
    if not data:
        logger.warning(
            "任务分配 LLM 未解析到 JSON sw={} scenario={} response_len={} preview={}",
            sales_wechat_id,
            scenario_key,
            len(full),
            (full[:400] + ("…" if len(full) > 400 else "")),
        )
        meta["parse_error"] = "no_json"
        return [], meta

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list):
        logger.warning(
            "任务分配 LLM tasks 非列表 sw={} scenario={} type={}",
            sales_wechat_id,
            scenario_key,
            type(raw_tasks).__name__,
        )
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
    meta["tasks_parsed"] = len(out)
    if not out and customer_payloads:
        logger.warning(
            "任务分配 LLM 返回空 tasks sw={} scenario={} candidates={} rationale={}",
            sales_wechat_id,
            scenario_key,
            len(customer_payloads),
            (str(data.get("rationale") or "")[:200]),
        )
        meta["parse_error"] = meta.get("parse_error") or "empty_tasks"
    return out, meta


async def run_task_allocation_llm_batch(
    db,
    llm: LLMClient,
    *,
    sales_wechat_id: str,
    period_type: str,
    period_start: date,
    period_end: date,
    ref_today: date,
    task_cap: int,
    customer_features: list[dict[str, Any]],
    wechat_cap: int | None = None,
    phone_cap: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Phase C 单批：输入 CustomerFeature 列表，非流式 LLM。"""
    messages, meta = await build_task_allocation_messages(
        db,
        sales_wechat_id=sales_wechat_id,
        period_type=period_type,
        period_start=period_start,
        period_end=period_end,
        ref_today=ref_today,
        task_cap=task_cap,
        wechat_cap=wechat_cap,
        phone_cap=phone_cap,
        customer_features=customer_features,
    )
    _log_allocation_io(
        log_tag="TASK_ALLOCATION_DEBUG",
        sales_wechat_id=sales_wechat_id,
        messages=messages,
        meta=meta,
        customer_payloads=customer_features,
    )
    max_out_tokens = MAX_TOKENS
    try:
        full = await _llm_complete_text(
            llm,
            messages,
            max_tokens=max_out_tokens,
            usage=LLMUsageContext(scenario_key=SCENARIO_KEY),
        )
    except Exception as e:
        logger.exception("任务分配分批 LLM 失败 sw={}: {}", sales_wechat_id, e)
        meta["llm_error"] = str(e)
        return [], meta

    meta["llm_response_len"] = len(full)
    meta["llm_non_stream"] = not USE_STREAM_FOR_ALLOCATION
    data = _extract_first_json_object(full)
    if not data:
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
        if rid:
            out.append(item)
    meta["tasks_parsed"] = len(out)
    meta["rationale"] = data.get("rationale")
    return out, meta


def normalize_llm_tasks(
    llm_rows: list[dict[str, Any]],
    lookup: dict[str, tuple[SalesCustomerProfile | None, RawCustomer | None]],
    *,
    task_cap: int,
    kind_default: str = "contact",
    allow_missing_scp: bool = False,
    wechat_cap: int | None = None,
    phone_cap: int | None = None,
) -> list[dict[str, Any]]:
    """校验 raw_customer_id、去重、截断条数，输出稳定结构供写库。"""
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    allowed_kinds = frozenset({"contact", "follow_up", "close_deal", "revisit", "icebreaker"})
    allowed_channels = frozenset({"wechat", "phone"})
    w_limit = int(wechat_cap) if wechat_cap is not None else int(task_cap)
    p_limit = int(phone_cap) if phone_cap is not None else 0
    w_count = 0
    p_count = 0
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
        channel = str(item.get("contact_channel") or "wechat").strip().lower()[:20]
        if channel not in allowed_channels:
            channel = "wechat"
        if channel == "phone":
            if p_count >= p_limit:
                continue
            p_count += 1
        else:
            if w_count >= w_limit:
                continue
            w_count += 1
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
                "contact_channel": channel,
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


def _target_phone_count(n: int, wechat_cap: int, phone_cap: int) -> int:
    """按渠道上限比例计算本批应有多少条电话（至少留 1 条微信当 n≥2）。"""
    n = max(0, int(n))
    w_cap = max(0, int(wechat_cap))
    p_cap = max(0, int(phone_cap))
    if n <= 0 or p_cap <= 0:
        return 0
    if w_cap <= 0:
        return min(p_cap, n)
    total = w_cap + p_cap
    tgt = int(round(n * p_cap / total)) if total > 0 else 0
    tgt = max(0, min(p_cap, tgt))
    if n >= 2 and w_cap > 0 and p_cap > 0:
        tgt = max(1, tgt)
        if tgt >= n:
            tgt = n - 1
    return tgt


def balance_main_channel_tasks(
    rows: list[dict[str, Any]],
    *,
    wechat_cap: int,
    phone_cap: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    按 wechat_cap / phone_cap 比例校正渠道标签，避免全 wx 或全 ph。
    仅在 LLM/兜底后渠道严重失衡时调整标签，不改变已选客户集合。
    """
    meta: dict[str, Any] = {"adjusted": 0}
    n = len(rows)
    if n <= 0:
        return rows, meta
    w_cap = max(0, int(wechat_cap))
    p_cap = max(0, int(phone_cap))
    target_phone = _target_phone_count(n, w_cap, p_cap)
    target_wechat = n - target_phone
    meta["target_phone"] = target_phone
    meta["target_wechat"] = target_wechat

    def _score(row: dict[str, Any]) -> tuple:
        return (-float(row.get("priority_score") or 0), int(row.get("priority_rank") or 999))

    phone_rows = [r for r in rows if (r.get("contact_channel") or "") == "phone"]
    phone_count = len(phone_rows)

    if phone_count == target_phone:
        return rows, meta

    if phone_count < target_phone:
        need = target_phone - phone_count
        for row in sorted(rows, key=_score):
            if need <= 0:
                break
            if (row.get("contact_channel") or "wechat") == "phone":
                continue
            row["contact_channel"] = "phone"
            title = str(row.get("title") or "").strip()
            if title and "电话" not in title:
                row["title"] = f"电话·{title[:190]}"
            elif not title:
                row["title"] = "电话跟进"
            need -= 1
            meta["adjusted"] += 1
        meta["action"] = "promote_to_phone"
    else:
        need = phone_count - target_phone
        for row in sorted(
            phone_rows,
            key=lambda r: (
                float(r.get("priority_score") or 0),
                int(r.get("priority_rank") or 999),
            ),
        ):
            if need <= 0:
                break
            row["contact_channel"] = "wechat"
            title = str(row.get("title") or "").strip()
            if title.startswith("电话·"):
                row["title"] = title[3:].strip() or "微信跟进"
            need -= 1
            meta["adjusted"] += 1
        meta["action"] = "demote_to_wechat"

    return rows, meta


def backfill_phone_channel_tasks(
    rows: list[dict[str, Any]],
    *,
    phone_cap: int,
    wechat_cap: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """兼容旧调用：委托给 balance_main_channel_tasks。"""
    w_cap = int(wechat_cap) if wechat_cap is not None else max(0, len(rows) - int(phone_cap))
    balanced, meta = balance_main_channel_tasks(
        rows, wechat_cap=w_cap, phone_cap=phone_cap
    )
    return balanced, int(meta.get("adjusted") or 0)
