"""桌面/内部读取当前匹配的营销活动 & 活动群发 API。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from ai.campaign_blast import (
    add_recipients,
    clear_custom_image_for_job,
    create_or_rebuild_blast_job,
    delete_recipients,
    ensure_custom_job_ready_to_send,
    get_current_job,
    get_job_for_user,
    is_custom_job,
    job_to_dict,
    list_running_campaigns_for_unit,
    lock_posters_for_job,
    mark_job_sending,
    patch_custom_job_meta,
    patch_recipient_script,
    query_blast_candidates,
    require_bound_sales_wechat,
    retry_failed_recipients,
    set_custom_image_for_job,
    ack_recipient_send,
)
from ai.campaign_blast_llm import generate_scripts_for_job, get_desktop_chat_llm_client
from ai.campaign_service import (
    campaigns_for_customer,
    pick_poster_for_customer,
    tags_forbid_outreach,
)
from api.auth import get_current_user
import crud
from core.upload_limits import UploadLimitError, read_capped_upload
from database import get_db
from models import RawCustomer, SalesCustomerProfile, User

router = APIRouter(prefix="/api/campaigns", tags=["Campaigns"])


class BlastJobCreate(BaseModel):
    sales_wechat_id: str = Field(..., min_length=1)
    unit_type: str = Field(..., min_length=1)
    campaign_id: Optional[int] = None
    job_kind: Literal["campaign", "custom"] = "campaign"
    custom_brief: str = ""
    media_mode: str = "poster"
    limit: int = Field(100, ge=1, le=500)


class BlastRecipientsAdd(BaseModel):
    raw_customer_ids: list[str] = Field(default_factory=list)


class BlastRecipientsDelete(BaseModel):
    recipient_ids: list[int] = Field(default_factory=list)


class BlastScriptPatch(BaseModel):
    script_text: str = ""


class BlastGenerateScripts(BaseModel):
    recipient_ids: list[int] | None = None


class BlastAck(BaseModel):
    success: bool
    outbound_action_id: int | None = None
    error_message: str | None = None


class BlastCustomMetaPatch(BaseModel):
    custom_brief: str | None = None
    media_mode: str | None = None


def _ok(data: Any) -> dict:
    return {"code": 200, "data": data}


def _err(code: int, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail=message)


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
        return _ok({"items": []})

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
    return _ok({"items": items})


@router.get("/running")
async def list_running_campaigns(
    unit_type: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items = await list_running_campaigns_for_unit(db, unit_type)
    return _ok({"items": items})


@router.get("/blast/candidates")
async def blast_candidates(
    sales_wechat_id: str = Query(..., min_length=1),
    campaign_id: int | None = Query(None),
    job_kind: str = Query("campaign"),
    unit_type: str | None = Query(None),
    job_id: int | None = Query(None),
    q: str | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        await require_bound_sales_wechat(db, current_user, sales_wechat_id)
        items, total = await query_blast_candidates(
            db,
            sales_wechat_id=sales_wechat_id,
            unit_type=unit_type,
            campaign_id=int(campaign_id) if campaign_id else None,
            job_kind=job_kind,
            job_id=job_id,
            search_q=q,
            skip=skip,
            limit=limit,
        )
    except PermissionError as e:
        raise _err(status.HTTP_403_FORBIDDEN, str(e)) from e
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return _ok({"items": items, "total": total, "skip": skip, "limit": limit})


@router.post("/blast/jobs")
async def create_blast_job(
    body: BlastJobCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        job = await create_or_rebuild_blast_job(
            db,
            current_user,
            sales_wechat_id=body.sales_wechat_id,
            unit_type=body.unit_type,
            campaign_id=body.campaign_id,
            job_kind=body.job_kind,
            custom_brief=body.custom_brief,
            media_mode=body.media_mode,
            limit=body.limit,
        )
        job = await get_job_for_user(db, current_user.id, int(job.id))
        return _ok(await job_to_dict(db, job))
    except PermissionError as e:
        raise _err(status.HTTP_403_FORBIDDEN, str(e)) from e
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.get("/blast/jobs/current")
async def get_current_blast_job(
    sales_wechat_id: str = Query(..., min_length=1),
    campaign_id: int | None = Query(None),
    job_kind: str = Query("campaign"),
    unit_type: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = await get_current_job(
        db,
        current_user.id,
        sales_wechat_id,
        int(campaign_id) if campaign_id else None,
        job_kind=job_kind,
        unit_type=unit_type,
    )
    if not job:
        return _ok(None)
    job = await get_job_for_user(db, current_user.id, int(job.id))
    return _ok(await job_to_dict(db, job))


@router.patch("/blast/jobs/{job_id}/custom-meta")
async def patch_blast_custom_meta(
    job_id: int,
    body: BlastCustomMetaPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        job = await patch_custom_job_meta(
            db,
            current_user,
            int(job_id),
            custom_brief=body.custom_brief,
            media_mode=body.media_mode,
        )
        return _ok(await job_to_dict(db, job))
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/blast/jobs/{job_id}/custom-image")
async def upload_blast_custom_image(
    job_id: int,
    file: UploadFile = File(...),
    media_mode: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        content = await read_capped_upload(file)
        job = await set_custom_image_for_job(
            db,
            current_user,
            int(job_id),
            filename=str(getattr(file, "filename", "") or "custom.png"),
            content=content,
            media_mode=media_mode,
        )
        return _ok(await job_to_dict(db, job))
    except UploadLimitError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.delete("/blast/jobs/{job_id}/custom-image")
async def delete_blast_custom_image(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        job = await clear_custom_image_for_job(db, current_user, int(job_id))
        return _ok(await job_to_dict(db, job))
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.get("/blast/jobs/{job_id}")
async def get_blast_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = await get_job_for_user(db, current_user.id, int(job_id))
    if not job:
        raise _err(status.HTTP_404_NOT_FOUND, "任务不存在")
    return _ok(await job_to_dict(db, job))


@router.post("/blast/jobs/{job_id}/recipients")
async def add_blast_recipients(
    job_id: int,
    body: BlastRecipientsAdd,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        job = await add_recipients(db, current_user, int(job_id), body.raw_customer_ids)
        return _ok(await job_to_dict(db, job))
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/blast/jobs/{job_id}/recipients/delete")
async def remove_blast_recipients(
    job_id: int,
    body: BlastRecipientsDelete,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        job = await delete_recipients(db, current_user, int(job_id), body.recipient_ids)
        return _ok(await job_to_dict(db, job))
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.patch("/blast/jobs/{job_id}/recipients/{recipient_id}")
async def patch_blast_recipient_script(
    job_id: int,
    recipient_id: int,
    body: BlastScriptPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        item = await patch_recipient_script(
            db, current_user, int(job_id), int(recipient_id), body.script_text
        )
        return _ok(item)
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/blast/jobs/{job_id}/generate-scripts")
async def generate_blast_scripts(
    job_id: int,
    body: BlastGenerateScripts | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = await get_job_for_user(db, current_user.id, int(job_id))
    if not job:
        raise _err(status.HTTP_404_NOT_FOUND, "任务不存在")
    llm = await get_desktop_chat_llm_client(db)
    try:
        stats = await generate_scripts_for_job(
            db,
            job,
            recipient_ids=(body.recipient_ids if body else None),
            llm=llm,
            actor_user_id=int(current_user.id),
        )
        job = await get_job_for_user(db, current_user.id, int(job_id))
        return _ok({"stats": stats, "job": await job_to_dict(db, job)})
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/blast/jobs/{job_id}/lock-posters")
async def lock_blast_posters(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = await get_job_for_user(db, current_user.id, int(job_id))
    if not job:
        raise _err(status.HTTP_404_NOT_FOUND, "任务不存在")
    try:
        await lock_posters_for_job(db, job)
        await db.commit()
        job = await get_job_for_user(db, current_user.id, int(job_id))
        return _ok(await job_to_dict(db, job))
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/blast/jobs/{job_id}/retry-failed")
async def retry_blast_failed(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        job = await retry_failed_recipients(db, current_user, int(job_id))
        return _ok(await job_to_dict(db, job))
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/blast/jobs/{job_id}/start-sending")
async def start_blast_sending(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = await get_job_for_user(db, current_user.id, int(job_id))
    if not job:
        raise _err(status.HTTP_404_NOT_FOUND, "任务不存在")
    try:
        if is_custom_job(job):
            await ensure_custom_job_ready_to_send(job)
        else:
            await lock_posters_for_job(db, job)
        await mark_job_sending(db, int(job_id))
        job = await get_job_for_user(db, current_user.id, int(job_id))
        return _ok(await job_to_dict(db, job))
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("/blast/jobs/{job_id}/recipients/{recipient_id}/ack")
async def ack_blast_recipient(
    job_id: int,
    recipient_id: int,
    body: BlastAck,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        item = await ack_recipient_send(
            db,
            current_user,
            int(job_id),
            int(recipient_id),
            success=bool(body.success),
            outbound_action_id=body.outbound_action_id,
            error_message=body.error_message,
        )
        job = await get_job_for_user(db, current_user.id, int(job_id))
        return _ok({"recipient": item, "job": await job_to_dict(db, job)})
    except ValueError as e:
        raise _err(status.HTTP_400_BAD_REQUEST, str(e)) from e
