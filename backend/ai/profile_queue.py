from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text, bindparam

from core.logger import logger
from database import AsyncSessionLocal


CFG_CANCEL_KEY = "profile_cancel_requested"
CFG_PAUSE_KEY = "profile_worker_paused"


def _worker_id() -> str:
    base = os.getenv("PROFILE_WORKER_ID") or socket.gethostname()
    return (base or "worker")[:80]


def _dedupe_key(raw_customer_id: str, sales_wechat_id: str) -> str:
    return f"profile:{raw_customer_id}:{sales_wechat_id}"


async def request_cancel_db(message: str = "已请求中断，将在当前条目完成后停止后续任务") -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                """
                INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
                VALUES (:k, :v, 'ai', NOW())
                ON DUPLICATE KEY UPDATE config_value=:v, updated_at=NOW()
                """
            ),
            {"k": CFG_CANCEL_KEY, "v": "1"},
        )
        await db.execute(
            text(
                """
                INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
                VALUES ('profile_cancel_message', :v, 'ai', NOW())
                ON DUPLICATE KEY UPDATE config_value=:v, updated_at=NOW()
                """
            ),
            {"v": str(message)[:800]},
        )
        await db.commit()


async def clear_cancel_db() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                """
                INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
                VALUES (:k, '0', 'ai', NOW())
                ON DUPLICATE KEY UPDATE config_value='0', updated_at=NOW()
                """
            ),
            {"k": CFG_CANCEL_KEY},
        )
        await db.commit()


async def pause_workers_db(message: str = "已暂停抢任务（进行中的单条仍会跑完）") -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                """
                INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
                VALUES (:k, '1', 'ai', NOW())
                ON DUPLICATE KEY UPDATE config_value='1', updated_at=NOW()
                """
            ),
            {"k": CFG_PAUSE_KEY},
        )
        await db.execute(
            text(
                """
                INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
                VALUES ('profile_pause_message', :v, 'ai', NOW())
                ON DUPLICATE KEY UPDATE config_value=:v, updated_at=NOW()
                """
            ),
            {"v": str(message)[:800]},
        )
        await db.commit()


async def resume_workers_db(message: str = "已恢复抢任务") -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                """
                INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
                VALUES (:k, '0', 'ai', NOW())
                ON DUPLICATE KEY UPDATE config_value='0', updated_at=NOW()
                """
            ),
            {"k": CFG_PAUSE_KEY},
        )
        await db.execute(
            text(
                """
                INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
                VALUES ('profile_pause_message', :v, 'ai', NOW())
                ON DUPLICATE KEY UPDATE config_value=:v, updated_at=NOW()
                """
            ),
            {"v": str(message)[:800]},
        )
        await db.commit()


async def _paused() -> bool:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("SELECT config_value FROM system_configs WHERE config_key=:k LIMIT 1"),
            {"k": CFG_PAUSE_KEY},
        )
        row = r.first()
        v = (row[0] if row else "") or ""
        return str(v).strip() not in ("0", "", "false", "False", "off", "OFF")


async def cancel_pending_batch(batch_id: str) -> int:
    bid = (batch_id or "").strip()
    if not bid:
        return 0
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text(
                """
                UPDATE profile_jobs
                SET status='cancelled', updated_at=NOW()
                WHERE status='pending' AND batch_id=:bid
                """
            ),
            {"bid": bid},
        )
        await db.commit()
        return int(getattr(res, "rowcount", 0) or 0)


async def cancel_all_pending() -> int:
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text(
                """
                UPDATE profile_jobs
                SET status='cancelled', updated_at=NOW()
                WHERE status='pending'
                """
            )
        )
        await db.commit()
        return int(getattr(res, "rowcount", 0) or 0)


async def _cancel_requested() -> bool:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("SELECT config_value FROM system_configs WHERE config_key=:k LIMIT 1"),
            {"k": CFG_CANCEL_KEY},
        )
        row = r.first()
        v = (row[0] if row else "") or ""
        return str(v).strip() not in ("0", "", "false", "False", "off", "OFF")


@dataclass
class EnqueueResult:
    batch_id: str
    enqueued: int
    deduped: int


