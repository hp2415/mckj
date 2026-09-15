"""运营鉴权：登录 / 邀请码注册 / me。"""
from __future__ import annotations

import datetime
import secrets
import string

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import OP_INVITE_DEFAULT_DAYS, OP_INVITE_DEFAULT_MAX_USES
from app.core.audit import write_audit
from app.core.context import _can_login, _load_profile, build_op_context, get_current_op_user
from app.core.dept_tree import dept_path_names
from app.core import permissions as P
from app.database import get_db
from app.models import (
    OpDepartment,
    OpDepartmentMember,
    OpInviteCode,
    OpInviteRedemption,
    OpUserProfile,
    User,
)
from app.security import create_op_token, get_password_hash, verify_password

router = APIRouter(prefix="/api/op/auth", tags=["op-auth"])

_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    invite_code: str = Field(min_length=4, max_length=16)
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=6, max_length=100)
    real_name: str = Field(min_length=1, max_length=50)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/login")
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    username = (body.username or "").strip()
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalars().first()
    if not user or not verify_password(body.password, user.password_hash):
        await write_audit(
            db,
            actor_user_id=None,
            action="login_fail",
            detail={"username": username},
            ip=_client_ip(request),
        )
        await db.commit()
        raise HTTPException(status_code=401, detail="用户名或密码不正确")

    if not user.is_active:
        raise HTTPException(status_code=400, detail="该账号已被禁用")

    op_role, st = await _load_profile(db, user)
    is_admin = (user.role or "").strip().lower() == "admin"
    if not _can_login(op_role, st, is_admin):
        await write_audit(
            db,
            actor_user_id=user.id,
            action="login_denied",
            detail={"op_role": op_role, "status": st},
            ip=_client_ip(request),
        )
        await db.commit()
        raise HTTPException(status_code=403, detail="账号未开通运营权限，请联系管理员")

    ctx = await build_op_context(db, user)
    token = create_op_token(
        user_id=ctx.user_id, op_role=ctx.op_role, dept_id=ctx.dept_id
    )
    await write_audit(
        db,
        actor_user_id=user.id,
        action="login_success",
        detail={"op_role": ctx.op_role},
        ip=_client_ip(request),
    )
    await db.commit()
    return {
        "code": 200,
        "message": "ok",
        "data": {
            "access_token": token,
            "token_type": "bearer",
            "user": await _me_payload(db, ctx),
        },
    }


@router.post("/register")
async def register(body: RegisterIn, request: Request, db: AsyncSession = Depends(get_db)):
    code = (body.invite_code or "").strip().upper()
    username = (body.username or "").strip()
    real_name = (body.real_name or "").strip()
    if not code or not username or not real_name:
        raise HTTPException(status_code=400, detail="请填写完整信息")

    exists = await db.execute(select(User).where(User.username == username))
    if exists.scalars().first():
        raise HTTPException(status_code=400, detail="用户名已存在")

    invite = (
        await db.execute(select(OpInviteCode).where(OpInviteCode.code == code))
    ).scalars().first()
    now = datetime.datetime.now()
    if not invite or invite.revoked_at:
        raise HTTPException(status_code=400, detail="邀请码无效")
    if invite.expires_at and invite.expires_at < now:
        raise HTTPException(status_code=400, detail="邀请码已过期")
    if int(invite.used_count or 0) >= int(invite.max_uses or 1):
        raise HTTPException(status_code=400, detail="邀请码已用尽")

    dept = await db.get(OpDepartment, invite.department_id)
    if not dept or not dept.is_active:
        raise HTTPException(status_code=400, detail="邀请码绑定部门不可用")

    grant = (invite.grant_op_role or P.OP_NONE).strip().lower()
    if grant not in (P.OP_NONE, P.OP_MANAGER):
        grant = P.OP_NONE

    user = User(
        username=username,
        password_hash=get_password_hash(body.password),
        real_name=real_name,
        role="staff",
        is_active=True,
    )
    db.add(user)
    await db.flush()

    status = P.STATUS_ACTIVE if grant == P.OP_MANAGER else P.STATUS_PENDING
    db.add(
        OpUserProfile(
            user_id=user.id,
            op_role=grant,
            status=status,
            created_via="operation_register",
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        OpDepartmentMember(
            department_id=int(invite.department_id),
            user_id=user.id,
            joined_at=now,
        )
    )
    if grant == P.OP_MANAGER and not dept.leader_user_id:
        dept.leader_user_id = user.id
        dept.updated_at = now

    invite.used_count = int(invite.used_count or 0) + 1
    db.add(
        OpInviteRedemption(invite_id=invite.id, user_id=user.id, redeemed_at=now)
    )
    await write_audit(
        db,
        actor_user_id=user.id,
        action="register_invite",
        target_type="invite",
        target_id=invite.id,
        detail={"department_id": invite.department_id, "grant_op_role": grant},
        ip=_client_ip(request),
    )
    await db.commit()

    msg = "注册成功"
    if grant == P.OP_NONE:
        msg = "注册成功，账号待开通后才能登录运营后台"
    return {"code": 200, "message": msg, "data": {"user_id": user.id, "op_role": grant}}


async def _me_payload(db: AsyncSession, ctx) -> dict:
    path = await dept_path_names(db, ctx.dept_id)
    return {
        "user_id": ctx.user_id,
        "username": ctx.username,
        "real_name": ctx.real_name,
        "desktop_role": ctx.desktop_role,
        "is_desktop_admin": ctx.is_desktop_admin,
        "op_role": ctx.op_role,
        "status": ctx.status,
        "dept_id": ctx.dept_id,
        "dept_kind": ctx.dept_kind,
        "dept_path": path,
        "permissions": sorted(ctx.permissions),
    }


@router.get("/me")
async def me(ctx=Depends(get_current_op_user), db: AsyncSession = Depends(get_db)):
    return {"code": 200, "message": "ok", "data": await _me_payload(db, ctx)}


def generate_invite_code(length: int = 10) -> str:
    return "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(length))


def default_invite_expiry() -> datetime.datetime:
    return datetime.datetime.now() + datetime.timedelta(days=OP_INVITE_DEFAULT_DAYS)


def default_invite_max_uses() -> int:
    return OP_INVITE_DEFAULT_MAX_USES
