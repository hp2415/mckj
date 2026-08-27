"""桌面/内部读取当前匹配的营销活动。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from ai.campaign_service import (
    campaigns_for_customer,
    pick_poster_for_customer,
    tags_forbid_outreach,
)
from api.auth import get_current_user
import crud
from database import get_db
from models import RawCustomer, SalesCustomerProfile, User

router = APIRouter(prefix="/api/campaigns", tags=["Campaigns"])


@router.get("/active")
async def list_active_campaigns_for_customer(
    raw_customer_id: str = Query(..., min_length=1),
    sales_wechat_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rid = (raw_customer_id or "").strip()
    rc_res = await db.execute(select(RawCustomer).where(RawCustomer.id == rid))
    customer = rc_res.scalars().first()
    if not customer:
        return {"code": 200, "data": {"items": []}}

    sw = (sales_wechat_id or "").strip()
    forbidden = False
    if sw:
        rel_res = await db.execute(
            select(SalesCustomerProfile).where(
                SalesCustomerProfile.raw_customer_id == rid,
                SalesCustomerProfile.sales_wechat_id == sw,
            )
        )
        relation = rel_res.scalars().first()
        if relation:
            tags = await crud.profile_tags_for_relation(db, relation.id)
            forbidden = tags_forbid_outreach([str(t.get("name") or "") for t in tags])

    camps = await campaigns_for_customer(
        db,
        unit_type=customer.unit_type,
        forbidden_outreach=forbidden,
    )
    items = []
    for camp in camps:
        poster = await pick_poster_for_customer(
            db, campaign_id=int(camp.id), raw_customer_id=rid
        )
        items.append(
            {
                "id": camp.id,
                "name": camp.name,
                "start_at": camp.start_at.isoformat() if camp.start_at else None,
                "end_at": camp.end_at.isoformat() if camp.end_at else None,
                "audience_unit_types": camp.audience_unit_types or [],
                "rules": camp.rules or "",
                "priority": camp.priority,
                "next_poster": (
                    {
                        "id": poster.id,
                        "image_path": poster.image_path,
                    }
                    if poster
                    else None
                ),
            }
        )
    return {"code": 200, "data": {"items": items}}
