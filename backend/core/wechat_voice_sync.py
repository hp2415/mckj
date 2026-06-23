"""
云客开放平台：增量获取微信语音/视频通话 /open/wechat/queryWeChatVoiceListByCursor
并写入 raw_wechat_voice_calls。

游标（system_configs）：
- wechat_voice_cursor_next_id: 上次同步最后一条记录的 nextId（首次为空/null）

注意：接口 5 秒限频；须顺序拉取，不可跳页。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import and_, text, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.future import select

from core.logger import logger
from core.system_config_store import upsert_system_config_row
from database import AsyncSessionLocal
from models import ContactTask, RawCustomerSalesWechat, RawWechatVoiceCall

_lock = asyncio.Lock()

# 批量 upsert 每批行数：raw_json 较大，控制批次避免超过 MySQL max_allowed_packet
UPSERT_BATCH_SIZE = 200

CFG_PARTNER = "wechat_open_partner_id"
CFG_VOICE_CURSOR = "wechat_voice_cursor_next_id"
CFG_VOICE_STATUS = "wechat_voice_sync_status"
CFG_VOICE_LAST_MSG = "wechat_voice_sync_last_message"
CFG_VOICE_LAST_OK = "wechat_voice_sync_last_success"

SHANGHAI_TZ = timezone(timedelta(hours=8))
API_PATH = "/open/wechat/queryWeChatVoiceListByCursor"


def _md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def _open_credentials() -> tuple[str, str, str, str]:
    base = (os.getenv("WECHAT_OPEN_BASE_URL") or "").strip().rstrip("/")
    company = (os.getenv("WECHAT_OPEN_COMPANY") or "").strip()
    admin_partner = (os.getenv("WECHAT_OPEN_ADMIN_PARTNER_ID") or "").strip()
    key = (os.getenv("WECHAT_OPEN_KEY") or "").strip()
    return base, company, admin_partner, key


async def _cfg_get(db, key: str) -> str:
    res = await db.execute(
        text("SELECT config_value FROM system_configs WHERE config_key=:k LIMIT 1"),
        {"k": key},
    )
    row = res.first()
    return (row[0] or "").strip() if row else ""


async def _cfg_set(db, key: str, value: str, group: str = "sync") -> None:
    await upsert_system_config_row(
        db,
        config_key=key,
        config_value=value,
        config_group=group,
    )


def _parse_dt_loose(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _normalize_voice_row(item: dict[str, Any]) -> dict[str, Any] | None:
    record_id = str(item.get("id") or "").strip()
    we_chat_id = str(item.get("weChatId") or "").strip()
    talker = str(item.get("talker") or "").strip()
    start_time = _parse_dt_loose(item.get("startTime"))
    if not record_id or not we_chat_id or not talker or start_time is None:
        return None
    end_time = _parse_dt_loose(item.get("endTime"))
    try:
        cursor_next_id = int(item.get("nextId") or 0) or None
    except (TypeError, ValueError):
        cursor_next_id = None
    try:
        duration_file = int(item.get("durationFile") or 0)
    except (TypeError, ValueError):
        duration_file = 0
    raw_json = json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)
    return {
        "record_id": record_id,
        "user_name": (item.get("userName") or "").strip() or None,
        "user_phone": (item.get("userPhone") or "").strip() or None,
        "user_we_chat_nick_name": (item.get("userWeChatNickName") or "").strip() or None,
        "user_we_chat_alias": (item.get("userWeChatAlias") or "").strip() or None,
        "user_we_chat_head_img": (item.get("userWeChatHeadImg") or "").strip() or None,
        "user_we_chat_phone": (item.get("userWeChatPhone") or "").strip() or None,
        "talker_head_img": (item.get("talkerHeadImg") or "").strip() or None,
        "talker_nick_name": (item.get("talkerNickName") or "").strip() or None,
        "talker_alias": (item.get("talkerAlias") or "").strip() or None,
        "call_type": int(item.get("callType") or 0),
        "is_send": int(item.get("isSend") or 0),
        "call_status": int(item.get("callStatus") or 0),
        "oss_file_name": (item.get("ossFileName") or "").strip() or None,
        "duration": str(item.get("duration") or "").strip() or None,
        "start_time": start_time,
        "end_time": end_time,
        "we_chat_id": we_chat_id,
        "talker": talker,
        "is_room": int(item.get("isRoom") or 0),
        "remark": (item.get("remark") or "").strip() or None,
        "duration_file": duration_file,
        "cursor_next_id": cursor_next_id,
        "user_id": (item.get("userId") or "").strip() or None,
        "raw_json": raw_json,
        "imported_at": datetime.now(),
    }


@dataclass
class VoiceSyncStats:
    partner_id: str = ""
    cursor_next_id: int | None = None
    api_calls: int = 0
    rows_received: int = 0
    rows_upserted: int = 0
    auto_completed_tasks: int = 0
    errors: list[str] = field(default_factory=list)
    # 本轮同步进来的「接通 + 有录音」候选通话 record_id（供自动转写筛选用）
    candidate_record_ids: list[str] = field(default_factory=list)


def _auto_transcribe_on_sync_enabled() -> bool:
    v = str(os.getenv("VOICE_AUTO_TRANSCRIBE_ON_SYNC") or "").strip()
    return v not in ("", "0", "false", "False", "off", "OFF")


async def _resolve_partner_id(db, partner_override: str | None) -> str:
    if partner_override is not None and partner_override.strip():
        return partner_override.strip()
    cfg = await _cfg_get(db, CFG_PARTNER)
    if cfg:
        return cfg
    _, _, admin_partner, _ = _open_credentials()
    return admin_partner


async def _post_voice_page(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    company: str,
    key: str,
    partner_id: str,
    page_size: int,
    next_id: int | None,
    call_type: int | None,
    is_send: int | None,
    is_room: int | None,
) -> dict[str, Any]:
    ts_ms = str(int(time.time() * 1000))
    sign = _md5_upper(key + company + partner_id + ts_ms)
    url = f"{base_url}{API_PATH}"
    headers = {
        "company": company,
        "partnerId": partner_id,
        "timestamp": ts_ms,
        "sign": sign,
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {"pageSize": int(page_size)}
    if next_id is not None:
        payload["nextId"] = int(next_id)
    if call_type is not None:
        payload["callType"] = int(call_type)
    if is_send is not None:
        payload["isSend"] = int(is_send)
    if is_room is not None:
        payload["isRoom"] = int(is_room)
    resp = await client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()


async def _mark_running(db) -> None:
    await _cfg_set(db, CFG_VOICE_STATUS, "running", "sync")
    await db.commit()


async def _mark_done(db, ok: bool, message: str) -> None:
    await _cfg_set(db, CFG_VOICE_STATUS, "success" if ok else "error", "sync")
    await _cfg_set(db, CFG_VOICE_LAST_MSG, message[:2000], "sync")
    if ok:
        await _cfg_set(db, CFG_VOICE_LAST_OK, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "sync")
    await db.commit()


def _calendar_day_window_shanghai(now: datetime | None = None) -> tuple[datetime, datetime]:
    base = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    start = base.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


async def _auto_complete_tasks_by_today_voice(db) -> int:
    """今日有接通的好友语音通话 → 自动完成当日电话主线任务。"""
    now = datetime.now(SHANGHAI_TZ)
    day_start, day_end = _calendar_day_window_shanghai(now)
    today = now.date()

    q = (
        select(ContactTask.id)
        .join(
            RawCustomerSalesWechat,
            and_(
                RawCustomerSalesWechat.raw_customer_id == ContactTask.raw_customer_id,
                RawCustomerSalesWechat.sales_wechat_id == ContactTask.sales_wechat_id,
            ),
        )
        .join(
            RawWechatVoiceCall,
            and_(
                RawWechatVoiceCall.we_chat_id == RawCustomerSalesWechat.sales_wechat_id,
                RawWechatVoiceCall.talker == RawCustomerSalesWechat.raw_customer_id,
                RawWechatVoiceCall.is_room == 0,
                RawWechatVoiceCall.call_status == 1,
                RawWechatVoiceCall.start_time >= day_start,
                RawWechatVoiceCall.start_time < day_end,
            ),
        )
        .where(ContactTask.due_date == today)
        .where(ContactTask.contact_channel == "phone")
        .where(ContactTask.status.in_(("pending", "in_progress", "overdue")))
        .distinct()
    )
    ids = [int(r[0]) for r in (await db.execute(q)).all() if r and r[0]]
    if not ids:
        return 0
    res = await db.execute(
        update(ContactTask)
        .where(ContactTask.id.in_(ids))
        .values(
            status="done",
            completed_at=datetime.now(),
            completed_by_user_id=None,
            completion_note="auto: 今日检测到微信语音通话已接通，自动完成电话任务",
        )
    )
    return int(res.rowcount or 0)


async def sync_wechat_voice_increment(
    *,
    start_next_id: int | None = None,
    max_pages: int = 10,
    page_size: int = 100,
    partner_id: str | None = None,
    persist_cursor: bool = True,
    call_type: int | None = 1,
    is_send: int | None = None,
    is_room: int | None = 0,
) -> VoiceSyncStats:
    """
    游标增量同步微信语音/视频通话。
    - start_next_id 为空：从 system_configs 游标继续；仍为空则首次从 null 开始。
    - is_room 默认 0（好友 1v1）；call_type 默认 1（语音）。
    """
    base, company, _, key = _open_credentials()
    if not base or not company or not key:
        raise ValueError("缺少开放平台环境变量：WECHAT_OPEN_BASE_URL / WECHAT_OPEN_COMPANY / WECHAT_OPEN_KEY")

    page_size = max(10, min(int(page_size), 500))
    stats = VoiceSyncStats()

    async with _lock:
        async with AsyncSessionLocal() as db:
            await _mark_running(db)
            p = await _resolve_partner_id(db, partner_id)
            if not p:
                msg = "缺少 partnerId：请在 system_configs.wechat_open_partner_id 或环境变量 WECHAT_OPEN_ADMIN_PARTNER_ID 配置"
                await _mark_done(db, False, msg)
                stats.errors.append(msg)
                return stats
            stats.partner_id = p

            cursor: int | None = start_next_id
            if cursor is None and persist_cursor:
                cur_raw = await _cfg_get(db, CFG_VOICE_CURSOR)
                if cur_raw.isdigit():
                    cursor = int(cur_raw)

            async with httpx.AsyncClient(timeout=90.0) as client:
                for i in range(max(1, int(max_pages))):
                    if i > 0:
                        await asyncio.sleep(5.1)
                    stats.api_calls += 1
                    try:
                        body = await _post_voice_page(
                            client,
                            base_url=base,
                            company=company,
                            key=key,
                            partner_id=p,
                            page_size=page_size,
                            next_id=cursor,
                            call_type=call_type,
                            is_send=is_send,
                            is_room=is_room,
                        )
                    except Exception as e:
                        stats.errors.append(str(e))
                        break

                    if not body.get("success"):
                        stats.errors.append(str(body.get("message") or "unknown error"))
                        break

                    data = body.get("data") or {}
                    items = data.get("data") or []
                    if not isinstance(items, list):
                        items = []

                    stats.rows_received += len(items)
                    if not items:
                        break

                    last_cursor: int | None = None
                    rows: list[dict[str, Any]] = []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        row = _normalize_voice_row(item)
                        if row is None:
                            continue
                        if row.get("cursor_next_id") is not None:
                            last_cursor = int(row["cursor_next_id"])
                        rows.append(row)
                        # 接通 + 有录音 + 好友1v1 + 语音 的通话作为自动转写候选
                        if (
                            int(row.get("call_status") or 0) == 1
                            and int(row.get("is_room") or 0) == 0
                            and int(row.get("call_type") or 0) == 1
                            and (row.get("oss_file_name") or "").strip()
                        ):
                            stats.candidate_record_ids.append(str(row["record_id"]))

                    n_up = 0
                    for start in range(0, len(rows), UPSERT_BATCH_SIZE):
                        chunk = rows[start : start + UPSERT_BATCH_SIZE]
                        stmt = mysql_insert(RawWechatVoiceCall).values(chunk)
                        stmt = stmt.on_duplicate_key_update(
                            user_name=stmt.inserted.user_name,
                            user_phone=stmt.inserted.user_phone,
                            user_we_chat_nick_name=stmt.inserted.user_we_chat_nick_name,
                            user_we_chat_alias=stmt.inserted.user_we_chat_alias,
                            user_we_chat_head_img=stmt.inserted.user_we_chat_head_img,
                            user_we_chat_phone=stmt.inserted.user_we_chat_phone,
                            talker_head_img=stmt.inserted.talker_head_img,
                            talker_nick_name=stmt.inserted.talker_nick_name,
                            talker_alias=stmt.inserted.talker_alias,
                            call_type=stmt.inserted.call_type,
                            is_send=stmt.inserted.is_send,
                            call_status=stmt.inserted.call_status,
                            oss_file_name=stmt.inserted.oss_file_name,
                            duration=stmt.inserted.duration,
                            start_time=stmt.inserted.start_time,
                            end_time=stmt.inserted.end_time,
                            we_chat_id=stmt.inserted.we_chat_id,
                            talker=stmt.inserted.talker,
                            is_room=stmt.inserted.is_room,
                            remark=stmt.inserted.remark,
                            duration_file=stmt.inserted.duration_file,
                            cursor_next_id=stmt.inserted.cursor_next_id,
                            user_id=stmt.inserted.user_id,
                            raw_json=stmt.inserted.raw_json,
                            imported_at=stmt.inserted.imported_at,
                        )
                        await db.execute(stmt)
                        n_up += len(chunk)

                    await db.commit()
                    stats.rows_upserted += n_up

                    if last_cursor is not None:
                        cursor = last_cursor
                        stats.cursor_next_id = cursor

                    try:
                        running_msg = (
                            f"语音通话同步进行中 partner={stats.partner_id} "
                            f"page={i+1}/{max(1,int(max_pages))} recv={stats.rows_received} upsert={stats.rows_upserted} "
                            f"cursor={stats.cursor_next_id}"
                        )
                        await _cfg_set(db, CFG_VOICE_LAST_MSG, running_msg[:2000], "sync")
                        await db.commit()
                    except Exception:
                        pass

                    if len(items) < page_size:
                        break

            if persist_cursor and stats.cursor_next_id is not None:
                await _cfg_set(db, CFG_VOICE_CURSOR, str(int(stats.cursor_next_id)), "sync")
                await db.commit()

            ok = not stats.errors
            msg = (
                f"语音通话增量同步完成 partner={stats.partner_id} pages={stats.api_calls} "
                f"recv={stats.rows_received} upsert={stats.rows_upserted} cursor={stats.cursor_next_id}"
            )
            if not ok:
                msg += " | " + "; ".join(stats.errors[:3])
            try:
                n_auto = await _auto_complete_tasks_by_today_voice(db)
                if n_auto:
                    await db.commit()
                stats.auto_completed_tasks = int(n_auto)
            except Exception as e:
                logger.warning("语音通话同步后自动完成电话任务失败: {}", e)
            await _mark_done(db, ok, msg)
            logger.info(msg)

    # 锁释放后再触发自动转写：避免转写期间占用同步锁；转写不画像（画像交夜间增量画像）
    if _auto_transcribe_on_sync_enabled() and stats.candidate_record_ids:
        try:
            from ai.voice_transcribe_queue import auto_transcribe_synced_calls

            await auto_transcribe_synced_calls(
                stats.candidate_record_ids,
                batch_label="同步自动转写",
            )
        except Exception as e:
            logger.warning("语音同步后自动转写失败: {}", e)

    return stats


async def scheduled_wechat_voice_increment() -> None:
    try:
        await sync_wechat_voice_increment(
            max_pages=8,
            page_size=100,
            persist_cursor=True,
            call_type=1,
            is_room=0,
        )
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.exception("scheduled wechat voice sync failed: %s", e)
        async with AsyncSessionLocal() as db:
            await _mark_done(db, False, str(e))
