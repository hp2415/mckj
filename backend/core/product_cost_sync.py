"""按主系统规格 ID 分批查询并回写商品成本价。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import logger
from core.mibuddy_client import MibuddyApiError, MibuddyConfigError, fetch_goods_info_by_ids
from core.system_config_store import upsert_system_config_row
from models import Product

BATCH_SIZE = 20


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


async def sync_cost_prices_from_mibuddy(
    db: AsyncSession,
    *,
    supplier_id: str | None = None,
    sp_ids: Sequence[str] | None = None,
    persist_status: bool = True,
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
        if persist_status:
            await _persist_status(db, stats)
        return stats

    missing: list[dict[str, Any]] = stats["missing_sp_ids"]
    errors: list[str] = stats["errors"]
    for batch_index, batch in enumerate(_chunk(unique_ids, BATCH_SIZE), start=1):
        try:
            items = await fetch_goods_info_by_ids(batch)
        except MibuddyConfigError as exc:
            stats["failed_batches"] += 1
            errors.append(str(exc))
            logger.warning("成本价同步中止：{}", exc)
            break
        except MibuddyApiError as exc:
            stats["failed_batches"] += 1
            msg = f"批次 {batch_index}：{exc}"
            if len(errors) < 20:
                errors.append(msg)
            logger.warning("成本价同步批次失败 {}", msg)
            continue
        except Exception as exc:
            stats["failed_batches"] += 1
            msg = f"批次 {batch_index}：{exc}"
            if len(errors) < 20:
                errors.append(msg)
            logger.exception("成本价同步批次异常 {}", msg)
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
    if persist_status:
        await _persist_status(db, stats)
    return stats


async def _persist_status(db: AsyncSession, stats: dict[str, Any]) -> None:
    parts = [
        f"查询 {stats['queried']} 个规格 ID",
        f"更新成本 {stats['updated']} 条",
        f"未命中 {stats['not_found']} 个",
        f"失败批次 {stats['failed_batches']} 个",
    ]
    missing_lines = format_missing_sp_id_lines(stats.get("missing_sp_ids") or [], limit=30)
    if missing_lines:
        parts.append("主系统未命中的规格 ID：\n- " + "\n- ".join(missing_lines))
    detail_errors = stats.get("errors") or []
    if detail_errors:
        parts.append("明细：\n- " + "\n- ".join(detail_errors[:20]))
    message = "；".join(parts[:4])
    extras = parts[4:]
    if extras:
        message = message + "。" + "。".join(extras)
    try:
        await upsert_system_config_row(
            db,
            config_key="cost_sync_last_message",
            config_value=message,
            config_group="sync",
            description="最近一次主系统成本价同步结果",
        )
        if not stats["failed_batches"]:
            await upsert_system_config_row(
                db,
                config_key="cost_sync_last_success",
                config_value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                config_group="sync",
                description="最近一次主系统成本价同步成功时间",
            )
        await db.commit()
    except Exception:
        logger.exception("写入成本价同步状态失败")
