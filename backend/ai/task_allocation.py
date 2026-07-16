"""
销售联系任务分配：按销售微信号 + 日/周/月周期，将已分析客户快照交给大模型，
结合管理平台「task_allocation」场景提示词及文档引用（含 scoring_criteria、strategy 等）生成 contact_tasks。

日任务（daily）在主线任务之后，可追加「破冰」任务：从好友关系表筛长期未私聊或从未私聊的联系人
（默认不含近期新加好友，可由 icebreaker_include_new 开启），
走独立场景「task_allocation_icebreaker」（优先注入 opening 话术），写入 task_kind=icebreaker；周/月任务不包含破冰。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import delete, func, update
from sqlalchemy.future import select

TASK_ALLOCATION_AUTO_CONFIG_KEY = "task_allocation_auto_enabled"
TASK_ALLOCATION_AUTO_ALLOWLIST_KEY = "task_allocation_auto_sales_allowlist"

from ai.task_allocation_limits import (
    adaptive_channel_caps_for_sales,
    channel_caps_for_period,
    get_task_allocation_limits,
    main_channel_floor_caps,
    task_cap_for_period,
)
from ai.task_allocation_feedback import sales_completion_stats
from ai.task_allocation_ranking import build_alloc_feature_snapshot, normalize_abc_grade
from ai.task_allocation_llm import (
    backfill_phone_channel_tasks,
    balance_main_channel_tasks,
    fallback_icebreaker_tasks_from_payloads,
    get_task_allocation_llm_client,
    load_allocation_customer_payloads,
    load_icebreaker_customer_payloads,
    normalize_activation_task_title,
    normalize_llm_tasks,
    run_icebreaker_task_allocation_llm,
    run_task_allocation_llm,
)
from ai.task_allocation_pipeline import run_scalable_main_allocation
from ai.task_allocation_features import (
    apply_profile_channel_authority,
    apply_profile_strategy_fallback,
    attach_profile_followup_from_payload,
    build_profile_suggest_snapshot,
)
from ai.task_allocation_reserve import (
    build_reserve_candidates_from_payloads,
    effective_reserve_cap,
    finalize_reserve_rows,
    top_up_main_rows_to_channel_floors,
)
from core.cn_workday import is_cn_workday
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
# 管理后台「重新生成激活」异步作业互斥键（非数据库 period_type）
ACTIVATION_REGEN_JOB_PERIOD = "daily_activation"

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
        try:
            await cb(**kw)
        except Exception:
            logger.exception("分配进度回调失败（不影响批次结果）")


def _progress_persist_kwargs(**kw: Any) -> dict[str, Any]:
    """去掉与 _persist_batch_progress(batch_id, ...) 冲突的键。"""
    out = dict(kw)
    out.pop("batch_id", None)
    if "status" in out:
        out["batch_status"] = out.pop("status")
    return out


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


async def _persist_batch_progress(batch_id: int, **progress: Any) -> None:
    """独立短事务写入批次进度，供管理端刷新/重进页面后轮询。"""
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(TaskAllocationBatch).where(TaskAllocationBatch.id == batch_id)
        )
        batch = res.scalars().first()
        if batch is None:
            return
        snap = dict(batch.input_snapshot_json or {})
        snap["progress"] = {**(snap.get("progress") or {}), **progress}
        batch.input_snapshot_json = snap
        await db.commit()


async def find_generating_batch(
    db,
    sales_wechat_id: str,
    period_type: str,
    period_start: date,
) -> TaskAllocationBatch | None:
    res = await db.execute(
        select(TaskAllocationBatch)
        .where(TaskAllocationBatch.sales_wechat_id == sales_wechat_id)
        .where(TaskAllocationBatch.period_type == period_type)
        .where(TaskAllocationBatch.period_start == period_start)
        .where(TaskAllocationBatch.status == "generating")
        .order_by(TaskAllocationBatch.id.desc())
        .limit(1)
    )
    return res.scalars().first()


async def create_generating_batch(
    db,
    sales_wechat_id: str,
    period_type: str,
    *,
    ref_date: date | None = None,
    source: str = "manual_regen",
) -> TaskAllocationBatch | None:
    """创建 status=generating 的占位批次，供异步 job 轮询。"""
    if period_type in (PERIOD_WEEKLY, PERIOD_MONTHLY):
        return None
    ref_date = ref_date or today_shanghai()
    period_start, period_end = period_bounds(period_type, ref_date)
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return None
    existing = await find_generating_batch(db, sw, period_type, period_start)
    if existing is not None:
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


async def _generate_icebreaker_task_rows(
    db,
    *,
    sw: str,
    ref_date: date,
    limits: dict[str, Any],
    exclude_raw_ids: set[str],
    llm: Any | None,
    on_progress: AllocationProgressFn = None,
) -> tuple[list[dict[str, Any]], dict[str, tuple[Any, Any]], dict[str, Any], Any]:
    """生成激活（icebreaker）任务行，供日任务全量分配与批次内重生成共用。"""
    if llm is None:
        await _emit_progress(on_progress, phase="读取 LLM 配置（激活）", pct=0.74)
        llm = await get_task_allocation_llm_client(db)
    await _emit_progress(on_progress, phase="加载激活候选（长期未聊等）", pct=0.76)

    ice_cap = int(limits["icebreaker_cap"])
    ice_fetch = int(limits["icebreaker_max_candidates"])
    ice_payloads, ice_lookup, ice_stats = await load_icebreaker_customer_payloads(
        db,
        sw,
        ref_date,
        exclude_raw_ids=exclude_raw_ids,
        cap_for_llm=ice_fetch,
        task_output_cap=ice_cap,
        limits=limits,
    )
    ice_snap: dict[str, Any] = {
        "stats": ice_stats,
        "tasks_from_llm": 0,
        "task_cap_configured": ice_cap,
        "candidates_for_llm": len(ice_payloads),
    }
    ice_rows: list[dict[str, Any]] = []
    if not ice_payloads:
        return ice_rows, ice_lookup, ice_snap, llm

    await _emit_progress(
        on_progress,
        phase="大模型生成激活任务",
        detail=f"候选 {len(ice_payloads)} 条",
        pct=0.78,
    )
    raw_ice, ice_llm = await run_icebreaker_task_allocation_llm(
        db,
        llm,
        sales_wechat_id=sw,
        ref_today=ref_date,
        task_cap=ice_cap,
        customer_payloads=ice_payloads,
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
    # LLM 全空或产出不足时，用规则兜底补满至 ice_cap（避免「候选很多、最终个位数」）
    if ice_payloads and len(ice_rows) < ice_cap:
        used_ids = {str(r.get("raw_customer_id") or "").strip() for r in ice_rows}
        remain = ice_cap - len(ice_rows)
        raw_fb = fallback_icebreaker_tasks_from_payloads(
            [p for p in ice_payloads if str(p.get("raw_customer_id") or "").strip() not in used_ids],
            task_cap=remain,
            ref_date=ref_date,
        )
        fb_rows = normalize_llm_tasks(
            raw_fb,
            ice_lookup,
            task_cap=remain,
            kind_default="icebreaker",
            allow_missing_scp=True,
        )
        if fb_rows:
            ice_rows.extend(fb_rows)
            ice_snap["fallback_used"] = True
            ice_snap["tasks_from_fallback"] = len(fb_rows)
            if not raw_ice:
                logger.warning(
                    "激活 LLM 无有效产出，已用规则兜底 sw={} pool={} llm={} fallback={} err={} llm_err={}",
                    sw,
                    ice_stats.get("merged_candidates"),
                    len(raw_ice),
                    len(fb_rows),
                    ice_llm.get("parse_error"),
                    ice_llm.get("llm_error"),
                )
            else:
                logger.info(
                    "激活 LLM 产出不足，规则补齐 sw={} llm={} fallback={} total={}",
                    sw,
                    len(raw_ice),
                    len(fb_rows),
                    len(ice_rows),
                )
    for r in ice_rows:
        r["task_kind"] = "icebreaker"
        r["contact_channel"] = "wechat"
        r["title"] = normalize_activation_task_title(r.get("title"))
    return ice_rows, ice_lookup, ice_snap, llm


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
    if period_type == PERIOD_WEEKLY:
        logger.info(
            "周任务已改为画像跟进日期动态汇总，跳过 LLM 分配 sw={}",
            sw,
        )
        return None

    limits = await get_task_allocation_limits(db)
    base_wechat_cap, base_phone_cap = channel_caps_for_period(period_type, limits)
    wechat_cap, phone_cap = base_wechat_cap, base_phone_cap
    cap = task_cap_for_period(period_type, limits)
    max_cust = int(limits["max_customers_main"])
    adaptive_meta: dict[str, Any] = {}

    reuse_batch: TaskAllocationBatch | None = None
    working_batch_id: int | None = reuse_batch_id
    if reuse_batch_id:
        res = await db.execute(
            select(TaskAllocationBatch).where(TaskAllocationBatch.id == reuse_batch_id)
        )
        reuse_batch = res.scalars().first()
        if reuse_batch is None:
            return None
        working_batch_id = reuse_batch.id
    else:
        await archive_active_batches(db, sw, period_type, period_start)
        user_id = await _resolve_user_id_for_sales_wechat(db, sw)
        placeholder = TaskAllocationBatch(
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
        db.add(placeholder)
        await db.commit()
        await db.refresh(placeholder)
        reuse_batch = placeholder
        working_batch_id = placeholder.id

    async def _progress_with_batch(**kw: Any) -> None:
        await _emit_progress(on_progress, **kw)
        if working_batch_id is not None:
            try:
                await _persist_batch_progress(
                    working_batch_id, **_progress_persist_kwargs(**kw)
                )
            except Exception:
                logger.exception(
                    "分配进度持久化失败 batch_id={}", working_batch_id
                )

    await _progress_with_batch(
        phase="加载已分析客户",
        detail=f"周期 {period_start} ~ {period_end}，微信 {wechat_cap} + 电话 {phone_cap}，候选 {max_cust}",
        pct=0.08,
    )
    payloads, lookup = await load_allocation_customer_payloads(
        db, sw, ref_date=ref_date, limit=max_cust, limits=limits
    )
    payload_breakdown_map: dict[str, Any] = {}
    if payloads and limits.get("adaptive_cap_enabled"):
        sales_stats = await sales_completion_stats(db, sw, ref_date=ref_date)
        ab_count = sum(
            1 for p in payloads if normalize_abc_grade(p.get("abc_grade")) in ("A", "B")
        )
        ab_ratio = ab_count / max(1, len(payloads))
        wechat_cap, phone_cap, adaptive_meta = adaptive_channel_caps_for_sales(
            base_wechat_cap,
            base_phone_cap,
            limits=limits,
            completion_rate=sales_stats.get("completion_rate"),
            pending_overdue=int(sales_stats.get("pending_overdue") or 0),
            ab_customer_ratio=ab_ratio,
        )
        cap = wechat_cap + phone_cap
    payload_breakdown_map = {
        str(p.get("raw_customer_id") or ""): p.get("_score_breakdown")
        for p in payloads
        if p.get("raw_customer_id")
    }
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
    main_rows: list[dict[str, Any]] = []
    reserve_rows: list[dict[str, Any]] = []
    use_scalable = bool(limits.get("scalable_pipeline_enabled"))
    if payloads:
        await _progress_with_batch(phase="读取 LLM 配置", pct=0.28)
        llm = await get_task_allocation_llm_client(db)
        llm_meta["model"] = llm.model
        if use_scalable:
            await _progress_with_batch(
                phase="可扩展管线分配（分批非流式）",
                detail=f"model={llm.model} candidates={len(payloads)}",
                pct=0.35,
            )
            all_rows, pipe_meta = await run_scalable_main_allocation(
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
                wechat_cap=wechat_cap,
                phone_cap=phone_cap,
                base_wechat_cap=base_wechat_cap,
                base_phone_cap=base_phone_cap,
            )
            reserve_rows = [r for r in all_rows if r.get("_pool_tier") == "reserve"]
            main_rows = [r for r in all_rows if r.get("_pool_tier") != "reserve"]
            pipe_meta = dict(pipe_meta or {})
            pipe_meta["main_task_count"] = len(main_rows)
            pipe_meta["reserve_task_count"] = len(reserve_rows)
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
                wechat_cap=wechat_cap,
                phone_cap=phone_cap,
                customer_payloads=payloads,
            )
            llm_meta.update(snap)
            llm_meta["tasks_from_llm"] = len(raw_llm_tasks)
            await _progress_with_batch(
                phase="模型已返回，正在解析 JSON",
                detail=f"原始 tasks 条数 {len(raw_llm_tasks)}",
                pct=0.72,
            )
            payload_feature_by_id = {
                str(p.get("raw_customer_id") or "").strip(): {
                    "_profile_followup_channel": str(p.get("followup_channel") or "").strip().lower(),
                    "_profile_followup_strategy": str(p.get("followup_strategy") or "").strip(),
                }
                for p in payloads
                if p.get("raw_customer_id")
            }
            if raw_llm_tasks and limits.get("followup_channel_authority"):
                raw_llm_tasks, ch_auth_meta = apply_profile_channel_authority(
                    raw_llm_tasks,
                    limits,
                    phone_cap=phone_cap,
                    feature_by_id=payload_feature_by_id,
                )
                llm_meta["profile_channel_authority"] = ch_auth_meta
            main_rows = (
                normalize_llm_tasks(
                    raw_llm_tasks,
                    lookup,
                    task_cap=cap,
                    wechat_cap=wechat_cap,
                    phone_cap=phone_cap,
                )
                if lookup
                else []
            )
            overridden = set(llm_meta.get("profile_channel_authority", {}).get("overridden_rids") or [])
            for row in main_rows:
                if row.get("raw_customer_id") in overridden:
                    row["_channel_authority_applied"] = True
            if main_rows and limits.get("followup_strategy_fallback"):
                main_rows, strat_meta = apply_profile_strategy_fallback(
                    main_rows,
                    limits,
                    feature_by_id=payload_feature_by_id,
                )
                llm_meta["profile_strategy_fallback"] = strat_meta
            reserve_cap = effective_reserve_cap(cap, limits)
            if reserve_cap > 0 and payloads:
                picked_rids = {r["raw_customer_id"] for r in main_rows}
                pool_reserve = build_reserve_candidates_from_payloads(
                    payloads,
                    picked_rids,
                    reserve_cap=reserve_cap,
                )
                reserve_rows = finalize_reserve_rows(
                    pool_reserve,
                    picked_count=len(main_rows),
                    period_start=period_start,
                    period_end=period_end,
                    period_type=period_type,
                    reserve_cap=reserve_cap,
                )
                for row in reserve_rows:
                    row["_pool_tier"] = "reserve"
                llm_meta["reserve_cap"] = reserve_cap
                llm_meta["reserve_from_pool"] = len(reserve_rows)
            w_floor, p_floor = main_channel_floor_caps(base_wechat_cap, base_phone_cap, limits)
            main_rows, reserve_rows, floor_meta = top_up_main_rows_to_channel_floors(
                main_rows,
                wechat_target=wechat_cap,
                phone_target=phone_cap,
                wechat_floor=w_floor,
                phone_floor=p_floor,
                lookup=lookup,
                payloads=payloads,
                reserve_rows=reserve_rows,
            )
            llm_meta["channel_floor_topup"] = floor_meta
            if floor_meta.get("applied"):
                logger.info(
                    "主线渠道下限补齐(legacy) sw={} +wx={} +ph={} final={}/{} floor={}/{}",
                    sw,
                    floor_meta.get("added_wechat"),
                    floor_meta.get("added_phone"),
                    floor_meta.get("wechat_final"),
                    floor_meta.get("phone_final"),
                    w_floor,
                    p_floor,
                )
                if reserve_rows:
                    reserve_rows = finalize_reserve_rows(
                        reserve_rows,
                        picked_count=len(main_rows),
                        period_start=period_start,
                        period_end=period_end,
                        period_type=period_type,
                        reserve_cap=int(llm_meta.get("reserve_cap") or 0),
                    )
    else:
        main_rows = []

    if main_rows and (wechat_cap > 0 or phone_cap > 0) and not limits.get("followup_channel_authority"):
        main_rows, channel_balance = balance_main_channel_tasks(
            main_rows,
            wechat_cap=wechat_cap,
            phone_cap=phone_cap,
        )
        if channel_balance.get("adjusted"):
            llm_meta["channel_balance"] = channel_balance
            logger.info(
                "任务分配：渠道比例校正 {} sw={} target_phone={} target_wechat={}",
                channel_balance.get("action"),
                sw,
                channel_balance.get("target_phone"),
                channel_balance.get("target_wechat"),
            )

    ice_rows: list[dict[str, Any]] = []
    ice_lookup: dict[str, tuple[Any, Any]] = {}
    if period_type == PERIOD_DAILY and limits.get("icebreaker_enabled"):
        exclude = {r["raw_customer_id"] for r in main_rows} | {r["raw_customer_id"] for r in reserve_rows}
        ice_rows, ice_lookup, ice_snap, llm = await _generate_icebreaker_task_rows(
            db,
            sw=sw,
            ref_date=ref_date,
            limits=limits,
            exclude_raw_ids=exclude,
            llm=llm,
            on_progress=_progress_with_batch,
        )
        if llm is not None:
            llm_meta["model"] = llm_meta.get("model") or getattr(llm, "model", None)
        llm_meta["icebreaker"] = ice_snap

    def _count_main_by_channel(rows: list[dict[str, Any]]) -> tuple[int, int]:
        w = sum(1 for r in rows if (r.get("contact_channel") or "wechat") != "phone")
        p = sum(1 for r in rows if (r.get("contact_channel") or "") == "phone")
        return w, p

    main_wechat_count, main_phone_count = _count_main_by_channel(main_rows)

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
        "main_wechat_count": main_wechat_count,
        "main_phone_count": main_phone_count,
        "channel_caps": {"wechat": wechat_cap, "phone": phone_cap},
        "channel_cap_floors": {
            "wechat": main_channel_floor_caps(base_wechat_cap, base_phone_cap, limits)[0],
            "phone": main_channel_floor_caps(base_wechat_cap, base_phone_cap, limits)[1],
            "min_factor": float(limits.get("adaptive_cap_min_factor") or 0.6),
        },
        "adaptive_cap": adaptive_meta,
        "icebreaker_task_count": len(ice_rows),
        "reserve_task_count": len(reserve_rows),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "llm": llm_meta,
        "progress": {"phase": "写入中", "pct": 0.85, "status": "generating"},
    }

    res = await db.execute(
        select(TaskAllocationBatch).where(TaskAllocationBatch.id == working_batch_id)
    )
    batch = res.scalars().first()
    if batch is None:
        return None
    batch.source = source
    batch.status = "published" if auto_publish else "draft"
    batch.task_count = task_insert_count
    batch.input_snapshot_json = snapshot
    batch.published_at = datetime.now() if auto_publish else None
    batch.user_id = batch.user_id or user_id
    await db.flush()

    default_due = period_start if period_type == PERIOD_DAILY else period_end

    payload_by_rid = {
        str(p.get("raw_customer_id") or "").strip(): p for p in payloads if p.get("raw_customer_id")
    }
    for row in main_rows + reserve_rows:
        attach_profile_followup_from_payload(row, payload_by_rid.get(row["raw_customer_id"]) or {})

    def _alloc_feature_for_row(row: dict[str, Any]) -> dict[str, Any] | None:
        alloc_feature = row.get("_alloc_feature") or build_alloc_feature_snapshot(
            raw_customer_id=row["raw_customer_id"],
            breakdown=payload_breakdown_map.get(row["raw_customer_id"]) or {},
        )
        alloc_feature = {
            **(alloc_feature or {}),
            "profile_suggest": build_profile_suggest_snapshot(row),
        }
        if row.get("_channel_authority_applied"):
            alloc_feature["channel_authority_applied"] = True
        instr_src = row.get("_instruction_source")
        if instr_src:
            alloc_feature["instruction_source"] = str(instr_src)
        if row.get("_pool_tier") == "reserve":
            alloc_feature = {
                **alloc_feature,
                "pool": {"tier": "reserve", "source_batch_period": period_type},
            }
        return alloc_feature

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
                contact_channel=row.get("contact_channel") or "wechat",
                priority_rank=int(row["priority_rank"]),
                priority_score=dec_ps,
                title=row.get("title"),
                instruction=row.get("instruction"),
                status="pending",
                dedupe_key=dedupe_key(batch.id, rid),
                alloc_feature_json=_alloc_feature_for_row(row),
            )
        )

    for row in reserve_rows:
        rid = row["raw_customer_id"]
        pair = combined_lookup.get(rid)
        if not pair:
            logger.warning("储备任务写库跳过：无 lookup rid={} batch={}", rid, batch.id)
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
                contact_channel=row.get("contact_channel") or "wechat",
                priority_rank=int(row["priority_rank"]),
                priority_score=dec_ps,
                title=row.get("title"),
                instruction=row.get("instruction"),
                status="reserve",
                dedupe_key=dedupe_key(batch.id, rid),
                alloc_feature_json=_alloc_feature_for_row(row),
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
        "任务分配(LLM) batch#{} sw={} period={} {}~{} tasks={} main={}(wx={} ph={}) reserve={} ice={} model={} published={}",
        batch.id,
        sw,
        period_type,
        period_start,
        period_end,
        batch.task_count,
        len(main_rows),
        main_wechat_count,
        main_phone_count,
        len(reserve_rows),
        len(ice_rows),
        llm_meta.get("model"),
        auto_publish,
    )
    return batch


async def regenerate_activation_tasks_in_batch(
    db,
    batch_id: int,
    *,
    ref_date: date | None = None,
    on_progress: AllocationProgressFn = None,
) -> TaskAllocationBatch | None:
    """
    在日任务批次内重新生成激活任务：保留主线及已办/进行中的激活任务，
    删除待办/已跳过的激活任务后写入新结果（用于 LLM 失败后的规则兜底替换）。
    """
    res = await db.execute(select(TaskAllocationBatch).where(TaskAllocationBatch.id == batch_id))
    batch = res.scalars().first()
    if batch is None:
        return None
    if batch.period_type != PERIOD_DAILY:
        raise ValueError("仅日任务批次支持重新生成激活任务")
    if batch.status not in ("draft", "published"):
        raise ValueError(f"批次状态为 {batch.status}，无法重新生成激活任务")

    sw = (batch.sales_wechat_id or "").strip()
    if not sw:
        return None

    ref_date = ref_date or batch.period_start or today_shanghai()
    prev_status = batch.status
    limits = await get_task_allocation_limits(db)
    if not limits.get("icebreaker_enabled"):
        raise ValueError("配置已关闭「日任务含激活」，无法重新生成")

    async def _notify(**kw: Any) -> None:
        await _emit_progress(on_progress, **kw)

    async def _persist_progress(**kw: Any) -> None:
        """进度写内存回调 + 独立短事务；勿在长事务持锁期间调用。"""
        await _notify(**kw)
        try:
            await _persist_batch_progress(batch_id, **_progress_persist_kwargs(**kw))
        except Exception:
            logger.warning("激活重生成进度持久化失败 batch_id={}", batch_id)

    tres = await db.execute(
        select(ContactTask).where(ContactTask.batch_id == batch_id)
    )
    existing = list(tres.scalars().all())
    main_tasks = [t for t in existing if (t.task_kind or "contact") != "icebreaker"]
    kept_ice = [
        t
        for t in existing
        if (t.task_kind or "") == "icebreaker" and (t.status or "") in ("done", "in_progress")
    ]
    removable_ice_ids = [
        t.id
        for t in existing
        if (t.task_kind or "") == "icebreaker" and (t.status or "") in ("pending", "skipped")
    ]

    exclude: set[str] = set()
    for t in main_tasks:
        rid = str(t.raw_customer_id or "").strip()
        if rid:
            exclude.add(rid)
    for t in kept_ice:
        rid = str(t.raw_customer_id or "").strip()
        if rid:
            exclude.add(rid)

    max_main_rank = max((int(t.priority_rank or 0) for t in main_tasks), default=0)
    max_kept_ice_rank = max((int(t.priority_rank or 0) for t in kept_ice), default=0)
    rank_base = max(max_main_rank, max_kept_ice_rank)

    await _notify(phase="准备重新生成激活任务", pct=0.05)
    batch.status = "generating"
    await _update_batch_progress(
        db,
        batch,
        phase="准备重新生成激活任务",
        pct=0.05,
        status="generating",
    )
    await db.commit()
    await db.refresh(batch)

    try:
        ice_rows, ice_lookup, ice_snap, llm = await _generate_icebreaker_task_rows(
            db,
            sw=sw,
            ref_date=ref_date,
            limits=limits,
            exclude_raw_ids=exclude,
            llm=None,
            on_progress=_persist_progress,
        )
        ice_snap["regenerated_at"] = datetime.now().isoformat(timespec="seconds")
        ice_snap["replaced_pending_count"] = len(removable_ice_ids)
        ice_snap["kept_done_count"] = len(kept_ice)

        await db.refresh(batch)
        due_default = batch.period_start or ref_date
        inserted = 0
        if removable_ice_ids:
            await db.execute(delete(ContactTask).where(ContactTask.id.in_(removable_ice_ids)))
        for i, row in enumerate(ice_rows, start=1):
            rid = str(row.get("raw_customer_id") or "").strip()
            pair = ice_lookup.get(rid)
            if not pair:
                continue
            scp, _rc = pair
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
                    period_type=PERIOD_DAILY,
                    due_date=due_default,
                    task_kind="icebreaker",
                    contact_channel="wechat",
                    priority_rank=rank_base + i,
                    priority_score=dec_ps,
                    title=row.get("title"),
                    instruction=row.get("instruction"),
                    status="pending",
                    dedupe_key=dedupe_key(batch.id, rid),
                    alloc_feature_json=build_alloc_feature_snapshot(
                        raw_customer_id=rid, breakdown={}
                    ),
                )
            )
            inserted += 1

        snap = batch.input_snapshot_json if isinstance(batch.input_snapshot_json, dict) else {}
        llm_block = snap.get("llm") if isinstance(snap.get("llm"), dict) else {}
        llm_block = dict(llm_block)
        if llm is not None:
            llm_block["model"] = getattr(llm, "model", llm_block.get("model"))
        llm_block["icebreaker"] = ice_snap
        snap = dict(snap)
        snap["llm"] = llm_block
        snap["icebreaker_task_count"] = len(kept_ice) + inserted
        snap["picked_count"] = len(main_tasks) + len(kept_ice) + inserted
        snap["progress"] = {"phase": "完成", "pct": 1.0, "status": prev_status}

        count_res = await db.execute(
            select(func.count(ContactTask.id)).where(ContactTask.batch_id == batch_id)
        )
        batch.task_count = int(count_res.scalar() or 0)
        batch.input_snapshot_json = snap
        batch.status = prev_status
        await db.commit()
        await db.refresh(batch)

        await _persist_progress(
            phase="激活任务已更新",
            detail=f"替换 {len(removable_ice_ids)} 条，新增 {inserted} 条，保留已办 {len(kept_ice)} 条",
            pct=1.0,
            batch_id=batch.id,
            task_count=batch.task_count,
            status=batch.status,
        )
        logger.info(
            "日任务批次#{} 重新生成激活 sw={} replaced={} inserted={} kept={} fallback={}",
            batch.id,
            sw,
            len(removable_ice_ids),
            inserted,
            len(kept_ice),
            bool(ice_snap.get("fallback_used")),
        )
        return batch
    except Exception:
        await db.rollback()
        try:
            async with AsyncSessionLocal() as recovery_db:
                rres = await recovery_db.execute(
                    select(TaskAllocationBatch).where(TaskAllocationBatch.id == batch_id)
                )
                rb = rres.scalars().first()
                if rb and rb.status == "generating":
                    rb.status = prev_status
                    rsnap = dict(rb.input_snapshot_json or {})
                    prog = dict(rsnap.get("progress") or {})
                    prog.update(
                        {
                            "phase": "激活重生成失败",
                            "pct": 1.0,
                            "status": prev_status,
                            "error": "激活重生成中断，已恢复批次状态",
                        }
                    )
                    rsnap["progress"] = prog
                    rb.input_snapshot_json = rsnap
                    await recovery_db.commit()
        except Exception:
            logger.exception("恢复批次状态失败 batch_id={}", batch_id)
        raise


def _truthy_config(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


async def is_task_allocation_auto_enabled(db) -> bool:
    """管理平台 SystemConfig：未配置或关闭时不跑定时日/周分配（仅工作日）。"""
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
        cfg.description = cfg.description or "是否启用定时联系任务分配（工作日日/周）"
    else:
        db.add(
            SystemConfig(
                config_key=TASK_ALLOCATION_AUTO_CONFIG_KEY,
                config_value=val,
                config_group="task",
                description="是否启用定时联系任务分配（工作日日/周）",
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
    """将销售批次入队，由 task_allocation_queue worker 并行消费。"""
    from ai.task_allocation_queue import enqueue_sales_allocations

    ref_date = ref_date or today_shanghai()
    sw_ids = sorted({(s or "").strip() for s in (sales_wechat_ids or []) if (s or "").strip()})
    label = f"批量分配 {period_type} {ref_date.isoformat()}（{len(sw_ids)} 销售）"
    result = await enqueue_sales_allocations(
        period_type,
        sw_ids,
        ref_date=ref_date,
        source="ai_auto",
        auto_publish=True,
        batch_label=label,
    )
    return {
        "period_type": period_type,
        "ref_date": ref_date.isoformat(),
        "sales_count": len(sw_ids),
        "enqueued": result.enqueued,
        "deduped": result.deduped,
        "skipped_invalid": result.skipped_invalid,
        "batch_id": result.batch_id,
        # 兼容旧字段名
        "batches": result.enqueued,
        "errors": [],
    }


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
    pair_res = await db.execute(
        select(ContactTask.raw_customer_id, ContactTask.sales_wechat_id)
        .where(ContactTask.status == "pending")
        .where(ContactTask.due_date < today)
    )
    overdue_pairs = [
        (str(r[0] or "").strip(), str(r[1] or "").strip())
        for r in pair_res.all()
        if (r[0] or "").strip() and (r[1] or "").strip()
    ]
    res = await db.execute(
        update(ContactTask)
        .where(ContactTask.status == "pending")
        .where(ContactTask.due_date < today)
        .values(status="overdue", updated_at=datetime.now())
    )
    await db.commit()
    n = int(res.rowcount or 0)
    if overdue_pairs:
        try:
            from ai.profile_triggers import trigger_profile_for_pairs

            await trigger_profile_for_pairs(db, overdue_pairs, reason="task_overdue")
        except Exception:
            logger.exception("任务超时后事件画像触发失败")
    return n


async def batch_stats(db, batch_id: int) -> dict[str, int]:
    res = await db.execute(
        select(ContactTask.status, func.count(ContactTask.id))
        .where(ContactTask.batch_id == batch_id)
        .group_by(ContactTask.status)
    )
    counts = {str(k): int(v) for k, v in res.all()}
    reserve = counts.get("reserve", 0)
    total = sum(counts.values())
    done = counts.get("done", 0)
    skipped = counts.get("skipped", 0)
    denom = max(1, total - skipped - reserve)
    return {
        "total": total,
        "done": done,
        "pending": counts.get("pending", 0),
        "in_progress": counts.get("in_progress", 0),
        "skipped": skipped,
        "overdue": counts.get("overdue", 0),
        "reserve": reserve,
        "completion_rate": round(done / denom, 4),
    }


async def _scheduled_allocation_if_enabled(period_type: str) -> None:
    ref = today_shanghai()
    if not is_cn_workday(ref):
        logger.info(
            "定时任务分配跳过：{} 非工作日（周末或法定节假日） period={}",
            ref.isoformat(),
            period_type,
        )
        return
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
        "定时任务分配已入队 period={} sales={} enqueued={} deduped={} skipped_invalid={} batch_id={}",
        period_type,
        stats.get("sales_count"),
        stats.get("enqueued"),
        stats.get("deduped"),
        stats.get("skipped_invalid"),
        stats.get("batch_id"),
    )


async def scheduled_daily_task_allocation() -> None:
    """日任务；周任务改由画像跟进日期动态汇总，不再走 LLM 分配。"""
    async with AsyncSessionLocal() as db:
        try:
            from ai.task_allocation_feedback import run_task_allocation_feedback_job

            await run_task_allocation_feedback_job(db, ref_date=today_shanghai())
            logger.info("任务分配反馈报表已更新")
        except Exception:
            logger.exception("任务分配反馈作业失败，继续执行分配")
    await _scheduled_allocation_if_enabled(PERIOD_DAILY)


async def scheduled_weekly_task_allocation() -> None:
    """周任务已改为画像跟进日期动态汇总，跳过 LLM 分配定时。"""
    logger.debug("周任务由画像跟进日期动态汇总，scheduled_weekly_task_allocation 跳过")


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
    """后台执行分配：优先走 DB 队列（与定时/管理端一致）。"""
    sw = (sales_wechat_id or "").strip()
    ref_date = ref_date or today_shanghai()
    from ai.task_allocation_queue import (
        QueueTableMissingError,
        enqueue_single_or_get_active,
        wait_and_sync_memory_job,
    )

    mem_job_id = f"api-batch-{batch_id}"
    try:
        qid = await enqueue_single_or_get_active(
            sw,
            period_type,
            ref_date=ref_date,
            source=source,
            auto_publish=auto_publish,
            batch_label=f"API 分配 batch#{batch_id}",
        )
        if not qid:
            raise RuntimeError("无法入队任务分配")
        await wait_and_sync_memory_job(mem_job_id, qid, sw, period_type)
    except QueueTableMissingError:
        logger.warning("队列表未就绪，API 分配回退为直接执行 batch_id={} sw={}", batch_id, sw)
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
                    b = res.scalars().first()
                    if b and b.status == "generating":
                        snap = dict(b.input_snapshot_json or {})
                        snap["progress"] = {
                            **(snap.get("progress") or {}),
                            "phase": "失败",
                            "error": str(e),
                            "pct": 1.0,
                        }
                        snap["error"] = str(e)
                        b.input_snapshot_json = snap
                        b.status = "failed"
                        await db.commit()
            except Exception:
                logger.exception("标记分配批次失败 batch_id={}", batch_id)


async def scheduled_mark_overdue_tasks() -> None:
    async with AsyncSessionLocal() as db:
        n = await mark_overdue_tasks(db)
        if n:
            logger.info("已将 {} 条联系任务标记为 overdue", n)
