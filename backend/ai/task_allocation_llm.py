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
    extract_followup_from_ai_profile,
    load_profile_tags_catalog_text,
)
from core.logger import logger
from ai.time_context import (
    is_school_defer_window,
    is_school_unit,
    resolve_unit_segment,
)
from ai.task_allocation_ranking import (
    MAIN_SCORE_POOL_MAX,
    ICEBREAKER_SCORE_POOL_MAX,
    compute_main_rule_score,
    icebreaker_fair_sort_key,
    load_last_task_due_by_customer,
    resolve_scoring_weights,
    should_skip_icebreaker_repeat_today,
    should_skip_repeat_contact_today,
)
from ai.profile_staff_tag import has_staff_profile_tag
from ai.profile_followup_policy import has_no_followup_profile_tag
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
ICEBREAKER_NEW_DAYS = int(os.getenv("TASK_ICEBREAKER_NEW_DAYS") or "7")
ICEBREAKER_STALE_DAYS = int(os.getenv("TASK_ICEBREAKER_STALE_DAYS") or "30")
ICEBREAKER_LAPSED_DAYS = int(os.getenv("TASK_ICEBREAKER_LAPSED_DAYS") or "14")
ICEBREAKER_COOLDOWN_DAYS = int(os.getenv("TASK_ICEBREAKER_COOLDOWN_DAYS") or "2")
# 销售近 N 日已给客户发过有效消息则不进激活池
ICEBREAKER_OUTBOUND_QUIET_DAYS = int(os.getenv("TASK_ICEBREAKER_OUTBOUND_QUIET_DAYS") or "10")
# 实际条数由 task_allocation.resolve_icebreaker_task_cap() 决定；此处仅作模块默认参考
ICEBREAKER_CAP = int(os.getenv("TASK_ICEBREAKER_CAP") or "25")
ICEBREAKER_MAX_FETCH = int(os.getenv("TASK_ICEBREAKER_MAX_CANDIDATES") or "200")
# 可选送入破冰 LLM 的客户条数硬上限；0=不限制（按产出上限与 fetch 动态放大）
# 旧默认 40 会在大好友池下把候选压死，导致激活任务长期卡在几十/个位数
ICEBREAKER_LLM_INPUT_CAP = int(os.getenv("TASK_ICEBREAKER_LLM_INPUT_CAP") or "0")
ICEBREAKER_LLM_CHUNK_SIZE = int(os.getenv("TASK_ICEBREAKER_LLM_CHUNK_SIZE") or "25")
# 非新加好友扫描上限：过小会只扫到「加好友最早」的一批，漏掉中后期沉默客户
ICEBREAKER_SCAN_LIMIT = int(os.getenv("TASK_ICEBREAKER_SCAN_LIMIT") or "3000")
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
        title="客户动态标签说明（仅标签；profile_tags_detail）",
        required=False,
        max_chars=12000,
    ),
    "unit_followup_playbook": DocInjectSpec(
        doc_key="unit_followup_playbook",
        title="单位性质跟进策略手册（非客户动态标签；key=unit_followup_playbook）",
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

_ICEBREAKER_REASON_ORDER = {
    "new_friend": 0,
    "long_no_chat": 1,
    "lapsed_contact": 2,
    "added_old_never_chat": 3,
}

_ICEBREAKER_FALLBACK_INSTRUCTION: dict[str, str] = {
    "new_friend": "新加好友：简短自我介绍，确认身份与单位，轻量寒暄，勿一上来推品压单。",
    "long_no_chat": "客户长期未回复：以关怀问候重新激活，可轻提上次话题或节日祝福，语气自然。",
    "lapsed_contact": "客户近期互动变少：轻量问候或价值分享，自然续聊，勿强推。",
    "added_old_never_chat": "加好友后客户从未回复：发送首触问候与自我介绍，确认是否方便简短沟通。",
}


def normalize_activation_task_title(title: str | None) -> str:
    """激活任务标题统一为「激活 · …」，兼容旧提示词/LLM 输出的「破冰」前缀。"""
    import re

    t = str(title or "").strip()
    if not t:
        return "激活"
    t = re.sub(r"^破冰\s*[·\-—]\s*", "激活 · ", t)
    t = re.sub(r"^破冰\s+", "激活 · ", t)
    if t.startswith("破冰"):
        t = "激活 · " + t[2:].lstrip(" ·")
    return t[:200]


def _icebreaker_llm_input_cap(task_output_cap: int, fetch_cap: int) -> int:
    """
    送入 LLM 的候选数：至少覆盖产出上限，并按 fetch 放大，避免「产出 50、只喂 40」或硬顶 80。
    TASK_ICEBREAKER_LLM_INPUT_CAP>0 时仍可作绝对硬顶。
    """
    out_cap = max(1, int(task_output_cap))
    fetch = max(1, int(fetch_cap))
    # 候选池宜明显大于产出，便于轮询与 LLM 挑选；不再用固定 80 封顶
    dynamic = max(out_cap + 20, min(out_cap * 3, fetch, ICEBREAKER_MAX_FETCH))
    upper = min(fetch, dynamic, ICEBREAKER_MAX_FETCH)
    if ICEBREAKER_LLM_INPUT_CAP > 0:
        upper = min(upper, ICEBREAKER_LLM_INPUT_CAP)
    return max(1, upper)


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
    defer_school = is_school_defer_window(ref)
    rows: list[dict[str, Any]] = []
    rank = 0
    for p in payloads:
        if len(rows) >= cap:
            break
        rid = str(p.get("raw_customer_id") or "").strip()
        if not rid:
            continue
        if defer_school and is_school_unit(
            unit_type=str(p.get("unit_type") or ""),
            unit_name=str(p.get("unit_name") or ""),
            unit_segment=str(p.get("unit_segment") or ""),
        ):
            continue
        if should_skip_icebreaker_repeat_today(p.get("recent_tasks"), ref):
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
                "title": normalize_activation_task_title(f"激活 · {name}"),
                "instruction": base_instr[:2000],
                "task_kind": "icebreaker",
            }
        )
    return rows


