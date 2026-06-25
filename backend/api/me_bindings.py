"""当前登录用户的销售微信号绑定 CRUD。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import update, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from database import get_db
from api.auth import get_current_user
from models import User, UserSalesWechat, SalesWechatAccount
from core.sales_wechat_bindings import reconcile_binding_side_effects
from core.mibuddy_client import (
    MibuddyApiError,
    MibuddyConfigError,
    build_update_info_from_form,
    fetch_my_leads,
    fetch_my_leads_album,
    add_remark_to_leads,
    approve_tel,
    ignore_my_lead,
    call_changhu,
    call_yunke,
    fetch_my_leads_remarks,
    fetch_uuid_user_info,
    map_album_lead_item_for_desktop,
    map_lead_item_for_desktop,
    map_remark_item_for_desktop,
    parse_changhu_phones,
    update_my_lead_info,
)
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

    # 强校验：必须存在于销售微信主数据表，否则不允许绑定（避免“显示成功但实际无法关联”）
    acc_res = await db.execute(
        select(SalesWechatAccount.sales_wechat_id).where(SalesWechatAccount.sales_wechat_id == sw).limit(1)
    )
    if not (acc_res.scalar_one_or_none() or "").strip():
        raise HTTPException(
            status_code=400,
            detail="未在「销售微信号」主数据中找到该标识，请先同步/录入后再绑定",
        )

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
            await reconcile_binding_side_effects(
                db, sales_wechat_id=sw, affected_user_ids=[current_user.id]
            )
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

    await reconcile_binding_side_effects(
        db, sales_wechat_id=sw, affected_user_ids=[current_user.id]
    )
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
        await reconcile_binding_side_effects(
            db, sales_wechat_id=prim_sid, affected_user_ids=[current_user.id]
        )

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
    sw = (row.sales_wechat_id or "").strip()
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

    await reconcile_binding_side_effects(
        db, sales_wechat_id=sw, affected_user_ids=[current_user.id]
    )
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
    sw = (row.sales_wechat_id or "").strip()
    await reconcile_binding_side_effects(
        db, sales_wechat_id=sw, affected_user_ids=[current_user.id]
    )
    await db.commit()
    await db.refresh(row)
    return {
        "code": 200,
        "message": "ok",
        "data": schemas.SalesWechatBindingOut.model_validate(row).model_dump(),
    }


def _profile_from_mibuddy(data: dict) -> schemas.MibuddyUserProfileOut:
    return schemas.MibuddyUserProfileOut(
        uuid=str(data.get("uuid") or "").strip(),
        name=str(data.get("name") or "").strip(),
        account=str(data.get("account") or "").strip(),
        changhu=parse_changhu_phones(data),
    )


@router.get("/mibuddy")
async def get_mibuddy_binding(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回当前用户绑定的米城 UUID；若已绑定则附带主系统用户基本信息。"""
    uuid = (current_user.mibuddy_uuid or "").strip()
    if not uuid:
        return {"code": 200, "message": "ok", "data": schemas.MibuddyBindingOut().model_dump()}

    profile = None
    try:
        remote = await fetch_uuid_user_info(uuid)
        profile = _profile_from_mibuddy(remote)
    except (MibuddyConfigError, MibuddyApiError):
        profile = None

    out = schemas.MibuddyBindingOut(uuid=uuid, profile=profile)
    return {"code": 200, "message": "ok", "data": out.model_dump()}


@router.post("/mibuddy")
async def bind_mibuddy_uuid(
    body: schemas.MibuddyBindingCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """绑定米城主系统 UUID；绑定前向主系统校验 UUID 有效性。"""
    uuid = body.uuid.strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="UUID 不能为空")

    taken = await db.execute(select(User).where(User.mibuddy_uuid == uuid, User.id != current_user.id))
    if taken.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="该米城 UUID 已被其他账号绑定")

    try:
        remote = await fetch_uuid_user_info(uuid)
    except MibuddyConfigError:
        raise HTTPException(status_code=503, detail="MiBuddy 服务未配置，请联系管理员")
    except MibuddyApiError as e:
        raise HTTPException(status_code=400, detail=str(e))

    remote_uuid = str(remote.get("uuid") or "").strip()
    if remote_uuid and remote_uuid != uuid:
        uuid = remote_uuid

    current_user.mibuddy_uuid = uuid
    await db.commit()
    await db.refresh(current_user)

    out = schemas.MibuddyBindingOut(uuid=uuid, profile=_profile_from_mibuddy(remote))
    return {"code": 200, "message": "ok", "data": out.model_dump()}


