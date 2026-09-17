"""组织、建号、邀请码。"""
from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    default_invite_expiry,
    default_invite_max_uses,
    generate_invite_code,
)
from app.core.audit import write_audit
from app.core.context import OpContext, get_current_op_user, require_perm
from app.core.dept_tree import rebuild_all_closure
from app.core import permissions as P
from app.database import get_db
from app.models import (
    OpDepartment,
    OpDepartmentMember,
    OpInviteCode,
    OpUserProfile,
    User,
)
from app.security import get_password_hash

router = APIRouter(prefix="/api/op/org", tags=["op-org"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ---------- 部门 ----------


class DeptCreateIn(BaseModel):
    parent_id: int | None = None
    name: str = Field(min_length=1, max_length=100)
    kind: str | None = None
    sort_order: int = 0


class DeptPatchIn(BaseModel):
    name: str | None = None
    kind: str | None = None
    sort_order: int | None = None
    parent_id: int | None = None
    is_active: bool | None = None
    leader_user_id: int | None = None


@router.get("/departments")
async def list_departments(
    ctx: OpContext = Depends(get_current_op_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(OpDepartment).order_by(OpDepartment.sort_order, OpDepartment.id)
        )
    ).scalars().all()

    # 经理：只返回自己祖先链 + 子树；人事/老板：全树
    allowed: set[int] | None = None
    if not (ctx.is_desktop_admin or ctx.op_role == P.OP_BOSS or ctx.dept_kind == P.KIND_HR):
        if not ctx.dept_id:
            allowed = set()
        else:
            from app.models import OpDepartmentClosure

            desc = {
                int(r[0])
                for r in (
                    await db.execute(
                        select(OpDepartmentClosure.descendant_id).where(
                            OpDepartmentClosure.ancestor_id == ctx.dept_id
                        )
                    )
                ).all()
            }
            anc = {
                int(r[0])
                for r in (
                    await db.execute(
                        select(OpDepartmentClosure.ancestor_id).where(
                            OpDepartmentClosure.descendant_id == ctx.dept_id
                        )
                    )
                ).all()
            }
            allowed = desc | anc

    leader_ids = {int(d.leader_user_id) for d in rows if d.leader_user_id}
    leaders: dict[int, User] = {}
    if leader_ids:
        leaders = {
            int(u.id): u
            for u in (
                await db.execute(select(User).where(User.id.in_(leader_ids)))
            ).scalars().all()
        }

    items = []
    for d in rows:
        if allowed is not None and int(d.id) not in allowed:
            continue
        leader = leaders.get(int(d.leader_user_id)) if d.leader_user_id else None
        items.append(
            {
                "id": d.id,
                "parent_id": d.parent_id,
                "name": d.name,
                "kind": d.kind,
                "leader_user_id": d.leader_user_id,
                "leader_name": (
                    (leader.real_name or leader.username) if leader else None
                ),
                "sort_order": d.sort_order,
                "is_active": bool(d.is_active),
            }
        )
    return {"code": 200, "message": "ok", "data": {"items": items}}


@router.post("/departments")
async def create_department(
    body: DeptCreateIn,
    request: Request,
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_DEPT_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.datetime.now()
    kind = (body.kind or "").strip().lower() or None
    if kind and kind not in (
        P.KIND_SALES,
        P.KIND_FINANCE,
        P.KIND_SUPPLY,
        P.KIND_HR,
        P.KIND_OPS_ASSISTANT,
        P.KIND_OTHER,
    ):
        raise HTTPException(status_code=400, detail="无效的部门类型")
    if body.parent_id:
        parent = await db.get(OpDepartment, body.parent_id)
        if not parent:
            raise HTTPException(status_code=400, detail="父部门不存在")

    dept = OpDepartment(
        parent_id=body.parent_id,
        name=body.name.strip(),
        kind=kind,
        sort_order=int(body.sort_order or 0),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(dept)
    await db.flush()
    await rebuild_all_closure(db)
    await write_audit(
        db,
        actor_user_id=ctx.user_id,
        action="dept_create",
        target_type="department",
        target_id=dept.id,
        detail={"name": dept.name, "parent_id": dept.parent_id},
        ip=_ip(request),
    )
    await db.commit()
    return {"code": 200, "message": "ok", "data": {"id": dept.id}}


@router.patch("/departments/{dept_id}")
async def patch_department(
    dept_id: int,
    body: DeptPatchIn,
    request: Request,
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_DEPT_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    dept = await db.get(OpDepartment, dept_id)
    if not dept:
        raise HTTPException(status_code=404, detail="部门不存在")
    now = datetime.datetime.now()
    if body.name is not None:
        dept.name = body.name.strip()
    if body.kind is not None:
        k = body.kind.strip().lower()
        dept.kind = k or None
    if body.sort_order is not None:
        dept.sort_order = int(body.sort_order)
    if body.is_active is not None:
        dept.is_active = bool(body.is_active)
    if body.parent_id is not None and body.parent_id != dept.parent_id:
        if body.parent_id == dept.id:
            raise HTTPException(status_code=400, detail="不能将部门设为自己的父节点")
        dept.parent_id = body.parent_id
        await db.flush()
        await rebuild_all_closure(db)
    if body.leader_user_id is not None:
        if body.leader_user_id == 0:
            dept.leader_user_id = None
        else:
            mem = (
                await db.execute(
                    select(OpDepartmentMember).where(
                        OpDepartmentMember.user_id == body.leader_user_id,
                        OpDepartmentMember.department_id == dept_id,
                    )
                )
            ).scalars().first()
            if not mem:
                raise HTTPException(status_code=400, detail="主管必须是该部门成员")
            dept.leader_user_id = body.leader_user_id
    dept.updated_at = now
    await write_audit(
        db,
        actor_user_id=ctx.user_id,
        action="dept_patch",
        target_type="department",
        target_id=dept_id,
        detail=body.model_dump(exclude_none=True),
        ip=_ip(request),
    )
    await db.commit()
    return {"code": 200, "message": "ok", "data": None}


@router.delete("/departments/{dept_id}")
async def delete_department(
    dept_id: int,
    request: Request,
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_DEPT_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    dept = await db.get(OpDepartment, dept_id)
    if not dept:
        raise HTTPException(status_code=404, detail="部门不存在")
    if dept.parent_id is None:
        raise HTTPException(status_code=400, detail="根部门不可删除")

    child = (
        await db.execute(
            select(OpDepartment.id).where(OpDepartment.parent_id == dept_id).limit(1)
        )
    ).first()
    if child:
        raise HTTPException(status_code=400, detail="请先删除或移走子部门")

    mem = (
        await db.execute(
            select(OpDepartmentMember.id)
            .where(OpDepartmentMember.department_id == dept_id)
            .limit(1)
        )
    ).first()
    if mem:
        raise HTTPException(status_code=400, detail="部门下仍有成员，请先调整花名册")

    invite = (
        await db.execute(
            select(OpInviteCode.id)
            .where(
                OpInviteCode.department_id == dept_id,
                OpInviteCode.revoked_at.is_(None),
            )
            .limit(1)
        )
    ).first()
    if invite:
        raise HTTPException(status_code=400, detail="仍有未停用的邀请码绑定该部门")

    await write_audit(
        db,
        actor_user_id=ctx.user_id,
        action="dept_delete",
        target_type="department",
        target_id=dept_id,
        detail={"name": dept.name},
        ip=_ip(request),
    )
    await db.execute(sa_delete(OpDepartment).where(OpDepartment.id == dept_id))
    await rebuild_all_closure(db)
    await db.commit()
    return {"code": 200, "message": "ok", "data": None}


# ---------- 成员 / 建号 / 角色 ----------


class MemberAssignIn(BaseModel):
    user_id: int
    department_id: int
    set_leader: bool = False


class UserCreateIn(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=6, max_length=100)
    real_name: str = Field(min_length=1, max_length=50)
    department_id: int
    op_role: str = "none"  # none/manager/boss
    set_leader: bool = False


class RoleAssignIn(BaseModel):
    op_role: str  # none/manager/boss
    status: str | None = None  # pending/active/disabled
    department_id: int | None = None
    set_leader: bool = False
    is_active: bool | None = None  # 对应 users.is_active（桌面端/主后台共用）


@router.post("/members")
async def assign_member(
    body: MemberAssignIn,
    request: Request,
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_MEMBER_ASSIGN)),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, body.user_id)
    dept = await db.get(OpDepartment, body.department_id)
    if not user or not dept:
        raise HTTPException(status_code=404, detail="用户或部门不存在")

    now = datetime.datetime.now()
    existing = (
        await db.execute(
            select(OpDepartmentMember).where(OpDepartmentMember.user_id == body.user_id)
        )
    ).scalars().first()
    if existing:
        existing.department_id = body.department_id
        existing.joined_at = now
    else:
        db.add(
            OpDepartmentMember(
                department_id=body.department_id,
                user_id=body.user_id,
                joined_at=now,
            )
        )
    if body.set_leader:
        # 人事可以设 leader（方案默认）
        dept.leader_user_id = body.user_id
        dept.updated_at = now
    await write_audit(
        db,
        actor_user_id=ctx.user_id,
        action="member_assign",
        target_type="user",
        target_id=body.user_id,
        detail=body.model_dump(),
        ip=_ip(request),
    )
    await db.commit()
    return {"code": 200, "message": "ok", "data": None}


@router.post("/users")
async def create_user(
    body: UserCreateIn,
    request: Request,
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_USER_CREATE)),
    db: AsyncSession = Depends(get_db),
):
    role = (body.op_role or P.OP_NONE).strip().lower()
    if role == P.OP_BOSS and not (ctx.is_desktop_admin or ctx.op_role == P.OP_BOSS):
        raise HTTPException(status_code=403, detail="人事不能授予老板角色")
    if role not in (P.OP_NONE, P.OP_MANAGER, P.OP_BOSS):
        raise HTTPException(status_code=400, detail="无效的运营角色")
    if role == P.OP_STAFF:
        raise HTTPException(status_code=400, detail="员工登录尚未开放")

    exists = await db.execute(select(User).where(User.username == body.username.strip()))
    if exists.scalars().first():
        raise HTTPException(status_code=400, detail="用户名已存在")
    dept = await db.get(OpDepartment, body.department_id)
    if not dept:
        raise HTTPException(status_code=400, detail="部门不存在")

    now = datetime.datetime.now()
    user = User(
        username=body.username.strip(),
        password_hash=get_password_hash(body.password),
        real_name=body.real_name.strip(),
        role="staff",
        is_active=True,
    )
    db.add(user)
    await db.flush()

    status = P.STATUS_ACTIVE if role in (P.OP_MANAGER, P.OP_BOSS) else P.STATUS_PENDING
    db.add(
        OpUserProfile(
            user_id=user.id,
            op_role=role,
            status=status,
            created_via="direct_create",
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        OpDepartmentMember(
            department_id=body.department_id, user_id=user.id, joined_at=now
        )
    )
    if body.set_leader:
        dept.leader_user_id = user.id
        dept.updated_at = now

    await write_audit(
        db,
        actor_user_id=ctx.user_id,
        action="user_create",
        target_type="user",
        target_id=user.id,
        detail={"username": user.username, "op_role": role, "department_id": body.department_id},
        ip=_ip(request),
    )
    await db.commit()
    return {"code": 200, "message": "ok", "data": {"user_id": user.id}}


@router.post("/users/{user_id}/role")
async def assign_role(
    user_id: int,
    body: RoleAssignIn,
    request: Request,
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_ROLE_ASSIGN)),
    db: AsyncSession = Depends(get_db),
):
    role = (body.op_role or "").strip().lower()
    if role == P.OP_BOSS and not (ctx.is_desktop_admin or ctx.op_role == P.OP_BOSS):
        raise HTTPException(status_code=403, detail="人事不能授予老板角色")
    if role not in (P.OP_NONE, P.OP_MANAGER, P.OP_BOSS):
        raise HTTPException(status_code=400, detail="无效的运营角色")

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    if body.is_active is False and int(user_id) == int(ctx.user_id):
        raise HTTPException(status_code=400, detail="不能停用自己的账号")

    now = datetime.datetime.now()

    # 与主后台一致：写 users.is_active；停用时吊销桌面端 token
    if body.is_active is not None:
        user.is_active = bool(body.is_active)
        if not user.is_active:
            user.active_token_jti = "revoked"

    prof = await db.get(OpUserProfile, user_id)
    status = (body.status or "").strip().lower()
    if body.is_active is False:
        status = P.STATUS_DISABLED
    elif not status:
        status = P.STATUS_ACTIVE if role in (P.OP_MANAGER, P.OP_BOSS) else P.STATUS_PENDING
    if body.is_active is True and status == P.STATUS_DISABLED:
        status = P.STATUS_ACTIVE if role in (P.OP_MANAGER, P.OP_BOSS) else P.STATUS_PENDING

    if prof:
        prof.op_role = role
        prof.status = status
        prof.updated_at = now
    else:
        db.add(
            OpUserProfile(
                user_id=user_id,
                op_role=role,
                status=status,
                created_via="direct_create",
                created_at=now,
                updated_at=now,
            )
        )

    if body.department_id:
        existing = (
            await db.execute(
                select(OpDepartmentMember).where(OpDepartmentMember.user_id == user_id)
            )
        ).scalars().first()
        if existing:
            existing.department_id = body.department_id
            existing.joined_at = now
        else:
            db.add(
                OpDepartmentMember(
                    department_id=body.department_id, user_id=user_id, joined_at=now
                )
            )
        if body.set_leader:
            dept = await db.get(OpDepartment, body.department_id)
            if dept:
                dept.leader_user_id = user_id
                dept.updated_at = now
        else:
            # 关闭「设为主管」时，若其仍是该部门主管则清除
            dept = await db.get(OpDepartment, body.department_id)
            if dept and dept.leader_user_id and int(dept.leader_user_id) == int(user_id):
                dept.leader_user_id = None
                dept.updated_at = now

    await write_audit(
        db,
        actor_user_id=ctx.user_id,
        action="role_assign",
        target_type="user",
        target_id=user_id,
        detail=body.model_dump(exclude_none=True),
        ip=_ip(request),
    )
    await db.commit()
    return {"code": 200, "message": "ok", "data": None}


@router.get("/roster")
async def roster(
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_ROSTER)),
    db: AsyncSession = Depends(get_db),
):
    ids = list(ctx.roster_user_ids)
    if not ids:
        return {"code": 200, "message": "ok", "data": {"items": []}}

    users = (
        await db.execute(
            select(User).where(User.id.in_(ids)).order_by(User.id)
        )
    ).scalars().all()
    profiles = {
        int(p.user_id): p
        for p in (
            await db.execute(
                select(OpUserProfile).where(OpUserProfile.user_id.in_(ids))
            )
        ).scalars().all()
    }
    members = {
        int(m.user_id): int(m.department_id)
        for m in (
            await db.execute(
                select(OpDepartmentMember).where(OpDepartmentMember.user_id.in_(ids))
            )
        ).scalars().all()
    }
    depts = {
        int(d.id): d
        for d in (await db.execute(select(OpDepartment))).scalars().all()
    }

    items = []
    for u in users:
        prof = profiles.get(int(u.id))
        did = members.get(int(u.id))
        dept = depts.get(did) if did else None
        op_role = P.OP_BOSS if (u.role or "").lower() == "admin" else (
            (prof.op_role if prof else P.OP_NONE) or P.OP_NONE
        )
        items.append(
            {
                "user_id": u.id,
                "username": u.username,
                "real_name": u.real_name,
                "is_active": bool(u.is_active),
                "desktop_role": u.role,
                "op_role": op_role,
                "status": (prof.status if prof else P.STATUS_PENDING),
                "department_id": did,
                "department_name": dept.name if dept else None,
                "is_leader": bool(
                    dept and dept.leader_user_id and int(dept.leader_user_id) == int(u.id)
                ),
            }
        )
    return {"code": 200, "message": "ok", "data": {"items": items}}


