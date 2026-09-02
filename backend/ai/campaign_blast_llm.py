"""活动群发：复用促销活动提示词，按批调用桌面端对话模型。"""
from __future__ import annotations

import datetime
import json
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ai.campaign_blast import build_script_payloads, lock_posters_for_job
from ai.campaign_blast_prompts import CAMPAIGN_BLAST_BATCH_USER
from ai.campaign_service import format_campaign_block
from ai.chat_models_catalog import (
    allowed_chat_model_ids,
    default_chat_model_id,
    resolve_chat_model_endpoint,
)
from ai.llm_client import LLMClient
from ai.llm_usage import LLMUsageContext
from ai.prompt_models import DocInjectSpec, PromptTemplate
from ai.prompt_renderer import render_system
from ai.prompt_seed import DOC_CHAR_LIMITS, PROMOTION_SYSTEM
from ai.prompt_store import get_prompt_store
from core.logger import logger
from models import Campaign, CampaignBlastJob

SCENARIO_KEY = "campaign_blast_scripts"
PROMPT_SCENARIO = "promotion"
CHUNK_SIZE = 8


async def get_desktop_chat_llm_client(db) -> LLMClient:
    """桌面端默认勾选模型：desktop_default_chat_models 的第一项。"""
    from sqlalchemy.future import select as sa_select

    from ai.raw_profiling import _fetch_ai_system_configs
    from models import SystemConfig

    configs = await _fetch_ai_system_configs(db)
    extra = await db.execute(
        sa_select(SystemConfig).where(
            SystemConfig.config_key.in_(
                ("desktop_default_chat_models", "llm_chat_model", "llm_chat_models_list")
            )
        )
    )
    for row in extra.scalars().all():
        configs[row.config_key] = row.config_value or ""
    model = _first_desktop_default_model(configs)
    api_url, api_key = resolve_chat_model_endpoint(configs, model)
    logger.info("活动群发使用桌面对话模型 model={}", model)
    return LLMClient(api_url=api_url, api_key=api_key, model=model)


def _first_desktop_default_model(configs: dict[str, str]) -> str:
    allowed = allowed_chat_model_ids(configs)
    raw = (configs.get("desktop_default_chat_models") or "").strip()
    parts: list[str] = []
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                parts = [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            parts = []
    if not parts:
        parts = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    for mid in parts:
        if mid in allowed:
            return mid
    cfg_chat = (configs.get("llm_chat_model") or "").strip()
    if cfg_chat in allowed:
        return cfg_chat
    return default_chat_model_id(configs)

_PROMOTION_DOC_SPECS = (
    DocInjectSpec(
        doc_key="ai_guide",
        title="销售角色与行为规范",
        required=False,
        max_chars=DOC_CHAR_LIMITS.get("ai_guide", 4000),
    ),
    DocInjectSpec(
        doc_key="strategy",
        title="客户分层话术参考",
        required=False,
        max_chars=DOC_CHAR_LIMITS.get("strategy", 6000),
    ),
    DocInjectSpec(
        doc_key="closing",
        title="促成成交话术参考",
        required=False,
        max_chars=DOC_CHAR_LIMITS.get("closing", 4000),
    ),
)


def _parse_scripts_json(raw: str) -> list[dict[str, str]]:
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, list):
        return []
    out: list[dict[str, str]] = []
    for item in scripts:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("raw_customer_id") or "").strip()
        txt = str(item.get("text") or "").strip()
        if rid and txt:
            out.append({"raw_customer_id": rid, "text": txt})
    return out


def _fill_user(tpl: str, ctx: dict[str, Any]) -> str:
    text = tpl or ""
    for key, val in ctx.items():
        text = text.replace("{{" + key + "}}", str(val if val is not None else ""))
    return text


async def _load_docs(store, specs: list[DocInjectSpec] | tuple[DocInjectSpec, ...]):
    docs_map: dict[str, tuple[str, int | None]] = {}
    for spec in specs:
        c, vid = await store.get_doc_text(spec.doc_key, spec.doc_version_id)
        docs_map[spec.doc_key] = (c or "", vid)
    return docs_map


