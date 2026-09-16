"""营销活动辅助：受众选项、生效状态。"""
from __future__ import annotations

import datetime
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Campaign, CampaignPoster, SystemConfig
from app.core.backend_media import absolute_media_url

AUDIENCE_GENERAL = "通用"
STATUS_ENABLED = "enabled"
STATUS_DISABLED = "disabled"

DEFAULT_UNIT_TYPE_CHOICES = [
    "学校",
    "卫健委",
    "消防",
    "街道办",
    "人民政府",
    "银行",
    "税务",
    "其他",
]


def normalize_audience(raw: Any) -> list[str]:
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def parse_unit_type_choices(raw: str | None) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return list(DEFAULT_UNIT_TYPE_CHOICES)
    # 支持换行 / 逗号 / JSON 列表风格
    if text.startswith("["):
        try:
            import json

            data = json.loads(text)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()] or list(
                    DEFAULT_UNIT_TYPE_CHOICES
                )
        except Exception:
            pass
    parts: list[str] = []
    for chunk in text.replace("，", ",").replace("\n", ",").split(","):
        name = chunk.strip().strip('"').strip("'")
        if name:
            parts.append(name)
    return parts or list(DEFAULT_UNIT_TYPE_CHOICES)


async def load_unit_type_choices(db: AsyncSession) -> list[str]:
    row = (
        await db.execute(
            select(SystemConfig).where(SystemConfig.config_key == "unit_type_choices")
        )
    ).scalars().first()
    raw = (row.config_value or "").strip() if row else ""
    return parse_unit_type_choices(raw)


async def audience_choices(db: AsyncSession) -> list[str]:
    units = await load_unit_type_choices(db)
    return [AUDIENCE_GENERAL, *units]


def effective_status(camp: Campaign, *, now: datetime.datetime | None = None) -> str:
    ref = now or datetime.datetime.now()
    if (camp.status or "") != STATUS_ENABLED:
        return "disabled"
    if camp.start_at and ref < camp.start_at:
        return "upcoming"
    if camp.end_at and ref > camp.end_at:
        return "ended"
    return "running"


EFFECTIVE_LABELS = {
    "running": "进行中",
    "upcoming": "未开始",
    "ended": "已结束",
    "disabled": "已关闭",
}


def serialize_campaign(
    camp: Campaign,
    *,
    poster_count: int = 0,
    active_poster_count: int = 0,
    cover_path: str | None = None,
) -> dict[str, Any]:
    audience = normalize_audience(camp.audience_unit_types)
    eff = effective_status(camp)
    return {
        "id": int(camp.id),
        "name": camp.name or "",
        "start_at": camp.start_at.isoformat(sep=" ") if camp.start_at else None,
        "end_at": camp.end_at.isoformat(sep=" ") if camp.end_at else None,
        "audience_unit_types": audience,
        "audience_label": "、".join(audience) if audience else "—",
        "rules": camp.rules or "",
        "status": camp.status or STATUS_DISABLED,
        "priority": int(camp.priority or 0),
        "effective_status": eff,
        "effective_label": EFFECTIVE_LABELS.get(eff, eff),
        "poster_count": poster_count,
        "active_poster_count": active_poster_count,
        "cover_path": cover_path,
        "cover_url": absolute_media_url(cover_path),
        "created_at": camp.created_at.isoformat(sep=" ") if camp.created_at else None,
        "updated_at": camp.updated_at.isoformat(sep=" ") if camp.updated_at else None,
    }


def serialize_poster(p: CampaignPoster) -> dict[str, Any]:
    return {
        "id": int(p.id),
        "campaign_id": int(p.campaign_id),
        "image_path": p.image_path,
        "image_url": absolute_media_url(p.image_path),
        "sort_order": int(p.sort_order or 0),
        "is_active": bool(p.is_active),
        "send_count": int(p.send_count or 0),
        "created_at": p.created_at.isoformat(sep=" ") if p.created_at else None,
    }


async def poster_stats_by_campaign(
    db: AsyncSession, campaign_ids: Sequence[int]
) -> dict[int, tuple[int, int, str | None]]:
    """返回 {campaign_id: (total, active, cover_path)}。"""
    if not campaign_ids:
        return {}
    rows = (
        await db.execute(
            select(CampaignPoster)
            .where(CampaignPoster.campaign_id.in_(list(campaign_ids)))
            .order_by(CampaignPoster.sort_order.asc(), CampaignPoster.id.asc())
        )
    ).scalars().all()
    out: dict[int, tuple[int, int, str | None]] = {
        int(cid): (0, 0, None) for cid in campaign_ids
    }
    for p in rows:
        cid = int(p.campaign_id)
        total, active, cover = out.get(cid, (0, 0, None))
        total += 1
        if p.is_active:
            active += 1
            if cover is None:
                cover = p.image_path
        elif cover is None:
            cover = p.image_path
        out[cid] = (total, active, cover)
    return out


async def count_campaigns_by_effective(db: AsyncSession) -> dict[str, int]:
    camps = (await db.execute(select(Campaign))).scalars().all()
    counts = {"running": 0, "upcoming": 0, "ended": 0, "disabled": 0, "total": 0}
    now = datetime.datetime.now()
    for c in camps:
        counts["total"] += 1
        counts[effective_status(c, now=now)] += 1
    return counts