# ---------- 邀请码 ----------


class InviteCreateIn(BaseModel):
    department_id: int
    grant_op_role: str = "none"
    max_uses: int | None = None
    expires_days: int | None = None
    note: str | None = None


@router.get("/invites")
async def list_invites(
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_INVITE)),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(select(OpInviteCode).order_by(OpInviteCode.id.desc()).limit(200))
    ).scalars().all()
    dept_ids = {int(r.department_id) for r in rows if r.department_id}
    dept_names: dict[int, str] = {}
    if dept_ids:
        dept_names = {
            int(d.id): str(d.name)
            for d in (
                await db.execute(select(OpDepartment).where(OpDepartment.id.in_(dept_ids)))
            ).scalars().all()
        }
    items = [
        {
            "id": r.id,
            "code": r.code,
            "department_id": r.department_id,
            "department_name": dept_names.get(int(r.department_id)) if r.department_id else None,
            "grant_op_role": r.grant_op_role,
            "max_uses": r.max_uses,
            "used_count": r.used_count,
            "expires_at": r.expires_at.isoformat(sep=" ") if r.expires_at else None,
            "revoked_at": r.revoked_at.isoformat(sep=" ") if r.revoked_at else None,
            "note": r.note,
            "created_at": r.created_at.isoformat(sep=" ") if r.created_at else None,
            "created_by_user_id": r.created_by_user_id,
        }
        for r in rows
    ]
    return {"code": 200, "message": "ok", "data": {"items": items}}


