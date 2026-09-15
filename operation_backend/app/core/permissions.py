"""运营角色 / 权限码。"""
from __future__ import annotations

from typing import Iterable

OP_NONE = "none"
OP_STAFF = "staff"
OP_MANAGER = "manager"
OP_BOSS = "boss"

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

KIND_SALES = "sales"
KIND_FINANCE = "finance"
KIND_SUPPLY = "supply"
KIND_HR = "hr"
KIND_OTHER = "other"

# 权限码
PERM_USAGE_DASHBOARD = "usage.dashboard.view"
PERM_USAGE_PERSON_LIST = "usage.person.list"
PERM_USAGE_PERSON_DETAIL = "usage.person.detail"
PERM_USAGE_EXPORT = "usage.export"
PERM_BIZ_CHAT = "biz.chat.view"
PERM_BIZ_OUTBOUND = "biz.outbound.view"
PERM_BIZ_TASK = "biz.task.view"
PERM_BIZ_BLAST = "biz.blast.view"
PERM_BIZ_PHONE = "biz.phone.view"
PERM_BIZ_PRODUCT = "biz.product_usage.view"
PERM_ORG_ROSTER = "org.roster.view"
PERM_ORG_DEPT_MANAGE = "org.dept.manage"
PERM_ORG_ROLE_ASSIGN = "org.op_role.assign"
PERM_ORG_MEMBER_ASSIGN = "org.member.assign"
PERM_ORG_USER_CREATE = "org.user.create"
PERM_ORG_INVITE = "org.invite.manage"
PERM_AUDIT_LOGIN = "audit.login.view"

_SALES_BIZ = frozenset(
    {
        PERM_USAGE_DASHBOARD,
        PERM_USAGE_PERSON_LIST,
        PERM_USAGE_PERSON_DETAIL,
        PERM_USAGE_EXPORT,
        PERM_BIZ_CHAT,
        PERM_BIZ_OUTBOUND,
        PERM_BIZ_TASK,
        PERM_BIZ_BLAST,
        PERM_BIZ_PHONE,
        PERM_BIZ_PRODUCT,
        PERM_ORG_ROSTER,
    }
)

_HR_ACCOUNT = frozenset(
    {
        PERM_ORG_ROSTER,
        PERM_ORG_ROLE_ASSIGN,
        PERM_ORG_MEMBER_ASSIGN,
        PERM_ORG_USER_CREATE,
        PERM_ORG_INVITE,
        PERM_AUDIT_LOGIN,
    }
)

_BOSS = frozenset(
    {
        *_SALES_BIZ,
        *_HR_ACCOUNT,
        PERM_ORG_DEPT_MANAGE,
    }
)


def permissions_for(*, op_role: str, dept_kind: str | None, is_desktop_admin: bool) -> set[str]:
    if is_desktop_admin or op_role == OP_BOSS:
        return set(_BOSS)
    if op_role != OP_MANAGER:
        # staff 预留：仅自己摘要，P0 不登录故通常用不到
        if op_role == OP_STAFF:
            return {
                PERM_USAGE_DASHBOARD,
                PERM_USAGE_PERSON_DETAIL,
                PERM_BIZ_CHAT,
                PERM_BIZ_OUTBOUND,
                PERM_BIZ_TASK,
                PERM_BIZ_BLAST,
                PERM_BIZ_PHONE,
                PERM_BIZ_PRODUCT,
            }
        return set()

    kind = (dept_kind or "").strip().lower()
    if kind == KIND_HR:
        return set(_HR_ACCOUNT)
    if kind == KIND_SALES:
        return set(_SALES_BIZ)
    # finance / supply / other：暂无业务菜单
    return {PERM_ORG_ROSTER}


def has_perm(perms: Iterable[str], code: str) -> bool:
    return code in set(perms)
