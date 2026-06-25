"""电话外呼转写：客户电话匹配 + 画像投喂。"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import and_, func, or_, select

from models import PhoneCallRecord, RawCustomer, RawCustomerSalesWechat

PHONE_STATUS_SUCCESS = "success"


def digits_phone(value: str | None) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _callee_digits_expr(callee_col):
    return func.regexp_replace(callee_col, "[^0-9]", "")


def _phone_digits_expr(phone_col):
    return func.regexp_replace(func.coalesce(phone_col, ""), "[^0-9]", "")


def phone_match_or_clauses(callee_col, *phone_cols):
    """SQL：外呼 callee 与客户电话列匹配（精确或仅数字一致）。"""
    clauses: list[Any] = []
    for col in phone_cols:
        if col is None:
            continue
        clauses.append(callee_col == col)
        cd = _callee_digits_expr(callee_col)
        pd = _phone_digits_expr(col)
        clauses.append(and_(func.length(cd) >= 7, cd == pd))
    return or_(*clauses) if clauses else None


def phone_match_clauses_for_phones(callee_col, phones: list[str]) -> list[Any]:
    """按客户电话列表生成 callee 匹配子句。"""
    clauses: list[Any] = []
    for p in phones:
        s = (p or "").strip()
        if not s:
            continue
        clauses.append(callee_col == s)
        d = digits_phone(s)
        if len(d) >= 7:
            clauses.append(_callee_digits_expr(callee_col) == d)
    return clauses


async def resolve_customer_phones(
    db,
    raw_customer_id: str,
    sales_wechat_id: str,
) -> list[str]:
    """解析客户在该销售号下可用于匹配电话明细的号码。"""
    rid = (raw_customer_id or "").strip()
    sw = (sales_wechat_id or "").strip()
    if not rid:
        return []

    rc = (
        await db.execute(select(RawCustomer).where(RawCustomer.id == rid))
    ).scalar_one_or_none()
    phones: list[str] = []
    if rc:
        for p in (rc.phone_normalized, rc.phone):
            s = (p or "").strip()
            if s and s not in phones:
                phones.append(s)

    if sw:
        rcsw = (
            await db.execute(
                select(RawCustomerSalesWechat.phone).where(
                    RawCustomerSalesWechat.raw_customer_id == rid,
                    RawCustomerSalesWechat.sales_wechat_id == sw,
                )
            )
        ).scalar_one_or_none()
        p = (rcsw or "").strip() if rcsw else ""
        if p and p not in phones:
            phones.append(p)
    return phones


async def load_phone_transcripts_for_profile(
    db,
    sales_wechat_id: str,
    raw_customer_id: str,
    *,
    max_calls: int = 5,
    max_chars: int = 6000,
) -> str:
    """加载已成功转写的电话外呼原文，供画像投喂（按客户电话匹配 callee）。"""
    sw = (sales_wechat_id or "").strip()
    rid = (raw_customer_id or "").strip()
    if not sw or not rid:
        return ""

    phones = await resolve_customer_phones(db, rid, sw)
    if not phones:
        return ""

    match_clauses = phone_match_clauses_for_phones(PhoneCallRecord.callee, phones)
    if not match_clauses:
        return ""

    stmt = (
        select(PhoneCallRecord)
        .where(PhoneCallRecord.user_wechat_account == sw)
        .where(PhoneCallRecord.status_text == PHONE_STATUS_SUCCESS)
        .where(PhoneCallRecord.transcript_text.isnot(None))
        .where(PhoneCallRecord.transcript_text != "")
        .where(or_(*match_clauses))
        .order_by(PhoneCallRecord.create_time.desc())
        .limit(max(1, int(max_calls)) * 3)
    )
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return ""

    blocks: list[str] = []
    used = 0
    for row in rows[: max(1, int(max_calls))]:
        txt = (row.transcript_text or "").strip()
        if not txt:
            continue
        when = row.create_time.strftime("%Y-%m-%d %H:%M") if row.create_time else "未知时间"
        callee = (row.callee or "").strip() or "—"
        header = f"--- 电话外呼 {when}（客户电话 {callee}）---"
        chunk = f"{header}\n{txt}"
        if used + len(chunk) > max_chars and blocks:
            break
        if len(chunk) > max_chars:
            chunk = chunk[: max_chars - 20] + "\n…(截断)"
        blocks.append(chunk)
        used += len(chunk)
        if used >= max_chars:
            break

    return "\n\n".join(blocks)
