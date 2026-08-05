from __future__ import annotations

from sqlalchemy import select

from models import SalesCustomerProfile


async def append_proposal_note(
    db,
    *,
    user_id: int,
    raw_customer_id: str,
    sales_wechat_id: str | None,
    note: str,
) -> bool:
    """把方案摘要简略追加到客户画像文本（本地跟进口径；不依赖 MiBuddy lead_id）。"""
    rid = (raw_customer_id or "").strip()
    if not rid or not (note or "").strip():
        return False

    stmt = select(SalesCustomerProfile).where(SalesCustomerProfile.raw_customer_id == rid)
    sw = (sales_wechat_id or "").strip()
    if sw:
        stmt = stmt.where(SalesCustomerProfile.sales_wechat_id == sw)
    else:
        stmt = stmt.where(SalesCustomerProfile.user_id == user_id).where(
            SalesCustomerProfile.sales_wechat_id.is_(None)
        )
    profile = (await db.execute(stmt.limit(1))).scalars().first()
    if profile is None and sw:
        # 兜底：同客户下该用户任意画像行
        profile = (
            await db.execute(
                select(SalesCustomerProfile)
                .where(SalesCustomerProfile.raw_customer_id == rid)
                .where(SalesCustomerProfile.user_id == user_id)
                .limit(1)
            )
        ).scalars().first()
    if profile is None:
        return False

    existing = (profile.ai_profile or "").rstrip()
    block = note.strip()
    if block in existing:
        return True
    profile.ai_profile = f"{existing}\n\n{block}".strip() if existing else block
    return True
