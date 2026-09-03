"""活动群发：候选过滤、任务/名单 CRUD、回执去重。"""
from __future__ import annotations

import datetime
from typing import Any, Iterable

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import crud
from ai.campaign_service import (
    campaign_matches_unit,
    list_running_campaigns,
    normalize_audience,
    pick_poster_for_customer,
    tags_forbid_outreach,
)
from ai.profile_followup_policy import no_followup_profile_tag_names
from models import (
    Campaign,
    CampaignBlastJob,
    CampaignBlastReceipt,
    CampaignBlastRecipient,
    CampaignPoster,
    CampaignPosterSend,
    ProfileTagDefinition,
    RawCustomer,
    RawCustomerSalesWechat,
    SalesCustomerProfile,
    SalesWechatAccount,
    User,
    WechatOutboundAction,
    scp_profile_tags,
)

GROUP_CHAT_CUSTOMER_SUFFIX = "@chatroom"


def _rcsw_customer_not_in_sales_master_where():
    """好友 wxid 不在销售微信主数据（同事/销售号互加）。"""
    return ~exists(
        select(1).where(
            SalesWechatAccount.sales_wechat_id == RawCustomerSalesWechat.raw_customer_id
        )
    )

BLAST_EXCLUDED_TAG_NAMES = frozenset(
    no_followup_profile_tag_names()
    | frozenset({"已删除"})
)
BLAST_FORBIDDEN_SUBSTRINGS = ("禁止打扰", "勿打扰", "已删除")

JOB_OPEN_STATUSES = frozenset({"draft", "scripting", "ready", "sending"})
RECIPIENT_SENDABLE = frozenset({"pending", "failed"})


def _tag_name_excluded(name: str) -> bool:
    text = (name or "").strip()
    if not text:
        return False
    if text in BLAST_EXCLUDED_TAG_NAMES:
        return True
    return any(h in text for h in BLAST_FORBIDDEN_SUBSTRINGS)


async def _load_excluded_tag_ids(db: AsyncSession) -> frozenset[int]:
    names = tuple(BLAST_EXCLUDED_TAG_NAMES)
    if not names:
        return frozenset()
    res = await db.execute(
        select(ProfileTagDefinition.id).where(
            ProfileTagDefinition.is_active.is_(True),
            ProfileTagDefinition.name.in_(names),
        )
    )
    ids = frozenset(int(x) for x in res.scalars().all() if x is not None)
    extra_res = await db.execute(
        select(ProfileTagDefinition.id).where(
            ProfileTagDefinition.is_active.is_(True),
            or_(
                ProfileTagDefinition.name.like("%禁止打扰%"),
                ProfileTagDefinition.name.like("%勿打扰%"),
                ProfileTagDefinition.name.like("%已删除%"),
            ),
        )
    )
    return ids | frozenset(int(x) for x in extra_res.scalars().all() if x is not None)


def _scp_without_excluded_tags_clause(tag_ids: frozenset[int]):
    if not tag_ids:
        return True
    return ~exists(
        select(1)
        .select_from(scp_profile_tags)
        .where(
            scp_profile_tags.c.sales_customer_profile_id == SalesCustomerProfile.id,
            scp_profile_tags.c.profile_tag_id.in_(tuple(tag_ids)),
        )
    )


async def require_bound_sales_wechat(
    db: AsyncSession, user: User, sales_wechat_id: str
) -> None:
    sw = (sales_wechat_id or "").strip()
    if not sw:
        raise ValueError("缺少 sales_wechat_id")
    bound = await crud.bound_sales_wechat_ids_for_user(db, user.id, user.username)
    if sw not in bound:
        raise PermissionError("未绑定该业务微信")


async def already_sent_customer_ids(
    db: AsyncSession, campaign_id: int
) -> set[str]:
    cid = int(campaign_id)
    out: set[str] = set()
    r1 = await db.execute(
        select(CampaignBlastReceipt.raw_customer_id).where(
            CampaignBlastReceipt.campaign_id == cid
        )
    )
    out.update(str(x).strip() for x in r1.scalars().all() if x)
    r2 = await db.execute(
        select(WechatOutboundAction.raw_customer_id).where(
            WechatOutboundAction.campaign_id == cid,
            WechatOutboundAction.status == "sent",
        )
    )
    out.update(str(x).strip() for x in r2.scalars().all() if x)
    r3 = await db.execute(
        select(CampaignPosterSend.raw_customer_id).where(
            CampaignPosterSend.campaign_id == cid
        )
    )
    out.update(str(x).strip() for x in r3.scalars().all() if x)
    return out