@router.delete("/mibuddy", status_code=status.HTTP_204_NO_CONTENT)
async def unbind_mibuddy_uuid(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not (current_user.mibuddy_uuid or "").strip():
        raise HTTPException(status_code=404, detail="尚未绑定米城 UUID")
    current_user.mibuddy_uuid = None
    await db.commit()


@router.get("/mibuddy/my-leads")
async def get_mibuddy_claimed_leads(
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """拉取当前用户绑定的米城 UUID 对应的认领客资列表。"""
    uuid = (current_user.mibuddy_uuid or "").strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="请先绑定米城 UUID")

    page = max(1, page)
    page_size = max(1, min(100, page_size))

    try:
        remote = await fetch_my_leads(uuid, page=page, page_size=page_size)
    except MibuddyConfigError:
        raise HTTPException(status_code=503, detail="MiBuddy 服务未配置，请联系管理员")
    except MibuddyApiError as e:
        raise HTTPException(status_code=400, detail=str(e))

    raw_list = remote.get("list") or []
    if not isinstance(raw_list, list):
        raw_list = []

    items = []
    for row in raw_list:
        if isinstance(row, dict):
            items.append(map_lead_item_for_desktop(row))

    out = schemas.MibuddyLeadsPageOut(
        page=int(remote.get("page") or page),
        page_size=int(remote.get("page_size") or page_size),
        total=int(remote.get("total") or 0),
        leads=[schemas.MibuddyLeadOut.model_validate(x) for x in items],
    )
    return {"code": 200, "message": "ok", "data": out.model_dump(by_alias=True)}


@router.get("/mibuddy/my-leads-album")
async def get_mibuddy_favorite_leads(
    page: int = 1,
    page_size: int = 50,
    client_name: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """拉取当前用户绑定的米城 UUID 对应的收藏客资列表。"""
    uuid = (current_user.mibuddy_uuid or "").strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="请先绑定米城 UUID")

    page = max(1, page)
    page_size = max(1, min(100, page_size))
    keyword = (client_name or "").strip() or None

    try:
        remote = await fetch_my_leads_album(
            uuid, page=page, page_size=page_size, client_name=keyword
        )
    except MibuddyConfigError:
        raise HTTPException(status_code=503, detail="MiBuddy 服务未配置，请联系管理员")
    except MibuddyApiError as e:
        raise HTTPException(status_code=400, detail=str(e))

    raw_list = remote.get("list") or []
    if not isinstance(raw_list, list):
        raw_list = []

    items = []
    for row in raw_list:
        if isinstance(row, dict):
            items.append(map_album_lead_item_for_desktop(row))

    out = schemas.MibuddyLeadsPageOut(
        page=int(remote.get("page") or page),
        page_size=int(remote.get("page_size") or page_size),
        total=int(remote.get("total") or 0),
        leads=[schemas.MibuddyLeadOut.model_validate(x) for x in items],
    )
    return {"code": 200, "message": "ok", "data": out.model_dump(by_alias=True)}


@router.get("/mibuddy/leads/{lead_id}/remarks")
async def get_mibuddy_lead_remarks(
    lead_id: int,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """拉取当前用户对某客资的历史跟进备注。"""
    uuid = (current_user.mibuddy_uuid or "").strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="请先绑定米城 UUID")

    page = max(1, page)
    page_size = max(1, min(100, page_size))

    try:
        remote = await fetch_my_leads_remarks(
            uuid, lead_id, page=page, page_size=page_size
        )
    except MibuddyConfigError:
        raise HTTPException(status_code=503, detail="MiBuddy 服务未配置，请联系管理员")
    except MibuddyApiError as e:
        raise HTTPException(status_code=400, detail=str(e))

    raw_list = remote.get("list") or []
    if not isinstance(raw_list, list):
        raw_list = []

    items = [
        map_remark_item_for_desktop(row)
        for row in raw_list
        if isinstance(row, dict)
    ]

    return {
        "code": 200,
        "message": "ok",
        "data": {
            "page": int(remote.get("page") or page),
            "page_size": int(remote.get("page_size") or page_size),
            "total": int(remote.get("total") or 0),
            "list": items,
        },
    }


@router.post("/mibuddy/leads/{lead_id}/remarks")
async def add_mibuddy_lead_remark(
    lead_id: int,
    body: schemas.MibuddyLeadRemarkCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """向客资添加跟进备注（同步至主系统）。"""
    uuid = (current_user.mibuddy_uuid or "").strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="请先绑定米城 UUID")

    try:
        remote = await add_remark_to_leads(uuid, lead_id, body.remark)
    except MibuddyConfigError:
        raise HTTPException(status_code=503, detail="MiBuddy 服务未配置，请联系管理员")
    except MibuddyApiError as e:
        raise HTTPException(status_code=400, detail=str(e))

    item = map_remark_item_for_desktop({**remote, "remark": remote.get("remark") or body.remark})
    return {"code": 200, "message": "ok", "data": item}


@router.post("/mibuddy/call-changhu")
async def mibuddy_call_changhu(
    body: schemas.MibuddyCallChanghuRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """用畅呼发起外呼：客资页传 lead_id，电话工作台传 tel。"""
    uuid = (current_user.mibuddy_uuid or "").strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="请先绑定米城 UUID")

    tel = (body.tel or "").strip()
    lead_id = body.lead_id
    if not tel and lead_id is None:
        raise HTTPException(status_code=400, detail="请提供被叫号码或客资 ID")

    try:
        remote = await call_changhu(
            uuid,
            tel=tel or None,
            lead_id=lead_id,
            changhu_tel=body.changhu_tel,
            user_wechat_account=body.user_wechat_account,
        )
    except MibuddyConfigError:
        raise HTTPException(status_code=503, detail="MiBuddy 服务未配置，请联系管理员")
    except MibuddyApiError as e:
        raise HTTPException(status_code=400, detail=str(e))

    call_id = str((remote or {}).get("call_id") or "").strip() or None
    return {
        "code": 200,
        "message": "ok",
        "data": schemas.MibuddyCallYunkeOut(call_id=call_id).model_dump(),
    }


@router.post("/mibuddy/call-yunke")
async def mibuddy_call_yunke(
    body: schemas.MibuddyCallYunkeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """用云客发起外呼：客资页传 lead_id，电话工作台传 tel。"""
    uuid = (current_user.mibuddy_uuid or "").strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="请先绑定米城 UUID")

    tel = (body.tel or "").strip()
    lead_id = body.lead_id
    if not tel and lead_id is None:
        raise HTTPException(status_code=400, detail="请提供被叫号码或客资 ID")

    try:
        remote = await call_yunke(
            uuid,
            tel=tel or None,
            lead_id=lead_id,
            user_wechat_account=body.user_wechat_account,
        )
    except MibuddyConfigError:
        raise HTTPException(status_code=503, detail="MiBuddy 服务未配置，请联系管理员")
    except MibuddyApiError as e:
        raise HTTPException(status_code=400, detail=str(e))

    call_id = str((remote or {}).get("call_id") or "").strip() or None
    return {
        "code": 200,
        "message": "ok",
        "data": schemas.MibuddyCallYunkeOut(call_id=call_id).model_dump(),
    }


@router.post("/mibuddy/leads/{lead_id}/approval_tel")
async def approve_mibuddy_lead_tel(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """发起查看客资完整电话的审批申请。"""
    uuid = (current_user.mibuddy_uuid or "").strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="请先绑定米城 UUID")

    try:
        await approve_tel(uuid, lead_id)
    except MibuddyConfigError:
        raise HTTPException(status_code=503, detail="MiBuddy 服务未配置，请联系管理员")
    except MibuddyApiError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"code": 200, "message": "ok"}


