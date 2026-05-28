from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text, bindparam

from core.logger import logger
from core.system_config_store import upsert_system_config_row
from database import AsyncSessionLocal


CFG_CANCEL_KEY = "profile_cancel_requested"
CFG_PAUSE_KEY = "profile_worker_paused"
CFG_CONCURRENCY_KEY = "profile_worker_concurrency"
PROFILE_CONCURRENCY_MIN = 1
PROFILE_CONCURRENCY_MAX = 32


def _env_concurrency_default() -> int:
    try:
        raw = int(os.getenv("PROFILE_WORKER_CONCURRENCY") or "4")
    except ValueError:
        raw = 4
    return max(PROFILE_CONCURRENCY_MIN, min(PROFILE_CONCURRENCY_MAX, raw))


def _clamp_concurrency(value: int) -> int:
    return max(PROFILE_CONCURRENCY_MIN, min(PROFILE_CONCURRENCY_MAX, int(value)))


async def get_worker_concurrency() -> int:
    """抢任务并发上限：优先 system_configs，未配置时回退 .env PROFILE_WORKER_CONCURRENCY。"""
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
            config_group="ai",
            description="画像 worker 抢任务并发上限（管理后台可调）",
            update_description=True,
        )
        await db.commit()
    logger.info("profile_worker_concurrency saved={}", v)
    return v


class _ConcurrencyGate:
    """可动态调高并发上限；调低时等已在跑的任务自然结束。"""

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


def _worker_id() -> str:
    base = os.getenv("PROFILE_WORKER_ID") or socket.gethostname()
    return (base or "worker")[:80]


def _dedupe_key(raw_customer_id: str, sales_wechat_id: str) -> str:
    return f"profile:{raw_customer_id}:{sales_wechat_id}"


async def request_cancel_db(message: str = "已请求中断，将在当前条目完成后停止后续任务") -> None:
    async with AsyncSessionLocal() as db:
        await upsert_system_config_row(
            db,
            config_key=CFG_CANCEL_KEY,
            config_value="1",
            config_group="ai",
        )
        await upsert_system_config_row(
            db,
            config_key="profile_cancel_message",
            config_value=str(message)[:800],
            config_group="ai",
        )
        await db.commit()


async def clear_cancel_db() -> None:
    async with AsyncSessionLocal() as db:
        await upsert_system_config_row(
            db,
            config_key=CFG_CANCEL_KEY,
            config_value="0",
            config_group="ai",
        )
        await db.commit()


async def pause_workers_db(message: str = "已暂停抢任务（进行中的单条仍会跑完）") -> None:
    async with AsyncSessionLocal() as db:
        await upsert_system_config_row(
            db,
            config_key=CFG_PAUSE_KEY,
            config_value="1",
            config_group="ai",
        )
        await upsert_system_config_row(
            db,
            config_key="profile_pause_message",
            config_value=str(message)[:800],
            config_group="ai",
        )
        await db.commit()


async def resume_workers_db(message: str = "已恢复抢任务") -> None:
    async with AsyncSessionLocal() as db:
        await upsert_system_config_row(
            db,
            config_key=CFG_PAUSE_KEY,
            config_value="0",
            config_group="ai",
        )
        await upsert_system_config_row(
            db,
            config_key="profile_pause_message",
            config_value=str(message)[:800],
            config_group="ai",
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
        worker_concurrency = await get_worker_concurrency()

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
        "worker_concurrency": worker_concurrency,
        "worker_concurrency_max": PROFILE_CONCURRENCY_MAX,
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
        apply_profile_to_main,
        get_llm_client,
        get_user_id_map,
        load_known_sales_wechat_ids,
        profile_raw_customer_with_llm,
        profile_skip_reason,
    )
    from sqlalchemy import update

    jid = int(job["id"])
    rid = str(job["raw_customer_id"])
    sw = str(job["sales_wechat_id"])

    try:
        async with AsyncSessionLocal() as db:
            user_map = await get_user_id_map(db)
            llm = await get_llm_client(db)
            known_sales_ids = await load_known_sales_wechat_ids(db)

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
            if profile_skip_reason(
                rid,
                snap,
                raw=raw,
                known_sales_wechat_ids=known_sales_ids,
            ):
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


