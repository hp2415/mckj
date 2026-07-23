"""订单/聊天读路径可见性：按角色与销售号 alias 隔离。

重要：raw_orders.wechat_idx 对应 sales_wechat_accounts.alias_name（不是 sales_wechat_id）。
空 wechat_idx = 未归属，所有人可见；非空则仅归属销售（或其 alias）及 old_customer/admin 可见。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import RawOrder, SalesWechatAccount, User, UserSalesWechat

ROLE_ADMIN = "admin"
ROLE_STAFF = "staff"
ROLE_OLD_CUSTOMER = "old_customer"

ROLES_VIEW_ALL_ORDERS = frozenset({ROLE_ADMIN, ROLE_OLD_CUSTOMER})
ROLES_READ_OTHERS_CHAT_SUMMARY = frozenset({ROLE_ADMIN, ROLE_OLD_CUSTOMER})


def normalize_role(role: Any) -> str:
    return str(role or ROLE_STAFF).strip().lower() or ROLE_STAFF


def can_view_all_orders(role: Any) -> bool:
    return normalize_role(role) in ROLES_VIEW_ALL_ORDERS


def can_read_others_chat_summary(role: Any) -> bool:
    return normalize_role(role) in ROLES_READ_OTHERS_CHAT_SUMMARY


def normalize_wechat_idx(value: Any) -> str:
    """订单 wechat_idx / alias 归一化；空串表示未归属。"""
    return str(value or "").strip()


@dataclass(frozen=True)
class OrderViewerContext:
    """读订单时的可见性上下文。"""

    view_all: bool
    allowed_aliases: frozenset[str]
    role: str = ROLE_STAFF
    sales_wechat_id: Optional[str] = None

    @property
    def include_others_chat_summary(self) -> bool:
        return can_read_others_chat_summary(self.role)


def order_visibility_clause(
    *,
    view_all: bool,
    allowed_aliases: Sequence[str] | None = None,
):
    """
    附加到订单查询的可见性条件。
    view_all=True 时返回 None（不加过滤）。
    staff：wechat_idx 空（未归属）或落入 allowed_aliases（= 绑定号的 alias_name）。
    """
    if view_all:
        return None
    aliases = sorted(
        {normalize_wechat_idx(a) for a in (allowed_aliases or []) if normalize_wechat_idx(a)}
    )
    parts = [
        RawOrder.wechat_idx.is_(None),
        RawOrder.wechat_idx == "",
    ]
    if aliases:
        parts.append(RawOrder.wechat_idx.in_(aliases))
    return or_(*parts)


def order_visible_in_memory(
    wechat_idx: Any,
    *,
    view_all: bool,
    allowed_aliases: Sequence[str] | frozenset[str] | None = None,
) -> bool:
    if view_all:
        return True
    key = normalize_wechat_idx(wechat_idx)
    if not key:
        return True
    allowed = {normalize_wechat_idx(a) for a in (allowed_aliases or []) if normalize_wechat_idx(a)}
    return key in allowed


async def alias_names_for_sales_wechat_ids(
    db: AsyncSession,
    sales_wechat_ids: Sequence[str],
) -> list[str]:
    ids = sorted({str(s).strip() for s in sales_wechat_ids if s and str(s).strip()})
    if not ids:
        return []
    res = await db.execute(
        select(SalesWechatAccount.alias_name).where(
            SalesWechatAccount.sales_wechat_id.in_(ids)
        )
    )
    out: list[str] = []
    seen: set[str] = set()
    for (alias,) in res.all():
        a = normalize_wechat_idx(alias)
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


async def sales_wechat_id_for_order_wechat_idx(
    db: AsyncSession,
    wechat_idx: Any,
) -> Optional[str]:
    """
    将订单 wechat_idx（= alias_name）解析为 sales_wechat_id。
    找不到则返回 None。
    """
    alias = normalize_wechat_idx(wechat_idx)
    if not alias:
        return None
    res = await db.execute(
        select(SalesWechatAccount.sales_wechat_id)
        .where(SalesWechatAccount.alias_name == alias)
        .limit(1)
    )
    row = res.first()
    if not row:
        return None
    sw = str(row[0] or "").strip()
    return sw or None


async def bound_sales_wechat_ids_for_user_id(
    db: AsyncSession,
    user_id: int,
) -> list[str]:
    res = await db.execute(
        select(UserSalesWechat.sales_wechat_id).where(UserSalesWechat.user_id == user_id)
    )
    return [str(s).strip() for s in res.scalars().all() if s and str(s).strip()]


async def resolve_order_viewer_for_user(
    db: AsyncSession,
    user: User,
) -> OrderViewerContext:
    role = normalize_role(getattr(user, "role", None))
    if can_view_all_orders(role):
        return OrderViewerContext(
            view_all=True,
            allowed_aliases=frozenset(),
            role=role,
        )
    bound = await bound_sales_wechat_ids_for_user_id(db, int(user.id))
    aliases = await alias_names_for_sales_wechat_ids(db, bound)
    return OrderViewerContext(
        view_all=False,
        allowed_aliases=frozenset(aliases),
        role=role,
    )


async def resolve_order_viewer_for_sales_wechat(
    db: AsyncSession,
    sales_wechat_id: str | None,
) -> OrderViewerContext:
    """
    画像任务等无登录用户场景：按销售号归属用户的角色决定可见性。
    staff → 仅该号 alias + 未归属；old_customer/admin → 全量。
    """
    sw = str(sales_wechat_id or "").strip()
    if not sw:
        return OrderViewerContext(
            view_all=False,
            allowed_aliases=frozenset(),
            role=ROLE_STAFF,
        )

    # 归属用户角色（一号一人；若多条取第一个）
    role = ROLE_STAFF
    owner_res = await db.execute(
        select(User.role)
        .join(UserSalesWechat, UserSalesWechat.user_id == User.id)
        .where(UserSalesWechat.sales_wechat_id == sw)
        .limit(1)
    )
    owner_role = owner_res.scalar_one_or_none()
    if owner_role is not None:
        role = normalize_role(owner_role)

    if can_view_all_orders(role):
        return OrderViewerContext(
            view_all=True,
            allowed_aliases=frozenset(),
            role=role,
            sales_wechat_id=sw,
        )

    aliases = await alias_names_for_sales_wechat_ids(db, [sw])
    return OrderViewerContext(
        view_all=False,
        allowed_aliases=frozenset(aliases),
        role=role,
        sales_wechat_id=sw,
    )
