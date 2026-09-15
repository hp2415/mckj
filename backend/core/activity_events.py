"""写入 user_activity_events；失败不影响主业务。"""
from __future__ import annotations

import datetime
from typing import Any

from core.logger import logger
from database import AsyncSessionLocal
from models import UserActivityEvent


async def record_activity_event(
    *,
    user_id: int | None,
    event_type: str,
    source: str = "api",
    sales_wechat_id: str | None = None,
    raw_customer_id: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    extra: dict[str, Any] | None = None,
    client_session_id: str | None = None,
) -> None:
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                UserActivityEvent(
                    occurred_at=datetime.datetime.now(),
                    user_id=user_id,
                    event_type=(event_type or "")[:40],
                    source=(source or "api")[:20],
                    sales_wechat_id=sales_wechat_id,
                    raw_customer_id=raw_customer_id,
                    object_type=object_type,
                    object_id=None if object_id is None else str(object_id)[:100],
                    extra_json=extra,
                    client_session_id=client_session_id,
                )
            )
            await db.commit()
    except Exception as e:
        logger.warning("record_activity_event failed type={} err={}", event_type, e)
