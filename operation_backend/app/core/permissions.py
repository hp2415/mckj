"""运营角色 / 权限码 / 部门菜单解析。"""
from __future__ import annotations

import datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
KIND_OPS_ASSISTANT = "ops_assistant"  # 运营助理
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
PERM_ACTIVITY_VIEW = "activity.campaign.view"
PERM_ACTIVITY_EDIT = "activity.campaign.edit"
PERM_PRODUCT_SPEC_VIEW = "product.spec.view"
PERM_PRODUCT_SPEC_EDIT = "product.spec.edit"
PERM_ORG_ROSTER = "org.roster.view"
PERM_ORG_DEPT_MANAGE = "org.dept.manage"
PERM_ORG_ROLE_ASSIGN = "org.op_role.assign"
PERM_ORG_MEMBER_ASSIGN = "org.member.assign"
PERM_ORG_USER_CREATE = "org.user.create"
PERM_ORG_INVITE = "org.invite.manage"
PERM_AUDIT_LOGIN = "audit.login.view"
PERM_ORG_PERM_MANAGE = "org.perm.manage"
PERM_SYSTEM_MENU_MANAGE = "system.menu.manage"

# 部门档位已配置哨兵（不出现在目录、不算真实权限）
PERM_CONFIGURED_MARKER = "__configured__"

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

_OPS_ACTIVITY = frozenset(
    {
        PERM_ACTIVITY_VIEW,
        PERM_ACTIVITY_EDIT,
        PERM_PRODUCT_SPEC_VIEW,
        PERM_PRODUCT_SPEC_EDIT,
    }
)

_PRODUCT_SPEC = frozenset(
    {
        PERM_PRODUCT_SPEC_VIEW,
        PERM_PRODUCT_SPEC_EDIT,
    }
)

_STAFF_DEFAULT = frozenset(
    {
        PERM_USAGE_DASHBOARD,
        PERM_USAGE_PERSON_DETAIL,
        PERM_BIZ_CHAT,
        PERM_BIZ_OUTBOUND,
        PERM_BIZ_TASK,
        PERM_BIZ_BLAST,
        PERM_BIZ_PHONE,
        PERM_BIZ_PRODUCT,
    }
)

_BOSS = frozenset(
    {
        *_SALES_BIZ,
        *_HR_ACCOUNT,
        *_OPS_ACTIVITY,
        *_PRODUCT_SPEC,
        PERM_ORG_DEPT_MANAGE,
        PERM_ORG_PERM_MANAGE,
        PERM_SYSTEM_MENU_MANAGE,
    }
)

# 仅 boss 可持有，部门配置 UI / PUT 不可勾选
BOSS_ONLY_PERMS = frozenset(
    {
        PERM_ORG_DEPT_MANAGE,
        PERM_ORG_PERM_MANAGE,
        PERM_SYSTEM_MENU_MANAGE,
    }
)

# 可配置目录（管理员勾选来源）
PERM_CATALOG: list[dict[str, Any]] = [
    {
        "group": "usage",
        "label": "使用率",
        "items": [
            {"code": PERM_USAGE_DASHBOARD, "label": "使用率大屏"},
            {"code": PERM_USAGE_PERSON_LIST, "label": "人员明细列表"},
            {"code": PERM_USAGE_PERSON_DETAIL, "label": "个人时间线"},
            {"code": PERM_USAGE_EXPORT, "label": "使用率导出"},
            {"code": PERM_BIZ_CHAT, "label": "对话数据"},
            {"code": PERM_BIZ_OUTBOUND, "label": "外发数据"},
            {"code": PERM_BIZ_TASK, "label": "任务数据"},
            {"code": PERM_BIZ_BLAST, "label": "群发数据"},
            {"code": PERM_BIZ_PHONE, "label": "外呼数据"},
            {"code": PERM_BIZ_PRODUCT, "label": "商品使用数据"},
        ],
    },
    {
        "group": "activity",
        "label": "活动管理",
        "items": [
            {"code": PERM_ACTIVITY_VIEW, "label": "活动查看"},
            {"code": PERM_ACTIVITY_EDIT, "label": "活动编辑"},
        ],
    },
    {
        "group": "org",
        "label": "组织账号",
        "items": [
            {"code": PERM_ORG_ROSTER, "label": "账号花名册"},
            {"code": PERM_ORG_ROLE_ASSIGN, "label": "开通/改角色"},
            {"code": PERM_ORG_MEMBER_ASSIGN, "label": "调整部门成员"},
            {"code": PERM_ORG_USER_CREATE, "label": "直接建号"},
            {"code": PERM_ORG_INVITE, "label": "邀请码"},
            {"code": PERM_AUDIT_LOGIN, "label": "登录审计"},
        ],
    },
    {
        "group": "product",
        "label": "商品管理",
        "items": [
            {"code": PERM_PRODUCT_SPEC_VIEW, "label": "商品规格查看"},
            {"code": PERM_PRODUCT_SPEC_EDIT, "label": "商品规格编辑"},
        ],
    },
]

