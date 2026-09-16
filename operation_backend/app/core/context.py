"""请求上下文：可见范围 + 权限。"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import OP_STAFF_LOGIN_ENABLED
from app.core import permissions as P
from app.core.dept_tree import (
    descendant_dept_ids,
    get_user_department_id,
    member_user_ids_in_depts,
    resolve_kind,
)
from app.database import get_db
from app.models import OpUserProfile, User
from app.security import safe_decode_op_token

_bearer = HTTPBearer(auto_error=False)


@dataclass
class OpContext:
    user_id: int
    username: str
    real_name: str
    desktop_role: str
    is_desktop_admin: bool
    op_role: str
    status: str
    dept_id: int | None
    dept_kind: str | None
    permissions: set[str] = field(default_factory=set)
    # 业务数据可见（usage/biz）
    visible_user_ids: frozenset[int] = field(default_factory=frozenset)
    # 花名册可见
    roster_user_ids: frozenset[int] = field(default_factory=frozenset)

    def require(self, code: str) -> None:
        if code not in self.permissions:
            raise HTTPException(status_code=403, detail="无权限")

    def ensure_visible(self, target_user_id: int, *, roster: bool = False) -> None:
        pool = self.roster_user_ids if roster else self.visible_user_ids
        if int(target_user_id) not in pool:
            raise HTTPException(status_code=404, detail="用户不存在")


async def _load_profile(db: AsyncSession, user: User) -> tuple[str, str]:
    if (user.role or "").strip().lower() == "admin":
        return P.OP_BOSS, P.STATUS_ACTIVE
    prof = await db.get(OpUserProfile, user.id)
    if not prof:
        return P.OP_NONE, P.STATUS_PENDING
    return (prof.op_role or P.OP_NONE).strip().lower(), (prof.status or P.STATUS_PENDING).strip().lower()


def _can_login(op_role: str, status: str, is_admin: bool) -> bool:
    if is_admin:
        return True
    if status != P.STATUS_ACTIVE:
        return False
    if op_role == P.OP_BOSS or op_role == P.OP_MANAGER:
        return True
    if op_role == P.OP_STAFF and OP_STAFF_LOGIN_ENABLED:
        return True
    return False


async def build_op_context(db: AsyncSession, user: User) -> OpContext:
    is_admin = (user.role or "").strip().lower() == "admin"
    op_role, st = await _load_profile(db, user)
    if is_admin:
        op_role, st = P.OP_BOSS, P.STATUS_ACTIVE

    dept_id = await get_user_department_id(db, user.id)
    dept_kind = await resolve_kind(db, dept_id)
    perms = await P.resolve_user_permissions(
        db,
        op_role=op_role,
        dept_id=dept_id,
        dept_kind=dept_kind,
        is_desktop_admin=is_admin,
    )

    all_active_ids = [
        int(r[0])
        for r in (
            await db.execute(select(User.id).where(User.is_active.is_(True)))
        ).all()
    ]
    # 花名册需含停用账号，否则无法再启停
    all_user_ids = [
        int(r[0]) for r in (await db.execute(select(User.id))).all()
    ]

    visible: set[int] = set()
    roster: set[int] = set()

    if is_admin or op_role == P.OP_BOSS:
        visible = set(all_active_ids)
        roster = set(all_user_ids)
    elif op_role == P.OP_MANAGER:
        if dept_kind == P.KIND_HR:
            # 人事：业务数据空，花名册全员（含停用）
            visible = set()
            roster = set(all_user_ids)
        elif dept_id:
            d_ids = await descendant_dept_ids(db, dept_id)
            u_ids = await member_user_ids_in_depts(db, d_ids)
            if dept_kind == P.KIND_SALES:
                visible = set(u_ids) & set(all_active_ids)
            else:
                visible = set()
            roster = set(u_ids)
            roster.add(user.id)
            if dept_kind == P.KIND_SALES:
                visible.add(user.id)
        else:
            roster = {user.id}
    elif op_role == P.OP_STAFF:
        visible = {user.id} if user.is_active else set()
        roster = {user.id}

    return OpContext(
        user_id=int(user.id),
        username=user.username or "",
        real_name=user.real_name or "",
        desktop_role=(user.role or "staff"),
        is_desktop_admin=is_admin,
        op_role=op_role,
        status=st,
        dept_id=dept_id,
        dept_kind=dept_kind,
        permissions=perms,
        visible_user_ids=frozenset(visible),
        roster_user_ids=frozenset(roster),
    )


async def get_current_op_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> OpContext:
    if not creds or not creds.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    payload = safe_decode_op_token(creds.credentials)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")
    try:
        uid = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")

    user = await db.get(User, uid)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用")

    op_role, st = await _load_profile(db, user)
    is_admin = (user.role or "").strip().lower() == "admin"
    if not _can_login(op_role, st, is_admin):
        raise HTTPException(status_code=403, detail="账号未开通运营权限，请联系管理员")

    ctx = await build_op_context(db, user)
    request.state.op_ctx = ctx
    return ctx


def require_perm(code: str):
    async def _dep(ctx: OpContext = Depends(get_current_op_user)) -> OpContext:
        ctx.require(code)
        return ctx

    return _dep
