"""桌面端「一键发微信」审计 API。"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

import crud
import schemas
from api.auth import get_current_user
from database import get_db
from models import (
    ChatMessage,
    RawCustomer,
    RawCustomerSalesWechat,
    SalesWechatAccount,
    User,
    WechatOutboundAction,
)

router = APIRouter(prefix="/api/wechat", tags=["WechatOutbound"])


def _dedupe_receiver_candidates(candidates: list[dict]) -> list[dict]:
    """按 keyword 去重，保留首次出现的 source 顺序。"""
    seen: set[str] = set()
    out: list[dict] = []
    for item in candidates:
        kw = (item.get("keyword") or "").strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        out.append({"keyword": kw, "source": (item.get("source") or "").strip()})
    return out


async def _resolve_receiver_candidates(
    db: AsyncSession,
    raw_customer_id: str,
    sales_wechat_id: str,
) -> tuple[list[dict], str | None]:
    """
    解析微信搜索框可用的 receiver 候选列表（按备注 → 昵称顺序）。
    返回 (candidates, err_code)。
    """
    stmt = select(RawCustomerSalesWechat).where(
        RawCustomerSalesWechat.raw_customer_id == raw_customer_id,
        RawCustomerSalesWechat.sales_wechat_id == sales_wechat_id,
    )
    res = await db.execute(stmt)
    rcsw = res.scalars().first()
    if not rcsw:
        return [], "customer_not_in_thread"

    rc_res = await db.execute(select(RawCustomer).where(RawCustomer.id == raw_customer_id))
    rc = rc_res.scalars().first()

    candidates: list[dict] = []
    rem = (rcsw.remark or "").strip()
    if rem:
        candidates.append({"keyword": rem, "source": "remark"})
    name = (rcsw.name or "").strip()
    if name:
        candidates.append({"keyword": name, "source": "name"})
    rid = (raw_customer_id or "").strip()
    # if rid.startswith("wxid_"):
    #     candidates.append({"keyword": rid, "source": "wxid"})
    # phone = (rcsw.phone or "").strip()
    # if not phone and rc:
    #     phone = (rc.phone_normalized or rc.phone or "").strip()
    # if phone:
    #     candidates.append({"keyword": phone, "source": "phone"})


    candidates = _dedupe_receiver_candidates(candidates)
    if not candidates:
        return [], "receiver_unresolved"
    return candidates, None


def _fmt_dt(value) -> str | None:
    if value is None:
        return None
    try:
        return value.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def _serialize_outbound_history_row(row: WechatOutboundAction) -> dict:
    text = (row.edited_text or "").strip()
    return {
        "id": row.id,
        "edited_text": text,
        "original_text": (row.original_text or "").strip() or None,
        "action_type": row.action_type,
        "status": row.status,
        "receiver": row.receiver,
        "raw_customer_id": row.raw_customer_id,
        "error": (row.error or "").strip() or None,
        "created_at": _fmt_dt(row.created_at),
        "completed_at": _fmt_dt(row.completed_at),
    }


@router.get("/outbound-actions")
async def list_outbound_actions(
    raw_customer_id: str | None = Query(None, description="按客户筛选；不传则返回当前用户最近记录"),
    status_filter: str | None = Query(
        "sent,failed",
        alias="status",
        description="逗号分隔状态，默认 sent,failed（含失败便于重发改稿）",
    ),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """桌面端编辑外发弹窗：拉取本人历史外发正文，供复制改稿。"""
    statuses = [
        s.strip()
        for s in str(status_filter or "").split(",")
        if s.strip() in ("pending", "sent", "failed", "blocked")
    ]
    if not statuses:
        statuses = ["sent", "failed"]

    stmt = select(WechatOutboundAction).where(
        WechatOutboundAction.actor_user_id == current_user.id,
        WechatOutboundAction.status.in_(statuses),
        WechatOutboundAction.edited_text.is_not(None),
        WechatOutboundAction.edited_text != "",
    )
    cid = (raw_customer_id or "").strip()
    if cid:
        stmt = stmt.where(WechatOutboundAction.raw_customer_id == cid)

    stmt = stmt.order_by(
        desc(WechatOutboundAction.completed_at),
        desc(WechatOutboundAction.created_at),
        desc(WechatOutboundAction.id),
    ).limit(limit)

    res = await db.execute(stmt)
    rows = list(res.scalars().all())

    return {
        "code": 200,
        "message": "ok",
        "data": {
            "list": [_serialize_outbound_history_row(r) for r in rows],
            "scope": "customer" if cid else "self",
        },
    }


@router.post("/outbound-actions")
async def create_outbound_action(
    body: schemas.WechatOutboundCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sw = (body.sales_wechat_id or "").strip()
    raw_cid = (body.raw_customer_id or "").strip()
    claimed = (body.claimed_local_sales_wechat_id or "").strip()

    if claimed != sw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="本机声明的销售微信与当前客户线程不一致，请切换本机微信或选择正确客户。",
        )

    bound = await crud.bound_sales_wechat_ids_for_user(db, current_user.id, current_user.username)
    if sw not in set(bound):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前账号未绑定该销售微信号，无法外发。",
        )

    candidates, err = await _resolve_receiver_candidates(db, raw_cid, sw)
    if err == "customer_not_in_thread":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该客户不在此销售微信好友维度下，无法外发。",
        )
    if not candidates or err == "receiver_unresolved":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无法解析微信搜索用的联系人（缺少备注/昵称），请完善云客好友数据。",
        )
    receiver = candidates[0]["keyword"]
    receiver_source = candidates[0]["source"]

    if body.source_chat_message_id is not None:
        mres = await db.execute(
            select(ChatMessage).where(ChatMessage.id == body.source_chat_message_id)
        )
        cm = mres.scalars().first()
        if (
            not cm
            or cm.user_id != current_user.id
            or (cm.raw_customer_id or "") != raw_cid
            or (cm.role or "") != "assistant"
        ):
            raise HTTPException(status_code=400, detail="引用的 AI 消息无效或无权操作")

    acc_res = await db.execute(
        select(SalesWechatAccount).where(SalesWechatAccount.sales_wechat_id == sw)
    )
    acc = acc_res.scalars().first()
    sw_display = (acc.nickname or acc.alias_name or "").strip() if acc else ""
    if not sw_display:
        sw_display = sw

    if body.action_type == "edit_send":
        orig_txt = (body.original_text or "").strip()
    else:
        orig_txt = (body.edited_text or "").strip()

    row = WechatOutboundAction(
        actor_user_id=current_user.id,
        raw_customer_id=raw_cid,
        sales_wechat_id=sw,
        source_chat_message_id=body.source_chat_message_id,
        receiver=receiver,
        receiver_source=receiver_source,
        action_type=body.action_type,
        original_text=orig_txt,
        edited_text=(body.edited_text or "").strip(),
        claimed_local_sales_wechat_id=claimed,
        status="pending",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    return {
        "code": 200,
        "message": "ok",
        "data": {
            "id": row.id,
            "receiver": receiver,
            "receiver_source": receiver_source,
            "receiver_candidates": candidates,
            "sales_wechat_id": sw,
            "sales_wechat_display": sw_display,
        },
    }


@router.post("/outbound-actions/{action_id}/result")
async def report_outbound_result(
    action_id: int,
    body: schemas.WechatOutboundResultIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(WechatOutboundAction).where(WechatOutboundAction.id == action_id)
    )
    row = res.scalars().first()
    if not row or row.actor_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="记录不存在")

    st = (body.status or "").strip()
    if st not in ("sent", "failed", "blocked"):
        raise HTTPException(status_code=400, detail="status 无效")

    row.status = st
    row.error = (body.error or None)
    row.block_reason = (body.block_reason or None) if st == "blocked" else None
    if body.auto_detected_wxid:
        row.auto_detected_wxid = (body.auto_detected_wxid or "").strip() or None
    row.completed_at = datetime.datetime.now()
    await db.commit()

    return {"code": 200, "message": "ok", "data": {"id": action_id, "status": st}}
