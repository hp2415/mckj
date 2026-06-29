"""任务分配 DB 队列：定时/批量分配时并行消费，避免串行跑不完。"""
from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import bindparam, text

from ai.task_allocation import (
    PERIOD_MONTHLY,
    generate_allocation_batch,
    list_active_sales_wechat_ids,
    period_bounds,
    today_shanghai,
)
from core.logger import logger
from core.system_config_store import upsert_system_config_row
from database import AsyncSessionLocal

CFG_CONCURRENCY_KEY = "task_allocation_worker_concurrency"
CFG_PAUSE_KEY = "task_allocation_worker_paused"
CONCURRENCY_MIN = 1
CONCURRENCY_MAX = 16


def _env_concurrency_default() -> int:
    try:
        raw = int(os.getenv("TASK_ALLOCATION_WORKER_CONCURRENCY") or "4")
    except ValueError:
        raw = 4
    return max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, raw))


def _clamp_concurrency(value: int) -> int:
    return max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, int(value)))


async def get_worker_concurrency() -> int:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("SELECT config_value FROM system_configs WHERE config_key=:k LIMIT 1"),
            {"k": CFG_CONCURRENCY_KEY},
        )
        row = r.first()
        if row and str(row[0] or "").strip():
            try:
                return _clamp_concurrency(int(str(row[0]).strip()))
            except ValueError:
                pass
    return _env_concurrency_default()


async def set_worker_concurrency(value: int) -> int:
    v = _clamp_concurrency(value)
    async with AsyncSessionLocal() as db:
        await upsert_system_config_row(
            db,
            config_key=CFG_CONCURRENCY_KEY,
            config_value=str(v),
            config_group="task",
            description="任务分配 worker 并发上限（单进程内同时执行的销售批次数）",
        )
        await db.commit()
    return v


def _worker_id() -> str:
    base = os.getenv("TASK_ALLOCATION_WORKER_ID") or socket.gethostname()
    return (base or "alloc-worker")[:80]


def _dedupe_key(sales_wechat_id: str, period_type: str, period_start: date) -> str:
    return f"alloc:{period_type}:{period_start.isoformat()}:{sales_wechat_id}"


@dataclass
class EnqueueResult:
    batch_id: str
    enqueued: int
    deduped: int
    skipped_invalid: int
    job_ids: dict[str, int]


class QueueTableMissingError(RuntimeError):
    """task_allocation_queue_jobs 表未迁移。"""


async def ensure_queue_table() -> None:
    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text("SELECT 1 FROM task_allocation_queue_jobs LIMIT 1"))
        except Exception as e:
            msg = (
                "task_allocation_queue_jobs 表不存在，请先执行："
                "cd backend && alembic upgrade head"
            )
            raise QueueTableMissingError(msg) from e