async def enqueue_pairs(
    pairs: list[tuple[str, str]],
    *,
    batch_id: str,
    batch_label: str,
) -> EnqueueResult:
    cleaned: list[tuple[str, str]] = []
    for rid, sw in pairs or []:
        rid = (rid or "").strip()
        sw = (sw or "").strip()
        if rid and sw:
            cleaned.append((rid, sw))
    if not cleaned:
        return EnqueueResult(batch_id=batch_id, enqueued=0, deduped=0)

    deduped = 0
    inserted = 0
    async with AsyncSessionLocal() as db:
        for rid, sw in cleaned:
            dk = _dedupe_key(rid, sw)
            # 幂等入队：仅对 pending/running 去重；允许对已完成/失败任务“重新入队重跑”
            res = await db.execute(
                text(
                    """
                    INSERT INTO profile_jobs
                      (raw_customer_id, sales_wechat_id, dedupe_key, batch_id, batch_label,
                       status, attempts, created_at, updated_at)
                    SELECT
                      :rid, :sw, :dk, :bid, :bl, 'pending', 0, NOW(), NOW()
                    FROM DUAL
                    WHERE NOT EXISTS (
                      SELECT 1 FROM profile_jobs
                      WHERE dedupe_key=:dk AND status IN ('pending','running')
                      LIMIT 1
                    )
                    """
                ),
                {"rid": rid, "sw": sw, "dk": dk, "bid": batch_id, "bl": batch_label[:120]},
            )
            # MySQL rowcount: 1 inserted, 0 ignored
            if getattr(res, "rowcount", 0) == 1:
                inserted += 1
            else:
                deduped += 1
        await db.commit()
    return EnqueueResult(batch_id=batch_id, enqueued=inserted, deduped=deduped)


