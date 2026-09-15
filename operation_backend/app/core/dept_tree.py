"""部门树与闭包维护。"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OpDepartment, OpDepartmentClosure, OpDepartmentMember


async def rebuild_closure_for_subtree(db: AsyncSession, dept_id: int) -> None:
    """重建以 dept_id 为根的闭包（含自身）。用于新建节点后。"""
    # 简化：全表重建对 P0 树规模可接受；改父时调用 rebuild_all_closure
    await rebuild_all_closure(db)


async def rebuild_all_closure(db: AsyncSession) -> None:
    depts = (
        await db.execute(select(OpDepartment.id, OpDepartment.parent_id))
    ).all()
    children: dict[int | None, list[int]] = {}
    ids: list[int] = []
    for did, pid in depts:
        ids.append(int(did))
        children.setdefault(pid, []).append(int(did))

    pairs: list[tuple[int, int, int]] = []

    def walk(node: int, ancestors: list[int]) -> None:
        # self
        pairs.append((node, node, 0))
        for i, anc in enumerate(ancestors):
            pairs.append((anc, node, len(ancestors) - i))
        for ch in children.get(node, []):
            walk(ch, ancestors + [node])

    for root in children.get(None, []):
        walk(root, [])
    # 也处理孤儿（parent 不在表里的异常）
    known_as_child = {c for cs in children.values() for c in cs}
    for did in ids:
        if did not in known_as_child and did not in children.get(None, []):
            walk(did, [])

    await db.execute(delete(OpDepartmentClosure))
    for a, d, depth in pairs:
        db.add(OpDepartmentClosure(ancestor_id=a, descendant_id=d, depth=depth))


async def resolve_kind(db: AsyncSession, dept_id: int | None) -> str | None:
    if not dept_id:
        return None
    cur = dept_id
    seen: set[int] = set()
    while cur and cur not in seen:
        seen.add(cur)
        row = (
            await db.execute(
                select(OpDepartment.kind, OpDepartment.parent_id).where(
                    OpDepartment.id == cur
                )
            )
        ).first()
        if not row:
            return None
        kind, parent_id = row[0], row[1]
        if kind:
            return str(kind).strip().lower()
        cur = parent_id
    return None


async def descendant_dept_ids(db: AsyncSession, dept_id: int) -> list[int]:
    rows = (
        await db.execute(
            select(OpDepartmentClosure.descendant_id).where(
                OpDepartmentClosure.ancestor_id == dept_id
            )
        )
    ).all()
    return [int(r[0]) for r in rows]


async def member_user_ids_in_depts(db: AsyncSession, dept_ids: list[int]) -> list[int]:
    if not dept_ids:
        return []
    rows = (
        await db.execute(
            select(OpDepartmentMember.user_id).where(
                OpDepartmentMember.department_id.in_(dept_ids)
            )
        )
    ).all()
    return [int(r[0]) for r in rows]


async def get_user_department_id(db: AsyncSession, user_id: int) -> int | None:
    row = (
        await db.execute(
            select(OpDepartmentMember.department_id).where(
                OpDepartmentMember.user_id == user_id
            )
        )
    ).first()
    return int(row[0]) if row else None


async def dept_path_names(db: AsyncSession, dept_id: int | None) -> list[str]:
    if not dept_id:
        return []
    # 从自身向上
    names: list[str] = []
    cur = dept_id
    seen: set[int] = set()
    while cur and cur not in seen:
        seen.add(cur)
        row = (
            await db.execute(
                select(OpDepartment.name, OpDepartment.parent_id).where(
                    OpDepartment.id == cur
                )
            )
        ).first()
        if not row:
            break
        names.append(str(row[0]))
        cur = row[1]
    names.reverse()
    return names
