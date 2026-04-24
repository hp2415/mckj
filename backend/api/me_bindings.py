"""当前登录用户的销售微信号绑定 CRUD。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import update, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from database import get_db
from api.auth import get_current_user
from models import User, UserSalesWechat
import schemas

router = APIRouter(prefix="/api/me", tags=["Account"])


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


@router.get("/sales-wechats")
async def list_sales_wechats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(UserSalesWechat)
        .where(UserSalesWechat.user_id == current_user.id)
        .order_by(UserSalesWechat.is_primary.desc(), UserSalesWechat.id.asc())
    )
    rows = list(res.scalars().all())
    data = [schemas.SalesWechatBindingOut.model_validate(r).model_dump() for r in rows]
    return {"code": 200, "message": "ok", "data": data}


@router.post("/sales-wechats")
async def add_sales_wechat(
    body: schemas.SalesWechatBindingCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sw = body.sales_wechat_id.strip()
    if not sw:
        raise HTTPException(status_code=400, detail="销售微信号不能为空")

    taken = await db.execute(select(UserSalesWechat).where(UserSalesWechat.sales_wechat_id == sw))
    if taken.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="该销售微信号已被其他账号绑定")

    if body.is_primary:
        await db.execute(
            update(UserSalesWechat)
            .where(UserSalesWechat.user_id == current_user.id)
            .values(is_primary=False)
        )

    row = UserSalesWechat(
        user_id=current_user.id,
        sales_wechat_id=sw,
        label=body.label,
        is_primary=body.is_primary,
    )
    db.add(row)
    await db.flush()

    n_res = await db.execute(
        select(func.count()).select_from(UserSalesWechat).where(UserSalesWechat.user_id == current_user.id)
    )
    if (n_res.scalar_one() or 0) == 1:
        row.is_primary = True

    if row.is_primary:
        await db.execute(
            update(UserSalesWechat)
            .where(UserSalesWechat.user_id == current_user.id)
            .where(UserSalesWechat.id != row.id)
            .values(is_primary=False)
        )

    await sync_user_legacy_wechat_column(db, current_user)
    await db.commit()
    await db.refresh(row)
    return {
        "code": 200,
        "message": "ok",
        "data": schemas.SalesWechatBindingOut.model_validate(row).model_dump(),
    }


@router.delete("/sales-wechats/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sales_wechat(
    binding_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(UserSalesWechat).where(
            UserSalesWechat.id == binding_id,
            UserSalesWechat.user_id == current_user.id,
        )
    )
    row = res.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="绑定不存在")

    was_primary = row.is_primary
    await db.delete(row)
    await db.flush()

    if was_primary:
        res2 = await db.execute(
            select(UserSalesWechat)
            .where(UserSalesWechat.user_id == current_user.id)
            .order_by(UserSalesWechat.id.asc())
            .limit(1)
        )
        first = res2.scalars().first()
        if first:
            first.is_primary = True

    await sync_user_legacy_wechat_column(db, current_user)
    await db.commit()


@router.post("/sales-wechats/{binding_id}/set-primary")
async def set_primary_sales_wechat(
    binding_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(UserSalesWechat).where(
            UserSalesWechat.id == binding_id,
            UserSalesWechat.user_id == current_user.id,
        )
    )
    row = res.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="绑定不存在")

    await db.execute(
        update(UserSalesWechat)
        .where(UserSalesWechat.user_id == current_user.id)
        .values(is_primary=False)
    )
    row.is_primary = True
    await sync_user_legacy_wechat_column(db, current_user)
    await db.commit()
    await db.refresh(row)
    return {
        "code": 200,
        "message": "ok",
        "data": schemas.SalesWechatBindingOut.model_validate(row).model_dump(),
    }
