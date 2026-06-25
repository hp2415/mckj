"""MiBuddy 电话外呼通话记录同步（history_call_record）。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.future import select

from ai.voice_transcript_format import format_transcript_from_result
from core.logger import logger
from core.mibuddy_client import MibuddyApiError, history_call_record
from core.system_config_store import upsert_system_config_row
from database import AsyncSessionLocal
from models import PhoneCallRecord

SHANGHAI_TZ = timezone(timedelta(hours=8))

CFG_STATUS = "phone_call_sync_status"
CFG_LAST_MSG = "phone_call_sync_last_message"
CFG_LAST_OK = "phone_call_sync_last_success"

_lock = asyncio.Lock()


@dataclass
class PhoneCallSyncStats:
    start_time: str = ""
    end_time: str = ""
    api_pages: int = 0
    rows_received: int = 0
    rows_upserted: int = 0
    with_transcript: int = 0
    errors: list[str] = field(default_factory=list)


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


def _normalize_row(item: dict[str, Any]) -> dict[str, Any] | None:
    call_id = str(item.get("call_id") or "").strip()
    callee = str(item.get("callee") or "").strip()
    create_time = _parse_dt_loose(item.get("create_time"))
    if not call_id or not callee or create_time is None:
        return None

    content = item.get("content")
    content_dict: dict[str, Any] | None = None
    if isinstance(content, dict):
        content_dict = content
    elif isinstance(content, str) and content.strip():
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                content_dict = parsed
        except json.JSONDecodeError:
            content_dict = None

    dialogue, json_str, sc, cc = format_transcript_from_result(content_dict)

    dial_type_raw = item.get("dial_type")
    try:
        dial_type = int(dial_type_raw) if dial_type_raw is not None else None
    except (TypeError, ValueError):
        dial_type = None

    status_text = str(item.get("status_text") or "").strip().lower() or None
    task_id = str(item.get("task_id") or "").strip() or None
    file_link = str(item.get("file_link") or "").strip() or None
    user_wechat = str(item.get("user_wechat_account") or "").strip() or None
    staff_name = str(item.get("staff_name") or "").strip() or None
    staff_uuid = str(item.get("staff_uuid") or "").strip() or None

    call_seconds_raw = item.get("call_seconds")
    call_seconds: int | None = None
    if call_seconds_raw is not None:
        try:
            call_seconds = int(call_seconds_raw)
            if call_seconds < 0:
                call_seconds = None
        except (TypeError, ValueError):
            call_seconds = None

    now = datetime.now()
    raw_json = json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)

    return {
        "call_id": call_id,
        "create_time": create_time,
        "dial_type": dial_type,
        "callee": callee,
        "user_wechat_account": user_wechat,
        "task_id": task_id,
        "file_link": file_link,
        "status_text": status_text,
        "call_seconds": call_seconds,
        "staff_name": staff_name,
        "staff_uuid": staff_uuid,
        "transcript_text": dialogue or None,
        "transcript_json": json_str[:65535] if json_str else None,
        "sentence_count": sc if sc else None,
        "char_count": cc if cc else None,
        "raw_json": raw_json,
        "imported_at": now,
        "updated_at": now,
    }


async def fetch_all_history_call_records(
    start_time: str,
    end_time: str,
    *,
    page_size: int = 100,
) -> tuple[list[dict[str, Any]], int]:
    """分页拉取时间区间内全部通话记录。"""
    page = 1
    page_size = max(1, min(100, int(page_size)))
    all_items: list[dict[str, Any]] = []
    pages = 0
    while True:
        data = await history_call_record(
            start_time, end_time, page=page, page_size=page_size
        )
        pages += 1
        items = [x for x in (data.get("list") or []) if isinstance(x, dict)]
        all_items.extend(items)
        total = int(data.get("total") or 0)
        if not items or page * page_size >= total:
            break
        page += 1
    return all_items, pages


def _calendar_day_sh(*, offset_days: int = 0) -> date:
    return (datetime.now(SHANGHAI_TZ) + timedelta(days=offset_days)).date()


def _day_window_strings(day: date) -> tuple[str, str]:
    start = datetime.combine(day, datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.combine(day + timedelta(days=1), datetime.min.time()).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return start, end


async def sync_phone_call_records_for_calendar_day(day: date) -> PhoneCallSyncStats:
    """按上海时区自然日同步电话通话（供定时任务调用）。"""
    start, end = _day_window_strings(day)
    return await sync_phone_call_records(start, end)


async def sync_phone_call_records(
    start_time: str,
    end_time: str,
    *,
    page_size: int = 100,
) -> PhoneCallSyncStats:
    """从 MiBuddy 同步电话通话记录并 upsert 到 phone_call_records。"""
    stats = PhoneCallSyncStats(
        start_time=(start_time or "").strip(),
        end_time=(end_time or "").strip(),
    )

    async with _lock:
        async with AsyncSessionLocal() as db:
            await upsert_system_config_row(
                db, config_key=CFG_STATUS, config_value="running", config_group="voice"
            )
            await db.commit()

        try:
            items, pages = await fetch_all_history_call_records(
                stats.start_time, stats.end_time, page_size=page_size
            )
            stats.api_pages = pages
            stats.rows_received = len(items)

            rows: list[dict[str, Any]] = []
            for item in items:
                row = _normalize_row(item)
                if row:
                    rows.append(row)
                    if row.get("transcript_text"):
                        stats.with_transcript += 1

            if rows:
                async with AsyncSessionLocal() as db:
                    for row in rows:
                        stmt = mysql_insert(PhoneCallRecord).values(row)
                        stmt = stmt.on_duplicate_key_update(
                            create_time=stmt.inserted.create_time,
                            dial_type=stmt.inserted.dial_type,
                            callee=stmt.inserted.callee,
                            user_wechat_account=stmt.inserted.user_wechat_account,
                            task_id=stmt.inserted.task_id,
                            file_link=stmt.inserted.file_link,
                            status_text=stmt.inserted.status_text,
                            call_seconds=stmt.inserted.call_seconds,
                            staff_name=stmt.inserted.staff_name,
                            staff_uuid=stmt.inserted.staff_uuid,
                            transcript_text=stmt.inserted.transcript_text,
                            transcript_json=stmt.inserted.transcript_json,
                            sentence_count=stmt.inserted.sentence_count,
                            char_count=stmt.inserted.char_count,
                            raw_json=stmt.inserted.raw_json,
                            updated_at=stmt.inserted.updated_at,
                        )
                        await db.execute(stmt)
                    await db.commit()
                stats.rows_upserted = len(rows)

            msg = (
                f"电话通话同步完成 {stats.start_time} ~ {stats.end_time}："
                f"API {stats.rows_received} 条，入库 {stats.rows_upserted} 条，"
                f"含转写 {stats.with_transcript} 条"
            )
            async with AsyncSessionLocal() as db:
                await upsert_system_config_row(
                    db, config_key=CFG_STATUS, config_value="success", config_group="voice"
                )
                await upsert_system_config_row(
                    db, config_key=CFG_LAST_MSG, config_value=msg[:2000], config_group="voice"
                )
                await upsert_system_config_row(
                    db,
                    config_key=CFG_LAST_OK,
                    config_value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    config_group="voice",
                )
                await db.commit()
            logger.info(msg)

        except (MibuddyApiError, Exception) as e:
            stats.errors.append(str(e))
            err_msg = f"电话通话同步失败：{e}"
            async with AsyncSessionLocal() as db:
                await upsert_system_config_row(
                    db, config_key=CFG_STATUS, config_value="error", config_group="voice"
                )
                await upsert_system_config_row(
                    db, config_key=CFG_LAST_MSG, config_value=err_msg[:2000], config_group="voice"
                )
                await db.commit()
            logger.exception(err_msg)
            raise

    return stats


async def sync_phone_call_records_yesterday() -> PhoneCallSyncStats:
    """同步昨日电话通话（供每日定时任务调用）。"""
    return await sync_phone_call_records_for_calendar_day(_calendar_day_sh(offset_days=-1))


async def sync_phone_call_records_today() -> PhoneCallSyncStats:
    """同步今日电话通话（供 30 分钟增量定时任务调用）。"""
    return await sync_phone_call_records_for_calendar_day(_calendar_day_sh(offset_days=0))


async def scheduled_phone_call_increment() -> None:
    """定时任务：每 30 分钟同步今日电话通话。"""
    try:
        await sync_phone_call_records_today()
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.exception("[APScheduler] 电话通话同步（今日）失败: %s", e)


async def scheduled_phone_call_sync_yesterday() -> None:
    """定时任务：每日 00:00 补同步昨日电话通话（早于夜间画像）。"""
    day = _calendar_day_sh(offset_days=-1)
    try:
        await sync_phone_call_records_for_calendar_day(day)
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.exception(
            "[APScheduler] 电话通话同步（昨日 %s）失败: %s", day.isoformat(), e
        )
