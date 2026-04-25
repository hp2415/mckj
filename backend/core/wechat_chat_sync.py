"""
云客开放平台：增量获取员工聊天记录 /open/wechat/allRecords 并写入 raw_chat_logs。

增量游标（持久化在 system_configs）：
- wechat_chat_cursor_time_ms: 上次返回 data.end（消息保存时间 time，13位ms）
- wechat_chat_cursor_create_ts_ms: 上次返回 data.createTimestamp（补充条件，13位ms；无则 0）

注意：
- 接口限制：5 秒调用一次；入参 timestamp 需小于当前时间 30 分钟以上（数据延迟）。
- 唯一索引：wechat_id + talker + msg_svr_id（RawChatLog 新增字段 + 唯一约束）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.dialects.mysql import insert as mysql_insert

from core.logger import logger
from database import AsyncSessionLocal
from models import RawChatLog

_lock = asyncio.Lock()

CFG_PARTNER = "wechat_open_partner_id"
CFG_CHAT_CURSOR_TIME = "wechat_chat_cursor_time_ms"
CFG_CHAT_CURSOR_CREATE = "wechat_chat_cursor_create_ts_ms"
CFG_CHAT_STATUS = "wechat_chat_sync_status"
CFG_CHAT_LAST_MSG = "wechat_chat_sync_last_message"
CFG_CHAT_LAST_OK = "wechat_chat_sync_last_success"


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


def _now_ms() -> int:
    return int(time.time() * 1000)


def _max_queryable_time_ms() -> int:
    # 文档：timestamp 要小于当前时间 30 分钟以上；留 40 分钟安全边界
    return _now_ms() - 40 * 60 * 1000


def _normalize_row_from_message(m: dict[str, Any]) -> dict[str, Any]:
    wechat_id = str(m.get("wechatId") or "").strip()
    talker = str(m.get("talker") or "").strip()
    msg_svr_id = str(m.get("msgSvrId") or "").strip()
    roomid = str(m.get("roomid") or "").strip() or None

    mine = bool(m.get("mine"))
    typ = int(m.get("type") or 0)

    send_ts = int(m.get("timestamp") or 0) or None
    time_ms = int(m.get("time") or 0) or None
    file_v = str(m.get("file") or "").strip()
    text_v = m.get("text")

    # 按开放平台文档解释 text 字段：对不同 type 的语义不同，但都存到 RawChatLog.text（可读内容）
    # 原始结构完整保留在 raw_json。
    content = ""
    if text_v is not None:
        content = str(text_v)

    raw_json = json.dumps(m, ensure_ascii=False, separators=(",", ":"), default=str)

    # 兼容历史字段 timestamp：仍写入保存时间（更稳定），便于旧查询逻辑继续工作
    legacy_ts = int(time_ms or send_ts or 0) or None

    return {
        "wechat_id": wechat_id,
        "talker": talker,
        "msg_svr_id": msg_svr_id,
        "roomid": roomid,
        "text": content,
        "raw_json": raw_json,
        "send_timestamp_ms": send_ts,
        "time_ms": time_ms,
        "timestamp": legacy_ts,
        "is_send": 1 if mine else 0,
        "message_type": typ,
        "file_source": file_v[:100] if file_v else "",
        "imported_at": datetime.now(),
    }


@dataclass
class ChatSyncStats:
    partner_id: str = ""
    start_time_ms: int = 0
    end_time_ms: int = 0
    create_ts_ms: int = 0
    api_calls: int = 0
    rows_received: int = 0
    rows_upserted: int = 0
    errors: list[str] = field(default_factory=list)


async def _resolve_partner_id(db, partner_override: str | None) -> str:
    if partner_override is not None and partner_override.strip():
        return partner_override.strip()
    cfg = await _cfg_get(db, CFG_PARTNER)
    if cfg:
        return cfg
    _, _, admin_partner, _ = _open_credentials()
    return admin_partner


async def _post_all_records(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    company: str,
    key: str,
    partner_id: str,
    timestamp_ms: int,
    create_timestamp_ms: int,
) -> dict[str, Any]:
    ts_ms = str(int(time.time() * 1000))
    sign = _md5_upper(key + company + partner_id + ts_ms)
    url = f"{base_url}/open/wechat/allRecords"
    headers = {
        "company": company,
        "partnerId": partner_id,
        "timestamp": ts_ms,
        "key": key,
        "sign": sign,
        "content-type": "application/json",
    }
    payload = {"timestamp": int(timestamp_ms), "createTimestamp": int(create_timestamp_ms or 0)}
    resp = await client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()


async def _mark_running(db) -> None:
    await _cfg_set(db, CFG_CHAT_STATUS, "running", "sync")
    await db.commit()


async def _mark_done(db, ok: bool, message: str) -> None:
    await _cfg_set(db, CFG_CHAT_STATUS, "success" if ok else "error", "sync")
    await _cfg_set(db, CFG_CHAT_LAST_MSG, message[:2000], "sync")
    if ok:
        await _cfg_set(db, CFG_CHAT_LAST_OK, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "sync")
    await db.commit()


async def sync_wechat_chat_increment(
    *,
    start_time_ms: int | None = None,
    max_calls: int = 6,
    partner_id: str | None = None,
    persist_cursor: bool = True,
) -> ChatSyncStats:
    """
    增量同步聊天记录：每次调用最多请求 max_calls 次（每次 1 小时窗口），遵守 5 秒限频。
    - start_time_ms 为空：从 system_configs 游标继续。
    - persist_cursor=True：成功后写回游标（用于自动追赶/下次手动继续）。
    """
    base, company, _, key = _open_credentials()
    if not base or not company or not key:
        raise ValueError("缺少开放平台环境变量：WECHAT_OPEN_BASE_URL / WECHAT_OPEN_COMPANY / WECHAT_OPEN_KEY")

    stats = ChatSyncStats()

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

            if start_time_ms is None:
                cur = await _cfg_get(db, CFG_CHAT_CURSOR_TIME)
                cur2 = await _cfg_get(db, CFG_CHAT_CURSOR_CREATE)
                start_time_ms = int(cur) if cur.isdigit() else 0
                stats.create_ts_ms = int(cur2) if cur2.isdigit() else 0

            if not start_time_ms:
                # 默认从“可查上限往前 2 小时”开始，避免 timestamp 太新
                start_time_ms = _max_queryable_time_ms() - 2 * 60 * 60 * 1000

            start_time_ms = int(start_time_ms)
            if start_time_ms > _max_queryable_time_ms():
                start_time_ms = _max_queryable_time_ms()

            cursor_time = start_time_ms
            cursor_create = int(stats.create_ts_ms or 0)
            stats.start_time_ms = cursor_time

            async with httpx.AsyncClient(timeout=60.0) as client:
                for i in range(max(1, int(max_calls))):
                    if i > 0:
                        try:
                            await asyncio.sleep(5.1)
                        except asyncio.CancelledError:
                            # 进程退出/重载时的正常取消，不应作为异常噪音
                            raise
                    stats.api_calls += 1

                    try:
                        body = await _post_all_records(
                            client,
                            base_url=base,
                            company=company,
                            key=key,
                            partner_id=p,
                            timestamp_ms=cursor_time,
                            create_timestamp_ms=cursor_create,
                        )
                    except Exception as e:
                        stats.errors.append(str(e))
                        break

                    if not body.get("success"):
                        stats.errors.append(str(body.get("message") or "unknown error"))
                        break

                    data = body.get("data") or {}
                    end_ms = int(data.get("end") or 0) or cursor_time
                    cursor_create = int(data.get("createTimestamp") or 0) or 0
                    msgs = data.get("messages") or []
                    if not isinstance(msgs, list):
                        msgs = []

                    stats.rows_received += len(msgs)

                    # upsert by unique key (wechat_id, talker, msg_svr_id)
                    n_up = 0
                    for m in msgs:
                        if not isinstance(m, dict):
                            continue
                        row = _normalize_row_from_message(m)
                        if not row["wechat_id"] or not row["talker"] or not row["msg_svr_id"]:
                            continue
                        stmt = mysql_insert(RawChatLog).values(**row)
                        stmt = stmt.on_duplicate_key_update(
                            roomid=stmt.inserted.roomid,
                            text=stmt.inserted.text,
                            raw_json=stmt.inserted.raw_json,
                            send_timestamp_ms=stmt.inserted.send_timestamp_ms,
                            time_ms=stmt.inserted.time_ms,
                            timestamp=stmt.inserted.timestamp,
                            is_send=stmt.inserted.is_send,
                            message_type=stmt.inserted.message_type,
                            file_source=stmt.inserted.file_source,
                            imported_at=stmt.inserted.imported_at,
                        )
                        await db.execute(stmt)
                        n_up += 1

                    await db.commit()
                    stats.rows_upserted += n_up

                    cursor_time = end_ms
                    stats.end_time_ms = cursor_time
                    stats.create_ts_ms = cursor_create

                    # 写入“进行中”进度摘要，便于管理后台页面轮询展示
                    try:
                        running_msg = (
                            f"聊天同步进行中 partner={stats.partner_id} "
                            f"step={i+1}/{max(1,int(max_calls))} recv={stats.rows_received} upsert={stats.rows_upserted} "
                            f"cursor_end={stats.end_time_ms} createTs={stats.create_ts_ms}"
                        )
                        await _cfg_set(db, CFG_CHAT_LAST_MSG, running_msg[:2000], "sync")
                        await db.commit()
                    except Exception:
                        pass

                    # 若窗口已经接近“可查上限”，就停（避免太新导致空/异常）
                    if cursor_time >= _max_queryable_time_ms():
                        break
                    if not msgs:
                        # 该小时无消息：也允许推进游标（依赖 end_ms），若 end_ms 未推进则停止避免死循环
                        if end_ms <= stats.start_time_ms:
                            break

            if persist_cursor and stats.end_time_ms:
                await _cfg_set(db, CFG_CHAT_CURSOR_TIME, str(int(stats.end_time_ms)), "sync")
                await _cfg_set(db, CFG_CHAT_CURSOR_CREATE, str(int(stats.create_ts_ms or 0)), "sync")
                await db.commit()

            ok = not stats.errors
            msg = (
                f"聊天增量同步完成 partner={stats.partner_id} calls={stats.api_calls} "
                f"recv={stats.rows_received} upsert={stats.rows_upserted} "
                f"cursor_end={stats.end_time_ms} createTs={stats.create_ts_ms}"
            )
            if not ok:
                msg += " | " + "; ".join(stats.errors[:3])
            await _mark_done(db, ok, msg)
            logger.info(msg)

    try:
        from database import engine

        await engine.dispose()
    except Exception:
        pass
    return stats


async def scheduled_wechat_chat_increment() -> None:
    """定时任务：从游标继续追赶（每次最多拉 6 小时窗口）。"""
    try:
        await sync_wechat_chat_increment(max_calls=6, start_time_ms=None, partner_id=None, persist_cursor=True)
    except asyncio.CancelledError:
        # uvicorn reload / Ctrl+C 触发的取消，忽略即可
        return
    except Exception as e:
        logger.exception("scheduled wechat chat sync failed: %s", e)
        async with AsyncSessionLocal() as db:
            await _mark_done(db, False, str(e))
        try:
            from database import engine

            await engine.dispose()
        except Exception:
            pass

