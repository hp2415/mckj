"""
销售联系任务分配：按销售微信号 + 日/周/月周期，将已分析客户快照交给大模型，
结合管理平台「task_allocation」场景提示词及文档引用（含 scoring_criteria、strategy 等）生成 contact_tasks。

日任务（daily）在主线任务之后，可追加「破冰」任务：从好友关系表筛新加好友、长期未私聊或从未私聊的联系人，
走独立场景「task_allocation_icebreaker」（优先注入 opening 话术），写入 task_kind=icebreaker；周/月任务不包含破冰。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.future import select

TASK_ALLOCATION_AUTO_CONFIG_KEY = "task_allocation_auto_enabled"
TASK_ALLOCATION_AUTO_ALLOWLIST_KEY = "task_allocation_auto_sales_allowlist"

from ai.raw_profiling import get_llm_client
from ai.task_allocation_limits import (
    get_task_allocation_limits,
    task_cap_for_period,
)
from ai.task_allocation_llm import (
    SCENARIO_ICEBREAKER_KEY,
    fallback_icebreaker_tasks_from_payloads,
    load_allocation_customer_payloads,
    load_icebreaker_customer_payloads,
    normalize_llm_tasks,
    run_task_allocation_llm,
)
from ai.task_allocation_pipeline import run_scalable_main_allocation
from core.logger import logger
from database import AsyncSessionLocal
from models import (
    ContactTask,
    SalesWechatAccount,
    SystemConfig,
    TaskAllocationBatch,
    UserSalesWechat,
)

SHANGHAI_TZ = timezone(timedelta(hours=8))

PERIOD_DAILY = "daily"
PERIOD_WEEKLY = "weekly"
PERIOD_MONTHLY = "monthly"

def today_shanghai() -> date:
    return datetime.now(SHANGHAI_TZ).date()


def monday_week_bounds(ref: date) -> tuple[date, date]:
    start = ref - timedelta(days=ref.weekday())
    return start, start + timedelta(days=6)


def month_bounds(ref: date) -> tuple[date, date]:
    start = ref.replace(day=1)
    if ref.month == 12:
        end = date(ref.year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(ref.year, ref.month + 1, 1) - timedelta(days=1)
    return start, end


def period_bounds(period_type: str, ref: date | None = None) -> tuple[date, date]:
    ref = ref or today_shanghai()
    if period_type == PERIOD_DAILY:
        return ref, ref
    if period_type == PERIOD_WEEKLY:
        return monday_week_bounds(ref)
    if period_type == PERIOD_MONTHLY:
        return month_bounds(ref)
    raise ValueError(f"unknown period_type: {period_type}")


def dedupe_key(batch_id: int, raw_customer_id: str) -> str:
    return f"alloc:{batch_id}:{raw_customer_id}"


AllocationProgressFn = Callable[..., Awaitable[None]] | None


async def _emit_progress(cb: AllocationProgressFn, **kw: Any) -> None:
    if cb is not None:
        await cb(**kw)


async def _resolve_user_id_for_sales_wechat(db, sales_wechat_id: str) -> int | None:
    res = await db.execute(
        select(UserSalesWechat.user_id)
        .where(UserSalesWechat.sales_wechat_id == sales_wechat_id)
        .where(UserSalesWechat.is_primary.is_(True))
        .limit(1)
    )
    row = res.first()
    if row and row[0]:
        return int(row[0])
    res2 = await db.execute(
        select(UserSalesWechat.user_id).where(UserSalesWechat.sales_wechat_id == sales_wechat_id).limit(1)
    )
    row2 = res2.first()
    return int(row2[0]) if row2 and row2[0] else None


async def archive_active_batches(
    db,
    sales_wechat_id: str,
    period_type: str,
    period_start: date,
) -> int:
    res = await db.execute(
        update(TaskAllocationBatch)
        .where(TaskAllocationBatch.sales_wechat_id == sales_wechat_id)
        .where(TaskAllocationBatch.period_type == period_type)
        .where(TaskAllocationBatch.period_start == period_start)
        .where(TaskAllocationBatch.status.in_(("draft", "published")))
        .values(status="archived")
    )
    return int(res.rowcount or 0)


async def _update_batch_progress(
    db,
    batch: TaskAllocationBatch | None,
    **progress: Any,
) -> None:
    if batch is None:
        return
    snap = dict(batch.input_snapshot_json or {})
    snap["progress"] = {**(snap.get("progress") or {}), **progress}
    batch.input_snapshot_json = snap
    await db.flush()


async def create_generating_batch(
    db,
    sales_wechat_id: str,
    period_type: str,
    *,
    ref_date: date | None = None,
    source: str = "manual_regen",
) -> TaskAllocationBatch | None:
    """创建 status=generating 的占位批次，供异步 job 轮询。"""
    ref_date = ref_date or today_shanghai()
    period_start, period_end = period_bounds(period_type, ref_date)
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return None
    await archive_active_batches(db, sw, period_type, period_start)
    user_id = await _resolve_user_id_for_sales_wechat(db, sw)
    batch = TaskAllocationBatch(
        sales_wechat_id=sw,
        user_id=user_id,
        period_type=period_type,
        period_start=period_start,
        period_end=period_end,
        source=source,
        status="generating",
        task_count=0,
        input_snapshot_json={
            "progress": {"phase": "排队中", "pct": 0.0, "status": "generating"},
        },
    )
    db.add(batch)
    await db.commit()
    await db.refresh(batch)
    return batch


async def generate_allocation_batch(
    db,
    sales_wechat_id: str,
    period_type: str,
    *,
    ref_date: date | None = None,
    source: str = "ai_auto",
    auto_publish: bool = True,
    on_progress: AllocationProgressFn = None,
    reuse_batch_id: int | None = None,
) -> TaskAllocationBatch | None:
    ref_date = ref_date or today_shanghai()
    period_start, period_end = period_bounds(period_type, ref_date)
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return None

    if period_type == PERIOD_MONTHLY:
        logger.info("月任务分配已停用（仅保留月进度统计），跳过 sw={}", sw)
        return None

    limits = await get_task_allocation_limits(db)
    cap = task_cap_for_period(period_type, limits)
    max_cust = int(limits["max_customers_main"])

    reuse_batch: TaskAllocationBatch | None = None
    if reuse_batch_id:
        res = await db.execute(
            select(TaskAllocationBatch).where(TaskAllocationBatch.id == reuse_batch_id)
        )
        reuse_batch = res.scalars().first()
    else:
        await archive_active_batches(db, sw, period_type, period_start)

    async def _progress_with_batch(**kw: Any) -> None:
        await _emit_progress(on_progress, **kw)
        if reuse_batch is not None:
            await _update_batch_progress(db, reuse_batch, **kw)

    await _progress_with_batch(
        phase="加载已分析客户",
        detail=f"周期 {period_start} ~ {period_end}，产出上限 {cap}，候选 {max_cust}",
        pct=0.08,
    )
    payloads, lookup = await load_allocation_customer_payloads(
        db, sw, ref_date=ref_date, limit=max_cust
    )
    await _progress_with_batch(
        phase=f"已加载 {len(payloads)} 个客户候选",
        detail="准备生成分配",
        pct=0.18,
    )
    await _progress_with_batch(phase="已归档旧批次", pct=0.22)

    llm_meta: dict[str, Any] = {
        "model": None,
        "tasks_from_llm": 0,
        "icebreaker": None,
        "limits": limits,
    }
    raw_llm_tasks: list[dict[str, Any]] = []
    llm = None
    use_scalable = bool(limits.get("scalable_pipeline_enabled"))
    if payloads:
        await _progress_with_batch(phase="读取 LLM 配置", pct=0.28)
        llm = await get_llm_client(db)
        llm_meta["model"] = llm.model
        if use_scalable:
            await _progress_with_batch(
                phase="可扩展管线分配（分批非流式）",
                detail=f"model={llm.model} candidates={len(payloads)}",
                pct=0.35,
            )
            main_rows, pipe_meta = await run_scalable_main_allocation(
                db,
                llm,
                sales_wechat_id=sw,
                period_type=period_type,
                period_start=period_start,
                period_end=period_end,
                ref_today=ref_date,
                task_cap=cap,
                customer_payloads=payloads,
                lookup=lookup,
                limits=limits,
                on_progress=_progress_with_batch,
            )
            llm_meta["scalable_pipeline"] = pipe_meta
            llm_meta["tasks_from_llm"] = pipe_meta.get("tasks_after_normalize", len(main_rows))
        else:
            await _progress_with_batch(
                phase="大模型生成任务清单",
                detail=f"model={llm.model}",
                pct=0.35,
            )
            raw_llm_tasks, snap = await run_task_allocation_llm(
                db,
                llm,
                sales_wechat_id=sw,
                period_type=period_type,
                period_start=period_start,
                period_end=period_end,
                ref_today=ref_date,
                task_cap=cap,
                customer_payloads=payloads,
            )
            llm_meta.update(snap)
            llm_meta["tasks_from_llm"] = len(raw_llm_tasks)
            await _progress_with_batch(
                phase="模型已返回，正在解析 JSON",
                detail=f"原始 tasks 条数 {len(raw_llm_tasks)}",
                pct=0.72,
            )
            main_rows = normalize_llm_tasks(raw_llm_tasks, lookup, task_cap=cap) if lookup else []
    else:
        main_rows = []

    ice_rows: list[dict[str, Any]] = []
    ice_lookup: dict[str, tuple[Any, Any]] = {}
    ice_snap: dict[str, Any] = {}
    if period_type == PERIOD_DAILY and limits.get("icebreaker_enabled"):
        if llm is None:
            await _progress_with_batch(phase="读取 LLM 配置（破冰）", pct=0.74)
            llm = await get_llm_client(db)
            llm_meta["model"] = llm_meta.get("model") or llm.model
        await _progress_with_batch(
            phase="加载破冰候选（新加/长期未聊）",
            pct=0.76,
        )
        exclude = {r["raw_customer_id"] for r in main_rows}
        ice_cap = int(limits["icebreaker_cap"])
        ice_fetch = int(limits["icebreaker_max_candidates"])
        ice_payloads, ice_lookup, ice_stats = await load_icebreaker_customer_payloads(
            db,
            sw,
            ref_date,
            exclude_raw_ids=exclude,
            cap_for_llm=ice_fetch,
            task_output_cap=ice_cap,
        )
        ice_snap = {
            "stats": ice_stats,
            "tasks_from_llm": 0,
            "task_cap_configured": ice_cap,
            "candidates_for_llm": len(ice_payloads),
        }
        if ice_payloads:
            await _progress_with_batch(
                phase="大模型生成破冰任务",
                detail=f"候选 {len(ice_payloads)} 条",
                pct=0.78,
            )
            raw_ice, ice_llm = await run_task_allocation_llm(
                db,
                llm,
                sales_wechat_id=sw,
                period_type=period_type,
                period_start=period_start,
                period_end=period_end,
                ref_today=ref_date,
                task_cap=ice_cap,
                customer_payloads=ice_payloads,
                scenario_key=SCENARIO_ICEBREAKER_KEY,
                log_tag="TASK_ICEBREAKER_DEBUG",
            )
            ice_snap.update(ice_llm)
            ice_snap["tasks_from_llm"] = len(raw_ice)
            ice_rows = normalize_llm_tasks(
                raw_ice,
                ice_lookup,
                task_cap=ice_cap,
                kind_default="icebreaker",
                allow_missing_scp=True,
            )
            if not ice_rows and ice_payloads:
                raw_fb = fallback_icebreaker_tasks_from_payloads(
                    ice_payloads, task_cap=ice_cap
                )
                ice_rows = normalize_llm_tasks(
                    raw_fb,
                    ice_lookup,
                    task_cap=ice_cap,
                    kind_default="icebreaker",
                    allow_missing_scp=True,
                )
                ice_snap["fallback_used"] = True
                ice_snap["tasks_from_fallback"] = len(ice_rows)
                logger.warning(
                    "破冰 LLM 无有效产出，已用规则兜底 sw={} pool={} llm={} fallback={} err={}",
                    sw,
                    ice_stats.get("merged_candidates"),
                    len(raw_ice),
                    len(ice_rows),
                    ice_llm.get("parse_error"),
                )
            for r in ice_rows:
                r["task_kind"] = "icebreaker"
        llm_meta["icebreaker"] = ice_snap

    tasks_rows = main_rows + ice_rows
    for i, row in enumerate(tasks_rows, start=1):
        row["priority_rank"] = i
    await _progress_with_batch(
        phase="写入分配批次与联系任务",
        detail=f"有效任务 {len(tasks_rows)} 条",
        pct=0.82,
    )

    user_id = await _resolve_user_id_for_sales_wechat(db, sw)
    combined_lookup: dict[str, tuple[Any, Any]] = dict(lookup)
    combined_lookup.update(ice_lookup)
    task_insert_count = sum(1 for r in tasks_rows if combined_lookup.get(r["raw_customer_id"]))

    snapshot = {
        "candidate_count": len(payloads),
        "picked_count": len(tasks_rows),
        "main_task_count": len(main_rows),
        "icebreaker_task_count": len(ice_rows),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "llm": llm_meta,
        "progress": {"phase": "写入中", "pct": 0.85, "status": "generating"},
    }

    if reuse_batch is not None:
        batch = reuse_batch
        batch.source = source
        batch.status = "published" if auto_publish else "draft"
        batch.task_count = task_insert_count
        batch.input_snapshot_json = snapshot
        batch.published_at = datetime.now() if auto_publish else None
        batch.user_id = batch.user_id or user_id
        await db.flush()
    else:
        batch = TaskAllocationBatch(
            sales_wechat_id=sw,
            user_id=user_id,
            period_type=period_type,
            period_start=period_start,
            period_end=period_end,
            source=source,
            status="published" if auto_publish else "draft",
            task_count=task_insert_count,
            input_snapshot_json=snapshot,
            published_at=datetime.now() if auto_publish else None,
        )
        db.add(batch)
        await db.flush()

    default_due = period_start if period_type == PERIOD_DAILY else period_end

    for row in tasks_rows:
        rid = row["raw_customer_id"]
        pair = combined_lookup.get(rid)
        if not pair:
            logger.warning("任务分配写库跳过：无 lookup rid={} batch={}", rid, batch.id)
            continue
        scp, _rc = pair
        due = row.get("_due_date") or default_due
        ps = row.get("priority_score")
        dec_ps = None
        if ps is not None:
            try:
                dec_ps = Decimal(str(round(float(ps), 2)))
            except (TypeError, ValueError):
                dec_ps = None
        db.add(
            ContactTask(
                batch_id=batch.id,
                scp_id=scp.id if scp else None,
                raw_customer_id=rid,
                sales_wechat_id=sw,
                period_type=period_type,
                due_date=due,
                task_kind=row.get("task_kind") or "contact",
                priority_rank=int(row["priority_rank"]),
                priority_score=dec_ps,
                title=row.get("title"),
                instruction=row.get("instruction"),
                status="pending",
                dedupe_key=dedupe_key(batch.id, rid),
            )
        )

    await db.commit()
    await db.refresh(batch)
    await _progress_with_batch(
        phase="完成",
        detail=f"batch_id={batch.id} tasks={batch.task_count}",
        pct=1.0,
        batch_id=batch.id,
        task_count=batch.task_count,
        status=batch.status,
    )
    logger.info(
        "任务分配(LLM) batch#{} sw={} period={} {}~{} tasks={} main={} ice={} model={} published={}",
        batch.id,
        sw,
        period_type,
        period_start,
        period_end,
        batch.task_count,
        len(main_rows),
        len(ice_rows),
        llm_meta.get("model"),
        auto_publish,
    )
    return batch


def _truthy_config(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


async def is_task_allocation_auto_enabled(db) -> bool:
    """管理平台 SystemConfig：未配置或关闭时不跑定时日/周/月分配。"""
    res = await db.execute(
        select(SystemConfig.config_value).where(
            SystemConfig.config_key == TASK_ALLOCATION_AUTO_CONFIG_KEY
        )
    )
    row = res.first()
    if not row:
        return False
    return _truthy_config(row[0])


async def set_task_allocation_auto_enabled(db, enabled: bool) -> None:
    val = "1" if enabled else "0"
    res = await db.execute(
        select(SystemConfig).where(SystemConfig.config_key == TASK_ALLOCATION_AUTO_CONFIG_KEY)
    )
    cfg = res.scalars().first()
    if cfg:
        cfg.config_value = val
        cfg.config_group = "task"
        cfg.description = cfg.description or "是否启用定时联系任务分配（日/周/月）"
    else:
        db.add(
            SystemConfig(
                config_key=TASK_ALLOCATION_AUTO_CONFIG_KEY,
                config_value=val,
                config_group="task",
                description="是否启用定时联系任务分配（日/周/月）",
            )
        )
    await db.commit()


def _parse_sales_allowlist_raw(raw: str | None) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return sorted({str(x).strip() for x in data if str(x).strip()})
        except json.JSONDecodeError:
            pass
    return sorted({s.strip() for s in text.replace("\n", ",").split(",") if s.strip()})


async def get_task_allocation_auto_allowlist(db) -> list[str]:
    """定时任务仅对这些 sales_wechat_id 跑分配；空列表表示未勾选任何销售。"""
    res = await db.execute(
        select(SystemConfig.config_value).where(
            SystemConfig.config_key == TASK_ALLOCATION_AUTO_ALLOWLIST_KEY
        )
    )
    row = res.first()
    if not row:
        return []
    return _parse_sales_allowlist_raw(row[0])


async def set_task_allocation_auto_allowlist(db, sales_wechat_ids: list[str]) -> None:
    clean = sorted({str(x).strip() for x in (sales_wechat_ids or []) if str(x).strip()})
    val = json.dumps(clean, ensure_ascii=False)
    res = await db.execute(
        select(SystemConfig).where(SystemConfig.config_key == TASK_ALLOCATION_AUTO_ALLOWLIST_KEY)
    )
    cfg = res.scalars().first()
    desc = "参与定时联系任务分配的销售微信号列表（JSON 数组，灰度勾选）"
    if cfg:
        cfg.config_value = val
        cfg.config_group = "task"
        cfg.description = cfg.description or desc
    else:
        db.add(
            SystemConfig(
                config_key=TASK_ALLOCATION_AUTO_ALLOWLIST_KEY,
                config_value=val,
                config_group="task",
                description=desc,
            )
        )
    await db.commit()


async def list_active_sales_wechat_ids(db) -> list[str]:
    res = await db.execute(
        select(SalesWechatAccount.sales_wechat_id).where(SalesWechatAccount.sales_wechat_id.isnot(None))
    )
    return sorted({(r[0] or "").strip() for r in res.all() if (r[0] or "").strip()})


async def run_allocation_for_sales(
    period_type: str,
    sales_wechat_ids: list[str],
    ref_date: date | None = None,
) -> dict[str, Any]:
    ref_date = ref_date or today_shanghai()
    stats = {
        "period_type": period_type,
        "ref_date": ref_date.isoformat(),
        "batches": 0,
        "errors": [],
        "sales_count": 0,
    }
    sw_ids = sorted({(s or "").strip() for s in (sales_wechat_ids or []) if (s or "").strip()})
    stats["sales_count"] = len(sw_ids)
    if not sw_ids:
        return stats
    async with AsyncSessionLocal() as db:
        active = set(await list_active_sales_wechat_ids(db))
        for sw in sw_ids:
            if sw not in active:
                logger.warning("定时任务分配跳过未知销售号 sw={}", sw)
                stats["errors"].append(
                    {"sales_wechat_id": sw, "error": "sales_wechat_id 不在销售主数据表"}
                )
                continue
            try:
                batch = await generate_allocation_batch(
                    db,
                    sw,
                    period_type,
                    ref_date=ref_date,
                    source="ai_auto",
                    auto_publish=True,
                )
                if batch:
                    stats["batches"] += 1
            except Exception as e:
                logger.exception("任务分配失败 sw={} period={}: {}", sw, period_type, e)
                stats["errors"].append({"sales_wechat_id": sw, "error": str(e)})
    return stats


async def run_allocation_for_all_sales(period_type: str, ref_date: date | None = None) -> dict[str, Any]:
    """手动/脚本：对库内全部销售号跑分配（不受灰度白名单限制）。"""
    async with AsyncSessionLocal() as db:
        sw_ids = await list_active_sales_wechat_ids(db)
    return await run_allocation_for_sales(period_type, sw_ids, ref_date=ref_date)


async def publish_batch(db, batch_id: int) -> TaskAllocationBatch | None:
    res = await db.execute(select(TaskAllocationBatch).where(TaskAllocationBatch.id == batch_id))
    batch = res.scalars().first()
    if not batch:
        return None
    batch.status = "published"
    batch.published_at = datetime.now()
    await db.commit()
    await db.refresh(batch)
    return batch


async def mark_overdue_tasks(db) -> int:
    today = today_shanghai()
    res = await db.execute(
        update(ContactTask)
        .where(ContactTask.status == "pending")
        .where(ContactTask.due_date < today)
        .values(status="overdue", updated_at=datetime.now())
    )
    await db.commit()
    return int(res.rowcount or 0)


async def batch_stats(db, batch_id: int) -> dict[str, int]:
    res = await db.execute(
        select(ContactTask.status, func.count(ContactTask.id))
        .where(ContactTask.batch_id == batch_id)
        .group_by(ContactTask.status)
    )
    counts = {str(k): int(v) for k, v in res.all()}
    total = sum(counts.values())
    done = counts.get("done", 0)
    skipped = counts.get("skipped", 0)
    denom = max(1, total - skipped)
    return {
        "total": total,
        "done": done,
        "pending": counts.get("pending", 0),
        "in_progress": counts.get("in_progress", 0),
        "skipped": skipped,
        "overdue": counts.get("overdue", 0),
        "completion_rate": round(done / denom, 4),
    }


async def _scheduled_allocation_if_enabled(period_type: str) -> None:
    async with AsyncSessionLocal() as db:
        if not await is_task_allocation_auto_enabled(db):
            logger.info(
                "定时任务分配已关闭（{}=0），跳过 period={}",
                TASK_ALLOCATION_AUTO_CONFIG_KEY,
                period_type,
            )
            return
        sw_ids = await get_task_allocation_auto_allowlist(db)
    if not sw_ids:
        logger.info(
            "定时任务分配已开启但未配置销售白名单（{} 为空），跳过 period={}",
            TASK_ALLOCATION_AUTO_ALLOWLIST_KEY,
            period_type,
        )
        return
    stats = await run_allocation_for_sales(period_type, sw_ids)
    logger.info(
        "定时任务分配完成 period={} sales={} batches={} errors={}",
        period_type,
        stats.get("sales_count"),
        stats.get("batches"),
        len(stats.get("errors") or []),
    )


async def scheduled_daily_task_allocation() -> None:
    """日任务；若开启周「每日滚动刷新」，同日重算当周计划（吸收夜间画像与聊天变化）。"""
    async with AsyncSessionLocal() as db:
        limits = await get_task_allocation_limits(db)
    await _scheduled_allocation_if_enabled(PERIOD_DAILY)
    if limits.get("weekly_refresh_daily"):
        await _scheduled_allocation_if_enabled(PERIOD_WEEKLY)


async def scheduled_weekly_task_allocation() -> None:
    async with AsyncSessionLocal() as db:
        limits = await get_task_allocation_limits(db)
    if limits.get("weekly_refresh_daily"):
        logger.debug("周任务已启用「每日滚动刷新」，跳过独立周一定时")
        return
    await _scheduled_allocation_if_enabled(PERIOD_WEEKLY)


async def scheduled_monthly_task_allocation() -> None:
    """月任务分配已停用；保留空实现以免旧调度 id 报错。"""
    logger.debug("月任务分配已停用，scheduled_monthly_task_allocation 跳过")


async def run_background_allocation_job(
    batch_id: int,
    sales_wechat_id: str,
    period_type: str,
    *,
    ref_date: date | None = None,
    auto_publish: bool = False,
    source: str = "api_async",
) -> None:
    """后台执行分配（供 API / 管理端 async=1 调用）。"""
    sw = (sales_wechat_id or "").strip()
    try:
        async with AsyncSessionLocal() as db:
            await generate_allocation_batch(
                db,
                sw,
                period_type,
                ref_date=ref_date,
                source=source,
                auto_publish=auto_publish,
                reuse_batch_id=batch_id,
            )
    except Exception as e:
        logger.exception("后台任务分配失败 batch_id={} sw={}", batch_id, sw)
        try:
            async with AsyncSessionLocal() as db:
                res = await db.execute(
                    select(TaskAllocationBatch).where(TaskAllocationBatch.id == batch_id)
                )
                batch = res.scalars().first()
                if batch:
                    snap = dict(batch.input_snapshot_json or {})
                    snap["progress"] = {
                        **(snap.get("progress") or {}),
                        "phase": "失败",
                        "error": str(e),
                        "pct": 1.0,
                    }
                    snap["error"] = str(e)
                    batch.input_snapshot_json = snap
                    batch.status = "failed"
                    await db.commit()
        except Exception:
            logger.exception("标记分配批次失败 batch_id={}", batch_id)


async def scheduled_mark_overdue_tasks() -> None:
    async with AsyncSessionLocal() as db:
        n = await mark_overdue_tasks(db)
        if n:
            logger.info("已将 {} 条联系任务标记为 overdue", n)
