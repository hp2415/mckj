"""
云客开放平台：分页拉取公司微信账号 POST /open/wechat/companyAccounts，写入 sales_wechat_accounts。

签名与聊天/好友接口一致：sign = MD5(key + company + partnerId + timestamp_ms).upper()，
请求头：company, partnerId, timestamp, key, sign。

返参 data.page[] 每项含 userPhone、可选 userId，以及 data[] 微信列表（wechatId、nickname、alias、phone 等）。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from typing import Any

import httpx
from sqlalchemy import text

from core.logger import logger
from database import AsyncSessionLocal
from sync.sales_wechat_accounts import upsert_rows

CFG_PARTNER = "wechat_open_partner_id"


def _md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def _open_credentials() -> tuple[str, str, str]:
    base = (os.getenv("WECHAT_OPEN_BASE_URL") or "").strip().rstrip("/")
    company = (os.getenv("WECHAT_OPEN_COMPANY") or "").strip()
    key = (os.getenv("WECHAT_OPEN_KEY") or "").strip()
    return base, company, key


async def _cfg_get(db, key: str) -> str:
    res = await db.execute(
        text("SELECT config_value FROM system_configs WHERE config_key=:k LIMIT 1"),
        {"k": key},
    )
    row = res.first()
    return (row[0] or "").strip() if row else ""


async def resolve_partner_id(partner_override: str | None) -> str:
    if partner_override is not None and str(partner_override).strip():
        return str(partner_override).strip()
    async with AsyncSessionLocal() as db:
        cfg = await _cfg_get(db, CFG_PARTNER)
        if cfg:
            return cfg
    return (os.getenv("WECHAT_OPEN_ADMIN_PARTNER_ID") or "").strip()


def _strip_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def rows_from_company_accounts_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    """将单次接口返回 JSON 转为 sales_wechat_accounts upsert 行（不去重，由 upsert 主键覆盖）。"""
    out: list[dict[str, Any]] = []
    if not body.get("success"):
        return out
    data = body.get("data") or {}
    page_groups = data.get("page")
    if not isinstance(page_groups, list):
        return out

    for group in page_groups:
        if not isinstance(group, dict):
            continue
        user_phone = _strip_or_none(group.get("userPhone"))
        user_id = _strip_or_none(group.get("userId"))
        # 云客账号：优先员工 userId，否则用登记手机号（可能带掩码）
        account_code = user_id or user_phone
        items = group.get("data")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            wid = _strip_or_none(item.get("wechatId"))
            if not wid:
                continue
            out.append(
                {
                    "sales_wechat_id": wid,
                    "nickname": _strip_or_none(item.get("nickname")),
                    "alias_name": _strip_or_none(item.get("alias")),
                    "account_code": account_code,
                    "phone": _strip_or_none(item.get("phone")),
                }
            )
    return out


async def post_company_accounts(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    company: str,
    key: str,
    partner_id: str,
    page_index: int,
    page_size: int,
    update_time_start: str | None = None,
    update_time_end: str | None = None,
) -> dict[str, Any]:
    ts_ms = str(int(time.time() * 1000))
    sign = _md5_upper(key + company + partner_id + ts_ms)
    url = f"{base_url}/open/wechat/companyAccounts"
    headers = {
        "company": company,
        "partnerId": partner_id,
        "timestamp": ts_ms,
        "key": key,
        "sign": sign,
        "content-type": "application/json",
    }
    payload: dict[str, Any] = {
        "pageIndex": max(1, int(page_index)),
        "pageSize": min(400, max(1, int(page_size))),
    }
    if update_time_start and str(update_time_start).strip():
        payload["updateTimeStart"] = str(update_time_start).strip()
    if update_time_end and str(update_time_end).strip():
        payload["updateTimeEnd"] = str(update_time_end).strip()

    resp = await client.post(url, json=payload, headers=headers, timeout=60.0)
    resp.raise_for_status()
    return resp.json()


async def fetch_company_accounts_page(
    *,
    partner_id: str | None = None,
    page_index: int = 1,
    page_size: int = 200,
    update_time_start: str | None = None,
    update_time_end: str | None = None,
) -> dict[str, Any]:
    """拉取单页原始 JSON（供测试脚本与管理后台试跑）。"""
    base, company, key = _open_credentials()
    if not base or not company or not key:
        raise ValueError(
            "缺少环境变量：WECHAT_OPEN_BASE_URL / WECHAT_OPEN_COMPANY / WECHAT_OPEN_KEY"
        )
    p = await resolve_partner_id(partner_id)
    if not p:
        raise ValueError(
            "缺少 partnerId：请配置 system_configs.wechat_open_partner_id 或 WECHAT_OPEN_ADMIN_PARTNER_ID"
        )
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await post_company_accounts(
            client,
            base_url=base,
            company=company,
            key=key,
            partner_id=p,
            page_index=page_index,
            page_size=page_size,
            update_time_start=update_time_start,
            update_time_end=update_time_end,
        )


async def sync_from_open_api(
    *,
    partner_id: str | None = None,
    page_size: int = 200,
    update_time_start: str | None = None,
    update_time_end: str | None = None,
    sleep_between_pages: float = 1.0,
) -> dict[str, Any]:
    """
    分页拉取全部公司微信账号并 upsert 到 sales_wechat_accounts。
    source 标记为 open_api。
    """
    base, company, key = _open_credentials()
    if not base or not company or not key:
        raise ValueError(
            "缺少环境变量：WECHAT_OPEN_BASE_URL / WECHAT_OPEN_COMPANY / WECHAT_OPEN_KEY"
        )
    p = await resolve_partner_id(partner_id)
    if not p:
        raise ValueError(
            "缺少 partnerId：请配置 system_configs.wechat_open_partner_id 或 WECHAT_OPEN_ADMIN_PARTNER_ID"
        )

    all_rows: list[dict[str, Any]] = []
    page_index = 1
    total_pages = 1
    total_count = 0
    last_message = ""

    async with httpx.AsyncClient(timeout=60.0) as client:
        while page_index <= total_pages:
            body = await post_company_accounts(
                client,
                base_url=base,
                company=company,
                key=key,
                partner_id=p,
                page_index=page_index,
                page_size=page_size,
                update_time_start=update_time_start,
                update_time_end=update_time_end,
            )
            if not body.get("success"):
                last_message = str(body.get("message") or "unknown error")
                raise RuntimeError(last_message)

            data = body.get("data") or {}
            if isinstance(data, dict):
                total_count = int(data.get("totalCount") or 0)
                total_pages = max(1, int(data.get("pageCount") or 1))
            rows = rows_from_company_accounts_body(body)
            all_rows.extend(rows)
            logger.info(
                "companyAccounts page %s/%s rows_this_page=%s totalCount=%s",
                page_index,
                total_pages,
                len(rows),
                total_count,
            )
            page_index += 1
            if page_index <= total_pages and sleep_between_pages > 0:
                await asyncio.sleep(sleep_between_pages)

    stats = await upsert_rows(all_rows, source="open_api")
    stats["pages_fetched"] = total_pages
    stats["total_count_api"] = total_count
    stats["flattened_rows"] = len(all_rows)
    stats["partner_id"] = p
    return stats


async def sync_from_open_api_and_dispose(**kwargs: Any) -> dict[str, Any]:
    try:
        return await sync_from_open_api(**kwargs)
    finally:
        try:
            from database import engine

            await engine.dispose()
        except Exception:
            pass
