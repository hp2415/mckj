"""回访提醒（当日 + 往日逾期）：由画像 callback_at 动态汇总，不经任务分配批次。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_
from sqlalchemy.future import select

from ai.profile_followup_policy import (
    has_no_followup_profile_tag,
    should_suppress_profile_followup,
)
from ai.profile_staff_tag import has_staff_profile_tag
from ai.raw_profiling import (
    extract_callback_from_ai_profile,
    rcsw_active_for_profile_where,
)
from crud import profile_tags_by_relation_ids
from models import RawCustomer, RawCustomerSalesWechat, SalesCustomerProfile

_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _shanghai_now() -> datetime:
    return datetime.now(_SHANGHAI_TZ).replace(tzinfo=None)


def _shanghai_today():
    return _shanghai_now().date()


def _display_name(rc: RawCustomer | None, scp: SalesCustomerProfile | None) -> str:
    for src in (rc, scp):
        if src is None:
            continue
        for key in ("customer_name", "wechat_remark", "unit_name"):
            val = str(getattr(src, key, None) or "").strip()
            if val:
                return val
    return "客户"


# 往日未处理回访回溯天数（避免极旧数据淹没列表）
_PAST_DAY_LOOKBACK_DAYS = 30


def _callback_dict(
    *,
    scp: SalesCustomerProfile,
    rc: RawCustomer,
    now: datetime,
    today,
) -> dict[str, Any]:
    phone_raw = (rc.phone or "").strip() or None
    phone_norm = (rc.phone_normalized or "").strip() or None
    phone_display = phone_norm or phone_raw
    note_meta = extract_callback_from_ai_profile(scp.ai_profile)
    note = str(note_meta.get("callback_note") or "").strip()
    cb_at = scp.callback_at
    past_day = bool(cb_at and cb_at.date() < today)
    overdue = bool(cb_at and now >= cb_at)
    return {
        "scp_id": int(scp.id),
        "raw_customer_id": str(scp.raw_customer_id or ""),
        "sales_wechat_id": str(scp.sales_wechat_id or ""),
        "customer_name": _display_name(rc, scp),
        "unit_name": (rc.unit_name or "").strip() or None,
        "wechat_remark": (scp.wechat_remark or "").strip() or None,
        "phone": phone_display,
        "phone_raw": phone_raw,
        "phone_normalized": phone_norm,
        "callback_at": cb_at,
        "callback_note": note or None,
        "ai_profile": scp.ai_profile,
        "overdue": overdue,
        "past_day": past_day,
    }


async def query_active_callbacks(
    db,
    *,
    sales_wechat_id: str,
) -> list[dict[str, Any]]:
    """
    返回未处理的回访提醒（当日 + 近 N 日往日逾期），按 callback_at 升序。
    条件：callback_at 非空且落在 [today-N, tomorrow)、callback_done_at 为空、好友仍有效。
    """
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return []
    today = _shanghai_today()
    now = _shanghai_now()
    day_start = datetime(today.year, today.month, today.day)
    day_end = day_start + timedelta(days=1)
    lookback_start = day_start - timedelta(days=_PAST_DAY_LOOKBACK_DAYS)

    stmt = (
        select(SalesCustomerProfile, RawCustomer, RawCustomerSalesWechat)
        .join(RawCustomer, RawCustomer.id == SalesCustomerProfile.raw_customer_id)
        .join(
            RawCustomerSalesWechat,
            and_(
                RawCustomerSalesWechat.raw_customer_id == SalesCustomerProfile.raw_customer_id,
                RawCustomerSalesWechat.sales_wechat_id == SalesCustomerProfile.sales_wechat_id,
            ),
        )
        .where(SalesCustomerProfile.sales_wechat_id == sw)
        .where(SalesCustomerProfile.profile_status == 1)
        .where(SalesCustomerProfile.callback_at.isnot(None))
        .where(SalesCustomerProfile.callback_at >= lookback_start)
        .where(SalesCustomerProfile.callback_at < day_end)
        .where(SalesCustomerProfile.callback_done_at.is_(None))
        .where(rcsw_active_for_profile_where())
        .order_by(SalesCustomerProfile.callback_at.asc())
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return []

    scp_ids = [int(scp.id) for scp, _rc, _rcsw in rows]
    tags_by_scp = await profile_tags_by_relation_ids(db, scp_ids)

    items: list[dict[str, Any]] = []
    for scp, rc, rcsw in rows:
        tags = tags_by_scp.get(int(scp.id)) or []
        if has_staff_profile_tag(tags) or has_no_followup_profile_tag(tags):
            continue
        # 必须传入 rcsw：否则 profile_skip_reason 会误判「无销售好友关系」
        suppress = should_suppress_profile_followup(
            tags=tags,
            ai_profile=scp.ai_profile,
            raw_customer_id=str(scp.raw_customer_id or ""),
            rcsw=rcsw,
            raw=rc,
        )
        if suppress:
            continue
        items.append(_callback_dict(scp=scp, rc=rc, now=now, today=today))
    return items


async def mark_callback_done(
    db,
    *,
    scp_id: int,
    sales_wechat_id: str,
) -> SalesCustomerProfile | None:
    """标记回访已处理；校验销售号归属。"""
    sw = (sales_wechat_id or "").strip()
    res = await db.execute(
        select(SalesCustomerProfile).where(
            SalesCustomerProfile.id == int(scp_id),
            SalesCustomerProfile.sales_wechat_id == sw,
        )
    )
    scp = res.scalar_one_or_none()
    if scp is None:
        return None
    scp.callback_done_at = _shanghai_now()
    await db.flush()
    return scp