@router.post("/invites")
async def create_invite(
    body: InviteCreateIn,
    request: Request,
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_INVITE)),
    db: AsyncSession = Depends(get_db),
):
    grant = (body.grant_op_role or P.OP_NONE).strip().lower()
    if grant not in (P.OP_NONE, P.OP_MANAGER):
        raise HTTPException(status_code=400, detail="邀请码只能授予 none 或 manager")
    dept = await db.get(OpDepartment, body.department_id)
    if not dept or not dept.is_active:
        raise HTTPException(status_code=400, detail="部门不可用")

    now = datetime.datetime.now()
    days = body.expires_days if body.expires_days is not None else None
    expires = (
        now + datetime.timedelta(days=max(1, int(days)))
        if days is not None
        else default_invite_expiry()
    )
    max_uses = (
        max(1, int(body.max_uses))
        if body.max_uses is not None
        else default_invite_max_uses()
    )

    code = generate_invite_code()
    for _ in range(5):
        clash = (
            await db.execute(select(OpInviteCode.id).where(OpInviteCode.code == code))
        ).first()
        if not clash:
            break
        code = generate_invite_code()

    invite = OpInviteCode(
        code=code,
        created_by_user_id=ctx.user_id,
        department_id=body.department_id,
        grant_op_role=grant,
        max_uses=max_uses,
        used_count=0,
        expires_at=expires,
        note=(body.note or None),
        created_at=now,
    )
    db.add(invite)
    await db.flush()
    await write_audit(
        db,
        actor_user_id=ctx.user_id,
        action="invite_create",
        target_type="invite",
        target_id=invite.id,
        detail={"code": code, "department_id": body.department_id, "grant_op_role": grant},
        ip=_ip(request),
    )
    await db.commit()
    return {
        "code": 200,
        "message": "ok",
        "data": {
            "id": invite.id,
            "code": code,
            "expires_at": expires.isoformat(sep=" "),
            "max_uses": max_uses,
        },
    }