@router.post("/mibuddy/leads/{lead_id}/ignore")
async def ignore_mibuddy_lead(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """用户移除(忽略)待拨打的客资（同步至主系统）。"""
    uuid = (current_user.mibuddy_uuid or "").strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="请先绑定米城 UUID")

    try:
        await ignore_my_lead(uuid, lead_id)
    except MibuddyConfigError:
        raise HTTPException(status_code=503, detail="MiBuddy 服务未配置，请联系管理员")
    except MibuddyApiError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"code": 200, "message": "ok"}


@router.patch("/mibuddy/leads/{lead_id}")
async def update_mibuddy_lead_info(
    lead_id: int,
    body: schemas.MibuddyLeadUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新当前用户名下客资的可编辑信息（同步至主系统）。"""
    uuid = (current_user.mibuddy_uuid or "").strip()
    if not uuid:
        raise HTTPException(status_code=400, detail="请先绑定米城 UUID")

    info = build_update_info_from_form(body.info.model_dump(exclude_unset=True))
    if not info:
        raise HTTPException(status_code=400, detail="没有可更新的字段")

    try:
        await update_my_lead_info(uuid, lead_id, info)
    except MibuddyConfigError:
        raise HTTPException(status_code=503, detail="MiBuddy 服务未配置，请联系管理员")
    except MibuddyApiError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"code": 200, "message": "ok", "data": {"lead_id": lead_id}}