async def _lookup_active_queue_job_id(
    db, *, sales_wechat_id: str, period_type: str, period_start: date
) -> int | None:
    dk = _dedupe_key(sales_wechat_id, period_type, period_start)
    res = await db.execute(
        text(
            """
            SELECT id FROM task_allocation_queue_jobs
            WHERE dedupe_key = :dk AND status IN ('pending', 'running')
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"dk": dk},
    )
    row = res.first()
    return int(row[0]) if row else None


async def get_queue_job(job_id: int) -> dict[str, Any] | None:
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text(
                """
                SELECT id, sales_wechat_id, period_type, period_start, status,
                       result_batch_id, last_error, batch_label
                FROM task_allocation_queue_jobs
                WHERE id = :id
                LIMIT 1
                """
            ),
            {"id": int(job_id)},
        )
        row = res.first()
        if not row:
            return None
        return {
            "id": int(row[0]),
            "sales_wechat_id": str(row[1]),
            "period_type": str(row[2]),
            "period_start": row[3],
            "status": str(row[4]),
            "result_batch_id": int(row[5]) if row[5] is not None else None,
            "last_error": (str(row[6] or "").strip() or None),
            "batch_label": str(row[7] or ""),
        }


async def enqueue_single_or_get_active(
    sales_wechat_id: str,
    period_type: str,
    *,
    ref_date: date | None = None,
    source: str = "manual_regen",
    auto_publish: bool = False,
    batch_label: str = "",
) -> int | None:
    """单销售入队；若已有 pending/running 则返回已有 job id。"""
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return None
    await ensure_queue_table()
    ref_date = ref_date or today_shanghai()
    period_start, _ = period_bounds(period_type, ref_date)
    async with AsyncSessionLocal() as db:
        existing = await _lookup_active_queue_job_id(
            db, sales_wechat_id=sw, period_type=period_type, period_start=period_start
        )
        if existing:
            return existing
    result = await enqueue_sales_allocations(
        period_type,
        [sw],
        ref_date=ref_date,
        source=source,
        auto_publish=auto_publish,
        batch_label=batch_label or f"手动分配 {sw}",
    )
    jid = result.job_ids.get(sw)
    if jid:
        return jid
    async with AsyncSessionLocal() as db:
        return await _lookup_active_queue_job_id(
            db, sales_wechat_id=sw, period_type=period_type, period_start=period_start
        )


async def wait_and_sync_memory_job(
    mem_job_id: str,
    queue_job_id: int,
    sales_wechat_id: str,
    period_type: str,
) -> None:
    """轮询 DB 队列任务，同步进度到内存 job（供管理端 format=job 轮询）。"""
    from ai.task_allocation import find_generating_batch
    from ai.task_allocation_jobs import update_job
    from models import TaskAllocationBatch
    from sqlalchemy.future import select

    sw = (sales_wechat_id or "").strip()
    p_start, _ = period_bounds(period_type, today_shanghai())
    while True:
        row = await get_queue_job(queue_job_id)
        if not row:
            await update_job(
                mem_job_id,
                status="error",
                phase="队列异常",
                error="队列任务不存在或已被清理",
                pct=1.0,
            )
            return

        st = row["status"]
        if st == "pending":
            await update_job(
                mem_job_id,
                status="queued",
                phase="队列排队中",
                detail=f"queue#{queue_job_id} · {sw}",
                pct=0.05,
            )
        elif st == "running":
            batch_id = row.get("result_batch_id")
            phase = "模型分配中"
            detail = sw
            pct = 0.1
            async with AsyncSessionLocal() as db:
                gen = await find_generating_batch(db, sw, period_type, p_start)
                if gen:
                    batch_id = gen.id
                    prog = (gen.input_snapshot_json or {}).get("progress") or {}
                    phase = str(prog.get("phase") or phase)
                    detail = str(prog.get("detail") or detail)
                    try:
                        pct = float(prog.get("pct") or pct)
                    except (TypeError, ValueError):
                        pct = 0.1
            await update_job(
                mem_job_id,
                status="running",
                phase=phase,
                detail=detail,
                pct=pct,
                batch_id=batch_id,
            )
        elif st == "succeeded":
            batch_id = row.get("result_batch_id")
            task_count = None
            if batch_id:
                async with AsyncSessionLocal() as db:
                    res = await db.execute(
                        select(TaskAllocationBatch).where(TaskAllocationBatch.id == int(batch_id))
                    )
                    b = res.scalars().first()
                    if b:
                        task_count = b.task_count
            await update_job(
                mem_job_id,
                status="done",
                phase="完成",
                detail=f"batch_id={batch_id}",
                pct=1.0,
                batch_id=batch_id,
                task_count=task_count,
            )
            return
        elif st == "failed":
            await update_job(
                mem_job_id,
                status="error",
                phase="失败",
                error=row.get("last_error") or "队列任务失败",
                pct=1.0,
                batch_id=row.get("result_batch_id"),
            )
            return

        await asyncio.sleep(1.2)


class _ConcurrencyGate:
    def __init__(self, limit: int) -> None:
        self.limit = _clamp_concurrency(limit)
        self.in_flight = 0
        self._lock = asyncio.Lock()

    def set_limit(self, limit: int) -> None:
        self.limit = _clamp_concurrency(limit)

    def slots_free(self) -> int:
        return max(0, self.limit - self.in_flight)

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                if self.in_flight < self.limit:
                    self.in_flight += 1
                    return
            await asyncio.sleep(0.05)

    def release(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)


async def _paused() -> bool:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("SELECT config_value FROM system_configs WHERE config_key=:k LIMIT 1"),
            {"k": CFG_PAUSE_KEY},
        )
        row = r.first()
        v = (row[0] if row else "") or ""
        return str(v).strip() not in ("0", "", "false", "False", "off", "OFF")


async def enqueue_sales_allocations(
    period_type: str,
    sales_wechat_ids: list[str],
    *,
    ref_date: date | None = None,
    source: str = "ai_auto",
    auto_publish: bool = True,
    batch_label: str = "",
) -> EnqueueResult:
    if period_type == PERIOD_MONTHLY:
        return EnqueueResult(
            batch_id="", enqueued=0, deduped=0, skipped_invalid=0, job_ids={}
        )

    await ensure_queue_table()

    ref_date = ref_date or today_shanghai()
    period_start, period_end = period_bounds(period_type, ref_date)
    run_batch_id = uuid.uuid4().hex[:16]
    label = (batch_label or f"任务分配 {period_type} {period_start}").strip()[:120]

    sw_ids = sorted({(s or "").strip() for s in (sales_wechat_ids or []) if (s or "").strip()})
    if not sw_ids:
        return EnqueueResult(
            batch_id=run_batch_id, enqueued=0, deduped=0, skipped_invalid=0, job_ids={}
        )

    async with AsyncSessionLocal() as db:
        active = set(await list_active_sales_wechat_ids(db))

    enqueued = 0
    deduped = 0
    skipped_invalid = 0
    job_ids: dict[str, int] = {}

    async with AsyncSessionLocal() as db:
        for sw in sw_ids:
            if sw not in active:
                skipped_invalid += 1
                logger.warning("任务分配入队跳过未知销售号 sw={}", sw)
                continue
            dk = _dedupe_key(sw, period_type, period_start)
            existing = await _lookup_active_queue_job_id(
                db, sales_wechat_id=sw, period_type=period_type, period_start=period_start
            )
            if existing:
                deduped += 1
                job_ids[sw] = existing
                continue
            res = await db.execute(
                text(
                    """
                    INSERT INTO task_allocation_queue_jobs
                      (sales_wechat_id, period_type, period_start, period_end, ref_date,
                       source, auto_publish, dedupe_key, batch_id, batch_label,
                       status, attempts, created_at, updated_at)
                    SELECT :sw, :pt, :ps, :pe, :rd, :src, :ap, :dk, :bid, :bl,
                           'pending', 0, NOW(), NOW()
                    WHERE NOT EXISTS (
                      SELECT 1 FROM task_allocation_queue_jobs q
                      WHERE q.dedupe_key = :dk
                        AND q.status IN ('pending', 'running')
                      LIMIT 1
                    )
                    """
                ),
                {
                    "sw": sw,
                    "pt": period_type,
                    "ps": period_start,
                    "pe": period_end,
                    "rd": ref_date,
                    "src": (source or "ai_auto")[:30],
                    "ap": 1 if auto_publish else 0,
                    "dk": dk,
                    "bid": run_batch_id,
                    "bl": label,
                },
            )
            n = int(getattr(res, "rowcount", 0) or 0)
            if n:
                enqueued += 1
                qid = await _lookup_active_queue_job_id(
                    db, sales_wechat_id=sw, period_type=period_type, period_start=period_start
                )
                if qid:
                    job_ids[sw] = qid
            else:
                deduped += 1
                qid = await _lookup_active_queue_job_id(
                    db, sales_wechat_id=sw, period_type=period_type, period_start=period_start
                )
                if qid:
                    job_ids[sw] = qid
        await db.commit()

    logger.info(
        "任务分配入队 period={} batch={} enqueued={} deduped={} skipped_invalid={}",
        period_type,
        run_batch_id,
        enqueued,
        deduped,
        skipped_invalid,
    )
    return EnqueueResult(
        batch_id=run_batch_id,
        enqueued=enqueued,
        deduped=deduped,
        skipped_invalid=skipped_invalid,
        job_ids=job_ids,
    )


async def snapshot_queue() -> dict[str, Any]:
    try:
        await ensure_queue_table()
    except QueueTableMissingError as e:
        return {
            "status": "unavailable",
            "table_missing": True,
            "message": str(e),
            "pending": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "current_batch_id": "",
            "current_batch_label": "",
            "recent_errors": [],
            "worker_concurrency": await get_worker_concurrency(),
            "worker_concurrency_max": CONCURRENCY_MAX,
            "paused": False,
        }
    async with AsyncSessionLocal() as db:
        agg = await db.execute(
            text(
                """
                SELECT status, COUNT(*) AS c
                FROM task_allocation_queue_jobs
                GROUP BY status
                """
            )
        )
        counts = {str(r[0] or ""): int(r[1] or 0) for r in agg.all()}
        pending = counts.get("pending", 0)
        running = counts.get("running", 0)
        done = counts.get("succeeded", 0)
        failed = counts.get("failed", 0)
        cancelled = counts.get("cancelled", 0)

        cur_res = await db.execute(
            text(
                """
                SELECT batch_id, batch_label, MIN(id) AS min_id
                FROM task_allocation_queue_jobs
                WHERE status IN ('running','pending')
                  AND batch_id IS NOT NULL AND batch_id <> ''
                GROUP BY batch_id, batch_label
                ORDER BY min_id ASC
                LIMIT 1
                """
            )
        )
        cur_row = cur_res.first()
        current_batch_id = str(cur_row[0] or "") if cur_row else ""
        current_batch_label = str(cur_row[1] or "") if cur_row else ""

        err_res = await db.execute(
            text(
                """
                SELECT id, sales_wechat_id, period_type, UNIX_TIMESTAMP(updated_at) AS ts, last_error
                FROM task_allocation_queue_jobs
                WHERE status='failed'
                ORDER BY updated_at DESC
                LIMIT 20
                """
            )
        )
        errors = [
            {
                "job_id": int(jid),
                "target": f"{sw}|{pt}",
                "at": int(ts or 0),
                "message": (str(msg or "").strip() or "failed")[:2000],
            }
            for jid, sw, pt, ts, msg in err_res.all()
        ]

    worker_concurrency = await get_worker_concurrency()
    paused = await _paused()
    total_active = pending + running
    return {
        "status": "paused" if paused else ("running" if total_active > 0 else "idle"),
        "table_missing": False,
        "pending": pending,
        "running": running,
        "succeeded": done,
        "failed": failed,
        "cancelled": cancelled,
        "current_batch_id": current_batch_id,
        "current_batch_label": current_batch_label,
        "recent_errors": errors,
        "worker_concurrency": worker_concurrency,
        "worker_concurrency_max": CONCURRENCY_MAX,
        "paused": paused,
    }


async def _claim_jobs(limit: int) -> list[dict[str, Any]]:
    wid = _worker_id()
    out: list[dict[str, Any]] = []
    async with AsyncSessionLocal() as db:
        async with db.begin():
            res = await db.execute(
                text(
                    """
                    SELECT id, sales_wechat_id, period_type, period_start, period_end,
                           ref_date, source, auto_publish
                    FROM task_allocation_queue_jobs
                    WHERE status='pending'
                    ORDER BY id ASC
                    LIMIT :n
                    FOR UPDATE SKIP LOCKED
                    """
                ),
                {"n": int(limit)},
            )
            rows = res.all()
            if not rows:
                return []
            ids = [int(r[0]) for r in rows]
            stmt = text(
                """
                UPDATE task_allocation_queue_jobs
                SET status='running', locked_by=:wid, locked_at=NOW(), updated_at=NOW()
                WHERE id IN :ids
                """
            ).bindparams(bindparam("ids", expanding=True))
            await db.execute(stmt, {"wid": wid, "ids": ids})
            for row in rows:
                out.append(
                    {
                        "id": int(row[0]),
                        "sales_wechat_id": str(row[1]),
                        "period_type": str(row[2]),
                        "period_start": row[3],
                        "period_end": row[4],
                        "ref_date": row[5],
                        "source": str(row[6] or "ai_auto"),
                        "auto_publish": bool(row[7]),
                    }
                )
    return out


async def _mark_done(job_id: int, result_batch_id: int | None) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                """
                UPDATE task_allocation_queue_jobs
                SET status='succeeded', result_batch_id=:bid, updated_at=NOW()
                WHERE id=:id
                """
            ),
            {"id": int(job_id), "bid": result_batch_id},
        )
        await db.commit()


async def _mark_failed(job_id: int, err: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                """
                UPDATE task_allocation_queue_jobs
                SET status='failed', attempts=attempts+1, last_error=:e, updated_at=NOW()
                WHERE id=:id
                """
            ),
            {"id": int(job_id), "e": str(err)[:8000]},
        )
        await db.commit()


async def reclaim_self_orphans() -> int:
    wid = _worker_id()
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text(
                """
                UPDATE task_allocation_queue_jobs
                SET status='pending', locked_by=NULL, locked_at=NULL, updated_at=NOW()
                WHERE status='running' AND locked_by=:wid
                """
            ),
            {"wid": wid},
        )
        await db.commit()
        return int(getattr(res, "rowcount", 0) or 0)


async def _run_one(job: dict[str, Any]) -> None:
    from ai.task_allocation import find_generating_batch

    jid = int(job["id"])
    sw = str(job["sales_wechat_id"])
    period_type = str(job["period_type"])
    ref_date = job["ref_date"]
    if not isinstance(ref_date, date):
        ref_date = today_shanghai()
    auto_publish = bool(job.get("auto_publish", True))
    source = str(job.get("source") or "ai_auto")
    p_start, _ = period_bounds(period_type, ref_date)

    try:
        async with AsyncSessionLocal() as db:
            existing = await find_generating_batch(db, sw, period_type, p_start)
            reuse_id = int(existing.id) if existing else None
            batch = await generate_allocation_batch(
                db,
                sw,
                period_type,
                ref_date=ref_date,
                source=source,
                auto_publish=auto_publish,
                reuse_batch_id=reuse_id,
            )
        await _mark_done(jid, batch.id if batch else None)
    except Exception as e:
        logger.exception(
            "task_allocation_queue failed job_id=%s sw=%s period=%s",
            jid,
            sw,
            period_type,
        )
        await _mark_failed(jid, f"{type(e).__name__}: {e}")


async def run_worker_loop(*, poll_interval: float = 0.8) -> None:
    """单进程 worker：并发执行多个销售批次的任务分配。"""
    gate = _ConcurrencyGate(await get_worker_concurrency())
    conc_cache_ts = 0.0
    wid = _worker_id()
    try:
        n_self = await reclaim_self_orphans()
        if n_self:
            logger.info("task_allocation_queue reclaimed {} self orphan(s) on start", n_self)
    except Exception as e:
        logger.warning("task_allocation_queue reclaim_self_orphans failed: {}", e)
    logger.info("task_allocation_queue worker starting id={} concurrency={}", wid, gate.limit)

    while True:
        try:
            now = time.monotonic()
            if now - conc_cache_ts >= 2.0:
                conc_cache_ts = now
                try:
                    new_lim = await get_worker_concurrency()
                    if new_lim != gate.limit:
                        logger.info(
                            "task_allocation_queue concurrency {} -> {}",
                            gate.limit,
                            new_lim,
                        )
                        gate.set_limit(new_lim)
                except Exception:
                    pass

            if await _paused():
                await asyncio.sleep(1.0)
                continue

            free = gate.slots_free()
            if free <= 0:
                await asyncio.sleep(0.05)
                continue

            jobs = await _claim_jobs(min(8, free))
            if not jobs:
                await asyncio.sleep(poll_interval)
                continue

            async def _wrap(j: dict[str, Any]) -> None:
                await gate.acquire()
                try:
                    await _run_one(j)
                finally:
                    gate.release()

            for j in jobs:
                asyncio.create_task(_wrap(j))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("task_allocation_queue worker loop error: %s", e)
            await asyncio.sleep(1.0)
