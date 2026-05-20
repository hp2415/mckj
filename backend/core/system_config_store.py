"""
system_configs（环境控制变量）统一写入路径。

优先使用 SELECT + UPDATE / INSERT（ORM），避免高频场景下大量使用
INSERT ... ON DUPLICATE KEY UPDATE——在 MySQL 上容易让 AUTO_INCREMENT「空涨」，
而 config_key 仍唯一一行，看起来像 id 乱跳。

用法：异步会话由调用方 commit。
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import SystemConfig


async def upsert_system_config_row(
    db: AsyncSession,
    *,
    config_key: str,
    config_value: str,
    config_group: str = "general",
    description: str | None = None,
    update_description: bool = False,
) -> SystemConfig:
    """
    :param update_description: 为 True 时，在非空传入 description 时覆盖已有备注；默认不改动已有备注。
    """
    k = (config_key or "").strip()
    if not k:
        raise ValueError("config_key required")
    now = datetime.datetime.now()

    res = await db.execute(select(SystemConfig).where(SystemConfig.config_key == k))
    row = res.scalars().first()
    if row:
        row.config_value = config_value
        row.config_group = (config_group or row.config_group or "general").strip() or "general"
        row.updated_at = now
        if update_description and description is not None:
            row.description = description
        elif description is not None and not row.description:
            row.description = description
        return row

    cfg = SystemConfig(
        config_key=k,
        config_value=config_value,
        config_group=(config_group or "general").strip() or "general",
        description=description,
    )
    cfg.updated_at = now
    db.add(cfg)
    return cfg
