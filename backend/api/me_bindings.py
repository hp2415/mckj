"""当前登录用户的销售微信号绑定 CRUD。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import update, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from database import get_db
from api.auth import get_current_user
from models import User, UserSalesWechat, SalesWechatAccount
import schemas

router = APIRouter(prefix="/api/me", tags=["Account"])

async def resolve_sales_wechat_id_from_input(db: AsyncSession, raw: str) -> str:
    """
    兼容桌面端输入：
    - 允许输入 alias_name（别名/备注）
    - 允许输入 sales_wechat_id（wxid_...）
    统一解析成 sales_wechat_id 落库，保持历史关联不变。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        res = await db.execute(
            select(SalesWechatAccount.sales_wechat_id).where(SalesWechatAccount.alias_name == s).limit(1)
        )
        sid = (res.scalar_one_or_none() or "").strip()
        return sid or s
    except Exception:
        return s


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
    # 附带 alias_name，便于桌面端显示（仍以 wxid 落库/关联）
    sw_ids = {(r.sales_wechat_id or "").strip() for r in rows if (r.sales_wechat_id or "").strip()}
    alias_by_sid: dict[str, str] = {}
    if sw_ids:
        a_res = await db.execute(
            select(SalesWechatAccount.sales_wechat_id, SalesWechatAccount.alias_name).where(
                SalesWechatAccount.sales_wechat_id.in_(sw_ids)
            )
        )
        for sid, als in a_res.all():
            sid = (sid or "").strip()
            als = (als or "").strip()
            if sid and als:
                alias_by_sid[sid] = als

    data = []
    for r in rows:
        d = schemas.SalesWechatBindingOut.model_validate(r).model_dump()
        sid = (r.sales_wechat_id or "").strip()
        if sid:
            d["alias_name"] = alias_by_sid.get(sid) or None
        data.append(d)
    return {"code": 200, "message": "ok", "data": data}


@router.post("/sales-wechats")
async def add_sales_wechat(
    body: schemas.SalesWechatBindingCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sw_in = (body.sales_wechat_id or "").strip()
    if not sw_in:
        raise HTTPException(status_code=400, detail="销售微信号不能为空")

    # 仅外层映射：输入 alias → 解析成 sales_wechat_id（wxid）再落库
    sw = await resolve_sales_wechat_id_from_input(db, sw_in)
    if not sw:
        raise HTTPException(status_code=400, detail="无法解析该别名对应的销售微信号")

    # 幂等：若已被当前用户绑定，直接返回成功（避免桌面端 auto-bind 后手工重复添加提示“被其他账号绑定”）
    exist_res = await db.execute(
        select(UserSalesWechat).where(
            UserSalesWechat.sales_wechat_id == sw,
            UserSalesWechat.user_id == current_user.id,
        )
    )
    existed = exist_res.scalars().first()
    if existed:
        if body.is_primary and not existed.is_primary:
            await db.execute(
                update(UserSalesWechat)
                .where(UserSalesWechat.user_id == current_user.id)
                .values(is_primary=False)
            )
            existed.is_primary = True
            await sync_user_legacy_wechat_column(db, current_user)
            await db.commit()
            await db.refresh(existed)
        return {
            "code": 200,
            "message": "ok",
            "data": schemas.SalesWechatBindingOut.model_validate(existed).model_dump(),
        }

    taken = await db.execute(select(UserSalesWechat).where(UserSalesWechat.sales_wechat_id == sw))
    taken_row = taken.scalar_one_or_none()
    if taken_row:
        raise HTTPException(status_code=400, detail="该业务微信标识已被其他账号绑定")

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


@router.post("/sales-wechats/auto-bind")
async def auto_bind_sales_wechats_for_me(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    自动绑定当前用户名下的业务微信号。

    绑定来源：sales_wechat_accounts.account_code == users.username
    - 幂等：已绑定的不重复插入
    - 若用户当前没有任何绑定，则把其中第一条设为主号，并同步 users.wechat_id
    """
    # 找到“应该属于当前用户”的 sales_wechat_id（wxid）
    res = await db.execute(
        select(SalesWechatAccount.sales_wechat_id).where(
            SalesWechatAccount.account_code == current_user.username
        )
    )
    sw_ids: list[str] = []
    for (sid,) in res.all():
        sid = (sid or "").strip()
        if sid and sid not in sw_ids:
            sw_ids.append(sid)
    if not sw_ids:
        return {"code": 200, "message": "ok", "data": {"created": 0, "total": 0}}

    # 当前用户已有绑定
    cur_res = await db.execute(
        select(UserSalesWechat.sales_wechat_id)
        .where(UserSalesWechat.user_id == current_user.id)
    )
    existing = {(r[0] or "").strip() for r in cur_res.all() if (r[0] or "").strip()}

    created = 0
    for sid in sw_ids:
        if sid in existing:
            continue
        # 若被别的用户占用，跳过（保持与手工接口一致的唯一约束语义）
        taken = await db.execute(select(UserSalesWechat).where(UserSalesWechat.sales_wechat_id == sid))
        if taken.scalar_one_or_none():
            continue
        row = UserSalesWechat(user_id=current_user.id, sales_wechat_id=sid, is_primary=False)
        db.add(row)
        created += 1

    # 如当前用户没有任何绑定（existing 为空且新增成功），设第一条为主号
    if not existing:
        prim_sid = sw_ids[0]
        await db.execute(
            update(UserSalesWechat)
            .where(UserSalesWechat.user_id == current_user.id)
            .values(is_primary=False)
        )
        # 把 prim_sid 那条设为 primary（可能是刚插入，也可能原来就有）
        await db.execute(
            update(UserSalesWechat)
            .where(UserSalesWechat.user_id == current_user.id)
            .where(UserSalesWechat.sales_wechat_id == prim_sid)
            .values(is_primary=True)
        )
        await sync_user_legacy_wechat_column(db, current_user)

    await db.commit()
    return {"code": 200, "message": "ok", "data": {"created": created, "total": len(sw_ids)}}


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