def _payload_is_school(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return is_school_unit(
        unit_type=str(payload.get("unit_type") or ""),
        unit_name=str(payload.get("unit_name") or ""),
        unit_segment=str(payload.get("unit_segment") or ""),
    )


def prefer_non_school_payloads(
    payloads: list[dict[str, Any]],
    *,
    ref_date: date,
) -> list[dict[str, Any]]:
    """深寒暑假把非学校候选排到前面，降低 LLM/兜底误选学校概率。"""
    if not payloads or not is_school_defer_window(ref_date):
        return payloads
    non_school = [p for p in payloads if not _payload_is_school(p)]
    school = [p for p in payloads if _payload_is_school(p)]
    return non_school + school


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
    用于激活池排除近 N 日（默认 10 天）已主动发过消息的客户。
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
    lapsed_days: int = ICEBREAKER_LAPSED_DAYS,
    last_customer_reply_d: date | None = None,
    include_new: bool = False,
) -> tuple[bool, str]:
    """判定是否属于激活池：可选新加 / 中度沉默 / 长期未回复 / 加好友后从未回复。"""
    rid = (rcsw.raw_customer_id or "").strip()
    if not rid or rid.endswith("@chatroom"):
        return False, ""
    add_d = _dt_to_date(rcsw.add_time)
    new_from = ref_date - timedelta(days=max(1, new_days) - 1)
    stale_cutoff = ref_date - timedelta(days=max(1, stale_days))
    lapsed_cutoff = ref_date - timedelta(days=max(1, lapsed_days))

    is_new = add_d is not None and add_d >= new_from
    is_stale = last_customer_reply_d is not None and last_customer_reply_d <= stale_cutoff
    is_lapsed = (
        last_customer_reply_d is not None
        and last_customer_reply_d <= lapsed_cutoff
        and last_customer_reply_d > stale_cutoff
    )
    is_cold_never = last_customer_reply_d is None and add_d is not None and add_d < new_from

    if include_new and is_new:
        return True, "new_friend"
    # 关闭新客进入时：近期新加好友一律不进激活池（即使同时满足沉默等条件）
    if not include_new and is_new:
        return False, ""
    if is_stale:
        return True, "long_no_chat"
    if is_lapsed:
        return True, "lapsed_contact"
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
    limits: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, tuple[SalesCustomerProfile, RawCustomer]]]:
    """
    返回 (发给模型的客户 JSON 列表, raw_customer_id -> (scp, rc) 校验用映射)。
    电话号合并销售好友绑定 phone 与 raw_customers 规范化/原始电话。
    """
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return [], {}

    pool_cap = max(limit, min(MAIN_SCORE_POOL_MAX, 2500))
    stmt = (
        select(SalesCustomerProfile, RawCustomer, RawCustomerSalesWechat)
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
    scp_ids = [rel.id for rel, _, _ in rows if rel and rel.id]
    tag_detail_map = await profile_tags_by_relation_ids(db, scp_ids)
    ref_date = ref_date or date.today()
    recent_tasks_map = await load_recent_contact_tasks_by_customer(db, sw, ref_date)
    last_main_due, _last_ice = await load_last_task_due_by_customer(db, sw)
    from ai.wechat_voice_stats import (
        empty_contact_voice_summary,
        load_contact_voice_summary_by_customer,
    )

    voice_summary_map = await load_contact_voice_summary_by_customer(db, sw, ref_date=ref_date)
    scoring_weights = resolve_scoring_weights(limits)

    scored: list[
        tuple[float, int | None, str, int | None, dict[str, Any], SalesCustomerProfile, RawCustomer, RawCustomerSalesWechat]
    ] = []
    for scp, rc, rcsw in rows:
        if not scp or not rc:
            continue
        rid = (scp.raw_customer_id or "").strip()
        if not rid:
            continue
        tags = tag_detail_map.get(scp.id, [])
        if has_staff_profile_tag(tags) or has_no_followup_profile_tag(tags):
            continue
        try:
            budget = float(scp.budget_amount or 0)
        except (TypeError, ValueError):
            budget = 0.0
        voice_summary = voice_summary_map.get(rid) or empty_contact_voice_summary()
        try:
            intent_val = float(scp.intent_score) if scp.intent_score is not None else None
        except (TypeError, ValueError):
            intent_val = None
        rule_score, tag_tier, band, days_since_main, breakdown = compute_main_rule_score(
            ref_date=ref_date,
            tags=tags,
            ai_profile=(scp.ai_profile or ""),
            budget_amount=budget,
            suggested_followup_date=scp.suggested_followup_date,
            recent_tasks=recent_tasks_map.get(rid, []),
            last_main_task_due=last_main_due.get(rid),
            abc_grade=getattr(scp, "abc_grade", None),
            intent_score=intent_val,
            contact_voice_summary=voice_summary,
            scoring_weights=scoring_weights,
            limits=limits,
        )
        scored.append((rule_score, tag_tier, band, days_since_main, breakdown, scp, rc, rcsw))

    scored.sort(key=lambda x: (-x[0], x[5].id or 0))
    pool_take = max(1, min(limit, pool_cap, len(scored)))
    rows_for_llm = scored[:pool_take]

    payloads: list[dict[str, Any]] = []
    lookup: dict[str, tuple[SalesCustomerProfile, RawCustomer]] = {}
    for rule_score, tag_tier, band, days_since_main, breakdown, scp, rc, rcsw in rows_for_llm:
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
        phone_fields = resolve_allocation_phone_fields(
            rc_phone=getattr(rc, "phone", None),
            rc_phone_normalized=getattr(rc, "phone_normalized", None),
            rcsw_phone=getattr(rcsw, "phone", None) if rcsw else None,
        )
        followup_meta = extract_followup_from_ai_profile(scp.ai_profile)
        payloads.append(
            {
                "raw_customer_id": rid,
                "scp_id": scp.id,
                "customer_name": (rc.customer_name or "").strip(),
                "unit_name": (rc.unit_name or "").strip(),
                **allocation_unit_fields(rc),
                **phone_fields,
                "wechat_remark": (scp.wechat_remark or "").strip(),
                "suggested_followup_date": scp.suggested_followup_date.isoformat()
                if scp.suggested_followup_date
                else followup_meta.get("suggested_followup_date", ""),
                "followup_strategy": followup_meta.get("followup_strategy", ""),
                "followup_channel": followup_meta.get("followup_channel", ""),
                "budget_amount": budget,
                "purchase_type": (scp.purchase_type or "").strip(),
                "profile_tags": [str(t.get("name") or "") for t in tags if t.get("name")],
                "profile_tags_detail": compose_profile_tags_detail(tags),
                "recent_tasks": recent_tasks_map.get(rid, []),
                "rule_priority_score": rule_score,
                "tag_tier": tag_tier,
                "priority_band": band,
                "days_since_last_main_task": days_since_main,
                "abc_grade": breakdown.get("abc_grade"),
                "ai_profile": ap,
                "contact_voice_summary": voice_summary,
                "_score_breakdown": breakdown,
            }
        )
    return payloads, lookup


def _purchase_months_list(rc: Any) -> list[str]:
    raw = getattr(rc, "purchase_months", None) if rc is not None else None
    if isinstance(raw, list):
        return [str(m).strip() for m in raw if str(m).strip()][:12]
    if isinstance(raw, str) and raw.strip():
        return [m.strip() for m in raw.replace("，", ",").split(",") if m.strip()][:12]
    return []


def allocation_unit_fields(rc: Any) -> dict[str, Any]:
    """单位性质/采购月/软分段，供主线与激活任务快照共用。"""
    unit_type = (getattr(rc, "unit_type", None) or "").strip() if rc is not None else ""
    unit_name = (getattr(rc, "unit_name", None) or "").strip() if rc is not None else ""
    return {
        "unit_type": unit_type,
        "purchase_months": _purchase_months_list(rc),
        "unit_segment": resolve_unit_segment(unit_type, unit_name),
    }


def resolve_allocation_phone_fields(
    *,
    rc_phone: str | None = None,
    rc_phone_normalized: str | None = None,
    rcsw_phone: str | None = None,
) -> dict[str, Any]:
    """
    合并销售好友绑定电话与客户主档规范化/原始电话，供任务分配快照使用。
    展示优先：好友绑定 phone → phone_normalized → raw phone。
    """
    sales_phone = (rcsw_phone or "").strip() or None
    normalized = (rc_phone_normalized or "").strip() or None
    raw = (rc_phone or "").strip() or None
    display = sales_phone or normalized or raw or ""
    return {
        "phone": display,
        "phone_raw": raw,
        "phone_normalized": normalized,
        "phone_sales_wechat": sales_phone,
        "has_phone": bool(display),
    }

async def load_icebreaker_customer_payloads(
    db,
    sales_wechat_id: str,
    ref_date: date,
    *,
    exclude_raw_ids: set[str],
    cap_for_llm: int,
    task_output_cap: int = ICEBREAKER_CAP,
    new_days: int | None = None,
    stale_days: int | None = None,
    lapsed_days: int | None = None,
    cooldown_days: int | None = None,
    outbound_quiet_days: int | None = None,
    limits: dict[str, Any] | None = None,
    per_query_limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, tuple[SalesCustomerProfile | None, RawCustomer | None]], dict[str, Any]]:
    """
    从 raw_customer_sales_wechats 筛激活候选（默认可含中度沉默 / 长期未回复 / 从未回复；
    是否纳入近期新加由 icebreaker_include_new 控制），排除已在主线任务中的 raw_customer_id。
    「有效聊天」以 raw_chat_logs 中客户发送消息（is_send=0）为准，不用云客 lastChatTime（含销售单向问候）。
    销售近 outbound_quiet_days（默认 10）日已主动发消息（is_send=1）的客户不进激活池。
    返回 (LLM 快照列表, raw_customer_id -> (scp|None, rc), 统计信息)。
    """
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return [], {}, {"skipped": "empty_sw"}

    lim = limits or {}
    eff_new = int(new_days if new_days is not None else lim.get("icebreaker_new_days") or ICEBREAKER_NEW_DAYS)
    eff_stale = int(
        stale_days if stale_days is not None else lim.get("icebreaker_stale_days") or ICEBREAKER_STALE_DAYS
    )
    eff_lapsed = int(
        lapsed_days if lapsed_days is not None else lim.get("icebreaker_lapsed_days") or ICEBREAKER_LAPSED_DAYS
    )
    eff_cooldown = int(
        cooldown_days
        if cooldown_days is not None
        else lim.get("icebreaker_cooldown_days", ICEBREAKER_COOLDOWN_DAYS)
    )
    eff_outbound_quiet = int(
        outbound_quiet_days
        if outbound_quiet_days is not None
        else lim.get("icebreaker_outbound_quiet_days", ICEBREAKER_OUTBOUND_QUIET_DAYS)
    )
    include_new = bool(lim.get("icebreaker_include_new", False))
    # 大好友池需扫足够多行；旧默认 max(500,…) 且按 add_time ASC，会只看到「最早加的一批」
    scan_limit = int(per_query_limit or max(ICEBREAKER_SCAN_LIMIT, ICEBREAKER_SCORE_POOL_MAX, cap_for_llm * 4))
    _last_main, last_ice_due = await load_last_task_due_by_customer(db, sw)
    last_customer_reply_map = await load_last_customer_reply_date_by_customer(db, sw)
    recent_tasks_map = await load_recent_contact_tasks_by_customer(db, sw, ref_date)
    sales_outbound_map = await load_last_sales_outbound_date_by_customer(db, sw)

    active = (RawCustomerSalesWechat.is_deleted.is_(False)) | (RawCustomerSalesWechat.is_deleted.is_(None))
    new_from = ref_date - timedelta(days=max(1, eff_new) - 1)

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

    stmt_new = None
    if include_new:
        stmt_new = (
            base.where(RawCustomerSalesWechat.add_time.isnot(None))
            .where(cast(RawCustomerSalesWechat.add_time, Date) >= new_from)
            .order_by(desc(RawCustomerSalesWechat.add_time))
            .limit(scan_limit)
        )
    # 非「近期新加」：按 last_chat_time 升序优先捞沉默客户（比纯 add_time ASC 更能覆盖大池）
    # 再补一批按 add_time 升序，覆盖「从未聊过 / last_chat 为空」的老好友
    not_new = or_(
        RawCustomerSalesWechat.add_time.is_(None),
        cast(RawCustomerSalesWechat.add_time, Date) < new_from,
    )
    half = max(200, scan_limit // 2)
    stmt_stale_by_chat = (
        base.where(not_new)
        .order_by(RawCustomerSalesWechat.last_chat_time.asc())
        .limit(half)
    )
    stmt_stale_by_add = (
        base.where(not_new)
        .order_by(RawCustomerSalesWechat.add_time.asc())
        .limit(half)
    )

    merged: dict[str, tuple[RawCustomerSalesWechat, RawCustomer, SalesCustomerProfile | None, str]] = {}
    skipped_cooldown = 0
    skipped_exclude = 0
    skipped_ineligible = 0
    scanned_rows = 0
    reason_counts: dict[str, int] = {}
    scan_stmts = [s for s in (stmt_new, stmt_stale_by_chat, stmt_stale_by_add) if s is not None]
    for stmt in scan_stmts:
        rows = (await db.execute(stmt)).all()
        scanned_rows += len(rows)
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
                new_days=eff_new,
                stale_days=eff_stale,
                lapsed_days=eff_lapsed,
                last_customer_reply_d=last_reply_d,
                include_new=include_new,
            )
            if not ok:
                skipped_ineligible += 1
                continue
            if should_skip_icebreaker_repeat_today(
                recent_tasks_map.get(rid),
                ref_date,
                last_sales_outbound=sales_outbound_map.get(rid),
                cooldown_days=eff_cooldown,
                outbound_quiet_days=eff_outbound_quiet,
            ):
                skipped_cooldown += 1
                continue
            if rid in exclude_raw_ids:
                skipped_exclude += 1
                continue
            prev = merged.get(rid)
            if prev is None:
                merged[rid] = (rcsw, rc, scp, reason)
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                continue
            prev_reason = prev[3]
            if _ICEBREAKER_REASON_ORDER.get(reason, 9) < _ICEBREAKER_REASON_ORDER.get(prev_reason, 9):
                reason_counts[prev_reason] = max(0, reason_counts.get(prev_reason, 1) - 1)
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                merged[rid] = (rcsw, rc, scp, reason)

    ice_scp_ids = [int(v[2].id) for v in merged.values() if v[2] and v[2].id]
    skipped_staff_tag = 0
    if ice_scp_ids:
        ice_tag_map = await profile_tags_by_relation_ids(db, ice_scp_ids)
        kept: dict[str, tuple[RawCustomerSalesWechat, RawCustomer, SalesCustomerProfile | None, str]] = {}
        for rid, row in merged.items():
            if (
                row[2]
                and row[2].id
                and (
                    has_staff_profile_tag(ice_tag_map.get(row[2].id, []))
                    or has_no_followup_profile_tag(ice_tag_map.get(row[2].id, []))
                )
            ):
                skipped_staff_tag += 1
                continue
            kept[rid] = row
        merged = kept

    ordered = sorted(
        merged.values(),
        key=lambda it: icebreaker_fair_sort_key(
            it,
            ref_date=ref_date,
            last_ice_due=last_ice_due,
            reason_order=_ICEBREAKER_REASON_ORDER,
        ),
    )
    pool_take = min(len(ordered), ICEBREAKER_SCORE_POOL_MAX, ICEBREAKER_MAX_FETCH, max(cap_for_llm, 1))
    take = _icebreaker_llm_input_cap(task_output_cap, cap_for_llm)
    take = min(take, pool_take) if pool_take else 0
    picked = ordered[:take]

    scp_ids = [int(scp.id) for _a, _b, scp, _r in picked if scp and scp.id]
    tag_detail_map = await profile_tags_by_relation_ids(db, scp_ids)

    payloads: list[dict[str, Any]] = []
    lookup: dict[str, tuple[SalesCustomerProfile | None, RawCustomer | None]] = {}
    for rcsw, rc, scp, reason in picked:
        if scp and scp.id and (
            has_staff_profile_tag(tag_detail_map.get(scp.id, []))
            or has_no_followup_profile_tag(tag_detail_map.get(scp.id, []))
        ):
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
        phone_fields = resolve_allocation_phone_fields(
            rc_phone=getattr(rc, "phone", None) if rc else None,
            rc_phone_normalized=getattr(rc, "phone_normalized", None) if rc else None,
            rcsw_phone=getattr(rcsw, "phone", None),
        )
        payloads.append(
            {
                "raw_customer_id": rid,
                "scp_id": scp.id if scp else None,
                "customer_name": (rc.customer_name or "").strip() if rc else "",
                "unit_name": (rc.unit_name or "").strip() if rc else "",
                **allocation_unit_fields(rc),
                **phone_fields,
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
        "scanned_rows": scanned_rows,
        "scan_limit": scan_limit,
        "merged_candidates": len(merged),
        "reason_counts": reason_counts,
        "skipped_ineligible": skipped_ineligible,
        "skipped_contact_cooldown": skipped_cooldown,
        "skipped_main_or_reserve": skipped_exclude,
        "skipped_staff_or_nofollowup_tag": skipped_staff_tag,
        "pool_ranked": len(ordered),
        "sent_to_llm": len(payloads),
        "llm_input_cap": take,
        "task_output_cap": int(task_output_cap),
        "new_days": eff_new,
        "include_new": include_new,
        "stale_days": eff_stale,
        "lapsed_days": eff_lapsed,
        "cooldown_days": eff_cooldown,
        "outbound_quiet_days": eff_outbound_quiet,
        "effective_chat": "raw_chat_logs.is_send=0",
        "rotation": "last_icebreaker_due_asc",
    }
    payloads = prefer_non_school_payloads(payloads, ref_date=ref_date)
    if is_school_defer_window(ref_date):
        school_n = sum(1 for p in payloads if _payload_is_school(p))
        stats["school_defer_window"] = True
        stats["school_candidates_in_llm_pool"] = school_n
        stats["non_school_candidates_in_llm_pool"] = len(payloads) - school_n
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
    wechat_floor: int | None = None,
    phone_floor: int | None = None,
    customer_payloads: list[dict[str, Any]] | None = None,
    customer_features: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if customer_features is not None:
        from ai.task_allocation_features import features_for_llm

        customers_json = json.dumps(
            features_for_llm(customer_features),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    else:
        # 完整 payload：去掉仅供规则侧使用的 _ 前缀字段
        slim_payloads = [
            {k: v for k, v in p.items() if not str(k).startswith("_")}
            for p in (customer_payloads or [])
            if isinstance(p, dict)
        ]
        customers_json = json.dumps(slim_payloads, ensure_ascii=False, separators=(",", ":"))
    tags_catalog = await load_profile_tags_catalog_text(db)
    cap = int(task_cap)
    w_cap = int(wechat_cap) if wechat_cap is not None else cap
    p_cap = int(phone_cap) if phone_cap is not None else 0
    from ai.task_allocation_limits import scale_channel_caps_to_task_cap
    from ai.wechat_voice_stats import DEFAULT_LOOKBACK_DAYS

    w_cap, p_cap = scale_channel_caps_to_task_cap(cap, w_cap, p_cap)
    # 调用方传入本批已对齐的 floor；此处仅夹到本批上限，避免二次缩放
    w_floor = max(0, int(wechat_floor)) if wechat_floor is not None else 0
    p_floor = max(0, int(phone_floor)) if phone_floor is not None else 0
    w_floor = min(w_floor, w_cap)
    p_floor = min(p_floor, p_cap)
    task_floor = w_floor + p_floor

    ctx: dict[str, Any] = {
        "current_date": ref_today.isoformat(),
        "sales_wechat_id": sales_wechat_id,
        "period_type": period_type,
        "period_type_label": _period_type_label(period_type),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "ref_today": ref_today.isoformat(),
        "task_cap": str(cap),
        "task_floor": str(task_floor),
        "wechat_cap": str(w_cap),
        "phone_cap": str(p_cap),
        "wechat_floor": str(w_floor),
        "phone_floor": str(p_floor),
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
        local_doc_keys=(
            "scoring_criteria",
            "profile_tags_detail",
            "unit_followup_playbook",
            "strategy",
        ),
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
        "ice_lapsed_days": str(ICEBREAKER_LAPSED_DAYS),
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
        local_doc_keys=("opening", "unit_followup_playbook", "scoring_criteria", "strategy"),
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
    wechat_floor: int | None = None,
    phone_floor: int | None = None,
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
            wechat_floor=wechat_floor,
            phone_floor=phone_floor,
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
            usage=LLMUsageContext(
                scenario_key=scenario_key,
                prompt_version_id=meta.get("prompt_version_id"),
            ),
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


async def run_icebreaker_task_allocation_llm(
    db,
    llm: LLMClient,
    *,
    sales_wechat_id: str,
    ref_today: date,
    task_cap: int,
    customer_payloads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    激活（破冰）任务 LLM：按 chunk 分批调用，降低单次 prompt 过大导致 ReadTimeout 的概率。
    """
    cap = max(0, int(task_cap))
    if cap <= 0 or not customer_payloads:
        return [], {"chunks": [], "tasks_parsed": 0}

    chunk_size = max(5, int(ICEBREAKER_LLM_CHUNK_SIZE))
    all_raw: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    combined: dict[str, Any] = {"chunks": [], "chunk_size": chunk_size}

    for offset in range(0, len(customer_payloads), chunk_size):
        if len(all_raw) >= cap:
            break
        chunk = customer_payloads[offset : offset + chunk_size]
        chunk_cap = min(cap - len(all_raw), len(chunk))
        raw, meta = await run_task_allocation_llm(
            db,
            llm,
            sales_wechat_id=sales_wechat_id,
            period_type="daily",
            period_start=ref_today,
            period_end=ref_today,
            ref_today=ref_today,
            task_cap=chunk_cap,
            customer_payloads=chunk,
            scenario_key=SCENARIO_ICEBREAKER_KEY,
            log_tag="TASK_ICEBREAKER_DEBUG",
        )
        combined["chunks"].append(
            {
                "offset": offset,
                "input": len(chunk),
                "parsed": len(raw),
                "llm_error": meta.get("llm_error"),
                "parse_error": meta.get("parse_error"),
            }
        )
        if meta.get("llm_error"):
            combined["llm_error"] = meta["llm_error"]
        if meta.get("parse_error") and not combined.get("parse_error"):
            combined["parse_error"] = meta["parse_error"]
        for item in raw:
            rid = str(item.get("raw_customer_id") or "").strip()
            if not rid or rid in seen_ids:
                continue
            seen_ids.add(rid)
            all_raw.append(item)
            if len(all_raw) >= cap:
                break

    combined["tasks_parsed"] = len(all_raw)
    combined["llm_response_len"] = sum(int(c.get("parsed") or 0) for c in combined["chunks"])
    return all_raw[:cap], combined


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
    wechat_floor: int | None = None,
    phone_floor: int | None = None,
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
        wechat_floor=wechat_floor,
        phone_floor=phone_floor,
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
            usage=LLMUsageContext(
                scenario_key=SCENARIO_KEY,
                prompt_version_id=meta.get("prompt_version_id"),
            ),
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
