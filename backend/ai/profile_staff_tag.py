"""
客户动态标签「工作人员」：送货师傅等内部联系人，不参与 LLM 画像与任务分配。
支持桌面手动打标与画像 LLM 写入 matched_profile_tag_ids 后的持久化标签。
"""
from __future__ import annotations

import os
from typing import Iterable

from sqlalchemy import and_, exists, tuple_
from sqlalchemy.future import select

from crud import profile_tags_for_relation
from models import ProfileTagDefinition, SalesCustomerProfile, scp_profile_tags

# 管理平台「客户动态标签」中的名称；可用环境变量逗号扩展（如 STAFF_PROFILE_TAG_NAMES=工作人员,内部员工）
_DEFAULT_STAFF_TAG_NAMES = ("工作人员",)


def staff_profile_tag_names() -> frozenset[str]:
    raw = (os.getenv("STAFF_PROFILE_TAG_NAMES") or "").strip()
    names = [n.strip() for n in raw.split(",") if n.strip()] if raw else list(_DEFAULT_STAFF_TAG_NAMES)
    return frozenset(names)


STAFF_PROFILE_SKIP_REASON = "已标记工作人员（跳过画像与任务分配）"


def has_staff_profile_tag(tags: list[dict] | None) -> bool:
    """tags 为 crud.profile_tags_by_relation_ids 返回的单条列表。"""
    names = staff_profile_tag_names()
    if not names or not tags:
        return False
    for t in tags:
        if (t.get("name") or "").strip() in names:
            return True
    return False


def staff_tag_skip_reason(tags: list[dict] | None) -> str | None:
    return STAFF_PROFILE_SKIP_REASON if has_staff_profile_tag(tags) else None


async def load_staff_profile_tag_ids(db) -> frozenset[int]:
    """按标签名解析工作人员类定义 id（仅启用标签）。"""
    names = staff_profile_tag_names()
    if not names:
        return frozenset()
    res = await db.execute(
        select(ProfileTagDefinition.id).where(
            ProfileTagDefinition.is_active.is_(True),
            ProfileTagDefinition.name.in_(tuple(names)),
        )
    )
    return frozenset(int(x) for x in res.scalars().all() if x is not None)


def scp_without_staff_tag_clause(staff_tag_ids: frozenset[int]):
    """
    SQL：当前 outerjoin 的 SalesCustomerProfile 行未绑定工作人员类标签。
    无 SCP 行（id IS NULL）时视为通过。
    """
    if not staff_tag_ids:
        return True
    return ~exists(
        select(1)
        .select_from(scp_profile_tags)
        .where(
            scp_profile_tags.c.sales_customer_profile_id == SalesCustomerProfile.id,
            scp_profile_tags.c.profile_tag_id.in_(tuple(staff_tag_ids)),
        )
    )


async def load_staff_tagged_pair_keys(
    db,
    pair_keys: Iterable[tuple[str, str]],
) -> frozenset[tuple[str, str]]:
    """批量查询已打工作人员标签的 (raw_customer_id, sales_wechat_id)。"""
    keys = [
        ((rid or "").strip(), (sw or "").strip())
        for rid, sw in pair_keys
        if (rid or "").strip() and (sw or "").strip()
    ]
    if not keys:
        return frozenset()
    staff_ids = await load_staff_profile_tag_ids(db)
    if not staff_ids:
        return frozenset()
    out: set[tuple[str, str]] = set()
    chunk = 400
    for i in range(0, len(keys), chunk):
        part = keys[i : i + chunk]
        res = await db.execute(
            select(
                SalesCustomerProfile.raw_customer_id,
                SalesCustomerProfile.sales_wechat_id,
            )
            .join(
                scp_profile_tags,
                scp_profile_tags.c.sales_customer_profile_id == SalesCustomerProfile.id,
            )
            .where(
                tuple_(
                    SalesCustomerProfile.raw_customer_id,
                    SalesCustomerProfile.sales_wechat_id,
                ).in_(part),
                scp_profile_tags.c.profile_tag_id.in_(tuple(staff_ids)),
            )
            .distinct()
        )
        for rid, sw in res.all():
            if rid and sw:
                out.add((str(rid).strip(), str(sw).strip()))
    return frozenset(out)


async def profile_tags_for_sales_pair(
    db,
    raw_customer_id: str,
    sales_wechat_id: str,
) -> list[dict]:
    rid = (raw_customer_id or "").strip()
    sw = (sales_wechat_id or "").strip()
    if not rid or not sw:
        return []
    res = await db.execute(
        select(SalesCustomerProfile.id)
        .where(
            SalesCustomerProfile.raw_customer_id == rid,
            SalesCustomerProfile.sales_wechat_id == sw,
        )
        .limit(1)
    )
    scp_id = res.scalar_one_or_none()
    if not scp_id:
        return []
    return await profile_tags_for_relation(db, int(scp_id))


async def profile_skip_reason_for_sales_pair(
    db,
    raw_customer_id: str,
    rcsw,
    *,
    raw=None,
    known_sales_wechat_ids: frozenset[str] | set[str] | None = None,
) -> str | None:
    """合并关系级跳过原因与工作人员动态标签（需 DB 查 SCP 标签）。"""
    from ai.raw_profiling import profile_skip_reason

    rid = (raw_customer_id or "").strip()
    sw = (getattr(rcsw, "sales_wechat_id", None) or "").strip() if rcsw else ""
    base = profile_skip_reason(
        rid,
        rcsw,
        raw=raw,
        known_sales_wechat_ids=known_sales_wechat_ids,
    )
    if base:
        return base
    if not rid or not sw:
        return None
    tags = await profile_tags_for_sales_pair(db, rid, sw)
    return staff_tag_skip_reason(tags)