async def campaign_has_active_posters(db: AsyncSession, campaign_id: int) -> bool:
    res = await db.execute(
        select(func.count())
        .select_from(CampaignPoster)
        .where(
            CampaignPoster.campaign_id == int(campaign_id),
            CampaignPoster.is_active.is_(True),
        )
    )
    return int(res.scalar() or 0) > 0


async def list_running_campaigns_for_unit(
    db: AsyncSession,
    unit_type: str,
    *,
    now: datetime.datetime | None = None,
) -> list[dict[str, Any]]:
    ut = (unit_type or "").strip()
    running = await list_running_campaigns(db, now=now)
    items: list[dict[str, Any]] = []
    for camp in running:
        types = normalize_audience(camp.audience_unit_types)
        if not campaign_matches_unit(types, ut):
            continue
        has_posters = await campaign_has_active_posters(db, int(camp.id))
        items.append(
            {
                "id": int(camp.id),
                "name": camp.name,
                "start_at": camp.start_at.isoformat() if camp.start_at else None,
                "end_at": camp.end_at.isoformat() if camp.end_at else None,
                "audience_unit_types": types,
                "rules": camp.rules or "",
                "priority": int(camp.priority or 0),
                "has_active_posters": has_posters,
            }
        )
    items.sort(key=lambda x: (-x["priority"], x.get("start_at") or ""))
    return items


def _base_candidate_stmt(
    sales_wechat_id: str,
    excluded_tag_ids: frozenset[int],
    *,
    unit_type: str | None = None,
    exclude_customer_ids: Iterable[str] | None = None,
    exclude_job_id: int | None = None,
    search_q: str | None = None,
):
    sw = (sales_wechat_id or "").strip()
    stmt = (
        select(
            RawCustomerSalesWechat,
            RawCustomer,
            SalesCustomerProfile,
        )
        .join(
            RawCustomer,
            RawCustomer.id == RawCustomerSalesWechat.raw_customer_id,
        )
        .outerjoin(
            SalesCustomerProfile,
            and_(
                SalesCustomerProfile.raw_customer_id == RawCustomerSalesWechat.raw_customer_id,
                SalesCustomerProfile.sales_wechat_id == RawCustomerSalesWechat.sales_wechat_id,
            ),
        )
        .where(
            RawCustomerSalesWechat.sales_wechat_id == sw,
            or_(
                RawCustomerSalesWechat.is_deleted.is_(False),
                RawCustomerSalesWechat.is_deleted.is_(None),
            ),
            ~RawCustomerSalesWechat.raw_customer_id.endswith(GROUP_CHAT_CUSTOMER_SUFFIX),
            _rcsw_customer_not_in_sales_master_where(),
            _scp_without_excluded_tags_clause(excluded_tag_ids),
        )
    )
    ut = (unit_type or "").strip()
    if ut:
        stmt = stmt.where(RawCustomer.unit_type == ut)
    ex = {str(x).strip() for x in (exclude_customer_ids or []) if str(x).strip()}
    if ex:
        stmt = stmt.where(~RawCustomerSalesWechat.raw_customer_id.in_(tuple(ex)))
    if exclude_job_id:
        sub = select(CampaignBlastRecipient.raw_customer_id).where(
            CampaignBlastRecipient.job_id == int(exclude_job_id)
        )
        stmt = stmt.where(~RawCustomerSalesWechat.raw_customer_id.in_(sub))
    q = (search_q or "").strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                RawCustomerSalesWechat.name.ilike(like),
                RawCustomerSalesWechat.remark.ilike(like),
                RawCustomerSalesWechat.phone.ilike(like),
                RawCustomer.name.ilike(like),
                RawCustomer.phone.ilike(like),
            )
        )
    return stmt


