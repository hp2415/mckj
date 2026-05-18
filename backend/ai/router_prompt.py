"""场景路由分类器提示词：从管理平台 published 版本加载，与画像 customer_profile 同模式。"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from core.logger import logger
from models import SystemConfig

from ai.prompt_models import PromptTemplate
from ai.prompt_renderer import render_system
from ai.prompt_store import get_prompt_store

ROUTER_SCENARIO_KEY = "ai_scene_router"

ROUTER_SYSTEM_PROMPT = (
    "你是一个对话场景分发器，负责把销售员当前的一句话指派给最合适的『场景 key』。\n"
    "请严格按以下规则工作：\n"
    "1. 只能从我给出的『候选场景』列表里挑选 scenario_key，禁止杜撰、禁止返回列表外的值。\n"
    "2. 若多个候选都贴近，优先选用『描述/示例』更匹配的那个。\n"
    "3. 结合客户路由摘要判断新老客、意向档位等；必要时可同时返回 auxiliary_scenarios（辅场景 key 列表）。\n"
    "4. 若【用户指定场景 hint】不是 auto，须结合其含义与当前发言综合判断；仅在明显一致时优先选用该 scenario_key（仍须在候选内），勿机械照搬。\n"
    "5. 任何时候只能输出一段 JSON，格式严格为："
    '{"scenario_key": "xxx", "auxiliary_scenarios": ["yyy"], "reason": "一句话解释"}。\n'
    "6. auxiliary_scenarios 可为空数组；辅场景也必须来自候选列表。\n"
    "7. 不要附加任何额外文字、Markdown 代码块、注释。"
)

ROUTER_USER_PROMPT = """【候选场景】
{{candidates_block}}

【客户路由摘要】
{{route_context_summary}}

【对话入口】
{{ui_category}}

【用户指定场景 hint】
{{scenario_hint}}

【用户当前发言】
{{user_query}}

请按规则只输出 JSON：{"scenario_key":"...","auxiliary_scenarios":[],"reason":"..."}"""

ROUTER_PROMPT_VARIABLE_CHOICES: list[tuple[str, str]] = [
    ("candidates_block", "候选场景清单（运行时按后台场景目录生成）"),
    ("route_context_summary", "客户路由摘要（生命周期/意向/预算/标签等）"),
    ("user_query", "销售员当前输入"),
    ("ui_category", "对话入口（customer_chat / free_chat）"),
    ("scenario_hint", "桌面端场景 hint（auto 或显式 scenario_key）"),
]

ROUTER_PROMPT_VARIABLE_TITLES: dict[str, str] = {
    "candidates_block": "候选场景",
    "route_context_summary": "客户路由摘要",
    "user_query": "用户当前发言",
    "ui_category": "对话入口",
    "scenario_hint": "场景 hint",
}


async def _use_db_prompts(db: AsyncSession) -> bool:
    try:
        stmt = select(SystemConfig).where(SystemConfig.config_key == "use_db_prompts")
        res = await db.execute(stmt)
        cfg = res.scalars().first()
        if not cfg:
            return True
        return str(cfg.config_value).strip() not in ("0", "false", "False", "off", "OFF", "")
    except Exception as e:
        logger.warning("路由分类提示词: 读取 use_db_prompts 失败，默认走 DB: {}", e)
        return True


async def build_router_chat_messages(
    db: AsyncSession,
    *,
    candidates_block: str,
    route_context_summary: str,
    user_query: str,
    ui_category: str,
    scenario_hint: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    ctx = {
        "candidates_block": (candidates_block or "").strip() or "（无可用候选场景）",
        "route_context_summary": (route_context_summary or "").strip() or "（未绑定客户）",
        "user_query": (user_query or "").strip(),
        "ui_category": (ui_category or "").strip() or "customer_chat",
        "scenario_hint": (scenario_hint or "").strip() or "auto",
    }

    if await _use_db_prompts(db):
        store = get_prompt_store()
        version = await store.get_published_version(ROUTER_SCENARIO_KEY)
        if version:
            docs_map: dict[str, tuple[str, Optional[int]]] = {}
            for spec in version.doc_refs or []:
                content, ver = await store.get_doc_text(spec.doc_key, spec.doc_version_id)
                docs_map[spec.doc_key] = (content, ver)
            system_text = render_system(version.template, ctx, docs_map, version.doc_refs or [])
            user_src = (version.template.user or "").strip() or ROUTER_USER_PROMPT.strip()
            user_text = render_system(PromptTemplate(system=user_src), ctx, {}, ())
            messages = [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ]
            meta = {
                "prompt_source": "db",
                "scenario_key": ROUTER_SCENARIO_KEY,
                "prompt_version_id": getattr(version, "id", None),
                "prompt_version": getattr(version, "version", None),
            }
            return messages, meta

    system_text = render_system(PromptTemplate(system=ROUTER_SYSTEM_PROMPT), ctx, {}, ())
    user_text = render_system(PromptTemplate(system=ROUTER_USER_PROMPT.strip()), ctx, {}, ())
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]
    meta = {
        "prompt_source": "local",
        "scenario_key": ROUTER_SCENARIO_KEY,
        "prompt_version_id": None,
        "prompt_version": None,
    }
    return messages, meta