CONFIGURABLE_PERM_CODES: frozenset[str] = frozenset(
    item["code"]
    for g in PERM_CATALOG
    for item in g["items"]
    if item.get("code") and item["code"] not in BOSS_ONLY_PERMS
)


def catalog_for_api() -> list[dict[str, Any]]:
    """供配置 UI：过滤 boss_only，保留空商品组占位。"""
    out: list[dict[str, Any]] = []
    for g in PERM_CATALOG:
        items = [
            {**it, "boss_only": False}
            for it in g["items"]
            if it.get("code") and it["code"] not in BOSS_ONLY_PERMS
        ]
        out.append({"group": g["group"], "label": g["label"], "items": items})
    return out


def catalog_code_label_map() -> dict[str, str]:
    return {
        str(it["code"]): str(it["label"])
        for g in PERM_CATALOG
        for it in g["items"]
        if it.get("code")
    }


async def menu_perm_codes(db: AsyncSession) -> set[str]:
    """启用菜单上挂的权限码（含按钮），排除超管专属。"""
    from app.core import menus as M

    rows = await M.load_all_menus(db)
    codes: set[str] = set()
    for r in rows:
        if not bool(r.is_enable):
            continue
        code = (r.perm_code or "").strip()
        if code and code not in BOSS_ONLY_PERMS:
            codes.add(code)
    return codes


async def all_configurable_codes(db: AsyncSession) -> set[str]:
    """部门可下放权限 = 静态目录 ∪ 菜单权限码 − 超管专属。"""
    return (CONFIGURABLE_PERM_CODES | await menu_perm_codes(db)) - BOSS_ONLY_PERMS


async def build_dept_perm_catalog(db: AsyncSession) -> dict[str, Any]:
    """部门权限抽屉：菜单树 + 未挂菜单的业务权限。"""
    from app.core import menus as M

    rows = await M.load_all_menus(db)
    tree, used = M.build_perm_assign_tree(
        M.build_menu_tree(rows),
        blocked_codes=set(BOSS_ONLY_PERMS),
    )
    labels = catalog_code_label_map()
    extras: list[dict[str, Any]] = []
    for g in catalog_for_api():
        items = [
            it
            for it in (g.get("items") or [])
            if it.get("code") and it["code"] not in used
        ]
        if items:
            extras.append({"group": g["group"], "label": g["label"], "items": items})
    return {
        "menu_tree": tree,
        "extra_groups": extras,
        "code_labels": labels,
        # 兼容旧前端
        "groups": catalog_for_api(),
    }


def permissions_for_kind_fallback(*, op_role: str, dept_kind: str | None) -> set[str]:
    """无部门授权表配置时的 kind 硬编码兜底。"""
    if op_role == OP_BOSS:
        return set(_BOSS)
    if op_role == OP_STAFF:
        return set(_STAFF_DEFAULT)
    if op_role != OP_MANAGER:
        return set()

    kind = (dept_kind or "").strip().lower()
    if kind == KIND_HR:
        return set(_HR_ACCOUNT)
    if kind == KIND_SALES:
        return set(_SALES_BIZ)
    if kind == KIND_OPS_ASSISTANT:
        return set(_OPS_ACTIVITY)
    return {PERM_ORG_ROSTER}


def permissions_for(*, op_role: str, dept_kind: str | None, is_desktop_admin: bool) -> set[str]:
    """兼容旧调用：同步 kind 矩阵（不含表解析）。"""
    if is_desktop_admin or op_role == OP_BOSS:
        return set(_BOSS)
    return permissions_for_kind_fallback(op_role=op_role, dept_kind=dept_kind)


def has_perm(perms: Iterable[str], code: str) -> bool:
    return code in set(perms)


def kind_default_perms(kind: str | None, op_role: str) -> set[str]:
    return permissions_for_kind_fallback(op_role=op_role, dept_kind=kind)


async def _load_role_rows(
    db: AsyncSession, department_id: int, op_role: str
) -> list[str]:
    from app.models import OpDeptRolePerm

    rows = (
        await db.execute(
            select(OpDeptRolePerm.perm_code).where(
                OpDeptRolePerm.department_id == int(department_id),
                OpDeptRolePerm.op_role == op_role,
            )
        )
    ).all()
    return [str(r[0]) for r in rows]


def _codes_from_rows(raw_codes: list[str]) -> set[str] | None:
    """None = 未配置；set = 已配置（可为空）。"""
    if not raw_codes:
        return None
    return {c for c in raw_codes if c and c != PERM_CONFIGURED_MARKER}