@router.post("/invites/{invite_id}/revoke")
async def revoke_invite(
    invite_id: int,
    request: Request,
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_INVITE)),
    db: AsyncSession = Depends(get_db),
):
    invite = await db.get(OpInviteCode, invite_id)
    if not invite:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    invite.revoked_at = datetime.datetime.now()
    await write_audit(
        db,
        actor_user_id=ctx.user_id,
        action="invite_revoke",
        target_type="invite",
        target_id=invite_id,
        detail=None,
        ip=_ip(request),
    )
    await db.commit()
    return {"code": 200, "message": "ok", "data": None}


# ---------- 部门菜单权限 ----------


class DeptPermsPutIn(BaseModel):
    manager: list[str] = Field(default_factory=list)
    staff: list[str] = Field(default_factory=list)


async def _role_perm_payload(db: AsyncSession, dept_id: int, op_role: str) -> dict:
    from app.core.dept_tree import resolve_kind

    local_rows = await P._load_role_rows(db, dept_id, op_role)
    local = P._codes_from_rows(local_rows)
    configured = local is not None
    effective, src_id = await P.load_dept_role_perms(db, dept_id, op_role)
    if effective is None:
        kind = await resolve_kind(db, dept_id)
        effective = P.permissions_for_kind_fallback(op_role=op_role, dept_kind=kind)
        src_id = None

    inherited_from = None
    if not configured and src_id and int(src_id) != int(dept_id):
        src = await db.get(OpDepartment, int(src_id))
        inherited_from = {
            "id": int(src_id),
            "name": src.name if src else f"#{src_id}",
        }

    form_codes = sorted(local) if configured else sorted(effective or [])
    return {
        "codes": form_codes,
        "configured": configured,
        "inherited_from": inherited_from,
        "effective_codes": sorted(effective or []),
    }


