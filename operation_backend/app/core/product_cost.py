"""按主系统规格 ID 分批查询并回写商品成本价（运营端独立实现，不 import backend）。"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product

BATCH_SIZE = 20
MIBUDDY_SUCCESS_CODE = 10000


class MibuddyConfigError(Exception):
    """MiBuddy 服务未配置或配置不完整。"""


class MibuddyApiError(Exception):
    """MiBuddy 接口返回业务错误。"""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


def _parse_cost(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return None
    else:
        text = str(value).strip().replace("￥", "").replace("¥", "").replace(",", "")
        if not text:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
    if number < 0:
        return None
    return number.quantize(Decimal("0.01"))


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _empty_sync_stats(*, errors: list[str] | None = None) -> dict[str, Any]:
    return {
        "queried": 0,
        "updated": 0,
        "not_found": 0,
        "failed_batches": 0,
        "missing_sp_ids": [],
        "errors": errors or [],
    }


def format_missing_sp_id_lines(
    missing: list[dict[str, Any]],
    *,
    limit: int = 50,
) -> list[str]:
    """把未命中规格 ID 格式化成可读行，便于对照修改。"""
    lines: list[str] = []
    for item in missing[:limit]:
        sp_id = str(item.get("sp_id") or "").strip()
        pids = [str(x) for x in (item.get("product_ids") or []) if x is not None]
        if not sp_id:
            continue
        if pids:
            shown = ",".join(pids[:10])
            extra = f" 等{len(pids)}个" if len(pids) > 10 else ""
            reason = str(item.get("reason") or "接口未返回")
            lines.append(f"{sp_id}（商品 id={shown}{extra}；{reason}）")
        else:
            reason = str(item.get("reason") or "接口未返回")
            lines.append(f"{sp_id}（{reason}）")
    overflow = len(missing) - limit
    if overflow > 0:
        lines.append(f"……还有 {overflow} 个未列出")
    return lines


def _credentials() -> tuple[str, str]:
    base = (os.getenv("MIBUDDY_BASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("MIBUDDY_API_KEY") or "").strip()
    return base, key


def _is_success_body(body: dict) -> bool:
    code = body.get("code")
    msg = str(body.get("message") or "").strip().lower()
    if code in (0, MIBUDDY_SUCCESS_CODE):
        return True
    if msg == "success":
        return True
    return False


async def _request_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    base, key = _credentials()
    if not base or not key:
        raise MibuddyConfigError(
            "MiBuddy API 未配置，请在 .env 中设置 MIBUDDY_BASE_URL 与 MIBUDDY_API_KEY"
        )

    url = f"{base}{path}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": key,
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPStatusError as e:
        raise MibuddyApiError(f"MiBuddy 请求失败: HTTP {e.response.status_code}") from e
    except httpx.RequestError as e:
        raise MibuddyApiError(f"无法连接 MiBuddy 服务: {e}") from e
    except ValueError as e:
        raise MibuddyApiError("MiBuddy 返回非 JSON 响应") from e

    if not isinstance(body, dict):
        raise MibuddyApiError("MiBuddy 响应格式异常")

    if not _is_success_body(body):
        msg = str(body.get("message") or "未知错误")
        code = body.get("code")
        biz_code = int(code) if code is not None else None
        raise MibuddyApiError(msg, code=biz_code)
    return body


async def fetch_goods_info_by_ids(ids: Sequence[str]) -> list[dict[str, Any]]:
    """通过商品规格 IDs 查询主系统商品信息（最多 20 个）。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        sp_id = str(raw or "").strip()
        if not sp_id or sp_id in seen:
            continue
        seen.add(sp_id)
        cleaned.append(sp_id)
    if not cleaned:
        return []
    if len(cleaned) > 20:
        raise MibuddyApiError("商品规格 ID 最多支持 20 个，请分批查询")

    body = await _request_json("/get_goods_info_by_ids", {"ids": ",".join(cleaned)})
    data = body.get("data")
    if data is None:
        return []
    if not isinstance(data, list):
        raise MibuddyApiError("MiBuddy 响应 data 不是列表")

    items: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        sp_id = str(row.get("sp_id") or "").strip()
        if not sp_id:
            continue
        items.append(
            {
                "sp_id": sp_id,
                "price": row.get("price"),
                "goods_id": row.get("goods_id"),
                "goods_name": row.get("goods_name"),
                "specification_name": row.get("specification_name"),
            }
        )
    return items


async def sync_cost_prices_from_mibuddy(
    db: AsyncSession,
    *,
    supplier_id: str | None = None,
    sp_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """查询已绑定规格 ID 的商品并回写 ``cost_price``。

    接口未返回对应 ``sp_id`` 或价格非法时保留旧成本，并记入 ``missing_sp_ids``。
    批次失败记入 ``failed_batches`` 并继续。
    """
    query = select(Product).where(
        Product.mibuddy_sp_id.isnot(None),
        Product.mibuddy_sp_id != "",
    )
    sid = (supplier_id or "").strip()
    if sid:
        query = query.where(Product.supplier_id == sid)
    if sp_ids is not None:
        wanted = {str(x).strip() for x in sp_ids if str(x).strip()}
        if not wanted:
            return _empty_sync_stats()
        query = query.where(Product.mibuddy_sp_id.in_(list(wanted)))

    result = await db.execute(query)
    products = list(result.scalars().all())
    by_sp: dict[str, list[Product]] = {}
    for product in products:
        key = (product.mibuddy_sp_id or "").strip()
        if not key:
            continue
        by_sp.setdefault(key, []).append(product)
    unique_ids = list(by_sp.keys())

    stats: dict[str, Any] = _empty_sync_stats()
    stats["queried"] = len(unique_ids)
    if not unique_ids:
        return stats

    missing: list[dict[str, Any]] = stats["missing_sp_ids"]
    errors: list[str] = stats["errors"]
    for batch_index, batch in enumerate(_chunk(unique_ids, BATCH_SIZE), start=1):
        try:
            items = await fetch_goods_info_by_ids(batch)
        except MibuddyConfigError as exc:
            stats["failed_batches"] += 1
            errors.append(str(exc))
            break
        except MibuddyApiError as exc:
            stats["failed_batches"] += 1
            msg = f"批次 {batch_index}：{exc}"
            if len(errors) < 20:
                errors.append(msg)
            continue
        except Exception as exc:
            stats["failed_batches"] += 1
            msg = f"批次 {batch_index}：{exc}"
            if len(errors) < 20:
                errors.append(msg)
            continue

        found: dict[str, Decimal] = {}
        returned_ids: set[str] = set()
        for item in items:
            sp_id = str(item.get("sp_id") or "").strip()
            if not sp_id:
                continue
            returned_ids.add(sp_id)
            cost = _parse_cost(item.get("price"))
            if cost is None:
                continue
            found[sp_id] = cost

        for sp_id in batch:
            cost = found.get(sp_id)
            if cost is None:
                stats["not_found"] += 1
                product_ids = [p.id for p in by_sp.get(sp_id, [])]
                reason = "无有效成本价" if sp_id in returned_ids else "接口未返回"
                missing.append(
                    {
                        "sp_id": sp_id,
                        "product_ids": product_ids,
                        "reason": reason,
                    }
                )
                continue
            for product in by_sp.get(sp_id, []):
                product.cost_price = cost
                stats["updated"] += 1

    await db.commit()
    return stats
