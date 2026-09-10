"""画像前按订单号刷新 MiBuddy 订单流转状态（order_fupin_status）。"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from sqlalchemy import select, update

from core.logger import logger
from core.mibuddy_client import (
    MibuddyApiError,
    MibuddyConfigError,
    fetch_order_fupin_status,
)
from models import RawOrder

# 单客户画像前最多刷新的订单数（按已加载列表顺序，通常已按时间倒序）
_MAX_REFRESH = 30
_CONCURRENCY = 5

_FLOW_LABELS = {
    "confirm": "确认",
    "tosc": "供应链",
    "express": "发货",
    "invoice": "开票",
    "pay": "回款",
}


def _parse_flow_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flow_stage_text(value: int | None) -> str:
    if value is None:
        return "空"
    if value >= 100:
        return "完成"
    if value >= 50:
        return "进行中"
    if value <= 0:
        return "未开始"
    return str(value)


def format_order_flow_brief(status: dict[str, Any] | None) -> str:
    """将 confirm/tosc/express/invoice/pay 格式化为画像注入短文案。"""
    if not status:
        return ""
    parts: list[str] = []
    for key, label in _FLOW_LABELS.items():
        if key not in status:
            continue
        parts.append(f"{label}{_flow_stage_text(_parse_flow_int(status.get(key)))}")
    return "/".join(parts)


def _normalize_status_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "dddh": str(data.get("dddh") or "").strip() or None,
        "status_name": str(data.get("status_name") or "").strip() or None,
        "confirm": _parse_flow_int(data.get("confirm")),
        "tosc": _parse_flow_int(data.get("tosc")),
        "express": _parse_flow_int(data.get("express")),
        "invoice": _parse_flow_int(data.get("invoice")),
        "pay": _parse_flow_int(data.get("pay")),
    }


async def _fetch_one(dddh: str) -> tuple[str, dict[str, Any] | None]:
    try:
        raw = await fetch_order_fupin_status(dddh)
        return dddh, _normalize_status_payload(raw)
    except MibuddyConfigError:
        raise
    except MibuddyApiError as e:
        logger.warning("订单流转状态查询失败 dddh={}: {}", dddh, e)
        return dddh, None
    except Exception:
        logger.exception("订单流转状态查询异常 dddh={}", dddh)
        return dddh, None


async def _persist_status_names(dddh_to_status: dict[str, str]) -> int:
    """独立短事务按主键回写 status_name。锁冲突自动重试；失败返回 0，不抛给画像。"""
    from database import AsyncSessionLocal
    from core.db_retry import run_with_mysql_lock_retry

    if not dddh_to_status:
        return 0

    dddh_list = list(dddh_to_status.keys())

    async def _once() -> int:
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(RawOrder.id, RawOrder.dddh, RawOrder.status_name).where(
                    RawOrder.dddh.in_(dddh_list)
                )
            )
            by_status: dict[str, list[int]] = defaultdict(list)
            for oid, dddh, current in res.all():
                new_name = dddh_to_status.get(str(dddh or "").strip())
                if not new_name or (current or "") == new_name:
                    continue
                by_status[new_name].append(int(oid))
            n = 0
            for status_name, row_ids in by_status.items():
                if not row_ids:
                    continue
                await db.execute(
                    update(RawOrder)
                    .where(RawOrder.id.in_(row_ids))
                    .values(status_name=status_name)
                )
                n += len(row_ids)
            await db.commit()
            return n

    try:
        return await run_with_mysql_lock_retry(_once, what="raw_orders.status_name")
    except Exception:
        logger.exception("订单流转状态回写失败（不阻断画像）")
        return 0


async def refresh_orders_fupin_status(
    orders: list[dict[str, Any]],
    *,
    max_refresh: int = _MAX_REFRESH,
) -> list[dict[str, Any]]:
    """
    画像前：按订单号调用 MiBuddy /order_fupin_status，回写本地 status_name，
    并在订单 dict 上附加流转字段供画像上下文使用。

    回写走独立短会话并立即 commit，避免占用画像主事务/LLM 期间的行锁。
    API 或 DB 失败不阻断画像（内存 dict 仍会带上本次拉取到的状态）。
    """
    if not orders:
        return orders

    dddh_list: list[str] = []
    seen: set[str] = set()
    for o in orders:
        dddh = str(o.get("dddh") or "").strip()
        if len(dddh) < 10 or dddh in seen:
            continue
        seen.add(dddh)
        dddh_list.append(dddh)
        if len(dddh_list) >= max(1, int(max_refresh)):
            break

    if not dddh_list:
        return orders

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _guarded(dddh: str) -> tuple[str, dict[str, Any] | None]:
        async with sem:
            return await _fetch_one(dddh)

    try:
        results = await asyncio.gather(*[_guarded(d) for d in dddh_list])
    except MibuddyConfigError as e:
        logger.info("跳过订单流转状态刷新（MiBuddy 未配置）: {}", e)
        return orders

    by_dddh: dict[str, dict[str, Any]] = {
        dddh: payload for dddh, payload in results if payload
    }
    if not by_dddh:
        return orders

    dddh_to_status = {
        dddh: str(payload.get("status_name") or "").strip()
        for dddh, payload in by_dddh.items()
        if str(payload.get("status_name") or "").strip()
    }
    persisted = await _persist_status_names(dddh_to_status)

    enriched: list[dict[str, Any]] = []
    for o in orders:
        row = dict(o)
        dddh = str(row.get("dddh") or "").strip()
        payload = by_dddh.get(dddh)
        if payload:
            if payload.get("status_name"):
                row["status_name"] = payload["status_name"]
            for key in ("confirm", "tosc", "express", "invoice", "pay"):
                row[key] = payload.get(key)
            brief = format_order_flow_brief(payload)
            if brief:
                row["flow_brief"] = brief
        enriched.append(row)

    logger.info(
        "画像前订单流转状态已刷新: requested={} fetched={} persisted={}",
        len(dddh_list),
        len(by_dddh),
        persisted,
    )
    return enriched