@router.get("/dept-perms/catalog")
async def dept_perms_catalog(
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_PERM_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    data = await P.build_dept_perm_catalog(db)
    return {"code": 200, "message": "ok", "data": data}


@router.get("/dept-perms/departments/{dept_id}")
async def get_dept_perms(
    dept_id: int,
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_PERM_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    dept = await db.get(OpDepartment, dept_id)
    if not dept:
        raise HTTPException(status_code=404, detail="部门不存在")
    return {
        "code": 200,
        "message": "ok",
        "data": {
            "department_id": int(dept.id),
            "department_name": dept.name,
            "kind": dept.kind,
            "manager": await _role_perm_payload(db, dept_id, P.OP_MANAGER),
            "staff": await _role_perm_payload(db, dept_id, P.OP_STAFF),
        },
    }


@router.put("/dept-perms/departments/{dept_id}")
async def put_dept_perms(
    dept_id: int,
    body: DeptPermsPutIn,
    request: Request,
    ctx: OpContext = Depends(require_perm(P.PERM_ORG_PERM_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    dept = await db.get(OpDepartment, dept_id)
    if not dept:
        raise HTTPException(status_code=404, detail="部门不存在")

    allowed = await P.all_configurable_codes(db)

    def _validate(codes: list[str], label: str) -> list[str]:
        out: list[str] = []
        for c in codes or []:
            code = str(c or "").strip()
            if not code:
                continue
            if code in P.BOSS_ONLY_PERMS:
                raise HTTPException(status_code=400, detail=f"{label}不可配置超管权限：{code}")
            if code not in allowed:
                raise HTTPException(status_code=400, detail=f"{label}含未知权限：{code}")
            out.append(code)
        return out

    manager = _validate(body.manager, "主管档")
    staff = _validate(body.staff, "普通用户档")
    await P.replace_dept_role_perms(
        db, dept_id, manager=manager, staff=staff, allowed_codes=allowed
    )
    await write_audit(
        db,
        actor_user_id=ctx.user_id,
        action="dept_perms_put",
        target_type="department",
        target_id=dept_id,
        detail={"manager": manager, "staff": staff},
        ip=_ip(request),
    )
    await db.commit()
    return {
        "code": 200,
        "message": "已保存",
        "data": {
            "department_id": int(dept.id),
            "department_name": dept.name,
            "manager": await _role_perm_payload(db, dept_id, P.OP_MANAGER),
            "staff": await _role_perm_payload(db, dept_id, P.OP_STAFF),
        },
    }
