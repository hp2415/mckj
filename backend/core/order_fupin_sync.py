"""MiBuddy 扶贫订单增量同步（order_fupin_increment）。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.future import select

from core.logger import logger
from core.mibuddy_client import MibuddyApiError, MibuddyConfigError, fetch_order_fupin_increment
from core.system_config_store import upsert_system_config_row
from database import AsyncSessionLocal
from models import RawOrder, RawOrderItem, SystemConfig

CFG_STATUS = "order_fupin_sync_status"
CFG_LAST_MSG = "order_fupin_sync_last_message"
CFG_LAST_OK = "order_fupin_sync_last_success"
CFG_START_ID = "order_fupin_sync_start_id"

_lock = asyncio.Lock()


@dataclass
class OrderFupinSyncStats:
    start_id: int = 0
    end_id: int = 0
    api_pages: int = 0
    rows_received: int = 0
    rows_upserted: int = 0
    items_written: int = 0
    errors: list[str] = field(default_factory=list)


def _digits_phone(value: Any) -> str:
    return "".join(filter(str.isdigit, str(value or "")))


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


async def _load_start_id(db) -> int:
    res = await db.execute(select(SystemConfig).where(SystemConfig.config_key == CFG_START_ID))
    row = res.scalars().first()
    if row and (row.config_value or "").strip().isdigit():
        return max(0, int(row.config_value))
    return 0


async def _upsert_order_row(db, item: dict[str, Any]) -> tuple[int, int] | None:
    order_id = str(item.get("order_id") or "").strip()
    if not order_id:
        return None

    consignee_phone = _digits_phone(item.get("consignee_phone"))
    now = datetime.now()
    row = {
        "order_id": order_id,
        "dddh": str(item.get("dddh") or "").strip() or None,
        "store": str(item.get("store") or "").strip() or None,
        "pay_type_name": str(item.get("pay_type_name") or "").strip() or None,
        "pay_amount": _parse_decimal(item.get("pay_amount")),
        "freight": _parse_decimal(item.get("freight")),
        "status_name": str(item.get("status_name") or "").strip() or None,
        "order_time": _parse_dt(item.get("order_time")),
        "update_time": _parse_dt(item.get("update_time")),
        "remark": str(item.get("remark") or "").strip() or None,
        "consignee": str(item.get("consignee") or "").strip() or None,
        "consignee_phone": consignee_phone or None,
        "consignee_address": str(item.get("consignee_address") or "").strip() or None,
        "buyer_id": str(item.get("buyer_id") or "").strip() or None,
        "buyer_name": str(item.get("buyer_name") or "").strip() or None,
        "buyer_phone": None,
        "purchase_type": 0,
        "wechat_idx": _optional_str(item.get("wechat_idx")),
        "staff_uuid": _optional_str(item.get("staff_uuid")),
        "raw_json": json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str),
        "imported_at": now,
    }

    stmt = mysql_insert(RawOrder).values(row)
    stmt = stmt.on_duplicate_key_update(
        dddh=stmt.inserted.dddh,
        store=stmt.inserted.store,
        pay_type_name=stmt.inserted.pay_type_name,
        pay_amount=stmt.inserted.pay_amount,
        freight=stmt.inserted.freight,
        status_name=stmt.inserted.status_name,
        order_time=stmt.inserted.order_time,
        update_time=stmt.inserted.update_time,
        remark=stmt.inserted.remark,
        consignee=stmt.inserted.consignee,
        consignee_phone=stmt.inserted.consignee_phone,
        consignee_address=stmt.inserted.consignee_address,
        buyer_id=stmt.inserted.buyer_id,
        buyer_name=stmt.inserted.buyer_name,
        wechat_idx=stmt.inserted.wechat_idx,
        staff_uuid=stmt.inserted.staff_uuid,
        raw_json=stmt.inserted.raw_json,
    )
    await db.execute(stmt)

    res = await db.execute(select(RawOrder.id).where(RawOrder.order_id == order_id))
    raw_order_id = res.scalar_one()

    await db.execute(delete(RawOrderItem).where(RawOrderItem.raw_order_id == raw_order_id))
    items_written = 0
    for gi in item.get("goodsInfo") or []:
        if not isinstance(gi, dict):
            continue
        product_name = str(gi.get("product_name") or "").strip()
        if not product_name:
            continue
        db.add(
            RawOrderItem(
                raw_order_id=raw_order_id,
                uuid=str(gi.get("uuid") or "").strip() or None,
                product_name=product_name,
                number=_parse_int(gi.get("number")),
                pay_price=_parse_decimal(gi.get("pay_price")),
                pay_money=_parse_decimal(gi.get("pay_money")),
                sku_id=str(gi.get("sku_id") or "").strip() or None,
            )
        )
        items_written += 1

    mibuddy_id = _parse_int(item.get("id")) or 0
    return mibuddy_id, items_written


async def sync_order_fupin_increment(
    *,
    start_id: int | None = None,
    page_size: int = 100,
    max_pages: int | None = None,
    persist_cursor: bool = True,
) -> OrderFupinSyncStats:
    """从 MiBuddy 增量同步订单到 raw_orders / raw_order_items。"""
    stats = OrderFupinSyncStats()

    async with _lock:
        async with AsyncSessionLocal() as db:
            cursor = max(0, int(start_id)) if start_id is not None else await _load_start_id(db)
            stats.start_id = cursor
            await upsert_system_config_row(
                db, config_key=CFG_STATUS, config_value="running", config_group="sync"
            )
            await db.commit()

        try:
            page = 1
            page_size = max(1, min(100, int(page_size)))
            max_id = cursor
            order_trigger_items: list[dict[str, Any]] = []

            while True:
                if max_pages is not None and stats.api_pages >= max_pages:
                    break

                data = await fetch_order_fupin_increment(cursor, page=page, page_size=page_size)
                stats.api_pages += 1
                items = [x for x in (data.get("list") or []) if isinstance(x, dict)]
                stats.rows_received += len(items)

                if items:
                    async with AsyncSessionLocal() as db:
                        for item in items:
                            order_trigger_items.append(
                                {
                                    "wechat_idx": item.get("wechat_idx"),
                                    "consignee_phone": _digits_phone(item.get("consignee_phone")),
                                }
                            )
                            result = await _upsert_order_row(db, item)
                            if result is None:
                                continue
                            mibuddy_id, item_count = result
                            stats.rows_upserted += 1
                            stats.items_written += item_count
                            if mibuddy_id > max_id:
                                max_id = mibuddy_id
                        await db.commit()

                pagination = data.get("pagination") or {}
                total = int(pagination.get("total") or 0)
                if not items or page * page_size >= total:
                    break
                page += 1

            stats.end_id = max_id
            if persist_cursor and max_id > cursor:
                async with AsyncSessionLocal() as db:
                    await upsert_system_config_row(
                        db,
                        config_key=CFG_START_ID,
                        config_value=str(max_id),
                        config_group="sync",
                    )
                    await db.commit()

            msg = (
                f"订单增量同步完成 start_id={stats.start_id} end_id={stats.end_id}："
                f"API {stats.rows_received} 条，入库 {stats.rows_upserted} 条，"
                f"商品行 {stats.items_written} 条，共 {stats.api_pages} 页"
            )
            async with AsyncSessionLocal() as db:
                await upsert_system_config_row(
                    db, config_key=CFG_STATUS, config_value="success", config_group="sync"
                )
                await upsert_system_config_row(
                    db, config_key=CFG_LAST_MSG, config_value=msg[:2000], config_group="sync"
                )
                await upsert_system_config_row(
                    db,
                    config_key=CFG_LAST_OK,
                    config_value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    config_group="sync",
                )
                await db.commit()
            logger.info(msg)
            if order_trigger_items:
                try:
                    from ai.profile_triggers import resolve_pairs_from_order_items, safe_trigger_profile_for_pairs

                    async with AsyncSessionLocal() as db:
                        order_pairs = await resolve_pairs_from_order_items(db, order_trigger_items)
                    if order_pairs:
                        await safe_trigger_profile_for_pairs(order_pairs, reason="new_order")
                        stats.profile_triggered = len(order_pairs)
                except Exception as e:
                    logger.warning("订单同步后事件画像触发失败: {}", e)

        except (MibuddyConfigError, MibuddyApiError, Exception) as e:
            stats.errors.append(str(e))
            err_msg = f"订单增量同步失败：{e}"
            async with AsyncSessionLocal() as db:
                await upsert_system_config_row(
                    db, config_key=CFG_STATUS, config_value="error", config_group="sync"
                )
                await upsert_system_config_row(
                    db, config_key=CFG_LAST_MSG, config_value=err_msg[:2000], config_group="sync"
                )
                await db.commit()
            logger.exception(err_msg)
            raise

    return stats


async def scheduled_order_fupin_increment() -> None:
    """定时任务：增量同步扶贫订单。"""
    try:
        await sync_order_fupin_increment()
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.exception("[APScheduler] 订单增量同步失败: %s", e)
