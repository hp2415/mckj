"""运营后台：商品规格 ID / 成本价 / 上架状态。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions as P
from app.core.context import OpContext, require_perm
from app.core.product_cost import format_missing_sp_id_lines, sync_cost_prices_from_mibuddy
from app.core.product_sp_id import parse_mibuddy_sp_id
from app.database import get_db
from app.models import Product

router = APIRouter(prefix="/api/op/products", tags=["op-products"])


class ProductPatchBody(BaseModel):
    mibuddy_sp_id: str | None = None
    cost_price: float | None = None
    is_active: bool | None = None
    clear_sp_id: bool = False
    clear_cost_price: bool = False


class SyncCostBody(BaseModel):
    product_ids: list[int] = Field(default_factory=list)
    sp_ids: list[str] = Field(default_factory=list)


def _serialize(p: Product) -> dict[str, Any]:
    return {
        "id": int(p.id),
        "product_id": p.product_id,
        "product_name": p.product_name,
        "price": float(p.price) if p.price is not None else 0.0,
        "cost_price": float(p.cost_price) if p.cost_price is not None else None,
        "mibuddy_sp_id": (p.mibuddy_sp_id or "").strip() or None,
        "is_active": bool(p.is_active),
        "cover_img": p.cover_img,
        "product_url": p.product_url,
        "unit": p.unit,
        "supplier_name": p.supplier_name,
        "supplier_id": p.supplier_id,
        "cat1": p.category_name_one,
        "cat2": p.category_name_two,
        "cat3": p.category_name_three,
        "province": p.origin_province,
        "city": p.origin_city,
        "district": p.origin_district,
    }


def _parse_cost_input(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="成本价格式无效") from None
    if number < 0:
        raise HTTPException(status_code=400, detail="成本价不能为负")
    return number.quantize(Decimal("0.01"))


@router.get("")
async def list_products(
    keyword: str = "",
    supplier_name: str = "",
    is_active: str | None = Query(None, description="1/0/true/false，空=全部"),
    has_sp_id: str | None = Query(None, description="1=有规格ID，0=无"),
    skip: int = Query(0, ge=0, le=100_000),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    ctx: OpContext = Depends(require_perm(P.PERM_PRODUCT_SPEC_VIEW)),
):
    _ = ctx
    query = select(Product)

    kw = (keyword or "").strip()
    if kw:
        for part in [k.strip() for k in kw.replace(",", " ").replace("，", " ").split() if k.strip()]:
            pattern = f"%{part}%"
            query = query.where(
                or_(
                    Product.product_name.ilike(pattern),
                    Product.product_id.ilike(pattern),
                    Product.mibuddy_sp_id.ilike(pattern),
                    Product.supplier_name.ilike(pattern),
                )
            )

    sn = (supplier_name or "").strip()
    if sn:
        query = query.where(Product.supplier_name.ilike(f"%{sn}%"))

    active_raw = (is_active or "").strip().lower()
    if active_raw in ("1", "true", "yes", "on"):
        query = query.where(Product.is_active.is_(True))
    elif active_raw in ("0", "false", "no", "off"):
        query = query.where(Product.is_active.is_(False))

    sp_raw = (has_sp_id or "").strip().lower()
    if sp_raw in ("1", "true", "yes", "on"):
        query = query.where(Product.mibuddy_sp_id.isnot(None), Product.mibuddy_sp_id != "")
    elif sp_raw in ("0", "false", "no", "off"):
        query = query.where(or_(Product.mibuddy_sp_id.is_(None), Product.mibuddy_sp_id == ""))

    count_stmt = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0

    query = query.order_by(Product.is_active.desc(), Product.id.desc()).offset(skip).limit(limit)
    rows = list((await db.execute(query)).scalars().all())

    return {
        "code": 200,
        "message": "ok",
        "data": {
            "items": [_serialize(p) for p in rows],
            "total": int(total),
            "skip": skip,
            "limit": limit,
            "has_more": skip + len(rows) < int(total),
        },
    }


@router.post("/sync-cost")
async def sync_cost(
    body: SyncCostBody,
    db: AsyncSession = Depends(get_db),
    ctx: OpContext = Depends(require_perm(P.PERM_PRODUCT_SPEC_EDIT)),
):
    _ = ctx
    wanted: set[str] = set()
    for raw in body.sp_ids or []:
        parsed, err = parse_mibuddy_sp_id(raw)
        if err:
            raise HTTPException(status_code=400, detail=err)
        if parsed:
            wanted.add(parsed)

    pids = [int(x) for x in (body.product_ids or []) if int(x) > 0]
    if pids:
        rows = list(
            (
                await db.execute(
                    select(Product).where(
                        Product.id.in_(pids),
                        Product.mibuddy_sp_id.isnot(None),
                        Product.mibuddy_sp_id != "",
                    )
                )
            )
            .scalars()
            .all()
        )
        for p in rows:
            key = (p.mibuddy_sp_id or "").strip()
            if key:
                wanted.add(key)

    if not wanted:
        raise HTTPException(status_code=400, detail="请选择已绑定规格 ID 的商品")

    stats = await sync_cost_prices_from_mibuddy(db, sp_ids=list(wanted))
    return {
        "code": 200,
        "message": "同步完成",
        "data": {
            **stats,
            "missing_lines": format_missing_sp_id_lines(stats.get("missing_sp_ids") or [], limit=20),
        },
    }


@router.patch("/{product_id}")
async def patch_product(
    product_id: int,
    body: ProductPatchBody,
    db: AsyncSession = Depends(get_db),
    ctx: OpContext = Depends(require_perm(P.PERM_PRODUCT_SPEC_EDIT)),
):
    _ = ctx
    product = await db.get(Product, int(product_id))
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")

    old_sp = (product.mibuddy_sp_id or "").strip() or None
    sp_changed = False
    sync_stats: dict[str, Any] | None = None

    if body.clear_sp_id:
        product.mibuddy_sp_id = None
        sp_changed = old_sp is not None
    elif "mibuddy_sp_id" in body.model_fields_set:
        parsed, err = parse_mibuddy_sp_id(body.mibuddy_sp_id)
        if err:
            raise HTTPException(status_code=400, detail=err)
        product.mibuddy_sp_id = parsed
        new_sp = parsed
        sp_changed = old_sp != new_sp

    if body.clear_cost_price:
        product.cost_price = None
    elif "cost_price" in body.model_fields_set:
        product.cost_price = _parse_cost_input(body.cost_price)

    if body.is_active is not None:
        product.is_active = bool(body.is_active)

    await db.commit()
    await db.refresh(product)

    new_sp = (product.mibuddy_sp_id or "").strip() or None
    if sp_changed and new_sp:
        sync_stats = await sync_cost_prices_from_mibuddy(db, sp_ids=[new_sp])
        await db.refresh(product)

    return {
        "code": 200,
        "message": "已保存",
        "data": {
            "item": _serialize(product),
            "sp_changed": sp_changed,
            "sync": sync_stats,
            "sync_missing_lines": format_missing_sp_id_lines(
                (sync_stats or {}).get("missing_sp_ids") or [], limit=10
            )
            if sync_stats
            else [],
        },
    }
