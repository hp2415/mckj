"""桌面端「一键发微信」审计 API。"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
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
    解析微信搜索框可用的 receiver 候选列表（按备注 → 昵称 → 微信号 → 手机顺序）。
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
    if rid.startswith("wxid_"):
        candidates.append({"keyword": rid, "source": "wxid"})
    phone = (rcsw.phone or "").strip()
    if not phone and rc:
        phone = (rc.phone_normalized or rc.phone or "").strip()
    if phone:
        candidates.append({"keyword": phone, "source": "phone"})

    candidates = _dedupe_receiver_candidates(candidates)
    if not candidates:
        return [], "receiver_unresolved"
    return candidates, None


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
            detail="无法解析微信搜索用的联系人（缺少备注/昵称/手机等），请完善云客好友数据。",
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