async def load_dept_role_perms(
    db: AsyncSession, department_id: int, op_role: str
) -> tuple[set[str] | None, int | None]:
    """返回 (perms_or_None, source_dept_id)。None 表示该档未配置（需继续继承）。"""
    from app.models import OpDepartment

    cur: int | None = int(department_id)
    seen: set[int] = set()
    while cur and cur not in seen:
        seen.add(cur)
        rows = await _load_role_rows(db, cur, op_role)
        parsed = _codes_from_rows(rows)
        if parsed is not None:
            return parsed, cur
        dept = await db.get(OpDepartment, cur)
        if not dept:
            break
        cur = int(dept.parent_id) if dept.parent_id is not None else None
    return None, None


async def resolve_user_permissions(
    db: AsyncSession,
    *,
    op_role: str,
    dept_id: int | None,
    dept_kind: str | None,
    is_desktop_admin: bool,
) -> set[str]:
    if is_desktop_admin or op_role == OP_BOSS:
        return set(_BOSS)
    if op_role not in (OP_MANAGER, OP_STAFF):
        return set()
    if not dept_id:
        return permissions_for_kind_fallback(op_role=op_role, dept_kind=dept_kind)

    perms, _src = await load_dept_role_perms(db, int(dept_id), op_role)
    if perms is not None:
        # 部门配置不得带出 boss_only；菜单新增的权限码同样放行
        return {c for c in perms if c not in BOSS_ONLY_PERMS and c != PERM_CONFIGURED_MARKER}
    return permissions_for_kind_fallback(op_role=op_role, dept_kind=dept_kind)


async def ensure_dept_role_perms_table(db: AsyncSession) -> None:
    """CREATE TABLE IF NOT EXISTS。"""
    from sqlalchemy import text

    await db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS op_dept_role_perms (
              id INT NOT NULL AUTO_INCREMENT,
              department_id INT NOT NULL,
              op_role VARCHAR(20) NOT NULL,
              perm_code VARCHAR(64) NOT NULL,
              updated_at DATETIME NOT NULL,
              PRIMARY KEY (id),
              UNIQUE KEY uq_op_dept_role_perms_dept_role_code (department_id, op_role, perm_code),
              KEY ix_op_dept_role_perms_department_id (department_id),
              CONSTRAINT fk_op_dept_role_perms_dept
                FOREIGN KEY (department_id) REFERENCES op_departments (id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
    )
    await db.commit()


async def seed_dept_role_perms_from_kinds(db: AsyncSession) -> dict[str, int]:
    """对尚无配置且自身声明了 kind 的部门，按 kind 灌入默认权限。

    kind 为空的子部门不灌，便于向上继承父级菜单。
    """
    from app.models import OpDepartment, OpDeptRolePerm

    depts = (
        await db.execute(select(OpDepartment).order_by(OpDepartment.id))
    ).scalars().all()
    now = datetime.datetime.now()
    seeded_manager = 0
    seeded_staff = 0

    for d in depts:
        did = int(d.id)
        kind = (d.kind or "").strip().lower() or None
        if not kind:
            continue
        for role in (OP_MANAGER, OP_STAFF):
            existing = await _load_role_rows(db, did, role)
            if existing:
                continue
            defaults = kind_default_perms(kind, role)
            db.add(
                OpDeptRolePerm(
                    department_id=did,
                    op_role=role,
                    perm_code=PERM_CONFIGURED_MARKER,
                    updated_at=now,
                )
            )
            for code in sorted(defaults):
                if code in BOSS_ONLY_PERMS:
                    continue
                db.add(
                    OpDeptRolePerm(
                        department_id=did,
                        op_role=role,
                        perm_code=code,
                        updated_at=now,
                    )
                )
            if role == OP_MANAGER:
                seeded_manager += 1
            else:
                seeded_staff += 1

    await db.commit()
    return {"seeded_manager_depts": seeded_manager, "seeded_staff_depts": seeded_staff}


async def replace_dept_role_perms(
    db: AsyncSession,
    department_id: int,
    *,
    manager: Iterable[str],
    staff: Iterable[str],
    allowed_codes: set[str] | None = None,
) -> None:
    from app.models import OpDeptRolePerm
    from sqlalchemy import delete as sa_delete

    did = int(department_id)
    await db.execute(sa_delete(OpDeptRolePerm).where(OpDeptRolePerm.department_id == did))
    now = datetime.datetime.now()
    allow = allowed_codes if allowed_codes is not None else set(CONFIGURABLE_PERM_CODES)

    def _add(role: str, codes: Iterable[str]) -> None:
        clean = sorted(
            {
                str(c).strip()
                for c in codes
                if str(c).strip() and str(c).strip() in allow and str(c).strip() not in BOSS_ONLY_PERMS
            }
        )
        db.add(
            OpDeptRolePerm(
                department_id=did,
                op_role=role,
                perm_code=PERM_CONFIGURED_MARKER,
                updated_at=now,
            )
        )
        for code in clean:
            db.add(
                OpDeptRolePerm(
                    department_id=did,
                    op_role=role,
                    perm_code=code,
                    updated_at=now,
                )
            )

    _add(OP_MANAGER, manager)
    _add(OP_STAFF, staff)
    await db.commit()
