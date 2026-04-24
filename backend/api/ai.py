from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from database import get_db
from api.auth import get_current_user
from models import User, SystemConfig, PromptScenario, PromptVersion
from ai.gateway import AIGateway
from ai.llm_client import LLMClient
from sqlalchemy.future import select
from sqlalchemy import exists
from core.logger import logger
from ai.chat_models_catalog import allowed_chat_model_ids, default_chat_model_id

router = APIRouter(prefix="/api/ai", tags=["AI"])


class AIChatRequest(BaseModel):
    customer_phone: str
    query: str
    scenario: str = "general_chat"      # "general_chat" 或 "product_recommend"
    conversation_id: Optional[str] = None
    # 对话专用模型；画像分析仍只读 system_configs.llm_model，不受此项影响
    chat_model: Optional[str] = None


@router.get("/scenarios")
async def list_ai_scenarios(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    给桌面端下拉框用的“可选场景列表”。

    只返回：
    - 场景 enabled=1
    - 且至少存在一条 published 的 PromptVersion
    （避免把 draft-only 场景暴露给一线员工，选了也用不了）
    """
    has_published = exists(
        select(PromptVersion.id)
        .where(PromptVersion.scenario_id == PromptScenario.id)
        .where(PromptVersion.status == "published")
    )
    res = await db.execute(
        select(PromptScenario)
        .where(PromptScenario.enabled == True)  # noqa: E712
        .where(has_published)
        .order_by(PromptScenario.id.asc())
    )
    items = []
    for s in res.scalars().all():
        items.append({
            "scenario_key": s.scenario_key,
            "name": s.name,
            "tools_enabled": bool(s.tools_enabled),
        })
    return {"code": 200, "message": "ok", "data": items}


def _resolve_chat_model(requested: Optional[str], config_map: dict) -> str:
    allowed = allowed_chat_model_ids(config_map)
    fallback = default_chat_model_id(config_map)
    if requested:
        mid = requested.strip()
        if mid in allowed:
            return mid
    cfg_chat = (config_map.get("llm_chat_model") or "").strip()
    if cfg_chat in allowed:
        return cfg_chat
    return fallback


async def _get_llm_client(db: AsyncSession, chat_model: Optional[str] = None) -> LLMClient:
    """从 system_configs 读取 URL/KEY；对话模型由请求或 llm_chat_model 决定，不用 llm_model。"""
    stmt = select(SystemConfig).where(SystemConfig.config_group == "ai")
    result = await db.execute(stmt)
    configs = result.scalars().all()
    config_map = {c.config_key: c.config_value for c in configs}

    api_url = config_map.get("llm_api_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    api_key = config_map.get("llm_api_key", "")
    model = _resolve_chat_model(chat_model, config_map)

    return LLMClient(api_url=api_url, api_key=api_key, model=model)


@router.post("/chat")
async def ai_chat(
    req: AIChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    AI 对话主入口 (SSE 流式响应)。
    """
    llm = await _get_llm_client(db, chat_model=req.chat_model)
    # loguru 使用 {} 占位，勿用 %s
    logger.info(
        "AI 对话请求 user_id={} scenario={} chat_model={} phone={}",
        current_user.id,
        req.scenario,
        llm.model,
        req.customer_phone,
    )
    gateway = AIGateway(db=db, llm=llm)

    async def event_generator():
        async for chunk_json in gateway.stream_chat(
            user_id=current_user.id,
            customer_phone=req.customer_phone,
            query=req.query,
            scenario=req.scenario,
            conversation_id=req.conversation_id,
        ):
            yield f"data: {chunk_json}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
