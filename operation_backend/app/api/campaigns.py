"""运营后台：营销活动 CRUD + 海报管理。"""
from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions as P
from app.core.backend_media import (
    delete_campaign_media_dir,
    delete_poster_media,
    upload_campaign_poster,
)
from app.core.campaign_helpers import (
    AUDIENCE_GENERAL,
    STATUS_DISABLED,
    STATUS_ENABLED,
    audience_choices,
    effective_status,
    normalize_audience,
    poster_stats_by_campaign,
    serialize_campaign,
    serialize_poster,
    count_campaigns_by_effective,
)
from app.core.campaign_media import CampaignMediaError
from app.core.context import OpContext, require_perm
from app.database import get_db
from app.models import Campaign, CampaignPoster

router = APIRouter(prefix="/api/op/campaigns", tags=["op-campaigns"])

_MAX_UPLOAD = 8 * 1024 * 1024


class CampaignBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    start_at: datetime.datetime
    end_at: datetime.datetime
    audience_unit_types: list[str] = Field(default_factory=lambda: [AUDIENCE_GENERAL])
    rules: str | None = None
    status: str = STATUS_ENABLED
    priority: int = 0


class StatusBody(BaseModel):
    status: str


class ReorderBody(BaseModel):
    poster_ids: list[int]


def _parse_dt(value: datetime.datetime | str) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text[:19] if len(text) >= 19 else text, fmt)
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail="时间格式无效")


def _normalize_payload(body: CampaignBody) -> dict[str, Any]:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写活动名称")
    start_at = _parse_dt(body.start_at)
    end_at = _parse_dt(body.end_at)
    if end_at < start_at:
        raise HTTPException(status_code=400, detail="结束时间不能早于开始时间")
    audience = normalize_audience(body.audience_unit_types)
    if not audience:
        audience = [AUDIENCE_GENERAL]
    if AUDIENCE_GENERAL in audience and len(audience) > 1:
        audience = [AUDIENCE_GENERAL]
    status = (body.status or STATUS_ENABLED).strip().lower()
    if status not in (STATUS_ENABLED, STATUS_DISABLED):
        status = STATUS_ENABLED
    try:
        priority = int(body.priority)
    except (TypeError, ValueError):
        priority = 0
    priority = max(-100, min(1000, priority))
    return {
        "name": name,
        "start_at": start_at,
        "end_at": end_at,
        "audience_unit_types": audience,
        "rules": (body.rules or "").strip() or None,
        "status": status,
        "priority": priority,
    }


@router.get("/meta/audience-choices")
async def get_audience_choices(
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_VIEW)),
    db: AsyncSession = Depends(get_db),
):
    choices = await audience_choices(db)
    return {"code": 200, "message": "ok", "data": {"choices": choices}}


@router.get("/stats")
async def campaign_stats(
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_VIEW)),
    db: AsyncSession = Depends(get_db),
):
    data = await count_campaigns_by_effective(db)
    return {"code": 200, "message": "ok", "data": data}


@router.get("")
async def list_campaigns(
    q: str = Query("", max_length=80),
    status: str | None = Query(None, description="enabled/disabled"),
    effective: str | None = Query(None, description="running/upcoming/ended/disabled"),
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_VIEW)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Campaign).order_by(Campaign.priority.desc(), Campaign.id.desc())
    keyword = (q or "").strip()
    if keyword:
        stmt = stmt.where(Campaign.name.like(f"%{keyword}%"))
    if status in (STATUS_ENABLED, STATUS_DISABLED):
        stmt = stmt.where(Campaign.status == status)
    rows = list((await db.execute(stmt)).scalars().all())
    if effective:
        rows = [c for c in rows if effective_status(c) == effective]
    ids = [int(c.id) for c in rows]
    stats = await poster_stats_by_campaign(db, ids)
    items = []
    for c in rows:
        total, active, cover = stats.get(int(c.id), (0, 0, None))
        items.append(
            serialize_campaign(
                c, poster_count=total, active_poster_count=active, cover_path=cover
            )
        )
    return {"code": 200, "message": "ok", "data": {"items": items}}


