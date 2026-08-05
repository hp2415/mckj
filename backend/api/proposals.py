from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai.proposal.service import (
    downloads_root,
    enqueue_proposal,
    enqueue_revision,
    latest_version,
    proposal_payload,
)
from api.auth import get_current_user
from database import get_db
from models import (
    AiProposal,
    AiProposalVersion,
    ChatMessage,
    RawCustomer,
    User,
    UserSalesWechat,
)


router = APIRouter(prefix="/api/proposals", tags=["Proposals"])


class ProposalCreateRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    raw_customer_id: str | None = None
    sales_wechat_id: str | None = None


class ProposalReviseRequest(BaseModel):
    feedback: str = Field(min_length=1, max_length=4000)


async def _owned(db: AsyncSession, proposal_id: int, user_id: int) -> AiProposal:
    result = await db.execute(
        select(AiProposal).where(
            AiProposal.id == proposal_id,
            AiProposal.user_id == user_id,
        )
    )
    proposal = result.scalars().first()
    if proposal is None:
        raise HTTPException(status_code=404, detail="方案不存在")
    return proposal


async def _validate_customer_scope(
    db: AsyncSession,
    *,
    user_id: int,
    raw_customer_id: str | None,
    sales_wechat_id: str | None,
) -> None:
    raw_id = (raw_customer_id or "").strip()
    sales_id = (sales_wechat_id or "").strip()
    if not raw_id:
        return
    if await db.get(RawCustomer, raw_id) is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    if sales_id:
        result = await db.execute(
            select(UserSalesWechat.id).where(
                UserSalesWechat.user_id == user_id,
                UserSalesWechat.sales_wechat_id == sales_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=403, detail="无权访问该销售微信下的客户")


@router.post("")
async def create_proposal(
    body: ProposalCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _validate_customer_scope(
        db,
        user_id=current_user.id,
        raw_customer_id=body.raw_customer_id,
        sales_wechat_id=body.sales_wechat_id,
    )
    try:
        proposal = await enqueue_proposal(
            db,
            user_id=current_user.id,
            query=body.query,
            raw_customer_id=body.raw_customer_id,
            sales_wechat_id=body.sales_wechat_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"code": 200, "message": "方案已进入生成队列", "data": proposal_payload(proposal)}


@router.get("/{proposal_id}")
async def get_proposal(
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    proposal = await _owned(db, proposal_id, current_user.id)
    version = await latest_version(db, proposal.id) if proposal.current_version else None
    return {"code": 200, "message": "ok", "data": proposal_payload(proposal, version)}


@router.get("/{proposal_id}/preview")
async def get_proposal_preview(
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    proposal = await _owned(db, proposal_id, current_user.id)
    version = await latest_version(db, proposal.id)
    if version is None:
        raise HTTPException(status_code=409, detail="方案尚未生成完成")
    return {"code": 200, "message": "ok", "data": version.spec_json}


@router.post("/{proposal_id}/revise")
async def revise_proposal(
    proposal_id: int,
    body: ProposalReviseRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    proposal = await _owned(db, proposal_id, current_user.id)
    try:
        await enqueue_revision(db, proposal=proposal, feedback=body.feedback)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    # 调整也要落对话记录，否则切换会话后这一轮就看不见了
    payload = proposal_payload(proposal)
    if proposal.raw_customer_id:
        db.add(
            ChatMessage(
                user_id=current_user.id,
                raw_customer_id=proposal.raw_customer_id,
                role="user",
                content=body.feedback,
                sales_wechat_id=proposal.sales_wechat_id,
            )
        )
        message = ChatMessage(
            user_id=current_user.id,
            raw_customer_id=proposal.raw_customer_id,
            role="assistant",
            content=f"方案调整已进入队列（#{proposal.id}），正在重新选品并生成 Excel。",
            sales_wechat_id=proposal.sales_wechat_id,
        )
        db.add(message)
        await db.commit()
        await db.refresh(message)
        proposal.chat_message_id = message.id
        await db.commit()
        payload["chat_message_id"] = message.id
    return {"code": 200, "message": "调整已进入队列", "data": payload}


@router.get("/{proposal_id}/versions")
async def list_versions(
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    proposal = await _owned(db, proposal_id, current_user.id)
    result = await db.execute(
        select(AiProposalVersion)
        .where(AiProposalVersion.proposal_id == proposal.id)
        .order_by(desc(AiProposalVersion.version))
    )
    items = [
        {
            "version": row.version,
            "source": row.source,
            "user_feedback": row.user_feedback,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in result.scalars().all()
    ]
    return {"code": 200, "message": "ok", "data": items}


@router.get("/{proposal_id}/download")
async def download_proposal(
    proposal_id: int,
    version: int | None = Query(None, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    proposal = await _owned(db, proposal_id, current_user.id)
    stmt = select(AiProposalVersion).where(AiProposalVersion.proposal_id == proposal.id)
    if version is not None:
        stmt = stmt.where(AiProposalVersion.version == version)
    else:
        stmt = stmt.order_by(desc(AiProposalVersion.version)).limit(1)
    result = await db.execute(stmt)
    artifact = result.scalars().first()
    if artifact is None:
        raise HTTPException(status_code=409, detail="方案尚未生成完成")
    path = (downloads_root() / artifact.file_path).resolve()
    root = downloads_root().resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="方案文件不存在")
    filename = f"方案_{proposal.id}_v{artifact.version}.xlsx"
    return FileResponse(
        Path(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )

