from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from database import get_db
from api.auth import get_current_user
from models import User, SystemConfig
from ai.gateway import AIGateway
from ai.llm_client import LLMClient
from sqlalchemy.future import select

router = APIRouter(prefix="/api/ai", tags=["AI"])


class AIChatRequest(BaseModel):
    customer_phone: str
    query: str
    scenario: str = "general_chat"      # "general_chat" 或 "product_recommend"
    conversation_id: Optional[str] = None


async def _get_llm_client(db: AsyncSession) -> LLMClient:
    """从 system_configs 表动态读取 LLM 配置"""
    stmt = select(SystemConfig).where(SystemConfig.config_group == "ai")
    result = await db.execute(stmt)
    configs = result.scalars().all()
    config_map = {c.config_key: c.config_value for c in configs}

    api_url = config_map.get("llm_api_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    api_key = config_map.get("llm_api_key", "")
    model = config_map.get("llm_model", "qwen-max")

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
    llm = await _get_llm_client(db)
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