@router.post("")
async def create_campaign(
    body: CampaignBody,
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_EDIT)),
    db: AsyncSession = Depends(get_db),
):
    data = _normalize_payload(body)
    camp = Campaign(**data)
    db.add(camp)
    await db.commit()
    await db.refresh(camp)
    return {
        "code": 200,
        "message": "已创建",
        "data": serialize_campaign(camp),
    }


@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: int,
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_VIEW)),
    db: AsyncSession = Depends(get_db),
):
    camp = await db.get(Campaign, campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="活动不存在")
    stats = await poster_stats_by_campaign(db, [campaign_id])
    total, active, cover = stats.get(campaign_id, (0, 0, None))
    posters = (
        await db.execute(
            select(CampaignPoster)
            .where(CampaignPoster.campaign_id == campaign_id)
            .order_by(CampaignPoster.sort_order.asc(), CampaignPoster.id.asc())
        )
    ).scalars().all()
    return {
        "code": 200,
        "message": "ok",
        "data": {
            **serialize_campaign(
                camp, poster_count=total, active_poster_count=active, cover_path=cover
            ),
            "posters": [serialize_poster(p) for p in posters],
        },
    }


@router.put("/{campaign_id}")
async def update_campaign(
    campaign_id: int,
    body: CampaignBody,
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_EDIT)),
    db: AsyncSession = Depends(get_db),
):
    camp = await db.get(Campaign, campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="活动不存在")
    data = _normalize_payload(body)
    for k, v in data.items():
        setattr(camp, k, v)
    await db.commit()
    await db.refresh(camp)
    stats = await poster_stats_by_campaign(db, [campaign_id])
    total, active, cover = stats.get(campaign_id, (0, 0, None))
    return {
        "code": 200,
        "message": "已保存",
        "data": serialize_campaign(
            camp, poster_count=total, active_poster_count=active, cover_path=cover
        ),
    }


@router.patch("/{campaign_id}/status")
async def patch_status(
    campaign_id: int,
    body: StatusBody,
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_EDIT)),
    db: AsyncSession = Depends(get_db),
):
    camp = await db.get(Campaign, campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="活动不存在")
    status = (body.status or "").strip().lower()
    if status not in (STATUS_ENABLED, STATUS_DISABLED):
        raise HTTPException(status_code=400, detail="状态无效")
    camp.status = status
    await db.commit()
    await db.refresh(camp)
    return {"code": 200, "message": "已更新", "data": serialize_campaign(camp)}


@router.delete("/{campaign_id}")
async def delete_campaign(
    campaign_id: int,
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_EDIT)),
    db: AsyncSession = Depends(get_db),
):
    camp = await db.get(Campaign, campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="活动不存在")
    await db.delete(camp)
    await db.commit()
    try:
        await delete_campaign_media_dir(campaign_id)
    except CampaignMediaError:
        pass
    return {"code": 200, "message": "已删除", "data": None}


@router.get("/{campaign_id}/posters")
async def list_posters(
    campaign_id: int,
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_VIEW)),
    db: AsyncSession = Depends(get_db),
):
    camp = await db.get(Campaign, campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="活动不存在")
    posters = (
        await db.execute(
            select(CampaignPoster)
            .where(CampaignPoster.campaign_id == campaign_id)
            .order_by(CampaignPoster.sort_order.asc(), CampaignPoster.id.asc())
        )
    ).scalars().all()
    return {
        "code": 200,
        "message": "ok",
        "data": {"items": [serialize_poster(p) for p in posters]},
    }


@router.post("/{campaign_id}/posters")
async def upload_posters(
    campaign_id: int,
    files: list[UploadFile] = File(...),
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_EDIT)),
    db: AsyncSession = Depends(get_db),
):
    camp = await db.get(Campaign, campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="活动不存在")
    max_order = (
        await db.execute(
            select(func.max(CampaignPoster.sort_order)).where(
                CampaignPoster.campaign_id == campaign_id
            )
        )
    ).scalar()
    next_order = int(max_order or 0) + 1
    saved = 0
    created: list[CampaignPoster] = []
    try:
        for upload in files or []:
            filename = upload.filename or ""
            if not filename:
                continue
            content = await upload.read()
            if len(content) > _MAX_UPLOAD:
                raise CampaignMediaError("图片过大，最大允许 8MB")
            rel = await upload_campaign_poster(campaign_id, filename, content)
            if not rel:
                raise CampaignMediaError("上传未返回路径")
            poster = CampaignPoster(
                campaign_id=campaign_id,
                image_path=rel,
                sort_order=next_order,
                is_active=True,
            )
            db.add(poster)
            created.append(poster)
            next_order += 1
            saved += 1
        if saved == 0:
            raise HTTPException(status_code=400, detail="请选择至少一张图片")
        await db.commit()
        for p in created:
            await db.refresh(p)
    except CampaignMediaError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "code": 200,
        "message": f"已上传 {saved} 张",
        "data": {"items": [serialize_poster(p) for p in created]},
    }