async def query_blast_candidates(
    db: AsyncSession,
    *,
    sales_wechat_id: str,
    unit_type: str | None = None,
    campaign_id: int,
    job_id: int | None = None,
    search_q: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    excluded_tag_ids = await _load_excluded_tag_ids(db)
    sent_ids = await already_sent_customer_ids(db, int(campaign_id))
    base = _base_candidate_stmt(
        sales_wechat_id,
        excluded_tag_ids,
        unit_type=unit_type,
        exclude_customer_ids=sent_ids,
        exclude_job_id=job_id,
        search_q=search_q,
    )
    count_stmt = select(func.count()).select_from(base.subquery())
    total = int((await db.execute(count_stmt)).scalar() or 0)
    rows = (
        await db.execute(
            base.order_by(RawCustomerSalesWechat.name.asc())
            .offset(max(0, int(skip)))
            .limit(max(1, min(200, int(limit))))
        )
    ).all()
    items: list[dict[str, Any]] = []
    for rcsw, rc, _scp in rows:
        rid = str(rcsw.raw_customer_id or "").strip()
        if not rid:
            continue
        items.append(
            {
                "raw_customer_id": rid,
                "display_name": (rcsw.name or rc.name or "").strip() or rid,
                "remark": (rcsw.remark or "").strip(),
                "unit_type": (rc.unit_type or "").strip(),
                "phone": (rcsw.phone or rc.phone or "").strip(),
            }
        )
    return items, total


async def _get_open_job(
    db: AsyncSession,
    user_id: int,
    sales_wechat_id: str,
    campaign_id: int,
) -> CampaignBlastJob | None:
    res = await db.execute(
        select(CampaignBlastJob)
        .where(
            CampaignBlastJob.user_id == int(user_id),
            CampaignBlastJob.sales_wechat_id == (sales_wechat_id or "").strip(),
            CampaignBlastJob.campaign_id == int(campaign_id),
            CampaignBlastJob.status.in_(tuple(JOB_OPEN_STATUSES)),
        )
        .order_by(CampaignBlastJob.updated_at.desc())
        .limit(1)
    )
    return res.scalars().first()


async def create_or_rebuild_blast_job(
    db: AsyncSession,
    user: User,
    *,
    sales_wechat_id: str,
    unit_type: str,
    campaign_id: int,
    limit: int = 100,
) -> CampaignBlastJob:
    await require_bound_sales_wechat(db, user, sales_wechat_id)
    ut = (unit_type or "").strip()
    cid = int(campaign_id)
    camp = await db.get(Campaign, cid)
    if not camp or (camp.status or "") != "enabled":
        raise ValueError("活动不存在或未启用")
    if not await campaign_has_active_posters(db, cid):
        raise ValueError("该活动暂无可用海报，无法群发")
    running = await list_running_campaigns(db)
    if not any(int(c.id) == cid for c in running):
        raise ValueError("活动不在进行中")
    types = normalize_audience(camp.audience_unit_types)
    if not campaign_matches_unit(types, ut):
        raise ValueError("活动与所选单位性质不匹配")

    job = await _get_open_job(db, user.id, sales_wechat_id, cid)
    now = datetime.datetime.now()
    if job is None:
        job = CampaignBlastJob(
            user_id=int(user.id),
            sales_wechat_id=(sales_wechat_id or "").strip(),
            unit_type=ut,
            campaign_id=cid,
            status="draft",
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        await db.flush()
    else:
        job.unit_type = ut
        job.updated_at = now
        job.status = "draft"

    await db.execute(
        delete(CampaignBlastRecipient).where(
            CampaignBlastRecipient.job_id == int(job.id),
            CampaignBlastRecipient.status != "sent",
        )
    )
    await db.flush()

    excluded_tag_ids = await _load_excluded_tag_ids(db)
    sent_ids = await already_sent_customer_ids(db, cid)
    existing_sent = await db.execute(
        select(CampaignBlastRecipient.raw_customer_id).where(
            CampaignBlastRecipient.job_id == int(job.id),
            CampaignBlastRecipient.status == "sent",
        )
    )
    skip_ids = sent_ids | {str(x).strip() for x in existing_sent.scalars().all() if x}

    base = _base_candidate_stmt(
        sales_wechat_id,
        excluded_tag_ids,
        unit_type=ut,
        exclude_customer_ids=skip_ids,
        exclude_job_id=int(job.id),
    )
    rows = (
        await db.execute(
            base.order_by(RawCustomerSalesWechat.name.asc()).limit(
                max(1, min(500, int(limit or 100)))
            )
        )
    ).all()
    for rcsw, rc, _scp in rows:
        rid = str(rcsw.raw_customer_id or "").strip()
        if not rid:
            continue
        db.add(
            CampaignBlastRecipient(
                job_id=int(job.id),
                raw_customer_id=rid,
                display_name=(rcsw.name or rc.name or "").strip() or rid,
                remark=(rcsw.remark or "").strip(),
                unit_type=(rc.unit_type or "").strip(),
                status="pending",
                created_at=now,
                updated_at=now,
            )
        )
    await db.commit()
    await db.refresh(job)
    return job


async def get_job_for_user(
    db: AsyncSession, user_id: int, job_id: int
) -> CampaignBlastJob | None:
    res = await db.execute(
        select(CampaignBlastJob)
        .options(selectinload(CampaignBlastJob.recipients))
        .execution_options(populate_existing=True)
        .where(
            CampaignBlastJob.id == int(job_id),
            CampaignBlastJob.user_id == int(user_id),
        )
    )
    return res.scalars().first()


async def get_current_job(
    db: AsyncSession,
    user_id: int,
    sales_wechat_id: str,
    campaign_id: int,
) -> CampaignBlastJob | None:
    return await _get_open_job(db, user_id, sales_wechat_id, int(campaign_id))


def _recipient_to_dict(r: CampaignBlastRecipient, poster_path: str | None = None) -> dict:
    return {
        "id": int(r.id),
        "raw_customer_id": r.raw_customer_id,
        "display_name": r.display_name or "",
        "remark": r.remark or "",
        "unit_type": r.unit_type or "",
        "script_text": r.script_text or "",
        "script_generated_at": (
            r.script_generated_at.isoformat() if r.script_generated_at else None
        ),
        "poster_id": int(r.poster_id) if r.poster_id else None,
        "poster_image_path": poster_path,
        "status": r.status or "pending",
        "error_message": r.error_message or "",
        "sent_at": r.sent_at.isoformat() if r.sent_at else None,
        "outbound_action_id": int(r.outbound_action_id) if r.outbound_action_id else None,
    }


async def job_to_dict(db: AsyncSession, job: CampaignBlastJob) -> dict[str, Any]:
    poster_paths: dict[int, str] = {}
    if job.recipients:
        pids = {int(r.poster_id) for r in job.recipients if r.poster_id}
        if pids:
            pres = await db.execute(
                select(CampaignPoster).where(CampaignPoster.id.in_(tuple(pids)))
            )
            for p in pres.scalars().all():
                poster_paths[int(p.id)] = p.image_path or ""
    recipients = [
        _recipient_to_dict(r, poster_paths.get(int(r.poster_id)) if r.poster_id else None)
        for r in sorted(job.recipients or [], key=lambda x: (x.display_name or "", x.id))
    ]
    stats = {"pending": 0, "sent": 0, "failed": 0, "skipped": 0}
    for r in job.recipients or []:
        st = (r.status or "pending").strip()
        stats[st] = stats.get(st, 0) + 1
    camp = await db.get(Campaign, int(job.campaign_id))
    return {
        "id": int(job.id),
        "sales_wechat_id": job.sales_wechat_id,
        "unit_type": job.unit_type,
        "campaign_id": int(job.campaign_id),
        "campaign_name": camp.name if camp else "",
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "stats": stats,
        "recipients": recipients,
    }


async def add_recipients(
    db: AsyncSession,
    user: User,
    job_id: int,
    raw_customer_ids: list[str],
) -> CampaignBlastJob:
    job = await get_job_for_user(db, user.id, job_id)
    if not job:
        raise ValueError("任务不存在")
    await require_bound_sales_wechat(db, user, job.sales_wechat_id)
    ids = [str(x).strip() for x in raw_customer_ids if str(x).strip()]
    if not ids:
        raise ValueError("未选择客户")
    excluded_tag_ids = await _load_excluded_tag_ids(db)
    sent_ids = await already_sent_customer_ids(db, int(job.campaign_id))
    now = datetime.datetime.now()
    for rid in ids:
        if rid in sent_ids:
            continue
        stmt = _base_candidate_stmt(
            job.sales_wechat_id,
            excluded_tag_ids,
            exclude_customer_ids=sent_ids,
            exclude_job_id=int(job.id),
        ).where(RawCustomerSalesWechat.raw_customer_id == rid)
        row = (await db.execute(stmt.limit(1))).first()
        if not row:
            continue
        rcsw, rc, _scp = row
        existing = await db.execute(
            select(CampaignBlastRecipient).where(
                CampaignBlastRecipient.job_id == int(job.id),
                CampaignBlastRecipient.raw_customer_id == rid,
            )
        )
        if existing.scalars().first():
            continue
        db.add(
            CampaignBlastRecipient(
                job_id=int(job.id),
                raw_customer_id=rid,
                display_name=(rcsw.name or rc.name or "").strip() or rid,
                remark=(rcsw.remark or "").strip(),
                unit_type=(rc.unit_type or "").strip(),
                status="pending",
                created_at=now,
                updated_at=now,
            )
        )
    job.updated_at = now
    job.status = "draft"
    uid = int(user.id)
    await db.commit()
    job = await get_job_for_user(db, uid, job_id)
    return job  # type: ignore[return-value]


async def delete_recipients(
    db: AsyncSession,
    user: User,
    job_id: int,
    recipient_ids: list[int],
) -> CampaignBlastJob:
    job = await get_job_for_user(db, user.id, job_id)
    if not job:
        raise ValueError("任务不存在")
    rids = [int(x) for x in recipient_ids if int(x) > 0]
    if rids:
        await db.execute(
            delete(CampaignBlastRecipient).where(
                CampaignBlastRecipient.job_id == int(job.id),
                CampaignBlastRecipient.id.in_(tuple(rids)),
                CampaignBlastRecipient.status != "sent",
            )
        )
    job.updated_at = datetime.datetime.now()
    uid = int(user.id)
    await db.commit()
    job = await get_job_for_user(db, uid, job_id)
    return job  # type: ignore[return-value]


async def patch_recipient_script(
    db: AsyncSession,
    user: User,
    job_id: int,
    recipient_id: int,
    script_text: str,
) -> dict:
    job = await get_job_for_user(db, user.id, job_id)
    if not job:
        raise ValueError("任务不存在")
    rec = next((r for r in job.recipients or [] if int(r.id) == int(recipient_id)), None)
    if not rec:
        raise ValueError("名单行不存在")
    if (rec.status or "") == "sent":
        raise ValueError("已发送客户不可修改话术")
    rec.script_text = (script_text or "").strip()
    rec.updated_at = datetime.datetime.now()
    job.updated_at = rec.updated_at
    if job.status == "draft" and rec.script_text:
        job.status = "ready"
    await db.commit()
    return _recipient_to_dict(rec)


async def lock_posters_for_job(db: AsyncSession, job: CampaignBlastJob) -> None:
    cid = int(job.campaign_id)
    for rec in job.recipients or []:
        if (rec.status or "") not in RECIPIENT_SENDABLE:
            continue
        if not (rec.script_text or "").strip():
            continue
        poster = await pick_poster_for_customer(
            db,
            campaign_id=cid,
            raw_customer_id=str(rec.raw_customer_id),
        )
        if not poster:
            raise ValueError(f"客户 {rec.display_name} 无可用海报")
        rec.poster_id = int(poster.id)
        rec.updated_at = datetime.datetime.now()


async def retry_failed_recipients(
    db: AsyncSession, user: User, job_id: int
) -> CampaignBlastJob:
    job = await get_job_for_user(db, user.id, job_id)
    if not job:
        raise ValueError("任务不存在")
    now = datetime.datetime.now()
    await db.execute(
        update(CampaignBlastRecipient)
        .where(
            CampaignBlastRecipient.job_id == int(job.id),
            CampaignBlastRecipient.status == "failed",
        )
        .values(status="pending", error_message=None, updated_at=now)
    )
    job.status = "ready"
    job.updated_at = now
    await db.commit()
    job = await get_job_for_user(db, user.id, job_id)
    return job  # type: ignore[return-value]


async def ack_recipient_send(
    db: AsyncSession,
    user: User,
    job_id: int,
    recipient_id: int,
    *,
    success: bool,
    outbound_action_id: int | None = None,
    error_message: str | None = None,
) -> dict:
    job = await get_job_for_user(db, user.id, job_id)
    if not job:
        raise ValueError("任务不存在")
    rec = next((r for r in job.recipients or [] if int(r.id) == int(recipient_id)), None)
    if not rec:
        raise ValueError("名单行不存在")
    now = datetime.datetime.now()
    if success:
        if not rec.poster_id:
            raise ValueError("缺少海报")
        existing = await db.execute(
            select(CampaignBlastReceipt).where(
                CampaignBlastReceipt.campaign_id == int(job.campaign_id),
                CampaignBlastReceipt.raw_customer_id == str(rec.raw_customer_id),
            )
        )
        if existing.scalars().first():
            rec.status = "failed"
            rec.error_message = "该客户已成功接收过本活动"
            rec.updated_at = now
            job.updated_at = now
            await db.commit()
            raise ValueError("该客户已成功接收过本活动")
        rec.status = "sent"
        rec.sent_at = now
        rec.error_message = None
        if outbound_action_id:
            rec.outbound_action_id = int(outbound_action_id)
        rec.updated_at = now
        db.add(
            CampaignBlastReceipt(
                campaign_id=int(job.campaign_id),
                raw_customer_id=str(rec.raw_customer_id),
                sales_wechat_id=job.sales_wechat_id,
                job_id=int(job.id),
                recipient_id=int(rec.id),
                outbound_action_id=int(outbound_action_id) if outbound_action_id else None,
                poster_id=int(rec.poster_id),
                sent_at=now,
            )
        )
        # campaign_poster_sends 由 report_wechat_outbound_result 在 status=sent 时写入
    else:
        rec.status = "failed"
        rec.error_message = (error_message or "发送失败").strip()[:500]
        rec.updated_at = now

    pending = sum(
        1
        for r in job.recipients or []
        if (r.status or "") in RECIPIENT_SENDABLE
    )
    failed_n = sum(1 for r in job.recipients or [] if (r.status or "") == "failed")
    sent_n = sum(1 for r in job.recipients or [] if (r.status or "") == "sent")
    job.updated_at = now
    if pending == 0:
        job.status = "done" if failed_n == 0 else "ready"
    else:
        job.status = "sending"
    if sent_n == 0 and failed_n == 0 and pending > 0:
        job.status = "sending"
    await db.commit()
    refreshed = (
        await db.execute(
            select(CampaignBlastRecipient).where(CampaignBlastRecipient.id == int(recipient_id))
        )
    ).scalars().first()
    return _recipient_to_dict(refreshed) if refreshed else {}


async def mark_job_sending(db: AsyncSession, job_id: int) -> None:
    await db.execute(
        update(CampaignBlastJob)
        .where(CampaignBlastJob.id == int(job_id))
        .values(status="sending", updated_at=datetime.datetime.now())
    )
    await db.commit()


async def build_script_payloads(
    db: AsyncSession, job: CampaignBlastJob, recipient_ids: list[int] | None
) -> list[dict[str, Any]]:
    """为批次 LLM 组装精简客户快照（标签 + 画像摘要，不含聊天/订单全文）。"""
    targets: list[CampaignBlastRecipient] = []
    id_set = {int(x) for x in (recipient_ids or []) if int(x) > 0}
    for rec in job.recipients or []:
        if (rec.status or "") == "sent":
            continue
        if id_set and int(rec.id) not in id_set:
            continue
        targets.append(rec)
    if not targets:
        return []

    rids = [str(rec.raw_customer_id) for rec in targets]
    name_by_rid: dict[str, str] = {}
    if rids:
        rc_rows = (
            await db.execute(
                select(RawCustomer.id, RawCustomer.customer_name).where(
                    RawCustomer.id.in_(tuple(rids))
                )
            )
        ).all()
        name_by_rid = {str(i): (n or "").strip() for i, n in rc_rows}

    scp_ids: list[int] = []
    scp_by_rid: dict[str, SalesCustomerProfile] = {}
    for rec in targets:
        scp_res = await db.execute(
            select(SalesCustomerProfile).where(
                SalesCustomerProfile.raw_customer_id == str(rec.raw_customer_id),
                SalesCustomerProfile.sales_wechat_id == job.sales_wechat_id,
            )
        )
        scp = scp_res.scalars().first()
        if scp and scp.id:
            scp_ids.append(int(scp.id))
            scp_by_rid[str(rec.raw_customer_id)] = scp
    tags_map = await crud.profile_tags_by_relation_ids(db, scp_ids)
    payloads: list[dict[str, Any]] = []
    for rec in targets:
        rid = str(rec.raw_customer_id)
        scp = scp_by_rid.get(rid)
        tags = (tags_map.get(int(scp.id)) or []) if scp and scp.id else []
        tag_names = [str(t.get("name") or "") for t in tags if t.get("name")]
        if _tag_name_excluded(" ".join(tag_names)) or tags_forbid_outreach(tag_names):
            continue
        ai_profile = ""
        if scp and scp.ai_profile:
            ai_profile = str(scp.ai_profile)[:280]
        payloads.append(
            {
                "raw_customer_id": rid,
                "customer_name": name_by_rid.get(rid, ""),
                "title": (scp.title or "").strip() if scp else "",
                "display_name": rec.display_name or "",
                "remark": rec.remark or "",
                "unit_type": rec.unit_type or "",
                "profile_tags": tag_names,
                "ai_profile_excerpt": ai_profile,
            }
        )
    return payloads
