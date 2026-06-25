"""销售微信号绑定：users.wechat_id 与 user_sales_wechats 同步（API / 管理后台共用）。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from models import User, UserSalesWechat, SalesWechatAccount


async def sync_user_legacy_wechat_column(db: AsyncSession, user: User) -> None:
    """将 users.wechat_id 与主绑定对齐，兼容旧代码路径。"""
    stmt = (
        select(UserSalesWechat)
        .where(UserSalesWechat.user_id == user.id)
        .where(UserSalesWechat.is_primary == True)  # noqa: E712
    )
    res = await db.execute(stmt)
    prim = res.scalars().first()
    if not prim:
        stmt2 = (
            select(UserSalesWechat)
            .where(UserSalesWechat.user_id == user.id)
            .order_by(UserSalesWechat.id.asc())
            .limit(1)
        )
        res2 = await db.execute(stmt2)
        prim = res2.scalars().first()
    user.wechat_id = prim.sales_wechat_id if prim else None


async def sync_user_legacy_wechat_column_by_id(db: AsyncSession, user_id: int) -> None:
    res = await db.execute(select(User).where(User.id == user_id))
    user = res.scalars().first()
    if user:
        await sync_user_legacy_wechat_column(db, user)


async def ensure_single_primary_binding(
    db: AsyncSession, user_id: int, *, keep_binding_id: int | None = None
) -> None:
    """同一用户仅保留一条主绑定；若未指定 keep_binding_id 则保留最早一条。"""
    res = await db.execute(
        select(UserSalesWechat)
        .where(UserSalesWechat.user_id == user_id)
        .order_by(UserSalesWechat.is_primary.desc(), UserSalesWechat.id.asc())
    )
    rows = list(res.scalars().all())
    if not rows:
        return
    primary_id = keep_binding_id
    if primary_id is None:
        primary_id = rows[0].id
    for row in rows:
        row.is_primary = row.id == primary_id


async def clear_orphan_users_wechat_id(db: AsyncSession, sales_wechat_id: str) -> None:
    """
    清理 users.wechat_id 残留：该 wxid 已不在对应用户的主绑定/绑定表中，或归属已变更。
    """
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return
    bind_res = await db.execute(
        select(UserSalesWechat.user_id).where(UserSalesWechat.sales_wechat_id == sw).limit(1)
    )
    owner_id = bind_res.scalar_one_or_none()
    stale_res = await db.execute(select(User).where(User.wechat_id == sw))
    for user in stale_res.scalars().all():
        if owner_id is None or user.id != owner_id:
            user.wechat_id = None


async def reconcile_binding_side_effects(
    db: AsyncSession,
    *,
    sales_wechat_id: str,
    affected_user_ids: list[int] | set[int],
) -> None:
    """
    绑定增删改后统一收尾：
    1) 清理非归属者的 users.wechat_id 残留；
    2) 同步相关用户的 users.wechat_id 与主绑定一致。
    """
    sw = (sales_wechat_id or "").strip()
    if sw:
        await clear_orphan_users_wechat_id(db, sw)
    for uid in {int(x) for x in affected_user_ids if x}:
        await sync_user_legacy_wechat_column_by_id(db, uid)


async def validate_sales_wechat_binding(
    db: AsyncSession,
    *,
    sales_wechat_id: str,
    user_id: int,
    binding_id: int | None = None,
) -> None:
    """校验绑定是否可保存；失败时抛出 ValueError（管理后台展示）。"""
    sw = (sales_wechat_id or "").strip()
    if not sw:
        raise ValueError("销售微信号不能为空")
    if not user_id:
        raise ValueError("请选择登录账号")

    acc_res = await db.execute(
        select(SalesWechatAccount.sales_wechat_id)
        .where(SalesWechatAccount.sales_wechat_id == sw)
        .limit(1)
    )
    if not (acc_res.scalar_one_or_none() or "").strip():
        raise ValueError(
            f"未在「销售微信主数据」中找到 {sw}，请先在主数据同步/录入后再绑定"
        )

    taken_stmt = select(UserSalesWechat).where(UserSalesWechat.sales_wechat_id == sw)
    if binding_id:
        taken_stmt = taken_stmt.where(UserSalesWechat.id != binding_id)
    taken_res = await db.execute(taken_stmt)
    if taken_res.scalar_one_or_none():
        raise ValueError(f"销售微信号 {sw} 已被其他账号绑定")


def resolve_user_id_from_admin_data(data: dict, model: UserSalesWechat | None = None) -> int | None:
    """解析 sqladmin 表单中的用户 ID（支持 user / user_id 两种字段名）。"""
    if data.get("user_id"):
        return int(data["user_id"])
    if "user" in data and data["user"] is not None:
        u = data["user"]
        if hasattr(u, "id"):
            return int(u.id)
        return int(u)
    if model is not None and model.user_id:
        return int(model.user_id)
    return None