@router.patch("/{campaign_id}/posters/{poster_id}")
async def patch_poster(
    campaign_id: int,
    poster_id: int,
    is_active: bool | None = Query(None),
    move: str | None = Query(None, description="up/down"),
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_EDIT)),
    db: AsyncSession = Depends(get_db),
):
    poster = await db.get(CampaignPoster, poster_id)
    if not poster or int(poster.campaign_id) != int(campaign_id):
        raise HTTPException(status_code=404, detail="海报不存在")
    if is_active is not None:
        poster.is_active = bool(is_active)
        await db.commit()
        await db.refresh(poster)
        return {"code": 200, "message": "已更新", "data": serialize_poster(poster)}
    if move in ("up", "down"):
        rows = list(
            (
                await db.execute(
                    select(CampaignPoster)
                    .where(CampaignPoster.campaign_id == campaign_id)
                    .order_by(CampaignPoster.sort_order.asc(), CampaignPoster.id.asc())
                )
            ).scalars().all()
        )
        idx = next((i for i, r in enumerate(rows) if int(r.id) == int(poster_id)), -1)
        swap_with = idx - 1 if move == "up" else idx + 1
        if idx < 0 or swap_with < 0 or swap_with >= len(rows):
            raise HTTPException(status_code=400, detail="已到边界，无法再移动")
        rows[idx].sort_order, rows[swap_with].sort_order = (
            rows[swap_with].sort_order,
            rows[idx].sort_order,
        )
        await db.commit()
        return {"code": 200, "message": "已调整顺序", "data": None}
    raise HTTPException(status_code=400, detail="无有效操作")


@router.post("/{campaign_id}/posters/reorder")
async def reorder_posters(
    campaign_id: int,
    body: ReorderBody,
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_EDIT)),
    db: AsyncSession = Depends(get_db),
):
    camp = await db.get(Campaign, campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="活动不存在")
    rows = list(
        (
            await db.execute(
                select(CampaignPoster).where(CampaignPoster.campaign_id == campaign_id)
            )
        ).scalars().all()
    )
    by_id = {int(r.id): r for r in rows}
    order_ids = [int(x) for x in (body.poster_ids or []) if int(x) in by_id]
    if len(order_ids) != len(by_id):
        raise HTTPException(status_code=400, detail="排序列表不完整")
    for i, pid in enumerate(order_ids):
        by_id[pid].sort_order = i + 1
    await db.commit()
    posters = (
        await db.execute(
            select(CampaignPoster)
            .where(CampaignPoster.campaign_id == campaign_id)
            .order_by(CampaignPoster.sort_order.asc(), CampaignPoster.id.asc())
        )
    ).scalars().all()
    return {
        "code": 200,
        "message": "已保存顺序",
        "data": {"items": [serialize_poster(p) for p in posters]},
    }


@router.delete("/{campaign_id}/posters/{poster_id}")
async def delete_poster(
    campaign_id: int,
    poster_id: int,
    ctx: OpContext = Depends(require_perm(P.PERM_ACTIVITY_EDIT)),
    db: AsyncSession = Depends(get_db),
):
    poster = await db.get(CampaignPoster, poster_id)
    if not poster or int(poster.campaign_id) != int(campaign_id):
        raise HTTPException(status_code=404, detail="海报不存在")
    path = poster.image_path
    await db.delete(poster)
    await db.commit()
    try:
        await delete_poster_media(path)
    except CampaignMediaError:
        pass
    return {"code": 200, "message": "已删除", "data": None}