async def reclaim_self_orphans() -> int:
    """启动时回收"上一进程残留的 running 任务"。

    判断依据：locked_by == 当前 worker_id。
    `_worker_id()` 默认使用 hostname/PROFILE_WORKER_ID，单机重启 / `--reload` 后值不变，
    所以可以安全断言：当前 ID 名下的 running 行一定是上次自己留下的（绝不会属于"另一个还活着的并发 worker"）。
    """
    wid = _worker_id()
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text(
                """
                UPDATE profile_jobs
                SET status='pending',
                    locked_by=NULL,
                    locked_at=NULL,
                    updated_at=NOW(),
                    last_error=CONCAT(
                        'reclaimed-on-start@', :wid, ' | prev=',
                        COALESCE(LEFT(last_error, 200), '')
                    )
                WHERE status='running' AND locked_by=:wid
                """
            ),
            {"wid": wid},
        )
        await db.commit()
        return int(getattr(res, "rowcount", 0) or 0)


async def reclaim_stale_running(*, stale_minutes: int = 30) -> int:
    """运营手动回收：超过 stale_minutes 仍 running 的视为孤儿（任意 worker_id）。

    单条画像 LLM 最长也就 2 分钟左右；30 分钟还在 running 一定是 worker 已死。
    安全降级：把 running → pending，由后续 worker 重新抢占；attempts 不递增（孤儿不算失败）。
    """
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text(
                """
                UPDATE profile_jobs
                SET status='pending',
                    locked_by=NULL,
                    locked_at=NULL,
                    updated_at=NOW(),
                    last_error=CONCAT(
                        'reclaimed-stale@', NOW(), ' | prev=',
                        COALESCE(LEFT(last_error, 200), '')
                    )
                WHERE status='running'
                  AND (locked_at IS NULL OR locked_at < (NOW() - INTERVAL :m MINUTE))
                """
            ),
            {"m": int(stale_minutes)},
        )
        await db.commit()
        return int(getattr(res, "rowcount", 0) or 0)


async def run_worker_loop(*, poll_interval: float = 0.5) -> None:
    """
    单进程 worker：可并发跑多个画像任务（每个任务独立 session）。
    并发上限由 system_configs.profile_worker_concurrency 控制（管理后台可调），
    未配置时回退 .env PROFILE_WORKER_CONCURRENCY。
    多进程/多机器：通过 SKIP LOCKED 自然扩展。
    启动时会自动回收"上一进程留下的 running 孤儿"。
    """
    gate = _ConcurrencyGate(await get_worker_concurrency())
    conc_cache_ts = 0.0
    wid = _worker_id()
    try:
        n_self = await reclaim_self_orphans()
        if n_self:
            logger.info("profile_jobs worker reclaimed {} self orphan(s) on start", n_self)
    except Exception as e:
        logger.warning("profile_jobs worker start: reclaim_self_orphans failed: {}", e)
    logger.info("profile_jobs worker starting id={} concurrency={}", wid, gate.limit)

    while True:
        try:
            now = time.monotonic()
            if now - conc_cache_ts >= 2.0:
                conc_cache_ts = now
                try:
                    new_lim = await get_worker_concurrency()
                    if new_lim != gate.limit:
                        logger.info(
                            "profile_jobs worker concurrency {} -> {}",
                            gate.limit,
                            new_lim,
                        )
                        gate.set_limit(new_lim)
                except Exception:
                    pass

            if await _paused():
                await asyncio.sleep(0.8)
                continue
            if await _cancel_requested():
                await asyncio.sleep(1.0)
                continue

            free = gate.slots_free()
            if free <= 0:
                await asyncio.sleep(0.05)
                continue

            jobs = await _claim_jobs(min(16, free))
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
            logger.exception("profile_jobs worker loop error: %s", e)
            await asyncio.sleep(1.0)

