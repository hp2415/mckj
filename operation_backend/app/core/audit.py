from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OpAuditLog


async def write_audit(
    db: AsyncSession,
    *,
    actor_user_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: str | int | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    db.add(
        OpAuditLog(
            occurred_at=datetime.datetime.now(),
            actor_user_id=actor_user_id,
            action=action[:60],
            target_type=(target_type or None),
            target_id=None if target_id is None else str(target_id)[:64],
            detail_json=detail,
            ip=(ip or None),
        )
    )
