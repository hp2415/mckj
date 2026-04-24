"""
提示词管理后端 API：管理平台/sqladmin 之外可被前端直接调用的能力。

所有端点都要求管理员身份（复用 api.auth.get_admin_user）。

功能：
- GET    /api/prompt/scenarios                  列出全部场景
- POST   /api/prompt/scenarios                  新增场景
- PATCH  /api/prompt/scenarios/{id}             修改场景元信息（name/description/enabled/tools_enabled）
- GET    /api/prompt/scenarios/{key}/versions   列出某场景的所有版本（按 version desc）
- POST   /api/prompt/scenarios/{key}/versions   基于当前 published 复制一份 draft（或传入 template/doc_refs/params 覆盖）
- PATCH  /api/prompt/versions/{id}              修改 draft 版本的 template/doc_refs/params/notes
- POST   /api/prompt/versions/{id}/publish      发布该版本，上一 published 自动 archived
- POST   /api/prompt/versions/{id}/rollback     把历史（archived/draft 均可）重新置为 published
- POST   /api/prompt/versions/{id}/preview      传入示例 ctx/query，返回最终 system 文本和 messages

- GET    /api/prompt/docs                       列出文档
- POST   /api/prompt/docs                       新增文档
- GET    /api/prompt/docs/{key}/versions        列出某文档的版本
- POST   /api/prompt/docs/{key}/versions        新增 draft（传 content）
- POST   /api/prompt/doc-versions/{id}/publish  发布该文档版本
- POST   /api/prompt/doc-versions/{id}/rollback 重置某历史版本为 published

所有写入动作都会写 prompt_audit_log 并触发 PromptStore 缓存失效。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, update

from core.logger import logger
from database import get_db
from api.auth import get_admin_user
from models import (
    User,
    PromptScenario,
    PromptVersion,
    PromptDoc,
    PromptDocVersion,
    PromptAuditLog,
)

from ai.prompt_store import get_prompt_store
from ai.prompt_models import (
    PromptTemplate,
    template_from_json,
    doc_refs_from_json,
    params_from_json,
)
from ai.prompt_renderer import render_system, build_messages


router = APIRouter(prefix="/api/prompt", tags=["PromptAdmin"])


# ---------- Pydantic I/O ----------

class ScenarioCreate(BaseModel):
    scenario_key: str
    name: str
    description: Optional[str] = None
    enabled: bool = True
    tools_enabled: bool = True


class ScenarioPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    tools_enabled: Optional[bool] = None


class VersionCreate(BaseModel):
    # 不传则"克隆当前 published"
    template_json: Optional[dict] = None
    doc_refs_json: Optional[list] = None
    params_json: Optional[dict] = None
    notes: Optional[str] = None


class VersionPatch(BaseModel):
    template_json: Optional[dict] = None
    doc_refs_json: Optional[list] = None
    params_json: Optional[dict] = None
    notes: Optional[str] = None


class DocCreate(BaseModel):
    doc_key: str
    name: str
    description: Optional[str] = None


class DocVersionCreate(BaseModel):
    content: str
    source_filename: Optional[str] = None


class PreviewRequest(BaseModel):
    ctx: dict = {}
    query: str = ""
    history: list[dict] = []


# ---------- 辅助 ----------

async def _audit(
    db: AsyncSession,
    *,
    actor: User,
    action: str,
    target_type: str,
    target_id: Optional[int],
    payload: Optional[dict] = None,
) -> None:
    try:
        db.add(PromptAuditLog(
            actor_id=actor.id if actor else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            payload_json=payload or {},
        ))
        await db.flush()
    except Exception as e:
        logger.warning("PromptAuditLog 写入失败（忽略，不影响主流程）: {}", e)


def _ok(data: Any = None, message: str = "ok") -> dict:
    return {"code": 200, "message": message, "data": data}


async def _get_scenario(db: AsyncSession, key_or_id: str | int) -> PromptScenario:
    if isinstance(key_or_id, int) or (isinstance(key_or_id, str) and key_or_id.isdigit()):
        stmt = select(PromptScenario).where(PromptScenario.id == int(key_or_id))
    else:
        stmt = select(PromptScenario).where(PromptScenario.scenario_key == str(key_or_id))
    res = await db.execute(stmt)
    sc = res.scalars().first()
    if not sc:
        raise HTTPException(status_code=404, detail="场景不存在")
    return sc


async def _next_version(db: AsyncSession, scenario_id: int) -> int:
    res = await db.execute(
        select(PromptVersion.version)
        .where(PromptVersion.scenario_id == scenario_id)
        .order_by(desc(PromptVersion.version))
        .limit(1)
    )
    last = res.scalars().first()
    return (last or 0) + 1


async def _next_doc_version(db: AsyncSession, doc_id: int) -> int:
    res = await db.execute(
        select(PromptDocVersion.version)
        .where(PromptDocVersion.doc_id == doc_id)
        .order_by(desc(PromptDocVersion.version))
        .limit(1)
    )
    last = res.scalars().first()
    return (last or 0) + 1


# ---------- 场景 ----------

@router.get("/scenarios")
async def list_scenarios(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    res = await db.execute(select(PromptScenario).order_by(PromptScenario.id.asc()))
    items = []
    for s in res.scalars().all():
        items.append({
            "id": s.id,
            "scenario_key": s.scenario_key,
            "name": s.name,
            "description": s.description,
            "enabled": bool(s.enabled),
            "tools_enabled": bool(s.tools_enabled),
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        })
    return _ok(items)


@router.post("/scenarios")
async def create_scenario(
    body: ScenarioCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    existing = (await db.execute(
        select(PromptScenario).where(PromptScenario.scenario_key == body.scenario_key)
    )).scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="场景 key 已存在")
    sc = PromptScenario(
        scenario_key=body.scenario_key,
        name=body.name,
        description=body.description,
        enabled=body.enabled,
        tools_enabled=body.tools_enabled,
    )
    db.add(sc)
    await db.flush()
    await _audit(db, actor=admin, action="scenario.create", target_type="scenario", target_id=sc.id, payload=body.model_dump())
    await db.commit()
    return _ok({"id": sc.id, "scenario_key": sc.scenario_key}, "已创建")


@router.patch("/scenarios/{id_or_key}")
async def patch_scenario(
    id_or_key: str,
    body: ScenarioPatch,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    sc = await _get_scenario(db, id_or_key)
    changed = {}
    if body.name is not None:
        sc.name = body.name; changed["name"] = body.name
    if body.description is not None:
        sc.description = body.description; changed["description"] = body.description
    if body.enabled is not None:
        sc.enabled = body.enabled; changed["enabled"] = body.enabled
    if body.tools_enabled is not None:
        sc.tools_enabled = body.tools_enabled; changed["tools_enabled"] = body.tools_enabled
    await _audit(db, actor=admin, action="scenario.patch", target_type="scenario", target_id=sc.id, payload=changed)
    await db.commit()
    await get_prompt_store().invalidate_scenario(sc.scenario_key)
    return _ok(changed, "已更新")


# ---------- 版本 ----------

@router.get("/scenarios/{id_or_key}/versions")
async def list_versions(
    id_or_key: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    sc = await _get_scenario(db, id_or_key)
    res = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.scenario_id == sc.id)
        .order_by(desc(PromptVersion.version))
    )
    items = []
    for v in res.scalars().all():
        items.append({
            "id": v.id,
            "version": v.version,
            "status": v.status,
            "template_json": v.template_json,
            "doc_refs_json": v.doc_refs_json,
            "params_json": v.params_json,
            "notes": v.notes,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "published_at": v.published_at.isoformat() if v.published_at else None,
        })
    return _ok({"scenario": {"id": sc.id, "key": sc.scenario_key}, "versions": items})


@router.post("/scenarios/{id_or_key}/versions")
async def create_version(
    id_or_key: str,
    body: VersionCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    sc = await _get_scenario(db, id_or_key)

    # 不传具体内容时，克隆当前 published
    if body.template_json is None and body.doc_refs_json is None and body.params_json is None:
        cur = (await db.execute(
            select(PromptVersion)
            .where(PromptVersion.scenario_id == sc.id)
            .where(PromptVersion.status == "published")
            .order_by(desc(PromptVersion.version))
            .limit(1)
        )).scalars().first()
        template_json = (cur.template_json if cur else {"system": ""})
        doc_refs_json = (cur.doc_refs_json if cur else [])
        params_json = (cur.params_json if cur else None)
    else:
        template_json = body.template_json or {"system": ""}
        doc_refs_json = body.doc_refs_json or []
        params_json = body.params_json

    # 校验 template 基本形态
    if not isinstance(template_json, dict) or not template_json.get("system"):
        raise HTTPException(status_code=400, detail="template_json.system 不能为空")
    if doc_refs_json is not None and not isinstance(doc_refs_json, list):
        raise HTTPException(status_code=400, detail="doc_refs_json 必须是数组")

    next_v = await _next_version(db, sc.id)
    v = PromptVersion(
        scenario_id=sc.id,
        version=next_v,
        status="draft",
        template_json=template_json,
        doc_refs_json=doc_refs_json,
        params_json=params_json,
        notes=body.notes,
        created_by=admin.id,
    )
    db.add(v)
    await db.flush()
    await _audit(db, actor=admin, action="version.create", target_type="version", target_id=v.id, payload={"scenario_id": sc.id, "version": next_v})
    await db.commit()
    return _ok({"id": v.id, "version": v.version, "status": v.status}, "已创建草稿")


@router.patch("/versions/{vid}")
async def patch_version(
    vid: int,
    body: VersionPatch,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    v = (await db.execute(select(PromptVersion).where(PromptVersion.id == vid))).scalars().first()
    if not v:
        raise HTTPException(status_code=404, detail="版本不存在")
    if v.status != "draft":
        raise HTTPException(status_code=400, detail="只允许修改 draft 状态的版本；请先基于当前版本创建新 draft")
    changed: dict[str, Any] = {}
    if body.template_json is not None:
        if not isinstance(body.template_json, dict) or not body.template_json.get("system"):
            raise HTTPException(status_code=400, detail="template_json.system 不能为空")
        v.template_json = body.template_json; changed["template_json"] = True
    if body.doc_refs_json is not None:
        if not isinstance(body.doc_refs_json, list):
            raise HTTPException(status_code=400, detail="doc_refs_json 必须是数组")
        v.doc_refs_json = body.doc_refs_json; changed["doc_refs_json"] = True
    if body.params_json is not None:
        v.params_json = body.params_json; changed["params_json"] = True
    if body.notes is not None:
        v.notes = body.notes; changed["notes"] = True
    await _audit(db, actor=admin, action="version.patch", target_type="version", target_id=v.id, payload=changed)
    await db.commit()
    return _ok({"id": v.id, "version": v.version}, "已更新")


@router.post("/versions/{vid}/publish")
async def publish_version(
    vid: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    v = (await db.execute(select(PromptVersion).where(PromptVersion.id == vid))).scalars().first()
    if not v:
        raise HTTPException(status_code=404, detail="版本不存在")
    sc = (await db.execute(select(PromptScenario).where(PromptScenario.id == v.scenario_id))).scalars().first()
    if not sc:
        raise HTTPException(status_code=404, detail="场景不存在")

    # 归档同场景当前 published
    await db.execute(
        update(PromptVersion)
        .where(PromptVersion.scenario_id == v.scenario_id)
        .where(PromptVersion.status == "published")
        .values(status="archived")
    )
    v.status = "published"
    v.published_at = datetime.now()
    await _audit(db, actor=admin, action="version.publish", target_type="version", target_id=v.id, payload={"scenario_id": v.scenario_id, "version": v.version})
    await db.commit()
    await get_prompt_store().invalidate_scenario(sc.scenario_key)
    return _ok({"id": v.id, "version": v.version, "status": v.status}, "已发布")


@router.post("/versions/{vid}/rollback")
async def rollback_version(
    vid: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    v = (await db.execute(select(PromptVersion).where(PromptVersion.id == vid))).scalars().first()
    if not v:
        raise HTTPException(status_code=404, detail="版本不存在")
    sc = (await db.execute(select(PromptScenario).where(PromptScenario.id == v.scenario_id))).scalars().first()
    if not sc:
        raise HTTPException(status_code=404, detail="场景不存在")

    # 回滚：把当前 published 归档，再把本版本置为 published
    await db.execute(
        update(PromptVersion)
        .where(PromptVersion.scenario_id == v.scenario_id)
        .where(PromptVersion.status == "published")
        .values(status="archived")
    )
    v.status = "published"
    v.published_at = datetime.now()
    await _audit(db, actor=admin, action="version.rollback", target_type="version", target_id=v.id, payload={"scenario_id": v.scenario_id, "version": v.version})
    await db.commit()
    await get_prompt_store().invalidate_scenario(sc.scenario_key)
    return _ok({"id": v.id, "version": v.version, "status": v.status}, "已回滚至该版本")


@router.post("/versions/{vid}/preview")
async def preview_version(
    vid: int,
    body: PreviewRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """离线渲染指定版本（不走开关/缓存），返回最终 system 文本与 messages，便于回归。"""
    v = (await db.execute(select(PromptVersion).where(PromptVersion.id == vid))).scalars().first()
    if not v:
        raise HTTPException(status_code=404, detail="版本不存在")

    template = template_from_json(v.template_json)
    doc_refs = doc_refs_from_json(v.doc_refs_json)

    # 按 doc_refs 拉取文档内容（取 doc_version_id 指定或当前 published）
    docs_map: dict[str, tuple[str, Optional[int]]] = {}
    for spec in doc_refs:
        d = (await db.execute(select(PromptDoc).where(PromptDoc.doc_key == spec.doc_key))).scalars().first()
        if not d:
            docs_map[spec.doc_key] = ("", None)
            continue
        if spec.doc_version_id:
            dv = (await db.execute(select(PromptDocVersion).where(PromptDocVersion.id == spec.doc_version_id))).scalars().first()
        else:
            dv = (await db.execute(
                select(PromptDocVersion)
                .where(PromptDocVersion.doc_id == d.id)
                .where(PromptDocVersion.status == "published")
                .order_by(desc(PromptDocVersion.version))
                .limit(1)
            )).scalars().first()
        if dv:
            docs_map[spec.doc_key] = (dv.content or "", int(dv.version))
        else:
            docs_map[spec.doc_key] = ("", None)

    system_text = render_system(template, body.ctx or {}, docs_map, doc_refs)
    messages = build_messages(system_text, body.history or [], body.query or "")
    user_text: str | None = None
    if template.user:
        user_text = render_system(
            PromptTemplate(system=template.user),
            body.ctx or {},
            {},
            (),
        )
    payload: dict = {
        "system_text": system_text,
        "messages": messages,
        "doc_versions": {k: v2[1] for k, v2 in docs_map.items()},
        "template_len": len(system_text),
    }
    if user_text is not None:
        payload["user_text"] = user_text
        payload["profile_messages"] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]
    return _ok(payload)


# ---------- 文档 ----------

@router.get("/docs")
async def list_docs(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    res = await db.execute(select(PromptDoc).order_by(PromptDoc.id.asc()))
    items = []
    for d in res.scalars().all():
        items.append({
            "id": d.id,
            "doc_key": d.doc_key,
            "name": d.name,
            "description": d.description,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        })
    return _ok(items)


@router.post("/docs")
async def create_doc(
    body: DocCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    existing = (await db.execute(
        select(PromptDoc).where(PromptDoc.doc_key == body.doc_key)
    )).scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="文档 key 已存在")
    d = PromptDoc(doc_key=body.doc_key, name=body.name, description=body.description)
    db.add(d)
    await db.flush()
    await _audit(db, actor=admin, action="doc.create", target_type="doc", target_id=d.id, payload=body.model_dump())
    await db.commit()
    return _ok({"id": d.id, "doc_key": d.doc_key}, "已创建")


@router.get("/docs/{id_or_key}/versions")
async def list_doc_versions(
    id_or_key: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    if id_or_key.isdigit():
        stmt = select(PromptDoc).where(PromptDoc.id == int(id_or_key))
    else:
        stmt = select(PromptDoc).where(PromptDoc.doc_key == id_or_key)
    d = (await db.execute(stmt)).scalars().first()
    if not d:
        raise HTTPException(status_code=404, detail="文档不存在")
    res = await db.execute(
        select(PromptDocVersion)
        .where(PromptDocVersion.doc_id == d.id)
        .order_by(desc(PromptDocVersion.version))
    )
    items = []
    for v in res.scalars().all():
        items.append({
            "id": v.id,
            "version": v.version,
            "status": v.status,
            "source_filename": v.source_filename,
            "content_len": len(v.content or ""),
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "published_at": v.published_at.isoformat() if v.published_at else None,
        })
    return _ok({"doc": {"id": d.id, "doc_key": d.doc_key, "name": d.name}, "versions": items})


@router.post("/docs/{id_or_key}/versions")
async def create_doc_version(
    id_or_key: str,
    body: DocVersionCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    if id_or_key.isdigit():
        stmt = select(PromptDoc).where(PromptDoc.id == int(id_or_key))
    else:
        stmt = select(PromptDoc).where(PromptDoc.doc_key == id_or_key)
    d = (await db.execute(stmt)).scalars().first()
    if not d:
        raise HTTPException(status_code=404, detail="文档不存在")
    next_v = await _next_doc_version(db, d.id)
    v = PromptDocVersion(
        doc_id=d.id,
        version=next_v,
        status="draft",
        content=body.content or "",
        source_filename=body.source_filename,
        created_by=admin.id,
    )
    db.add(v)
    await db.flush()
    await _audit(db, actor=admin, action="doc_version.create", target_type="doc_version", target_id=v.id, payload={"doc_id": d.id, "version": next_v, "content_len": len(v.content)})
    await db.commit()
    return _ok({"id": v.id, "version": v.version, "status": v.status}, "已创建文档草稿")


@router.post("/doc-versions/{vid}/publish")
async def publish_doc_version(
    vid: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    v = (await db.execute(select(PromptDocVersion).where(PromptDocVersion.id == vid))).scalars().first()
    if not v:
        raise HTTPException(status_code=404, detail="文档版本不存在")
    d = (await db.execute(select(PromptDoc).where(PromptDoc.id == v.doc_id))).scalars().first()

    await db.execute(
        update(PromptDocVersion)
        .where(PromptDocVersion.doc_id == v.doc_id)
        .where(PromptDocVersion.status == "published")
        .values(status="archived")
    )
    v.status = "published"
    v.published_at = datetime.now()
    await _audit(db, actor=admin, action="doc_version.publish", target_type="doc_version", target_id=v.id, payload={"doc_id": v.doc_id, "version": v.version})
    await db.commit()
    if d:
        await get_prompt_store().invalidate_doc(d.doc_key)
    return _ok({"id": v.id, "version": v.version, "status": v.status}, "已发布文档")


@router.post("/doc-versions/{vid}/rollback")
async def rollback_doc_version(
    vid: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    v = (await db.execute(select(PromptDocVersion).where(PromptDocVersion.id == vid))).scalars().first()
    if not v:
        raise HTTPException(status_code=404, detail="文档版本不存在")
    d = (await db.execute(select(PromptDoc).where(PromptDoc.id == v.doc_id))).scalars().first()

    await db.execute(
        update(PromptDocVersion)
        .where(PromptDocVersion.doc_id == v.doc_id)
        .where(PromptDocVersion.status == "published")
        .values(status="archived")
    )
    v.status = "published"
    v.published_at = datetime.now()
    await _audit(db, actor=admin, action="doc_version.rollback", target_type="doc_version", target_id=v.id, payload={"doc_id": v.doc_id, "version": v.version})
    await db.commit()
    if d:
        await get_prompt_store().invalidate_doc(d.doc_key)
    return _ok({"id": v.id, "version": v.version, "status": v.status}, "已回滚文档")


# ---------- 缓存与健康 ----------

@router.post("/cache/invalidate")
async def invalidate_cache(
    _: User = Depends(get_admin_user),
):
    await get_prompt_store().invalidate()
    return _ok(message="已清空 PromptStore 缓存")
