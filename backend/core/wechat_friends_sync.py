"""
云客开放平台：按自然日增量同步微信好友/群到 raw_customers + raw_customer_sales_wechats。

设计要点：
- 接口限频 5 秒/次：同 partner、同 type 的请求之间 asyncio.sleep(5)。
- 「只同步这一天」：以 Asia/Shanghai 自然日 [00:00:00, 23:59:59] 为窗口，按 queryMode 取每条记录的参考时间，
  仅 upsert 落在窗口内的记录；分页仍走开放平台 queryEndTime+1 秒，直到游标越过当日末尾或本批无数据。
- partnerId：优先调用参数，其次 system_configs.wechat_open_partner_id，最后环境变量 WECHAT_OPEN_ADMIN_PARTNER_ID。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text
from sqlalchemy.future import select

from core.logger import logger
from database import AsyncSessionLocal
from models import RawCustomer, RawCustomerSalesWechat

TZ_SH = ZoneInfo("Asia/Shanghai")

_wechat_sync_lock = asyncio.Lock()

CFG_TARGET_DAY = "wechat_friends_sync_target_day"
CFG_PARTNER = "wechat_open_partner_id"
CFG_QUERY_MODE = "wechat_friends_query_mode"
STATUS_KEY = "wechat_friends_sync_status"
MSG_KEY = "wechat_friends_sync_last_message"
SUCCESS_KEY = "wechat_friends_sync_last_success"


async def read_wechat_sync_ui_settings(db) -> dict[str, str]:
    """供管理后台页面读取当前目标日、partner 覆盖与上次状态。"""
    keys = [CFG_TARGET_DAY, CFG_PARTNER, STATUS_KEY, MSG_KEY, SUCCESS_KEY, CFG_QUERY_MODE]
    out = {k: "" for k in keys}
    for k in keys:
        out[k] = await _cfg_get(db, k)
    return out


async def persist_wechat_sync_prefs(db, *, calendar_day: str, partner_field: str) -> None:
    """写入目标自然日与开放平台 partner 覆盖（可空串表示回退环境变量）。"""
    await _cfg_set(db, CFG_TARGET_DAY, calendar_day.strip(), "sync")
    await _cfg_set(db, CFG_PARTNER, (partner_field or "").strip(), "sync")
    await db.commit()


def _md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def _digits_phone(s: str | None) -> str | None:
    if not s:
        return None
    d = "".join(ch for ch in str(s) if ch.isdigit())
    return d or None


def _parse_dt_loose(v: Any) -> datetime | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{10,}", s):
        ms = int(s)
        if ms >= 10**15:  # unlikely
            ms //= 1000
        if ms >= 10**12:  # ms
            ms //= 1000
        try:
            return datetime.fromtimestamp(ms, tz=TZ_SH).replace(tzinfo=None)
        except (OSError, ValueError):
            return None
    if len(s) >= 19:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return None


def _day_bounds_naive(calendar_day: str) -> tuple[datetime, datetime]:
    d = datetime.strptime(calendar_day.strip(), "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, 0, 0, 0)
    end = datetime(d.year, d.month, d.day, 23, 59, 59)
    return start, end


def _now_sh_naive() -> datetime:
    return datetime.now(tz=TZ_SH).replace(tzinfo=None)


def _api_start_time_cap(now_naive: datetime) -> datetime:
    """开放平台：startTime 不能超过当前时间减去约 5 秒。"""
    return now_naive - timedelta(seconds=6)


def _item_reference_time(item: dict[str, Any], query_mode: str) -> datetime | None:
    if query_mode == "createTime":
        return _parse_dt_loose(item.get("createTime")) or _parse_dt_loose(item.get("updateTime"))
    return _parse_dt_loose(item.get("updateTime")) or _parse_dt_loose(item.get("createTime"))


def _next_start_time(query_end_time: str | None) -> str | None:
    dt = _parse_dt_loose(query_end_time)
    if not dt:
        return None
    return (dt + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")


async def _cfg_get(db, key: str) -> str:
    res = await db.execute(
        text("SELECT config_value FROM system_configs WHERE config_key=:k LIMIT 1"),
        {"k": key},
    )
    row = res.first()
    return (row[0] or "").strip() if row else ""


async def _cfg_set(db, key: str, value: str, group: str = "sync") -> None:
    await db.execute(
        text(
            """
            INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
            VALUES (:k, :v, :g, NOW())
            ON DUPLICATE KEY UPDATE config_value=:v, updated_at=NOW()
            """
        ),
        {"k": key, "v": value, "g": group},
    )


async def _set_running(db) -> None:
    await _cfg_set(db, STATUS_KEY, "running", "sync")
    await db.commit()


async def _set_done(db, ok: bool, message: str) -> None:
    await _cfg_set(db, STATUS_KEY, "success" if ok else "error", "sync")
    await _cfg_set(db, MSG_KEY, message[:2000], "sync")
    if ok:
        await _cfg_set(db, SUCCESS_KEY, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "sync")
    await db.commit()


def _open_credentials() -> tuple[str, str, str]:
    base = (os.getenv("WECHAT_OPEN_BASE_URL") or "").strip().rstrip("/")
    company = (os.getenv("WECHAT_OPEN_COMPANY") or "").strip()
    key = (os.getenv("WECHAT_OPEN_KEY") or "").strip()
    return base, company, key


async def _resolve_partner_id(db, partner_override: str | None) -> str:
    if partner_override and partner_override.strip():
        return partner_override.strip()
    v = await _cfg_get(db, CFG_PARTNER)
    if v:
        return v
    return (os.getenv("WECHAT_OPEN_ADMIN_PARTNER_ID") or "").strip()


async def _resolve_query_mode(db) -> str:
    v = (await _cfg_get(db, CFG_QUERY_MODE)).strip().lower()
    if v in ("createtime", "create_time"):
        return "createTime"
    return "updateTime"


async def _post_increment(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    company: str,
    key: str,
    partner_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    ts_ms = str(int(time.time() * 1000))
    sign = _md5_upper(key + company + partner_id + ts_ms)
    url = f"{base_url}/open/wechat/getAllFriendsIncrement"
    headers = {
        "company": company,
        "partnerId": partner_id,
        "timestamp": ts_ms,
        "key": key,
        "sign": sign,
        "content-type": "application/json",
    }
    resp = await client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()


def _map_item_to_rc_fields(item: dict[str, Any]) -> dict[str, Any]:
    del_v = item.get("delete")
    is_deleted = bool(int(del_v)) if str(del_v).strip().isdigit() else False
    gender = item.get("gender")
    gender_s = None
    if gender is not None and str(gender).strip().isdigit():
        gender_s = str(int(gender))
    phone = (item.get("phone") or "").strip() or None
    return {
        "id": (item.get("id") or "").strip(),
        "type": (int(item["type"]) if str(item.get("type") or "").strip().isdigit() else None),
        "from_type": (str(item.get("fromType")).strip() if item.get("fromType") is not None else None) or None,
        "head_url": (item.get("headUrl") or "").strip() or None,
        "create_time": _parse_dt_loose(item.get("createTime")),
        "add_time": _parse_dt_loose(item.get("addTime")),
        # 开放平台返回 salesWechatId（wxid_...），保持原始值作为关联键。
        "sales_wechat_id": (item.get("salesWechatId") or "").strip() or None,
        "is_deleted": is_deleted,
        "update_time": _parse_dt_loose(item.get("updateTime")),
        "alias": (item.get("alias") or "").strip() or None,
        "name": (item.get("name") or "").strip() or None,
        "remark": (item.get("remark") or "").strip() or None,
        "phone": phone,
        "phone_normalized": _digits_phone(phone),
        "description": (item.get("description") or "").strip() or None,
        "note_des": (item.get("noteDes") or "").strip() or None,
        "gender": gender_s,
        "region": (item.get("region") or "").strip() or None,
        "label": (item.get("label") or "").strip() or None,
    }


@dataclass
class WechatDaySyncStats:
    calendar_day: str = ""
    partner_id: str = ""
    query_mode: str = ""
    types: tuple[int, ...] = (1, 2)
    api_calls: int = 0
    batches: int = 0
    rows_received: int = 0
    rows_applied_in_day: int = 0
    errors: list[str] = field(default_factory=list)


async def _upsert_rcsw(db, item: dict[str, Any], fields: dict[str, Any]) -> None:
    wid = fields["id"]
    sw = (fields["sales_wechat_id"] or "").strip()
    if not wid or not sw:
        return

    last_chat = _parse_dt_loose(item.get("lastChatTime"))
    now = datetime.now()

    stmt = select(RawCustomerSalesWechat).where(
        RawCustomerSalesWechat.raw_customer_id == wid,
        RawCustomerSalesWechat.sales_wechat_id == sw,
    )
    ex = (await db.execute(stmt)).scalars().first()
    if ex:
        ex.alias = fields["alias"]
        ex.name = fields["name"]
        ex.remark = fields["remark"]
        ex.phone = fields["phone"]
        ex.label = fields["label"]
        ex.head_url = fields["head_url"]
        ex.description = fields["description"]
        ex.note_des = fields["note_des"]
        ex.gender = fields["gender"]
        ex.region = fields["region"]
        ex.type = fields["type"]
        ex.from_type = fields["from_type"]
        ex.create_time = fields["create_time"]
        ex.add_time = fields["add_time"]
        ex.update_time = fields["update_time"]
        ex.last_chat_time = last_chat
        ex.is_deleted = fields["is_deleted"]
        ex.synced_at = now
    else:
        db.add(
            RawCustomerSalesWechat(
                raw_customer_id=wid,
                sales_wechat_id=sw,
                alias=fields["alias"],
                name=fields["name"],
                remark=fields["remark"],
                phone=fields["phone"],
                label=fields["label"],
                head_url=fields["head_url"],
                description=fields["description"],
                note_des=fields["note_des"],
                gender=fields["gender"],
                region=fields["region"],
                type=fields["type"],
                from_type=fields["from_type"],
                create_time=fields["create_time"],
                add_time=fields["add_time"],
                update_time=fields["update_time"],
                last_chat_time=last_chat,
                is_deleted=fields["is_deleted"],
                synced_at=now,
            )
        )


def _dt_cmp(a: datetime | None, b: datetime | None) -> int:
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    return (a > b) - (a < b)


async def _merge_raw_customer(db, fields: dict[str, Any]) -> None:
    """raw_customers 按 id 聚合：在多条销售关系之间保留 update_time 较新的一条快照。"""
    rid = fields["id"]
    if not rid:
        return
    ex = (await db.execute(select(RawCustomer).where(RawCustomer.id == rid))).scalars().first()
    now = datetime.now()
    if not ex:
        db.add(
            RawCustomer(
                id=rid,
                type=fields["type"],
                from_type=fields["from_type"],
                head_url=fields["head_url"],
                create_time=fields["create_time"],
                add_time=fields["add_time"],
                sales_wechat_id=fields["sales_wechat_id"],
                is_deleted=fields["is_deleted"],
                update_time=fields["update_time"],
                alias=fields["alias"],
                name=fields["name"],
                remark=fields["remark"],
                phone=fields["phone"],
                phone_normalized=fields["phone_normalized"],
                description=fields["description"],
                note_des=fields["note_des"],
                gender=fields["gender"],
                region=fields["region"],
                label=fields["label"],
                synced_at=now,
            )
        )
        return

    if _dt_cmp(fields["update_time"], ex.update_time) < 0:
        return

    ex.type = fields["type"]
    ex.from_type = fields["from_type"]
    ex.head_url = fields["head_url"]
    ex.create_time = fields["create_time"] or ex.create_time
    ex.add_time = fields["add_time"] or ex.add_time
    ex.sales_wechat_id = fields["sales_wechat_id"] or ex.sales_wechat_id
    ex.is_deleted = fields["is_deleted"]
    ex.update_time = fields["update_time"]
    ex.alias = fields["alias"]
    ex.name = fields["name"]
    ex.remark = fields["remark"]
    ex.phone = fields["phone"]
    ex.phone_normalized = fields["phone_normalized"] or ex.phone_normalized
    ex.description = fields["description"]
    ex.note_des = fields["note_des"]
    ex.gender = fields["gender"]
    ex.region = fields["region"]
    ex.label = fields["label"]
    ex.synced_at = now


async def _sync_one_type_for_day(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    company: str,
    key: str,
    partner_id: str,
    friend_type: int,
    calendar_day: str,
    query_mode: str,
    day_start: datetime,
    day_end: datetime,
    filter_end: datetime,
    stats: WechatDaySyncStats,
    db,
) -> None:
    now_naive = _now_sh_naive()
    cap = _api_start_time_cap(now_naive)
    cursor = day_start
    if cursor > cap:
        stats.errors.append(f"type={friend_type}: 当日 00:00 已超过当前可查询时间上限（当前时间-5秒），跳过")
        return

    first = True
    while True:
        if not first:
            await asyncio.sleep(5.1)
        first = False

        start_str = cursor.strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "type": friend_type,
            "getFirstData": False,
            "queryMode": query_mode,
            "startTime": start_str,
        }
        stats.api_calls += 1
        try:
            body = await _post_increment(
                client,
                base_url=base_url,
                company=company,
                key=key,
                partner_id=partner_id,
                payload=payload,
            )
        except Exception as e:
            stats.errors.append(f"type={friend_type} start={start_str}: {e}")
            logger.exception("wechat increment HTTP failed")
            break

        if not body.get("success"):
            stats.errors.append(f"type={friend_type} start={start_str}: {body.get('message')}")
            break

        stats.batches += 1
        data = body.get("data") or {}
        items = data.get("data") or []
        if not isinstance(items, list):
            items = []

        stats.rows_received += len(items)
        for item in items:
            if not isinstance(item, dict):
                continue
            ref = _item_reference_time(item, query_mode)
            if ref is not None and (ref < day_start or ref > filter_end):
                continue
            fields = _map_item_to_rc_fields(item)
            if not fields["id"] or not fields["sales_wechat_id"]:
                continue
            stats.rows_applied_in_day += 1
            await _merge_raw_customer(db, fields)
            await db.flush()
            await _upsert_rcsw(db, item, fields)

        await db.commit()

        # 写入“进行中”进度摘要，便于管理后台轮询展示
        try:
            running_msg = (
                f"微信原始池同步进行中 day={calendar_day} type={friend_type} "
                f"calls={stats.api_calls} batches={stats.batches} recv={stats.rows_received} applied={stats.rows_applied_in_day} "
                f"cursor_start={start_str} qEnd={data.get('queryEndTime') or ''}"
            )
            await _cfg_set(db, MSG_KEY, running_msg[:2000], "sync")
            await db.commit()
        except Exception:
            pass

        q_end = data.get("queryEndTime")
        nxt = _next_start_time(q_end)
        if not nxt:
            break
        nxt_dt = _parse_dt_loose(nxt)
        if not nxt_dt or nxt_dt > day_end:
            break
        cursor = nxt_dt
        if cursor > cap:
            break
        if not items:
            break


async def sync_wechat_friends_for_calendar_day(
    calendar_day: str,
    *,
    partner_id: str | None = None,
    types: tuple[int, ...] = (1, 2),
) -> WechatDaySyncStats:
    """
    同步单个自然日（上海时区）内的好友/群变更到原始客户池。
    幂等：可重复执行；同 id 以较新 update_time 覆盖 raw_customers。
    """
    calendar_day = calendar_day.strip()
    day_start, day_end = _day_bounds_naive(calendar_day)
    now_naive = _now_sh_naive()
    filter_end = min(day_end, _api_start_time_cap(now_naive))
    if filter_end < day_start:
        raise ValueError("当日窗口在当前时间下不可查询（已过日界或可查询上限）")

    base, company, key = _open_credentials()
    if not base or not company or not key:
        raise ValueError("缺少开放平台环境变量：WECHAT_OPEN_BASE_URL / WECHAT_OPEN_COMPANY / WECHAT_OPEN_KEY")

    stats = WechatDaySyncStats(
        calendar_day=calendar_day,
        query_mode="updateTime",
        types=types,
    )

    async with _wechat_sync_lock:
        async with AsyncSessionLocal() as db:
            await _set_running(db)
            partner = await _resolve_partner_id(db, partner_id)
            if not partner:
                msg = "缺少 partnerId：请在系统配置 wechat_open_partner_id 或环境变量 WECHAT_OPEN_ADMIN_PARTNER_ID 中设置"
                await _set_done(db, False, msg)
                stats.errors.append(msg)
                return stats

            stats.partner_id = partner
            qmode = await _resolve_query_mode(db)
            stats.query_mode = qmode

            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    for t in types:
                        await _sync_one_type_for_day(
                            client=client,
                            base_url=base,
                            company=company,
                            key=key,
                            partner_id=partner,
                            friend_type=int(t),
                            calendar_day=calendar_day,
                            query_mode=qmode,
                            day_start=day_start,
                            day_end=day_end,
                            filter_end=filter_end,
                            stats=stats,
                            db=db,
                        )
                        if t != types[-1]:
                            await asyncio.sleep(5.1)

                ok = not stats.errors
                msg = (
                    f"微信原始池同步完成 day={calendar_day} partner={partner} "
                    f"calls={stats.api_calls} batches={stats.batches} "
                    f"recv={stats.rows_received} applied_in_day={stats.rows_applied_in_day}"
                )
                await _set_done(db, ok, msg if ok else msg + " | " + "; ".join(stats.errors[:3]))
                logger.info(msg)
            except Exception as e:
                await _set_done(db, False, str(e))
                logger.exception("wechat day sync failed")
                stats.errors.append(str(e))

    return stats


async def scheduled_wechat_friends_day_sync() -> None:
    """定时任务：读取 system_configs 中的目标自然日并同步（与手动页使用同一配置）。"""
    async with AsyncSessionLocal() as db:
        day = (await _cfg_get(db, CFG_TARGET_DAY)).strip()
        partner_ov = (await _cfg_get(db, CFG_PARTNER)).strip()
        if not day:
            day = (_now_sh_naive() - timedelta(days=1)).strftime("%Y-%m-%d")
            await _cfg_set(db, CFG_TARGET_DAY, day, "sync")
            await db.commit()
            logger.info(f"[APScheduler] wechat_friends_sync_target_day 未配置，已写入默认昨天：{day}")
    try:
        await sync_wechat_friends_for_calendar_day(day, partner_id=partner_ov or None)
    except Exception as e:
        logger.exception("scheduled wechat friends sync failed: %s", e)
        async with AsyncSessionLocal() as db3:
            await _set_done(db3, False, str(e))