async def _build_messages(
    db: AsyncSession,
    *,
    job: CampaignBlastJob,
    campaign: Campaign,
    customer_payloads: list[dict[str, Any]],
    sales_wechat_persona: str,
    staff_identity: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    from ai.raw_profiling import _use_db_prompts

    campaign_block = (
        "本场群发指定以下活动（每条话术都必须围绕它，禁止编造规则外优惠）：\n"
        + format_campaign_block([campaign])
    )
    ctx: dict[str, Any] = {
        "staff_identity": staff_identity or "未登记",
        "sales_wechat_persona": sales_wechat_persona or "未登记",
        "customer_card": "见下方批次客户列表；请为每一位各写一条，不要只写一条。",
        "ai_profile": "见各客户条目中的 ai_profile_excerpt / profile_tags。",
        "order_summary": "群发不注入订单明细，勿引用订单。",
        "chat_summary": "群发不注入聊天记录，勿引用历史聊天。",
        "campaign_block": campaign_block,
        "customers_json": json.dumps(
            customer_payloads, ensure_ascii=False, separators=(",", ":")
        ),
    }
    store = get_prompt_store()
    meta: dict[str, Any] = {"prompt_source": "local", "scenario_key": PROMPT_SCENARIO}
    system_src = PROMOTION_SYSTEM
    specs: list[DocInjectSpec] | tuple[DocInjectSpec, ...] = _PROMOTION_DOC_SPECS
    if await _use_db_prompts(db):
        version = await store.get_published_version(PROMPT_SCENARIO)
        if version and (version.template.system or "").strip():
            system_src = version.template.system
            specs = version.doc_refs or list(_PROMOTION_DOC_SPECS)
            meta = {
                "prompt_source": "db",
                "scenario_key": PROMPT_SCENARIO,
                "prompt_version_id": getattr(version, "id", None),
                "prompt_version": getattr(version, "version", None),
            }
    docs_map = await _load_docs(store, specs)
    system_text = render_system(PromptTemplate(system=system_src), ctx, docs_map, specs)
    user_text = _fill_user(CAMPAIGN_BLAST_BATCH_USER.strip(), ctx)
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]
    return messages, meta


async def _llm_text(llm, messages: list[dict[str, str]], *, usage: LLMUsageContext | None) -> str:
    data = await llm.chat(messages, temperature=0.6, max_tokens=4096, usage=usage)
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(p.get("text") or "") for p in content if isinstance(p, dict)]
        return "".join(parts)
    return str(content or "")


async def generate_scripts_for_job(
    db: AsyncSession,
    job: CampaignBlastJob,
    *,
    recipient_ids: list[int] | None = None,
    llm,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    """按批调用桌面对话模型，复用促销活动提示词为每位客户生成话术。"""
    from ai.context import ContextAssembler

    campaign = await db.get(Campaign, int(job.campaign_id))
    if not campaign:
        raise ValueError("活动不存在")

    payloads = await build_script_payloads(db, job, recipient_ids)
    if not payloads:
        return {"generated": 0, "failed": 0, "total": 0, "llm_calls": 0}

    identity = await ContextAssembler(db).assemble_sales_identity_for_wechat(job.sales_wechat_id)
    persona = identity.get("sales_wechat_persona") or ""
    staff_id = identity.get("staff_identity") or ""

    generated = 0
    failed = 0
    llm_calls = 0
    now = datetime.datetime.now()
    job.status = "scripting"
    job.updated_at = now
    last_meta: dict[str, Any] = {}

    for i in range(0, len(payloads), CHUNK_SIZE):
        chunk = payloads[i : i + CHUNK_SIZE]
        messages, meta = await _build_messages(
            db,
            job=job,
            campaign=campaign,
            customer_payloads=chunk,
            sales_wechat_persona=persona,
            staff_identity=staff_id,
        )
        last_meta = meta
        usage = LLMUsageContext(
            user_id=actor_user_id,
            scenario_key=SCENARIO_KEY,
            prompt_version_id=meta.get("prompt_version_id"),
        )
        try:
            raw = await _llm_text(llm, messages, usage=usage)
            scripts = _parse_scripts_json(raw)
            llm_calls += 1
        except Exception as e:
            logger.exception("活动群发话术 LLM 失败: {}", e)
            scripts = []
            failed += len(chunk)
            continue

        by_id = {s["raw_customer_id"]: s["text"] for s in scripts}
        for p in chunk:
            rid = p["raw_customer_id"]
            text = by_id.get(rid, "").strip()
            rec = next(
                (r for r in job.recipients or [] if str(r.raw_customer_id) == rid),
                None,
            )
            if not rec or (rec.status or "") == "sent":
                continue
            if text:
                rec.script_text = text
                rec.script_generated_at = now
                rec.updated_at = now
                generated += 1
            else:
                failed += 1
        await db.flush()

    job.status = "ready"
    job.updated_at = datetime.datetime.now()
    await lock_posters_for_job(db, job)
    await db.commit()
    return {
        "generated": generated,
        "failed": failed,
        "total": len(payloads),
        "llm_calls": llm_calls,
        "prompt_version_id": last_meta.get("prompt_version_id"),
    }