async def snapshot_queue() -> dict[str, Any]:
    """
    供管理后台进度页轮询：统计 pending/running/succeeded/failed，展示最近错误。
    """
    async with AsyncSessionLocal() as db:
        agg = await db.execute(
            text(
                """
                SELECT status, COUNT(*) AS c
                FROM profile_jobs
                GROUP BY status
                """
            )
        )
        counts = {str(r[0] or ""): int(r[1] or 0) for r in agg.all()}
        total = sum(counts.values())
        pending = counts.get("pending", 0)
        running = counts.get("running", 0)
        done = counts.get("succeeded", 0)
        failed = counts.get("failed", 0)
        cancelled = counts.get("cancelled", 0)

        # 选出“当前批次”（优先 running，其次 pending；取最早开始/最早入队的一批）
        cur_res = await db.execute(
            text(
                """
                SELECT batch_id, batch_label, MIN(id) AS min_id
                FROM profile_jobs
                WHERE status IN ('running','pending') AND batch_id IS NOT NULL AND batch_id <> ''
                GROUP BY batch_id, batch_label
                ORDER BY min_id ASC
                LIMIT 1
                """
            )
        )
        cur_row = cur_res.first()
        current_batch_id = str(cur_row[0] or "") if cur_row else ""
        current_batch_label = str(cur_row[1] or "") if cur_row else ""

        counts_current: dict[str, int] = {}
        total_current = 0
        if current_batch_id:
            cur_agg = await db.execute(
                text(
                    """
                    SELECT status, COUNT(*) AS c
                    FROM profile_jobs
                    WHERE batch_id=:bid
                    GROUP BY status
                    """
                ),
                {"bid": current_batch_id},
            )
            counts_current = {str(r[0] or ""): int(r[1] or 0) for r in cur_agg.all()}
            total_current = sum(counts_current.values())

        # 最近错误（时间使用 DB updated_at，避免“跟随当前时间漂移”）
        err_res = await db.execute(
            text(
                """
                SELECT id, raw_customer_id, sales_wechat_id, UNIX_TIMESTAMP(updated_at) AS ts, last_error
                FROM profile_jobs
                WHERE status='failed'
                ORDER BY updated_at DESC
                LIMIT 40
                """
            )
        )
        errors = []
        for jid, rid, sw, ts, msg in err_res.all():
            errors.append(
                {
                    "job_id": int(jid),
                    "target": f"{rid}|{sw}",
                    "at": int(ts or 0),
                    "message": (str(msg or "").strip() or "failed")[:4000],
                }
            )

        # pending 批次概览（取最近 64 个 batch；入队时间用 DB first_at）
        pend_res = await db.execute(
            text(
                """
                SELECT batch_id, batch_label, UNIX_TIMESTAMP(MIN(created_at)) AS first_at, COUNT(*) AS c
                FROM profile_jobs
                WHERE status='pending'
                GROUP BY batch_id, batch_label
                ORDER BY first_at ASC
                LIMIT 64
                """
            )
        )
        pending_batches = []
        for bid, bl, first_at, c in pend_res.all():
            pending_batches.append(
                {
                    "batch_id": str(bid or ""),
                    "kind": "pairs",
                    "count": int(c or 0),
                    "label": str(bl or ""),
                    "enqueued_at": int(first_at or 0),
                }
            )

        run_res = await db.execute(
            text(
                """
                SELECT id, raw_customer_id, sales_wechat_id, locked_by, locked_at
                FROM profile_jobs
                WHERE status='running'
                ORDER BY locked_at DESC
                LIMIT 12
                """
            )
        )
        running_jobs = []
        for jid, rid, sw, lb, la in run_res.all():
            running_jobs.append(
                {
                    "job_id": int(jid),
                    "target": f"{rid}|{sw}",
                    "locked_by": str(lb or ""),
                    "locked_at": str(la or ""),
                }
            )

        cancel = await db.execute(
            text("SELECT config_value FROM system_configs WHERE config_key=:k LIMIT 1"),
            {"k": CFG_CANCEL_KEY},
        )
        row = cancel.first()
        cancel_requested = False
        if row:
            v = str(row[0] or "").strip()
            cancel_requested = v not in ("0", "", "false", "False", "off", "OFF")

        paused = await _paused()

    processed = done + failed + cancelled
    percent = round(100.0 * processed / total, 1) if total > 0 else 0.0
    done_current = counts_current.get("succeeded", 0)
    failed_current = counts_current.get("failed", 0)
    cancelled_current = counts_current.get("cancelled", 0)
    running_current = counts_current.get("running", 0)
    pending_current = counts_current.get("pending", 0)
    processed_current = done_current + failed_current + cancelled_current
    percent_current = round(100.0 * processed_current / total_current, 1) if total_current > 0 else 0.0
    return {
        "status": "paused" if paused else ("running" if (pending > 0 or running > 0) else "idle"),
        # 本次（当前批次）
        "current_batch": {
            "batch_id": current_batch_id,
            "batch_label": current_batch_label,
            "total": total_current,
            "counts_by_status": counts_current,
            "done": done_current,
            "failed": failed_current,
            "cancelled": cancelled_current,
            "running": running_current,
            "pending": pending_current,
            "processed": processed_current,
            "percent": percent_current,
        },
        # 全量历史（参考）
        "overall": {
            "total": total,
            "counts_by_status": counts,
            "done": done,
            "failed": failed,
            "cancelled": cancelled,
            "running": running,
            "pending": pending,
            "processed": processed,
            "percent": percent,
        },
        "skipped": 0,
        "queue_size": pending,
        "current_raw_id": None,
        "message": "",
        "pending_batches": pending_batches,
        "running_jobs": running_jobs,
        "recent_errors": errors,
        "cancel_requested": cancel_requested,
        "paused": paused,
        "active_batch": None,
        "batch_id": current_batch_id,
        "batch_label": current_batch_label,
        "started_at": None,
        "finished_at": None,
    }


async def _claim_jobs(limit: int) -> list[dict[str, Any]]:
    """
    抢占一批 pending 任务并标记为 running。使用 SKIP LOCKED 支持多 worker 并行。
    """
    wid = _worker_id()
    out: list[dict[str, Any]] = []
    async with AsyncSessionLocal() as db:
        # 事务内锁定 + 更新状态
        async with db.begin():
            res = await db.execute(
                text(
                    """
                    SELECT id, raw_customer_id, sales_wechat_id
                    FROM profile_jobs
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
                UPDATE profile_jobs
                SET status='running', locked_by=:wid, locked_at=NOW(), updated_at=NOW()
                WHERE id IN :ids
                """
            ).bindparams(bindparam("ids", expanding=True))
            await db.execute(stmt, {"wid": wid, "ids": ids})
            for jid, rid, sw in rows:
                out.append({"id": int(jid), "raw_customer_id": str(rid), "sales_wechat_id": str(sw)})
    return out


async def _mark_done(job_id: int) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                """
                UPDATE profile_jobs
                SET status='succeeded', updated_at=NOW()
                WHERE id=:id
                """
            ),
            {"id": int(job_id)},
        )
        await db.commit()


