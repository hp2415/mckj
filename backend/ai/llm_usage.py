"""
LLM 调用 token/成本计量：落库 + 按场景/日聚合查询。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select

from core.logger import logger
from database import AsyncSessionLocal
from models import LlmUsageLog


@dataclass
class LLMUsageContext:
    scenario_key: str | None = None
    user_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def api_host_from_url(api_url: str | None) -> str | None:
    if not api_url:
        return None
    try:
        return urlparse(api_url).netloc or None
    except Exception:
        return None


def parse_usage_fields(usage: dict[str, Any] | None) -> tuple[int, int, int]:
    u = usage or {}
    pt = int(u.get("prompt_tokens") or 0)
    ct = int(u.get("completion_tokens") or 0)
    tt = int(u.get("total_tokens") or (pt + ct))
    return pt, ct, tt


async def log_llm_usage(
    *,
    model: str,
    api_url: str | None = None,
    scenario_key: str | None = None,
    user_id: int | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int | None = None,
    duration_ms: int = 0,
    stream_mode: str = "stream",
    fallback_reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    pt, ct, tt = prompt_tokens, completion_tokens, total_tokens or (prompt_tokens + completion_tokens)
    async with AsyncSessionLocal() as db:
        db.add(
            LlmUsageLog(
                model=(model or "")[:120],
                api_host=api_host_from_url(api_url),
                scenario_key=(scenario_key or "")[:80] or None,
                user_id=user_id,
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=tt,
                duration_ms=max(0, int(duration_ms)),
                stream_mode=(stream_mode or "stream")[:20],
                fallback_reason=(fallback_reason or "")[:120] or None,
                extra_json=extra or None,
            )
        )
        await db.commit()


def schedule_log_llm_usage(**kwargs: Any) -> None:
    """异步落库，不阻塞 LLM 响应路径。"""

    async def _safe() -> None:
        try:
            await log_llm_usage(**kwargs)
        except Exception as e:
            logger.warning("llm_usage_log 写入失败: {}", e)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_safe())
    except RuntimeError:
        asyncio.run(_safe())


async def usage_summary_by_scenario(*, days: int = 7) -> list[dict[str, Any]]:
    """按场景聚合近 N 日 token 用量（管理/验收用）。"""
    days = max(1, min(int(days), 90))
    since = datetime.now() - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        return await aggregate_llm_usage_by_scenario(db, since=since)


SCENARIO_DISPLAY_NAMES: dict[str, str] = {
    "general_chat": "客户沟通",
    "product_recommend": "推品报价",
    "staff_assistant": "内部问答",
    "customer_profile": "客户画像",
    "ai_scene_router": "场景路由",
    "new_customer_greeting": "新客问候",
    "task_allocation": "任务分配",
    "task_allocation_icebreaker": "破冰任务",
    "phone_call_script": "电话话术",
    "model_identity": "模型身份",
    "old_customer_wake": "老客唤醒",
    "old_customer_close": "老客逼单",
    "order_guide": "下单引导",
}


def scenario_display_name(key: str | None) -> str:
    k = (key or "").strip() or "(unknown)"
    return SCENARIO_DISPLAY_NAMES.get(k, k)


async def aggregate_llm_usage_totals(db, *, since: datetime) -> dict[str, Any]:
    from sqlalchemy import case

    stmt = select(
        func.count(LlmUsageLog.id),
        func.coalesce(func.sum(LlmUsageLog.prompt_tokens), 0),
        func.coalesce(func.sum(LlmUsageLog.completion_tokens), 0),
        func.coalesce(func.sum(LlmUsageLog.total_tokens), 0),
        func.coalesce(func.sum(LlmUsageLog.duration_ms), 0),
        func.coalesce(
            func.sum(case((LlmUsageLog.fallback_reason.isnot(None), 1), else_=0)),
            0,
        ),
    ).where(LlmUsageLog.created_at >= since)
    row = (await db.execute(stmt)).first() or (0, 0, 0, 0, 0, 0)
    calls, pt, ct, tt, dur, fb = [int(x or 0) for x in row]
    return {
        "call_count": calls,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": tt,
        "duration_ms": dur,
        "fallback_count": fb,
    }


async def aggregate_llm_usage_by_scenario(db, *, since: datetime) -> list[dict[str, Any]]:
    from sqlalchemy import case

    stmt = (
        select(
            LlmUsageLog.scenario_key,
            func.count(LlmUsageLog.id).label("call_count"),
            func.coalesce(func.sum(LlmUsageLog.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(LlmUsageLog.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(LlmUsageLog.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(LlmUsageLog.duration_ms), 0).label("duration_ms"),
            func.coalesce(
                func.sum(case((LlmUsageLog.fallback_reason.isnot(None), 1), else_=0)),
                0,
            ).label("fallback_count"),
        )
        .where(LlmUsageLog.created_at >= since)
        .group_by(LlmUsageLog.scenario_key)
        .order_by(func.coalesce(func.sum(LlmUsageLog.total_tokens), 0).desc())
    )
    rows = (await db.execute(stmt)).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        key = r[0] or "(unknown)"
        calls = int(r[1] or 0)
        dur = int(r[5] or 0)
        out.append(
            {
                "scenario_key": key,
                "scenario_label": scenario_display_name(key),
                "call_count": calls,
                "prompt_tokens": int(r[2] or 0),
                "completion_tokens": int(r[3] or 0),
                "total_tokens": int(r[4] or 0),
                "duration_ms": dur,
                "avg_duration_ms": int(dur / calls) if calls else 0,
                "fallback_count": int(r[6] or 0),
            }
        )
    return out


async def trend_llm_usage_daily(db, *, since: datetime, days: int) -> dict[str, Any]:
    day_col = func.date(LlmUsageLog.created_at)
    stmt = (
        select(
            day_col.label("d"),
            func.coalesce(func.sum(LlmUsageLog.prompt_tokens), 0),
            func.coalesce(func.sum(LlmUsageLog.completion_tokens), 0),
            func.coalesce(func.sum(LlmUsageLog.total_tokens), 0),
            func.count(LlmUsageLog.id),
        )
        .where(LlmUsageLog.created_at >= since)
        .group_by(day_col)
        .order_by(day_col.asc())
    )
    res = await db.execute(stmt)
    raw = {
        str(d): (
            int(pt or 0),
            int(ct or 0),
            int(tt or 0),
            int(calls or 0),
        )
        for d, pt, ct, tt, calls in res.all()
        if d
    }

    labels: list[str] = []
    prompt_arr: list[int] = []
    completion_arr: list[int] = []
    total_arr: list[int] = []
    calls_arr: list[int] = []
    start = (datetime.utcnow() - timedelta(days=days - 1)).date()
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        labels.append(day)
        pt, ct, tt, c = raw.get(day, (0, 0, 0, 0))
        prompt_arr.append(pt)
        completion_arr.append(ct)
        total_arr.append(tt)
        calls_arr.append(c)
    return {
        "labels": labels,
        "prompt_tokens": prompt_arr,
        "completion_tokens": completion_arr,
        "total_tokens": total_arr,
        "call_count": calls_arr,
    }
