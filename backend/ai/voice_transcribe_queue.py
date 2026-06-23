"""微信语音转写：入队、提交 MiBuddy、轮询结果、管理后台快照。"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, text, bindparam
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.future import select

from core.logger import logger
from core.mibuddy_client import MibuddyApiError, get_file_trans_result, submit_file_trans_request
from core.system_config_store import upsert_system_config_row
from database import AsyncSessionLocal
from models import RawWechatVoiceCall, WechatVoiceTranscript

CFG_PAUSE = "voice_transcribe_worker_paused"
CFG_CANCEL = "voice_transcribe_cancel_requested"
CFG_SUBMIT_BATCH = "voice_transcribe_submit_batch"
CFG_POLL_BATCH = "voice_transcribe_poll_batch"

STATUS_PENDING = "pending"
STATUS_SUBMITTED = "submitted"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES = (STATUS_PENDING, STATUS_SUBMITTED, STATUS_RUNNING)
TERMINAL_OK = (STATUS_SUCCEEDED, STATUS_SKIPPED)
MAX_SUBMIT_ATTEMPTS = 3
MAX_POLL_ATTEMPTS = 120
POLL_STALE_HOURS = 72

_worker_lock = asyncio.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default


def default_min_duration_sec() -> int:
    return max(0, _env_int("VOICE_MIN_DURATION_SEC", 5))


def _is_http_url(value: str | None) -> bool:
    s = (value or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


@dataclass
class EnqueueFilters:
    sales_wechat_id: str | None = None
    talker: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    min_duration_sec: int | None = None
    limit: int | None = None
    include_failed: bool = True


@dataclass
class EnqueueResult:
    batch_id: str
    enqueued: int
    skipped_existing: int


async def get_submit_batch_size() -> int:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("SELECT config_value FROM system_configs WHERE config_key=:k LIMIT 1"),
            {"k": CFG_SUBMIT_BATCH},
        )
        row = r.first()
        if row and str(row[0] or "").strip().isdigit():
            return max(1, min(100, int(str(row[0]).strip())))
    return max(1, min(100, _env_int("VOICE_TRANSCRIBE_SUBMIT_BATCH", 10)))


async def get_poll_batch_size() -> int:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("SELECT config_value FROM system_configs WHERE config_key=:k LIMIT 1"),
            {"k": CFG_POLL_BATCH},
        )
        row = r.first()
        if row and str(row[0] or "").strip().isdigit():
            return max(1, min(200, int(str(row[0]).strip())))
    return max(1, min(200, _env_int("VOICE_TRANSCRIBE_POLL_BATCH", 20)))


async def set_batch_sizes(*, submit: int | None = None, poll: int | None = None) -> dict[str, int]:
    out: dict[str, int] = {}
    async with AsyncSessionLocal() as db:
        if submit is not None:
            s = max(1, min(100, int(submit)))
            await upsert_system_config_row(
                db, config_key=CFG_SUBMIT_BATCH, config_value=str(s), config_group="voice"
            )
            out["submit_batch"] = s
        if poll is not None:
            p = max(1, min(200, int(poll)))
            await upsert_system_config_row(
                db, config_key=CFG_POLL_BATCH, config_value=str(p), config_group="voice"
            )
            out["poll_batch"] = p
        await db.commit()
    if "submit_batch" not in out:
        out["submit_batch"] = await get_submit_batch_size()
    if "poll_batch" not in out:
        out["poll_batch"] = await get_poll_batch_size()
    return out


async def pause_workers_db(message: str = "已暂停语音转写 worker") -> None:
    async with AsyncSessionLocal() as db:
        await upsert_system_config_row(db, config_key=CFG_PAUSE, config_value="1", config_group="voice")
        await upsert_system_config_row(
            db, config_key="voice_transcribe_pause_message", config_value=message[:800], config_group="voice"
        )
        await db.commit()


async def resume_workers_db(message: str = "已恢复语音转写 worker") -> None:
    async with AsyncSessionLocal() as db:
        await upsert_system_config_row(db, config_key=CFG_PAUSE, config_value="0", config_group="voice")
        await upsert_system_config_row(
            db, config_key="voice_transcribe_pause_message", config_value=message[:800], config_group="voice"
        )
        await db.commit()


async def request_cancel_db(message: str = "已请求中断语音转写入队/提交") -> None:
    async with AsyncSessionLocal() as db:
        await upsert_system_config_row(db, config_key=CFG_CANCEL, config_value="1", config_group="voice")
        await upsert_system_config_row(
            db, config_key="voice_transcribe_cancel_message", config_value=message[:800], config_group="voice"
        )
        await db.commit()


async def clear_cancel_db() -> None:
    async with AsyncSessionLocal() as db:
        await upsert_system_config_row(db, config_key=CFG_CANCEL, config_value="0", config_group="voice")
        await db.commit()


async def _paused() -> bool:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("SELECT config_value FROM system_configs WHERE config_key=:k LIMIT 1"),
            {"k": CFG_PAUSE},
        )
        row = r.first()
        v = (row[0] if row else "") or ""
        return str(v).strip() not in ("0", "", "false", "False", "off", "OFF")


async def _cancel_requested() -> bool:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("SELECT config_value FROM system_configs WHERE config_key=:k LIMIT 1"),
            {"k": CFG_CANCEL},
        )
        row = r.first()
        v = (row[0] if row else "") or ""
        return str(v).strip() not in ("0", "", "false", "False", "off", "OFF")


def _candidate_filters(filters: EnqueueFilters, min_dur: int):
    clauses = [
        RawWechatVoiceCall.call_status == 1,
        RawWechatVoiceCall.is_room == 0,
        RawWechatVoiceCall.call_type == 1,
        RawWechatVoiceCall.oss_file_name.isnot(None),
        RawWechatVoiceCall.oss_file_name != "",
        RawWechatVoiceCall.duration_file >= min_dur,
    ]
    sw = (filters.sales_wechat_id or "").strip()
    if sw:
        clauses.append(RawWechatVoiceCall.we_chat_id == sw)
    tk = (filters.talker or "").strip()
    if tk:
        clauses.append(RawWechatVoiceCall.talker == tk)
    if filters.start_date:
        clauses.append(RawWechatVoiceCall.start_time >= datetime.combine(filters.start_date, datetime.min.time()))
    if filters.end_date:
        end = datetime.combine(filters.end_date + timedelta(days=1), datetime.min.time())
        clauses.append(RawWechatVoiceCall.start_time < end)
    return clauses


async def count_candidates(filters: EnqueueFilters | None = None) -> dict[str, int]:
    """统计通话与转写覆盖（可按筛选）。"""
    filters = filters or EnqueueFilters()
    min_dur = filters.min_duration_sec if filters.min_duration_sec is not None else default_min_duration_sec()

    async with AsyncSessionLocal() as db:
        cand_q = select(func.count(RawWechatVoiceCall.record_id)).where(and_(*_candidate_filters(filters, min_dur)))
        candidates = int((await db.execute(cand_q)).scalar() or 0)

        succ_subq = (
            select(WechatVoiceTranscript.record_id)
            .where(WechatVoiceTranscript.status == STATUS_SUCCEEDED)
            .subquery()
        )
        covered_q = (
            select(func.count(RawWechatVoiceCall.record_id))
            .where(and_(*_candidate_filters(filters, min_dur)))
            .where(RawWechatVoiceCall.record_id.in_(select(succ_subq.c.record_id)))
        )
        covered = int((await db.execute(covered_q)).scalar() or 0)

        status_rows = (
            await db.execute(
                select(WechatVoiceTranscript.status, func.count(WechatVoiceTranscript.record_id)).group_by(
                    WechatVoiceTranscript.status
                )
            )
        ).all()
        by_status = {str(r[0] or ""): int(r[1] or 0) for r in status_rows}

        total_calls = int((await db.execute(select(func.count(RawWechatVoiceCall.record_id)))).scalar() or 0)
        connected = int(
            (await db.execute(select(func.count(RawWechatVoiceCall.record_id)).where(RawWechatVoiceCall.call_status == 1))).scalar()
            or 0
        )
        with_url = int(
            (
                await db.execute(
                    select(func.count(RawWechatVoiceCall.record_id)).where(
                        RawWechatVoiceCall.oss_file_name.isnot(None),
                        RawWechatVoiceCall.oss_file_name != "",
                    )
                )
            ).scalar()
            or 0
        )

    return {
        "total_calls": total_calls,
        "connected_calls": connected,
        "with_recording_url": with_url,
        "candidates": candidates,
        "covered_succeeded": covered,
        "coverage_pct": round(100.0 * covered / candidates, 1) if candidates else 0.0,
        "transcript_by_status": by_status,
        "min_duration_sec": min_dur,
    }


async def _upsert_transcript_rows(
    db,
    calls: list[RawWechatVoiceCall],
    *,
    batch_id: str,
    batch_label: str,
) -> tuple[int, int]:
    enqueued = 0
    skipped = 0
    now = datetime.now()
    label = (batch_label or "").strip() or f"voice-transcribe-{batch_id}"

    for call in calls:
        link = (call.oss_file_name or "").strip()
        if not _is_http_url(link):
            skipped += 1
            continue
        row = {
            "record_id": call.record_id,
            "we_chat_id": call.we_chat_id,
            "talker": call.talker,
            "file_link": link,
            "status": STATUS_PENDING,
            "batch_id": batch_id,
            "batch_label": label[:120],
            "attempts": 0,
            "poll_attempts": 0,
            "last_error": None,
            "is_send": call.is_send,
            "call_start_time": call.start_time,
            "duration_file": call.duration_file,
            "created_at": now,
            "updated_at": now,
        }
        stmt = mysql_insert(WechatVoiceTranscript).values(row)
        stmt = stmt.on_duplicate_key_update(
            file_link=stmt.inserted.file_link,
            status=STATUS_PENDING,
            batch_id=stmt.inserted.batch_id,
            batch_label=stmt.inserted.batch_label,
            attempts=0,
            poll_attempts=0,
            task_id=None,
            last_error=None,
            is_send=stmt.inserted.is_send,
            call_start_time=stmt.inserted.call_start_time,
            duration_file=stmt.inserted.duration_file,
            submitted_at=None,
            completed_at=None,
            transcript_text=None,
            transcript_json=None,
            sentence_count=None,
            char_count=None,
            updated_at=now,
        )
        res = await db.execute(stmt)
        if int(getattr(res, "rowcount", 0) or 0):
            enqueued += 1
        else:
            skipped += 1
    return enqueued, skipped


async def enqueue_voice_transcripts(
    filters: EnqueueFilters | None = None,
    *,
    batch_label: str = "",
) -> EnqueueResult:
    """将符合条件的通话写入转写队列表（幂等：已成功/进行中跳过）。"""
    filters = filters or EnqueueFilters()
    min_dur = filters.min_duration_sec if filters.min_duration_sec is not None else default_min_duration_sec()
    batch_id = uuid.uuid4().hex[:12]
    label = (batch_label or "").strip() or f"voice-transcribe-{batch_id}"

    async with AsyncSessionLocal() as db:
        q = (
            select(RawWechatVoiceCall)
            .outerjoin(WechatVoiceTranscript, WechatVoiceTranscript.record_id == RawWechatVoiceCall.record_id)
            .where(and_(*_candidate_filters(filters, min_dur)))
            .where(
                or_(
                    WechatVoiceTranscript.record_id.is_(None),
                    and_(
                        filters.include_failed,
                        WechatVoiceTranscript.status == STATUS_FAILED,
                    ),
                )
            )
            .where(
                or_(
                    WechatVoiceTranscript.record_id.is_(None),
                    WechatVoiceTranscript.status.notin_(
                        (STATUS_PENDING, STATUS_SUBMITTED, STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_SKIPPED)
                    ),
                )
            )
            .order_by(RawWechatVoiceCall.start_time.desc())
        )
        if filters.limit is not None and int(filters.limit) > 0:
            q = q.limit(int(filters.limit))

        rows = (await db.execute(q)).scalars().all()
        enqueued, skipped = await _upsert_transcript_rows(
            db, list(rows), batch_id=batch_id, batch_label=label
        )

        await db.commit()

    logger.info("voice_transcribe enqueue batch={} enqueued={} skipped={}", batch_id, enqueued, skipped)
    return EnqueueResult(batch_id=batch_id, enqueued=enqueued, skipped_existing=skipped)


async def enqueue_voice_transcripts_by_record_ids(
    record_ids: list[str],
    *,
    batch_label: str = "选中入队",
    require_connected: bool = True,
) -> EnqueueResult:
    """按 record_id 列表入队（通话明细批量操作用）。"""
    ids = [str(rid or "").strip() for rid in record_ids if str(rid or "").strip()]
    if not ids:
        return EnqueueResult(batch_id="", enqueued=0, skipped_existing=0)

    batch_id = uuid.uuid4().hex[:12]
    label = (batch_label or "").strip() or f"voice-transcribe-{batch_id}"

    async with AsyncSessionLocal() as db:
        q = (
            select(RawWechatVoiceCall)
            .outerjoin(WechatVoiceTranscript, WechatVoiceTranscript.record_id == RawWechatVoiceCall.record_id)
            .where(RawWechatVoiceCall.record_id.in_(ids))
            .where(RawWechatVoiceCall.is_room == 0)
            .where(RawWechatVoiceCall.call_type == 1)
            .where(
                or_(
                    WechatVoiceTranscript.record_id.is_(None),
                    WechatVoiceTranscript.status == STATUS_FAILED,
                )
            )
            .where(
                or_(
                    WechatVoiceTranscript.record_id.is_(None),
                    WechatVoiceTranscript.status.notin_(
                        (STATUS_PENDING, STATUS_SUBMITTED, STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_SKIPPED)
                    ),
                )
            )
        )
        if require_connected:
            q = q.where(RawWechatVoiceCall.call_status == 1)
        rows = (await db.execute(q)).scalars().unique().all()
        enqueued, skipped = await _upsert_transcript_rows(
            db, list(rows), batch_id=batch_id, batch_label=label
        )
        await db.commit()

    logger.info(
        "voice_transcribe enqueue_by_ids batch={} requested={} enqueued={} skipped={}",
        batch_id,
        len(ids),
        enqueued,
        skipped,
    )
    return EnqueueResult(batch_id=batch_id, enqueued=enqueued, skipped_existing=skipped)


async def retry_transcribe_record_ids(record_ids: list[str]) -> int:
    """将指定 record_id 的 failed/cancelled 重置为 pending，便于重新提交。"""
    ids = [str(rid or "").strip() for rid in (record_ids or []) if str(rid or "").strip()]
    if not ids:
        return 0
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text(
                """
                UPDATE wechat_voice_transcripts
                SET status='pending', attempts=0, poll_attempts=0, task_id=NULL,
                    last_error=NULL, submitted_at=NULL, completed_at=NULL,
                    transcript_text=NULL, transcript_json=NULL,
                    sentence_count=NULL, char_count=NULL,
                    updated_at=NOW()
                WHERE record_id IN :ids AND status IN ('failed','cancelled')
                """
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        )
        await db.commit()
        return int(getattr(res, "rowcount", 0) or 0)


async def _count_pending_transcripts(record_ids: list[str] | None = None) -> int:
    async with AsyncSessionLocal() as db:
        q = select(func.count(WechatVoiceTranscript.record_id)).where(
            WechatVoiceTranscript.status == STATUS_PENDING
        )
        ids = [str(rid or "").strip() for rid in (record_ids or []) if str(rid or "").strip()]
        if ids:
            q = q.where(WechatVoiceTranscript.record_id.in_(ids))
        return int((await db.execute(q)).scalar() or 0)


async def transcribe_calls_by_record_ids(
    record_ids: list[str],
    *,
    batch_label: str = "通话明细选中转写",
    auto_submit: bool = True,
    auto_poll: bool = True,
    enqueue_profile: bool = True,
) -> dict[str, Any]:
    """入队并可选立即提交 MiBuddy、轮询结果。

    enqueue_profile=True：转写成功后立即触发画像（手动/控制台路径）。
    enqueue_profile=False：仅产出转写文本，不画像（同步自动转写路径，画像交由夜间增量画像统一处理）。
    """
    ids = [str(rid or "").strip() for rid in record_ids if str(rid or "").strip()]
    await retry_transcribe_record_ids(ids)
    res = await enqueue_voice_transcripts_by_record_ids(ids, batch_label=batch_label)
    out: dict[str, Any] = {"enqueue": res}
    pending_n = await _count_pending_transcripts(ids) if ids else 0
    if auto_submit and ids and (res.enqueued > 0 or pending_n > 0):
        out["submit"] = await submit_pending(
            batch_size=max(res.enqueued, pending_n, await get_submit_batch_size()),
            record_ids=ids,
        )
    else:
        out["submit"] = {"claimed": 0, "submitted": 0, "failed": 0}

    if auto_poll and ids:
        active = await _count_active_transcripts(ids)
        if active > 0 or out["submit"].get("submitted") or out["submit"].get("failed"):
            poll_batch = max(
                int(active or 0),
                int(out["submit"].get("submitted") or 0),
                await get_poll_batch_size(),
            )
            out["poll"] = await poll_running(
                batch_size=poll_batch, record_ids=ids, enqueue_profile=enqueue_profile
            )
            if out["poll"].get("still_running") or out["poll"].get("claimed", 0):
                spawn_auto_poll_until_settled(ids, enqueue_profile=enqueue_profile)
        else:
            out["poll"] = {"claimed": 0, "succeeded": 0, "still_running": 0, "failed": 0}
    elif auto_poll:
        out["poll"] = {"claimed": 0, "succeeded": 0, "still_running": 0, "failed": 0}
    return out


async def load_bound_sales_wechat_ids() -> set[str]:
    """已绑定登录用户的销售微信号集合（与夜间画像绑定口径一致）。"""
    from models import UserSalesWechat

    async with AsyncSessionLocal() as db:
        res = await db.execute(select(UserSalesWechat.sales_wechat_id))
        return {
            str(s).strip() for s in res.scalars().all() if s and str(s).strip()
        }


async def auto_transcribe_synced_calls(
    record_ids: list[str],
    *,
    batch_label: str = "同步自动转写",
    min_duration_sec: int | None = None,
) -> dict[str, Any]:
    """语音增量同步入库后调用：对「绑定销售号 + 接通 + 有录音 + 达到时长」的新通话自动转写。

    不触发画像（enqueue_profile=False）；画像由夜间增量画像统一处理。
    前提：销售微信号必须已绑定登录用户，否则跳过。
    """
    ids = [str(rid or "").strip() for rid in (record_ids or []) if str(rid or "").strip()]
    base = {"eligible": 0, "transcribe": None}
    if not ids:
        return base

    min_dur = min_duration_sec if min_duration_sec is not None else default_min_duration_sec()
    bound = await load_bound_sales_wechat_ids()
    if not bound:
        logger.info("voice auto-transcribe skipped: 无已绑定销售微信号")
        return base

    async with AsyncSessionLocal() as db:
        q = (
            select(RawWechatVoiceCall.record_id)
            .where(RawWechatVoiceCall.record_id.in_(ids))
            .where(RawWechatVoiceCall.call_status == 1)
            .where(RawWechatVoiceCall.is_room == 0)
            .where(RawWechatVoiceCall.call_type == 1)
            .where(RawWechatVoiceCall.oss_file_name.isnot(None))
            .where(RawWechatVoiceCall.oss_file_name != "")
            .where(RawWechatVoiceCall.duration_file >= min_dur)
            .where(RawWechatVoiceCall.we_chat_id.in_(bound))
        )
        eligible = [str(r[0]) for r in (await db.execute(q)).all() if r and r[0]]

    base["eligible"] = len(eligible)
    if not eligible:
        return base

    base["transcribe"] = await transcribe_calls_by_record_ids(
        eligible,
        batch_label=batch_label,
        enqueue_profile=False,
    )
    logger.info(
        "voice auto-transcribe on sync: eligible={} result={}",
        len(eligible),
        base["transcribe"],
    )
    return base


async def cancel_all_pending() -> int:
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text(
                """
                UPDATE wechat_voice_transcripts
                SET status='cancelled', updated_at=NOW()
                WHERE status='pending'
                """
            )
        )
        await db.commit()
        return int(getattr(res, "rowcount", 0) or 0)


async def retry_failed(*, limit: int = 500) -> int:
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text(
                """
                UPDATE wechat_voice_transcripts
                SET status='pending', attempts=0, poll_attempts=0, task_id=NULL,
                    last_error=NULL, submitted_at=NULL, completed_at=NULL,
                    updated_at=NOW()
                WHERE status='failed'
                LIMIT :n
                """
            ),
            {"n": int(limit)},
        )
        await db.commit()
        return int(getattr(res, "rowcount", 0) or 0)


async def reclaim_stale_running(*, stale_hours: int = 2) -> int:
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text(
                """
                UPDATE wechat_voice_transcripts
                SET status='pending', task_id=NULL, poll_attempts=0,
                    last_error=CONCAT('reclaimed-stale@', NOW(), ' | ', COALESCE(LEFT(last_error,200),'')),
                    updated_at=NOW()
                WHERE status IN ('submitted','running')
                  AND (submitted_at IS NULL OR submitted_at < (NOW() - INTERVAL :h HOUR))
                """
            ),
            {"h": int(stale_hours)},
        )
        await db.commit()
        return int(getattr(res, "rowcount", 0) or 0)


async def _claim_by_status(
    statuses: tuple[str, ...],
    limit: int,
    *,
    record_ids: list[str] | None = None,
) -> list[WechatVoiceTranscript]:
    out: list[WechatVoiceTranscript] = []
    ids = [str(rid or "").strip() for rid in (record_ids or []) if str(rid or "").strip()]
    async with AsyncSessionLocal() as db:
        async with db.begin():
            q = (
                select(WechatVoiceTranscript)
                .where(WechatVoiceTranscript.status.in_(statuses))
                .order_by(WechatVoiceTranscript.record_id.asc())
                .limit(int(limit))
                .with_for_update(skip_locked=True)
            )
            if ids:
                q = q.where(WechatVoiceTranscript.record_id.in_(ids))
            res = await db.execute(q)
            rows = list(res.scalars().all())
            if not rows:
                return []
            row_ids = [r.record_id for r in rows]
            stmt = text(
                """
                UPDATE wechat_voice_transcripts SET updated_at=NOW()
                WHERE record_id IN :ids
                """
            ).bindparams(bindparam("ids", expanding=True))
            await db.execute(stmt, {"ids": row_ids})
            out = rows
    return out


async def submit_pending(
    *,
    batch_size: int | None = None,
    record_ids: list[str] | None = None,
) -> dict[str, int]:
    """提交 pending 任务到 MiBuddy。"""
    n = batch_size if batch_size is not None else await get_submit_batch_size()
    submitted = failed = 0
    rows = await _claim_by_status((STATUS_PENDING,), n, record_ids=record_ids)
    for row in rows:
        link = (row.file_link or "").strip()
        if not _is_http_url(link):
            async with AsyncSessionLocal() as db:
                await db.execute(
                    text(
                        """
                        UPDATE wechat_voice_transcripts
                        SET status='skipped', last_error='无有效录音 URL', updated_at=NOW(), completed_at=NOW()
                        WHERE record_id=:rid
                        """
                    ),
                    {"rid": row.record_id},
                )
                await db.commit()
            continue
        try:
            task_id = await submit_file_trans_request(link)
            async with AsyncSessionLocal() as db:
                await db.execute(
                    text(
                        """
                        UPDATE wechat_voice_transcripts
                        SET status='submitted', task_id=:tid, submitted_at=NOW(), updated_at=NOW(),
                            attempts=attempts+1, last_error=NULL
                        WHERE record_id=:rid
                        """
                    ),
                    {"rid": row.record_id, "tid": task_id},
                )
                await db.commit()
            submitted += 1
        except (MibuddyApiError, Exception) as e:
            err = f"{type(e).__name__}: {e}"
            async with AsyncSessionLocal() as db:
                await db.execute(
                    text(
                        """
                        UPDATE wechat_voice_transcripts
                        SET attempts=attempts+1, last_error=:err, updated_at=NOW(),
                            status=CASE WHEN attempts+1 >= :max_a THEN 'failed' ELSE 'pending' END
                        WHERE record_id=:rid
                        """
                    ),
                    {"rid": row.record_id, "err": err[:8000], "max_a": MAX_SUBMIT_ATTEMPTS},
                )
                await db.commit()
            failed += 1
    return {"claimed": len(rows), "submitted": submitted, "failed": failed}


async def _enqueue_profile_for_pairs(pairs: list[tuple[str, str]]) -> int:
    if not pairs:
        return 0
    from ai.profile_queue import enqueue_pairs
    from ai.profiling_progress import new_batch_meta

    batch = new_batch_meta("voice_transcript", len(pairs), "语音转写完成后重算画像")
    res = await enqueue_pairs(pairs, batch_id=batch["batch_id"], batch_label=batch["label"])
    return res.enqueued


async def poll_running(
    *,
    batch_size: int | None = None,
    enqueue_profile: bool = True,
    record_ids: list[str] | None = None,
) -> dict[str, int]:
    """轮询 submitted/running 任务。"""
    from ai.voice_transcript_format import format_transcript_from_result

    n = batch_size if batch_size is not None else await get_poll_batch_size()
    succeeded = still_running = failed = 0
    profile_pairs: list[tuple[str, str]] = []

    rows = await _claim_by_status(
        (STATUS_SUBMITTED, STATUS_RUNNING),
        n,
        record_ids=record_ids,
    )
    for row in rows:
        tid = (row.task_id or "").strip()
        if not tid:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    text(
                        """
                        UPDATE wechat_voice_transcripts
                        SET status='pending', updated_at=NOW(), last_error='缺少 task_id'
                        WHERE record_id=:rid
                        """
                    ),
                    {"rid": row.record_id},
                )
                await db.commit()
            continue

        try:
            data = await get_file_trans_result(tid)
            st = str(data.get("status") or "").upper()
            if st in ("RUNNING", "QUEUEING"):
                new_poll = int(row.poll_attempts or 0) + 1
                if new_poll >= MAX_POLL_ATTEMPTS:
                    async with AsyncSessionLocal() as db:
                        await db.execute(
                            text(
                                """
                                UPDATE wechat_voice_transcripts
                                SET status='failed', poll_attempts=:pa, last_error='轮询超时',
                                    completed_at=NOW(), updated_at=NOW()
                                WHERE record_id=:rid
                                """
                            ),
                            {"rid": row.record_id, "pa": new_poll},
                        )
                        await db.commit()
                    failed += 1
                    continue
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        text(
                            """
                            UPDATE wechat_voice_transcripts
                            SET status='running', poll_attempts=:pa, updated_at=NOW()
                            WHERE record_id=:rid
                            """
                        ),
                        {"rid": row.record_id, "pa": new_poll},
                    )
                    await db.commit()
                still_running += 1
                continue

            if st != "SUCCESS":
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        text(
                            """
                            UPDATE wechat_voice_transcripts
                            SET status='failed', poll_attempts=poll_attempts+1,
                                last_error=:err, completed_at=NOW(), updated_at=NOW()
                            WHERE record_id=:rid
                            """
                        ),
                        {"rid": row.record_id, "err": f"未知状态: {st}"},
                    )
                    await db.commit()
                failed += 1
                continue

            dialogue, json_str, sc, cc = format_transcript_from_result(
                data.get("result"), is_send=row.is_send
            )
            if not dialogue.strip():
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        text(
                            """
                            UPDATE wechat_voice_transcripts
                            SET status='failed', transcript_json=:js, last_error='转写结果为空',
                                completed_at=NOW(), updated_at=NOW()
                            WHERE record_id=:rid
                            """
                        ),
                        {"rid": row.record_id, "js": json_str[:65535] if json_str else None},
                    )
                    await db.commit()
                failed += 1
                continue

            async with AsyncSessionLocal() as db:
                await db.execute(
                    text(
                        """
                        UPDATE wechat_voice_transcripts
                        SET status='succeeded', transcript_text=:txt, transcript_json=:js,
                            sentence_count=:sc, char_count=:cc, completed_at=NOW(), updated_at=NOW(),
                            last_error=NULL
                        WHERE record_id=:rid
                        """
                    ),
                    {
                        "rid": row.record_id,
                        "txt": dialogue,
                        "js": json_str[:65535] if json_str else None,
                        "sc": sc,
                        "cc": cc,
                    },
                )
                await db.commit()
            succeeded += 1
            profile_pairs.append((row.talker, row.we_chat_id))

        except (MibuddyApiError, Exception) as e:
            err = f"{type(e).__name__}: {e}"
            async with AsyncSessionLocal() as db:
                await db.execute(
                    text(
                        """
                        UPDATE wechat_voice_transcripts
                        SET poll_attempts=poll_attempts+1, last_error=:err, updated_at=NOW(),
                            status=CASE WHEN poll_attempts+1 >= :max_p THEN 'failed' ELSE status END,
                            completed_at=CASE WHEN poll_attempts+1 >= :max_p THEN NOW() ELSE completed_at END
                        WHERE record_id=:rid
                        """
                    ),
                    {"rid": row.record_id, "err": err[:8000], "max_p": MAX_POLL_ATTEMPTS},
                )
                await db.commit()
            failed += 1

    profile_enqueued = 0
    if enqueue_profile and profile_pairs:
        uniq = list(dict.fromkeys(profile_pairs))
        profile_enqueued = await _enqueue_profile_for_pairs(uniq)

    return {
        "claimed": len(rows),
        "succeeded": succeeded,
        "still_running": still_running,
        "failed": failed,
        "profile_enqueued": profile_enqueued,
    }


async def _count_active_transcripts(record_ids: list[str] | None = None) -> int:
    async with AsyncSessionLocal() as db:
        q = select(func.count(WechatVoiceTranscript.record_id)).where(
            WechatVoiceTranscript.status.in_((STATUS_SUBMITTED, STATUS_RUNNING))
        )
        ids = [str(rid or "").strip() for rid in (record_ids or []) if str(rid or "").strip()]
        if ids:
            q = q.where(WechatVoiceTranscript.record_id.in_(ids))
        return int((await db.execute(q)).scalar() or 0)


_auto_poll_tasks: set[asyncio.Task] = set()


async def auto_poll_until_settled(
    record_ids: list[str] | None = None,
    *,
    max_rounds: int = 36,
    interval_sec: float = 5.0,
    initial_delay_sec: float = 2.0,
    enqueue_profile: bool = True,
) -> dict[str, int]:
    """后台轮询直至指定记录（或全部在途任务）结束。"""
    ids = [str(rid or "").strip() for rid in (record_ids or []) if str(rid or "").strip()] or None
    totals = {"rounds": 0, "succeeded": 0, "failed": 0, "still_running": 0}
    if initial_delay_sec > 0:
        await asyncio.sleep(initial_delay_sec)
    for round_i in range(max_rounds):
        if round_i > 0:
            await asyncio.sleep(interval_sec)
        active = await _count_active_transcripts(ids)
        if active <= 0:
            break
        pol = await poll_running(
            batch_size=max(active, await get_poll_batch_size()),
            record_ids=ids,
            enqueue_profile=enqueue_profile,
        )
        totals["rounds"] += 1
        totals["succeeded"] += int(pol.get("succeeded") or 0)
        totals["failed"] += int(pol.get("failed") or 0)
        totals["still_running"] = int(pol.get("still_running") or 0)
    return totals


def spawn_auto_poll_until_settled(
    record_ids: list[str] | None = None,
    *,
    max_rounds: int = 36,
    interval_sec: float = 5.0,
    enqueue_profile: bool = True,
) -> None:
    """非阻塞：提交转写后在后台持续轮询 MiBuddy 直至完成。"""
    ids = [str(rid or "").strip() for rid in (record_ids or []) if str(rid or "").strip()] or None

    async def _runner() -> None:
        try:
            res = await auto_poll_until_settled(
                ids,
                max_rounds=max_rounds,
                interval_sec=interval_sec,
                enqueue_profile=enqueue_profile,
            )
            if res.get("succeeded") or res.get("failed"):
                logger.info("voice_transcribe auto_poll settled ids={} result={}", ids, res)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("voice_transcribe auto_poll error: %s", e)

    task = asyncio.create_task(_runner())
    _auto_poll_tasks.add(task)
    task.add_done_callback(_auto_poll_tasks.discard)


async def snapshot_queue(filters: EnqueueFilters | None = None) -> dict[str, Any]:
    stats = await count_candidates(filters)
    async with AsyncSessionLocal() as db:
        agg = (
            await db.execute(
                select(WechatVoiceTranscript.status, func.count(WechatVoiceTranscript.record_id)).group_by(
                    WechatVoiceTranscript.status
                )
            )
        ).all()
        counts = {str(r[0] or ""): int(r[1] or 0) for r in agg}
        total = sum(counts.values())
        pending = counts.get(STATUS_PENDING, 0)
        submitted = counts.get(STATUS_SUBMITTED, 0)
        running = counts.get(STATUS_RUNNING, 0)
        succeeded = counts.get(STATUS_SUCCEEDED, 0)
        failed = counts.get(STATUS_FAILED, 0)
        skipped = counts.get(STATUS_SKIPPED, 0)
        cancelled = counts.get(STATUS_CANCELLED, 0)

        cur = (
            await db.execute(
                text(
                    """
                    SELECT batch_id, batch_label, MIN(record_id) AS min_id
                    FROM wechat_voice_transcripts
                    WHERE status IN ('pending','submitted','running')
                      AND batch_id IS NOT NULL AND batch_id <> ''
                    GROUP BY batch_id, batch_label
                    ORDER BY min_id ASC
                    LIMIT 1
                    """
                )
            )
        ).first()
        batch_id = str(cur[0] or "") if cur else ""
        batch_label = str(cur[1] or "") if cur else ""

        err_res = await db.execute(
            text(
                """
                SELECT record_id, task_id, UNIX_TIMESTAMP(updated_at) AS ts, last_error
                FROM wechat_voice_transcripts
                WHERE status='failed'
                ORDER BY updated_at DESC
                LIMIT 30
                """
            )
        )
        errors = [
            {
                "record_id": str(r[0] or ""),
                "task_id": str(r[1] or ""),
                "at": int(r[2] or 0),
                "message": (str(r[3] or "").strip() or "failed")[:2000],
            }
            for r in err_res.all()
        ]

        recent_res = await db.execute(
            text(
                """
                SELECT record_id, we_chat_id, talker, call_start_time, duration_file,
                       char_count, LEFT(transcript_text, 280) AS preview
                FROM wechat_voice_transcripts
                WHERE status='succeeded'
                ORDER BY completed_at DESC
                LIMIT 8
                """
            )
        )
        recent_success = [
            {
                "record_id": str(r[0] or ""),
                "sales_wechat_id": str(r[1] or ""),
                "talker": str(r[2] or ""),
                "call_start_time": str(r[3] or ""),
                "duration_file": int(r[4] or 0),
                "char_count": int(r[5] or 0),
                "preview": str(r[6] or ""),
            }
            for r in recent_res.all()
        ]

    processed = succeeded + failed + skipped + cancelled
    percent = round(100.0 * processed / total, 1) if total else 0.0
    paused = await _paused()
    cancel_req = await _cancel_requested()
    submit_batch = await get_submit_batch_size()
    poll_batch = await get_poll_batch_size()

    active = pending + submitted + running
    status = "paused" if paused else ("running" if active > 0 else "idle")

    return {
        "status": status,
        "stats": stats,
        "counts_by_status": counts,
        "total": total,
        "pending": pending,
        "submitted": submitted,
        "running": running,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "cancelled": cancelled,
        "processed": processed,
        "percent": percent,
        "current_batch_id": batch_id,
        "current_batch_label": batch_label,
        "recent_errors": errors,
        "recent_success": recent_success,
        "cancel_requested": cancel_req,
        "paused": paused,
        "submit_batch": submit_batch,
        "poll_batch": poll_batch,
    }


async def run_worker_loop(*, poll_interval: float = 15.0) -> None:
    """后台 worker：周期性 submit + poll（测试阶段可在管理页手动触发）。"""
    logger.info("voice_transcribe worker starting poll_interval={}s", poll_interval)
    while True:
        try:
            if await _paused():
                await asyncio.sleep(1.0)
                continue
            if await _cancel_requested():
                await asyncio.sleep(1.0)
                continue
            async with _worker_lock:
                sub = await submit_pending()
                pol = await poll_running()
                if sub.get("submitted") or pol.get("succeeded") or pol.get("failed"):
                    logger.info("voice_transcribe worker submit={} poll={}", sub, pol)
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("voice_transcribe worker loop error: %s", e)
            await asyncio.sleep(2.0)


async def load_voice_transcripts_for_profile(
    db,
    sales_wechat_id: str,
    raw_customer_id: str,
    *,
    max_calls: int = 5,
    max_chars: int = 6000,
) -> str:
    """加载已成功转写的通话原文，供画像投喂。"""
    sw = (sales_wechat_id or "").strip()
    rid = (raw_customer_id or "").strip()
    if not sw or not rid:
        return ""

    stmt = (
        select(WechatVoiceTranscript)
        .where(WechatVoiceTranscript.we_chat_id == sw)
        .where(WechatVoiceTranscript.talker == rid)
        .where(WechatVoiceTranscript.status == STATUS_SUCCEEDED)
        .where(WechatVoiceTranscript.transcript_text.isnot(None))
        .order_by(WechatVoiceTranscript.call_start_time.desc())
        .limit(max(1, int(max_calls)))
    )
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return ""

    blocks: list[str] = []
    used = 0
    for row in rows:
        txt = (row.transcript_text or "").strip()
        if not txt:
            continue
        dur = int(row.duration_file or 0)
        when = row.call_start_time.strftime("%Y-%m-%d %H:%M") if row.call_start_time else "未知时间"
        header = f"--- 通话 {when}（时长{dur}秒）---"
        chunk = f"{header}\n{txt}"
        if used + len(chunk) > max_chars and blocks:
            break
        if len(chunk) > max_chars:
            chunk = chunk[: max_chars - 20] + "\n…(截断)"
        blocks.append(chunk)
        used += len(chunk)
        if used >= max_chars:
            break

    return "\n\n".join(blocks)


async def load_transcript_summary_for_customer(
    db,
    sales_wechat_id: str,
    raw_customer_id: str,
) -> dict[str, Any]:
    """任务分配用：转写覆盖与最近一通摘要。"""
    sw = (sales_wechat_id or "").strip()
    rid = (raw_customer_id or "").strip()
    if not sw or not rid:
        return {}

    cnt = int(
        (
            await db.execute(
                select(func.count(WechatVoiceTranscript.record_id))
                .where(WechatVoiceTranscript.we_chat_id == sw)
                .where(WechatVoiceTranscript.talker == rid)
                .where(WechatVoiceTranscript.status == STATUS_SUCCEEDED)
            )
        ).scalar()
        or 0
    )
    if cnt <= 0:
        return {"has_transcript": False, "transcript_count": 0}

    last = (
        await db.execute(
            select(WechatVoiceTranscript)
            .where(WechatVoiceTranscript.we_chat_id == sw)
            .where(WechatVoiceTranscript.talker == rid)
            .where(WechatVoiceTranscript.status == STATUS_SUCCEEDED)
            .order_by(WechatVoiceTranscript.call_start_time.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    gist = ""
    if last and last.transcript_text:
        t = last.transcript_text.replace("\n", " ").strip()
        gist = (t[:120] + "…") if len(t) > 120 else t

    return {
        "has_transcript": True,
        "transcript_count": cnt,
        "last_call_gist": gist,
    }