async def _mark_failed(job_id: int, err: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                """
                UPDATE profile_jobs
                SET status='failed', attempts=attempts+1, last_error=:e, updated_at=NOW()
                WHERE id=:id
                """
            ),
            {"id": int(job_id), "e": str(err)[:8000]},
        )
        await db.commit()


async def _mark_cancelled(job_id: int) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("UPDATE profile_jobs SET status='cancelled', updated_at=NOW() WHERE id=:id"),
            {"id": int(job_id)},
        )
        await db.commit()


async def _run_one(job: dict[str, Any]) -> None:
    from sqlalchemy.future import select
    from models import RawCustomer, RawCustomerSalesWechat
    from ai.raw_profiling import (
        _rcsw_relation_inactive,
        apply_profile_to_main,
        get_llm_client,
        get_user_id_map,
        profile_raw_customer_with_llm,
    )
    from sqlalchemy import update

    jid = int(job["id"])
    rid = str(job["raw_customer_id"])
    sw = str(job["sales_wechat_id"])

    try:
        async with AsyncSessionLocal() as db:
            user_map = await get_user_id_map(db)
            llm = await get_llm_client(db)

            res = await db.execute(select(RawCustomer).where(RawCustomer.id == rid))
            raw = res.scalar_one_or_none()
            if not raw:
                await db.rollback()
                await _mark_failed(jid, f"无此原始客户 raw_id={rid}")
                return

            snap_res = await db.execute(
                select(RawCustomerSalesWechat)
                .where(
                    RawCustomerSalesWechat.raw_customer_id == rid,
                    RawCustomerSalesWechat.sales_wechat_id == sw,
                )
                .limit(1)
            )
            snap = snap_res.scalars().first()
            if _rcsw_relation_inactive(snap):
                await db.rollback()
                await _mark_cancelled(jid)
                return

            uid = user_map.get(sw)
            p = await profile_raw_customer_with_llm(
                db,
                llm,
                raw,
                sales_wechat_id_override=sw,
                rcsw_snapshot=snap,
            )
            if not p:
                await db.rollback()
                await _mark_failed(jid, "LLM 无有效结果（解析失败或无 JSON）")
                return

            await apply_profile_to_main(db, p, user_id=uid)
            await db.execute(update(RawCustomer).where(RawCustomer.id == rid).values(profile_status=1))
            await db.commit()
        await _mark_done(jid)
    except Exception as e:
        logger.exception("profile_jobs worker failed job_id=%s raw_id=%s sales_wechat_id=%s", jid, rid, sw)
        await _mark_failed(jid, f"{type(e).__name__}: {e}")


async def run_worker_loop(*, concurrency: int = 4, poll_interval: float = 0.5) -> None:
    """
    单进程 worker：可并发跑多个画像任务（每个任务独立 session）。
    多进程/多机器：通过 SKIP LOCKED 自然扩展。
    """
    conc = max(1, int(concurrency))
    sem = asyncio.Semaphore(conc)
    wid = _worker_id()
    logger.info("profile_jobs worker starting id=%s concurrency=%s", wid, conc)

    while True:
        try:
            if await _paused():
                await asyncio.sleep(0.8)
                continue
            if await _cancel_requested():
                await asyncio.sleep(1.0)
                continue

            free = sem._value  # noqa: SLF001 (internal but OK for sizing)
            if free <= 0:
                await asyncio.sleep(0.05)
                continue

            jobs = await _claim_jobs(min(16, free))
            if not jobs:
                await asyncio.sleep(poll_interval)
                continue

            async def _wrap(j: dict[str, Any]) -> None:
                async with sem:
                    await _run_one(j)

            for j in jobs:
                asyncio.create_task(_wrap(j))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("profile_jobs worker loop error: %s", e)
            await asyncio.sleep(1.0)

