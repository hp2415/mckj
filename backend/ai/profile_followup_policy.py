"""画像「下一步跟进」抑制策略：不参与任务分配的角色不产出结构化跟进建议。"""
from __future__ import annotations

import os
import re
from typing import Any

from sqlalchemy.future import select

from ai.profile_staff_tag import has_staff_profile_tag, staff_profile_tag_names
from models import ProfileTagDefinition

# 打上这些动态标签的客户不产出跟进日期/策略/渠道（可与工作人员重叠）
_DEFAULT_NO_FOLLOWUP_TAG_NAMES = ("工作人员", "不负责", "被删除")

# ai_profile 正文命中则抑制（未打标时的兜底，偏保守）
_AI_PROFILE_NO_FOLLOWUP_PATTERNS = (
    re.compile(r"内部同事"),
    re.compile(r"米城内部"),
    re.compile(r"本公司(同事|员工)"),
    re.compile(r"集团内部"),
    re.compile(r"销售号互加"),
    re.compile(r"送货师傅"),
    re.compile(r"非采购决策人"),
    re.compile(r"不负责.{0,6}采购"),
    re.compile(r"被删"),
)


def no_followup_profile_tag_names() -> frozenset[str]:
    raw = (os.getenv("NO_FOLLOWUP_PROFILE_TAG_NAMES") or "").strip()
    if raw:
        names = [n.strip() for n in raw.split(",") if n.strip()]
    else:
        names = list(_DEFAULT_NO_FOLLOWUP_TAG_NAMES)
    return frozenset(names) | staff_profile_tag_names()


def has_no_followup_profile_tag(tags: list[dict] | None) -> bool:
    names = no_followup_profile_tag_names()
    if not names or not tags:
        return False
    for t in tags:
        if (t.get("name") or "").strip() in names:
            return True
    return False


def _ai_profile_implies_no_followup(ai_profile: str | None) -> str | None:
    from ai.raw_profiling import strip_followup_block

    text = strip_followup_block(ai_profile or "")
    if not text:
        return None
    for pat in _AI_PROFILE_NO_FOLLOWUP_PATTERNS:
        if pat.search(text):
            return f"画像判定：{pat.pattern}"
    return None


def should_suppress_profile_followup(
    *,
    tags: list[dict] | None = None,
    ai_profile: str | None = None,
    raw_customer_id: str | None = None,
    rcsw: Any = None,
    raw: Any = None,
    known_sales_wechat_ids: frozenset[str] | set[str] | None = None,
) -> str | None:
    """
    返回抑制原因；None 表示可正常产出跟进建议。
    与任务分配侧排除口径对齐：工作人员标签、不负责标签、画像跳过身份、内部同事语义。
    """
    if has_staff_profile_tag(tags) or has_no_followup_profile_tag(tags):
        hit = []
        for t in tags or []:
            name = (t.get("name") or "").strip()
            if name in no_followup_profile_tag_names():
                hit.append(name)
        return f"动态标签：{','.join(hit)}" if hit else "动态标签：工作人员/不负责"

    from ai.raw_profiling import (
        CORP_WECHAT_NICKNAME_MARKER,
        is_group_chat_customer,
        nickname_has_corp_marker,
        profile_skip_reason,
    )

    skip = profile_skip_reason(
        raw_customer_id or "",
        rcsw,
        raw=raw,
        known_sales_wechat_ids=known_sales_wechat_ids,
        profile_tags=tags,
    )
    if skip:
        return skip

    ai_hit = _ai_profile_implies_no_followup(ai_profile)
    if ai_hit:
        return ai_hit

    rid = (raw_customer_id or "").strip()
    if rid and is_group_chat_customer(rid):
        return "群聊客户"

    if nickname_has_corp_marker(raw, rcsw):
        return f"昵称含企业标识（{CORP_WECHAT_NICKNAME_MARKER}）"

    return None


def clear_profile_followup_fields(p: dict[str, Any]) -> None:
    """清空结构化跟进字段，并移除 ai_profile 中的【下一步跟进】块。"""
    from ai.raw_profiling import strip_followup_block

    p["suggested_followup_date"] = ""
    p["followup_strategy"] = ""
    p["followup_channel"] = ""
    p["followup_reason"] = ""
    base = strip_followup_block(str(p.get("ai_profile") or ""))
    p["ai_profile"] = base or None


async def tags_for_matched_profile_ids(db, matched_ids: Any) -> list[dict[str, Any]]:
    ids: list[int] = []
    for x in matched_ids or []:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not ids:
        return []
    res = await db.execute(
        select(ProfileTagDefinition)
        .where(ProfileTagDefinition.id.in_(ids))
        .where(ProfileTagDefinition.is_active.is_(True))
    )
    return [
        {
            "id": r.id,
            "name": r.name,
            "feature_note": r.feature_note,
            "strategy_note": r.strategy_note,
        }
        for r in res.scalars().all()
    ]


async def finalize_profile_followup_fields(
    db,
    p: dict[str, Any],
    *,
    raw: Any = None,
    rcsw: Any = None,
) -> str | None:
    """根据标签与身份抑制或规范化跟进字段。返回抑制原因（若有）。"""
    from ai.raw_profiling import load_known_sales_wechat_ids, normalize_profile_followup_fields

    tag_list = await tags_for_matched_profile_ids(db, p.get("matched_profile_tag_ids"))
    known = await load_known_sales_wechat_ids(db)
    rid = str(p.get("raw_id") or (getattr(raw, "id", None) or ""))
    reason = should_suppress_profile_followup(
        tags=tag_list,
        ai_profile=str(p.get("ai_profile") or ""),
        raw_customer_id=rid,
        rcsw=rcsw,
        raw=raw,
        known_sales_wechat_ids=known,
    )
    if reason:
        clear_profile_followup_fields(p)
        p["followup_suppressed_reason"] = reason
        return reason
    normalize_profile_followup_fields(p)
    return None
