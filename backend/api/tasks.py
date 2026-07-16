"""销售联系任务 API（桌面端 + 管理端 JSON）。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func
from sqlalchemy.future import select
from sqlalchemy import desc

import schemas
from ai.task_allocation_limits import get_task_allocation_limits
from ai.task_scheduler import (
    claim_reserve_task,
    daily_claimed_count,
    load_claimable_tasks_with_customer,
    pool_meta_from_alloc,
)
from ai.task_callbacks import mark_callback_done, query_active_callbacks
from ai.task_weekly_profile import (
    WEEKLY_PROFILE_VIEW_MODE,
    is_virtual_weekly_task_id,
    materialize_weekly_profile_task,
    query_weekly_profile_items,
    query_weekly_profile_stats,
    resolve_weekly_profile_task_for_action,
    scp_id_from_virtual_weekly_task_id,
)
from ai.task_allocation import (
    PERIOD_DAILY,
    PERIOD_MONTHLY,
    PERIOD_WEEKLY,
    create_generating_batch,
    generate_allocation_batch,
    period_bounds,
    run_background_allocation_job,
    today_shanghai,
)
from ai.task_month_progress import (
    query_month_progress_rows,
    query_month_progress_stats,
    stats_from_task_dicts,
)
from ai.profile_triggers import safe_trigger_profile_for_contact_task
from api.auth import get_current_user
from crud import get_user_bound_sales_wechat_ids
from database import get_db
from models import (
    ContactTask,
    RawCustomer,
    SalesCustomerProfile,
    TaskAllocationBatch,
    User,
)

router = APIRouter(prefix="/api/tasks", tags=["Tasks"])


async def _resolve_sales_wechat_id(
    db,
    user: User,
    sales_wechat_id: Optional[str],
) -> str:
    sw = (sales_wechat_id or "").strip()
    bound = await get_user_bound_sales_wechat_ids(db, user.id)
    if not bound:
        raise HTTPException(status_code=400, detail="当前账号未绑定销售微信号")
    if not sw:
        if len(bound) == 1:
            return bound[0]
        raise HTTPException(status_code=400, detail="请指定 sales_wechat_id")
    if sw not in bound:
        raise HTTPException(status_code=403, detail="无权访问该销售微信号下的任务")
    return sw


def _customer_phone_fields(rc: RawCustomer | None) -> tuple[str | None, str | None, str | None]:
    """返回 (联系电话, 规范化电话, 展示用电话)；展示优先规范化。"""
    if rc is None:
        return None, None, None
    phone_raw = (rc.phone or "").strip() or None
    phone_norm = (rc.phone_normalized or "").strip() or None
    display = phone_norm or phone_raw
    return phone_raw, phone_norm, display


def _task_to_out(task: ContactTask, scp: SalesCustomerProfile | None, rc: RawCustomer | None) -> dict:
    phone_raw, phone_norm, phone_display = _customer_phone_fields(rc)
    return {
        "id": task.id,
        "batch_id": task.batch_id,
        "raw_customer_id": task.raw_customer_id,
        "sales_wechat_id": task.sales_wechat_id,
        "period_type": task.period_type,
        "due_date": task.due_date,
        "task_kind": task.task_kind,
        "contact_channel": getattr(task, "contact_channel", None) or "wechat",
        "priority_rank": task.priority_rank,
        "priority_score": float(task.priority_score) if task.priority_score is not None else None,
        "title": task.title,
        "instruction": task.instruction,
        "status": task.status,
        "completed_at": task.completed_at,
        "completion_note": task.completion_note,
        "customer_name": (rc.customer_name if rc else None) or None,
        "unit_name": (rc.unit_name if rc else None) or None,
        "wechat_remark": (scp.wechat_remark if scp else None) or None,
        "phone": phone_display,
        "phone_raw": phone_raw,
        "phone_normalized": phone_norm,
        "ai_profile": (scp.ai_profile if scp else None) or None,
        "suggested_followup_date": scp.suggested_followup_date if scp else None,
        "pool": pool_meta_from_alloc(task.alloc_feature_json if task else None) or None,
    }


async def _load_tasks_with_customer(
    db,
    *,
    sales_wechat_id: str,
    period_type: str,
    period_start: date,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 0,
) -> tuple[TaskAllocationBatch | None, list[dict], int]:
    # 桌面端选批策略：
    #   1) 优先返回当期 published 批次（管理员审核发布过的版本）；
    #   2) 若该周期还没有 published 批次（例如日任务草稿刚生成、管理员还没点"发布"），
    #      则回落到最新的 draft，让销售立刻看到任务，避免空白页。
    #   3) generating 批次用于异步分配轮询（此时 tasks 可能为空）。
    #   不返回 archived / failed 批次。
    batch_res = await db.execute(
        select(TaskAllocationBatch)
        .where(TaskAllocationBatch.sales_wechat_id == sales_wechat_id)
        .where(TaskAllocationBatch.period_type == period_type)
        .where(TaskAllocationBatch.period_start == period_start)
        .where(TaskAllocationBatch.status.in_(("published", "draft", "generating")))
        .order_by(
            case(
                (TaskAllocationBatch.status == "published", 0),
                (TaskAllocationBatch.status == "generating", 1),
                else_=2,
            ),
            TaskAllocationBatch.id.desc(),
        )
        .limit(1)
    )
    batch = batch_res.scalars().first()
    if not batch:
        return None, [], 0
    if batch.status == "generating":
        return batch, [], 0

    stmt = (
        select(ContactTask, SalesCustomerProfile, RawCustomer)
        .outerjoin(SalesCustomerProfile, SalesCustomerProfile.id == ContactTask.scp_id)
        .outerjoin(RawCustomer, RawCustomer.id == ContactTask.raw_customer_id)
        .where(ContactTask.batch_id == batch.id)
        .order_by(ContactTask.priority_rank.asc(), ContactTask.id.asc())
    )
    if status:
        stmt = stmt.where(ContactTask.status == status)
    else:
        stmt = stmt.where(ContactTask.status != "reserve")

    if page_size and page_size > 0:
        count_stmt = select(func.count(ContactTask.id)).where(ContactTask.batch_id == batch.id)
        if status:
            count_stmt = count_stmt.where(ContactTask.status == status)
        else:
            count_stmt = count_stmt.where(ContactTask.status != "reserve")
        total = int((await db.execute(count_stmt)).scalar() or 0)
        offset = max(0, (max(1, page) - 1) * page_size)
        stmt = stmt.offset(offset).limit(page_size)
        rows = (await db.execute(stmt)).all()
        items = [_task_to_out(t, scp, rc) for t, scp, rc in rows]
        return batch, items, total

    rows = (await db.execute(stmt)).all()
    items = [_task_to_out(t, scp, rc) for t, scp, rc in rows]
    return batch, items, len(items)


async def _load_month_progress_tasks(
    db,
    *,
    sales_wechat_id: str,
    month_start: date,
    month_end: date,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 0,
) -> tuple[list[dict], int]:
    """月进度：汇总本月内 due_date 落在区间内的全部联系任务。"""
    rows, total = await query_month_progress_rows(
        db,
        sales_wechat_id=sales_wechat_id,
        month_start=month_start,
        month_end=month_end,
        status=status,
        page=page,
        page_size=page_size,
    )
    items = [_task_to_out(t, scp, rc) for t, scp, rc in rows]
    return items, total


def _batch_snapshot_summary(batch: TaskAllocationBatch | None) -> dict | None:
    if batch is None or batch.status == "generating":
        return None
    snap = batch.input_snapshot_json if isinstance(batch.input_snapshot_json, dict) else {}
    if not snap:
        return None
    return {
        "main_task_count": snap.get("main_task_count"),
        "main_wechat_count": snap.get("main_wechat_count"),
        "main_phone_count": snap.get("main_phone_count"),
        "icebreaker_task_count": snap.get("icebreaker_task_count"),
        "reserve_task_count": snap.get("reserve_task_count"),
        "channel_caps": snap.get("channel_caps"),
    }


def _stats_from_items(items: list[dict]) -> schemas.TaskPeriodStatsOut:
    raw = stats_from_task_dicts(items)
    return schemas.TaskPeriodStatsOut(**raw)


async def _load_or_materialize_task(
    db,
    task_id: int,
    *,
    user: User,
    sales_wechat_id: str | None = None,
) -> ContactTask:
    if is_virtual_weekly_task_id(task_id):
        sw = await _resolve_sales_wechat_id(db, user, sales_wechat_id)
        task = await resolve_weekly_profile_task_for_action(
            db,
            task_id,
            sales_wechat_id=sw,
            user_id=user.id,
        )
        if not task:
            raise HTTPException(status_code=404, detail="周任务画像不存在或已不在本周")
        return task
    res = await db.execute(select(ContactTask).where(ContactTask.id == task_id))
    task = res.scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.get("/callbacks")
async def list_callbacks(
    sales_wechat_id: Optional[str] = Query(None),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """再联系提醒列表（当日 + 往日逾期；画像派生，独立于任务分配批次）。"""
    sw = await _resolve_sales_wechat_id(db, current_user, sales_wechat_id)
    items = await query_active_callbacks(db, sales_wechat_id=sw)
    due_count = sum(1 for it in items if it.get("overdue"))
    past_day_count = sum(1 for it in items if it.get("past_day"))
    return {
        "code": 200,
        "message": "ok",
        "data": {
            "items": [schemas.CallbackReminderOut(**it) for it in items],
            "total": len(items),
            "due_count": due_count,
            "past_day_count": past_day_count,
            "sales_wechat_id": sw,
        },
    }


@router.post("/callbacks/{scp_id}/done")
async def complete_callback(
    scp_id: int,
    sales_wechat_id: Optional[str] = Query(None),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """标记回访已处理。"""
    sw = await _resolve_sales_wechat_id(db, current_user, sales_wechat_id)
    scp = await mark_callback_done(db, scp_id=scp_id, sales_wechat_id=sw)
    if scp is None:
        raise HTTPException(status_code=404, detail="回访提醒不存在或无权操作")
    await db.commit()
    return {
        "code": 200,
        "message": "已标记处理",
        "data": {"scp_id": int(scp.id), "callback_done_at": scp.callback_done_at},
    }


@router.post("/allocation/jobs")
async def start_allocation_job(
    period: str = Query(PERIOD_DAILY, description="daily|weekly|monthly"),
    date_str: Optional[str] = Query(None, alias="date"),
    sales_wechat_id: Optional[str] = Query(None),
    auto_publish: bool = Query(False),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """提交异步任务分配，立即返回 batch_id（job_id）。"""
    if period not in (PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY):
        return {"code": 400, "message": "period 须为 daily|weekly|monthly", "data": None}
    if period == PERIOD_MONTHLY:
        return {
            "code": 400,
            "message": "月任务分配已停用；「月」视图仅作本月任务进度统计，请使用日/周任务分配",
            "data": None,
        }
    if period == PERIOD_WEEKLY:
        return {
            "code": 400,
            "message": "周任务已改为画像跟进日期动态汇总，不再支持手动触发 LLM 分配",
            "data": None,
        }
    ref = today_shanghai()
    if date_str:
        try:
            ref = date.fromisoformat(date_str[:10])
        except ValueError:
            return {"code": 400, "message": "date 格式须为 YYYY-MM-DD", "data": None}
    sw = await _resolve_sales_wechat_id(db, current_user, sales_wechat_id)
    batch = await create_generating_batch(db, sw, period, ref_date=ref, source="api_async")
    if not batch:
        return {"code": 400, "message": "无法创建分配批次", "data": None}
    asyncio.create_task(
        run_background_allocation_job(
            batch.id,
            sw,
            period,
            ref_date=ref,
            auto_publish=auto_publish,
        )
    )
    p_start, p_end = period_bounds(period, ref)
    data = schemas.TaskAllocationJobOut(
        job_id=batch.id,
        batch_id=batch.id,
        status="generating",
        phase="排队中",
        pct=0.0,
        period_type=period,
        period_start=p_start,
        period_end=p_end,
        sales_wechat_id=sw,
    )
    return {"code": 200, "message": "ok", "data": data}


@router.get("/allocation/jobs/{job_id}")
async def get_allocation_job(
    job_id: int,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(select(TaskAllocationBatch).where(TaskAllocationBatch.id == job_id))
    batch = res.scalars().first()
    if not batch:
        raise HTTPException(status_code=404, detail="job 不存在")
    await _resolve_sales_wechat_id(db, current_user, batch.sales_wechat_id)
    snap = batch.input_snapshot_json or {}
    prog = snap.get("progress") or {}
    err = prog.get("error") or snap.get("error")
    status = batch.status
    if status == "generating":
        st = "running"
    elif status in ("draft", "published"):
        st = "done"
    elif status == "failed":
        st = "error"
    else:
        st = status
    data = schemas.TaskAllocationJobOut(
        job_id=batch.id,
        batch_id=batch.id,
        status=st,
        phase=str(prog.get("phase") or ""),
        detail=str(prog.get("detail") or ""),
        pct=float(prog.get("pct") or 0.0),
        task_count=batch.task_count,
        error=str(err) if err else None,
        period_type=batch.period_type,
        period_start=batch.period_start,
        period_end=batch.period_end,
        sales_wechat_id=batch.sales_wechat_id,
    )
    return {"code": 200, "message": "ok", "data": data}


@router.get("/overview")
async def task_overview(
    period: str = Query(PERIOD_DAILY, description="daily|weekly|monthly"),
    date_str: Optional[str] = Query(None, alias="date"),
    sales_wechat_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(0, ge=0, le=500),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if period not in (PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY):
        return {"code": 400, "message": "period 须为 daily|weekly|monthly", "data": None}
    ref = today_shanghai()
    if date_str:
        try:
            ref = date.fromisoformat(date_str[:10])
        except ValueError:
            return {"code": 400, "message": "date 格式须为 YYYY-MM-DD", "data": None}
    sw = await _resolve_sales_wechat_id(db, current_user, sales_wechat_id)
    p_start, p_end = period_bounds(period, ref)

    if period == PERIOD_MONTHLY:
        items, total = await _load_month_progress_tasks(
            db,
            sales_wechat_id=sw,
            month_start=p_start,
            month_end=p_end,
            status=status,
            page=page,
            page_size=page_size,
        )
        raw_stats = await query_month_progress_stats(
            db,
            sales_wechat_id=sw,
            month_start=p_start,
            month_end=p_end,
            status=status,
        )
        stats = schemas.TaskPeriodStatsOut(**raw_stats)
        eff_page_size = page_size if page_size > 0 else (total if total else len(items))
        data = schemas.TaskOverviewOut(
            period_type=period,
            period_start=p_start,
            period_end=p_end,
            batch_id=None,
            batch_status=None,
            stats=stats,
            items=[schemas.ContactTaskOut(**it) for it in items],
            page=page,
            page_size=eff_page_size,
            total_items=total,
            progress=None,
            view_mode="month_progress",
        )
        return {"code": 200, "message": "ok", "data": data}

    if period == PERIOD_WEEKLY:
        if page_size and page_size > 0:
            items, total = await query_weekly_profile_items(
                db,
                sales_wechat_id=sw,
                week_start=p_start,
                week_end=p_end,
                ref_date=ref,
                status=status,
                page=page,
                page_size=page_size,
            )
            raw_stats = await query_weekly_profile_stats(
                db,
                sales_wechat_id=sw,
                week_start=p_start,
                week_end=p_end,
                ref_date=ref,
                status=status,
            )
        else:
            items, total = await query_weekly_profile_items(
                db,
                sales_wechat_id=sw,
                week_start=p_start,
                week_end=p_end,
                ref_date=ref,
                status=status,
            )
            raw_stats = stats_from_task_dicts(items)
        stats = schemas.TaskPeriodStatsOut(**raw_stats)
        eff_page_size = page_size if page_size > 0 else (total if total else len(items))
        wechat_n = sum(1 for it in items if (it.get("contact_channel") or "wechat") != "phone")
        phone_n = sum(1 for it in items if (it.get("contact_channel") or "wechat") == "phone")
        data = schemas.TaskOverviewOut(
            period_type=period,
            period_start=p_start,
            period_end=p_end,
            batch_id=None,
            batch_status=None,
            stats=stats,
            items=[schemas.ContactTaskOut(**{k: v for k, v in it.items() if not k.startswith("_")}) for it in items],
            page=page,
            page_size=eff_page_size,
            total_items=total,
            progress=None,
            view_mode=WEEKLY_PROFILE_VIEW_MODE,
            snapshot={
                "main_task_count": total,
                "main_wechat_count": wechat_n,
                "main_phone_count": phone_n,
                "source": "profile_followup",
            },
        )
        return {"code": 200, "message": "ok", "data": data}

    batch, items, total = await _load_tasks_with_customer(
        db,
        sales_wechat_id=sw,
        period_type=period,
        period_start=p_start,
        status=status,
        page=page,
        page_size=page_size,
    )
    if batch and batch.status != "generating" and page_size <= 0:
        _, stats_items, total_all = await _load_tasks_with_customer(
            db,
            sales_wechat_id=sw,
            period_type=period,
            period_start=p_start,
            status=status,
        )
        stats = _stats_from_items(stats_items)
        total = total_all
    else:
        stats = _stats_from_items(items)
    progress = None
    snapshot = None
    if batch and batch.status == "generating":
        progress = (batch.input_snapshot_json or {}).get("progress")
    elif batch:
        snapshot = _batch_snapshot_summary(batch)
    eff_page_size = page_size if page_size > 0 else (total if total else len(items))
    data = schemas.TaskOverviewOut(
        period_type=period,
        period_start=p_start,
        period_end=p_end,
        batch_id=batch.id if batch else None,
        batch_status=batch.status if batch else None,
        stats=stats,
        items=[schemas.ContactTaskOut(**it) for it in items],
        page=page,
        page_size=eff_page_size,
        total_items=total,
        progress=progress,
        snapshot=snapshot,
    )
    return {"code": 200, "message": "ok", "data": data}


@router.get("/list")
async def task_list(
    period: str = Query(PERIOD_DAILY),
    date_str: Optional[str] = Query(None, alias="date"),
    sales_wechat_id: Optional[str] = Query(None),
    status: Optional[str] = Query("pending"),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    resp = await task_overview(
        period=period,
        date_str=date_str,
        sales_wechat_id=sales_wechat_id,
        status=status,
        db=db,
        current_user=current_user,
    )
    if resp.get("code") != 200:
        return resp
    data = resp["data"]
    return {"code": 200, "message": "ok", "data": data.items}


@router.get("/reserve")
async def list_reserve_tasks(
    sales_wechat_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """可认领的储备任务池（日优先、周次之）。"""
    limits = await get_task_allocation_limits(db)
    if not limits.get("claim_enabled"):
        raise HTTPException(status_code=400, detail="任务认领未开启")
    ref = today_shanghai()
    sw = await _resolve_sales_wechat_id(db, current_user, sales_wechat_id)
    claimed_today = await daily_claimed_count(db, sales_wechat_id=sw, ref_date=ref)
    daily_limit = int(limits.get("claim_daily_limit") or 0)
    rows = await load_claimable_tasks_with_customer(
        db,
        sales_wechat_id=sw,
        ref_date=ref,
        limit=limit,
    )
    items = [_task_to_out(t, scp, rc) for t, scp, rc in rows]
    data = schemas.TaskReservePoolOut(
        items=[schemas.ContactTaskOut(**it) for it in items],
        claimed_today=claimed_today,
        claim_daily_limit=daily_limit,
        claims_remaining=max(0, daily_limit - claimed_today),
        claim_enabled=True,
        ref_date=ref,
    )
    return {"code": 200, "message": "ok", "data": data}


async def _claim_one_reserve_candidate(
    db,
    *,
    task: ContactTask,
    scp: SalesCustomerProfile | None,
    rc: RawCustomer | None,
    sales_wechat_id: str,
    user_id: int,
    ref_date: date,
) -> tuple[ContactTask, SalesCustomerProfile | None, RawCustomer | None] | None:
    """认领池中的一条候选（含周画像虚任务）；失败返回 None。"""
    task_id = int(task.id or 0)
    if is_virtual_weekly_task_id(task_id):
        claimed = await materialize_weekly_profile_task(
            db,
            scp_id=scp_id_from_virtual_weekly_task_id(task_id),
            sales_wechat_id=sales_wechat_id,
            ref_date=ref_date,
            user_id=user_id,
            status="pending",
            due_date=ref_date,
            pool_meta={
                "tier": "profile_weekly",
                "claimed": True,
                "claimed_by": int(user_id),
                "claimed_on": ref_date.isoformat(),
            },
        )
        if not claimed:
            return None
        return claimed, scp, rc

    claimed = await claim_reserve_task(
        db,
        task_id=task_id,
        sales_wechat_id=sales_wechat_id,
        user_id=user_id,
        ref_date=ref_date,
    )
    if not claimed:
        return None
    return claimed, scp, rc


@router.post("/claim-more")
async def claim_more_tasks(
    sales_wechat_id: Optional[str] = Query(None),
    count: int = Query(5, ge=1, le=5, description="一次认领条数，默认 5，最多 5"),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """从储备池批量认领任务（默认一次 5 条，受每日上限约束）。"""
    limits = await get_task_allocation_limits(db)
    if not limits.get("claim_enabled"):
        raise HTTPException(status_code=400, detail="任务认领未开启")
    ref = today_shanghai()
    sw = await _resolve_sales_wechat_id(db, current_user, sales_wechat_id)
    daily_limit = int(limits.get("claim_daily_limit") or 0)
    claimed_today = await daily_claimed_count(db, sales_wechat_id=sw, ref_date=ref)
    remaining = max(0, daily_limit - claimed_today)
    if remaining <= 0:
        raise HTTPException(status_code=429, detail="今日认领已达上限")

    want = min(int(count or 5), remaining, 5)
    candidates = await load_claimable_tasks_with_customer(
        db,
        sales_wechat_id=sw,
        ref_date=ref,
        limit=want,
    )
    claimed_rows: list[tuple[ContactTask, SalesCustomerProfile | None, RawCustomer | None]] = []
    for task, scp, rc in candidates:
        if len(claimed_rows) >= want:
            break
        result = await _claim_one_reserve_candidate(
            db,
            task=task,
            scp=scp,
            rc=rc,
            sales_wechat_id=sw,
            user_id=current_user.id,
            ref_date=ref,
        )
        if result:
            claimed_rows.append(result)

    if claimed_rows:
        await db.commit()
        for claimed, _, _ in claimed_rows:
            await db.refresh(claimed)
    else:
        await db.rollback()

    claimed_today = await daily_claimed_count(db, sales_wechat_id=sw, ref_date=ref)
    items = [_task_to_out(t, scp, rc) for t, scp, rc in claimed_rows]
    data = schemas.TaskClaimMoreOut(
        items=[schemas.ContactTaskOut(**it) for it in items],
        claimed_count=len(items),
        claimed_today=claimed_today,
        claim_daily_limit=daily_limit,
        claims_remaining=max(0, daily_limit - claimed_today),
        ref_date=ref,
    )
    if not items:
        return {"code": 200, "message": "暂无可认领任务", "data": data}
    return {"code": 200, "message": f"成功认领 {len(items)} 条任务", "data": data}


@router.post("/{task_id}/claim")
async def claim_task(
    task_id: int,
    sales_wechat_id: Optional[str] = Query(None),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """原子认领储备任务：reserve → pending，due_date 设为今日。"""
    limits = await get_task_allocation_limits(db)
    if not limits.get("claim_enabled"):
        raise HTTPException(status_code=400, detail="任务认领未开启")
    ref = today_shanghai()
    sw = await _resolve_sales_wechat_id(db, current_user, sales_wechat_id)
    daily_limit = int(limits.get("claim_daily_limit") or 0)
    claimed_today = await daily_claimed_count(db, sales_wechat_id=sw, ref_date=ref)
    if claimed_today >= daily_limit:
        raise HTTPException(status_code=429, detail="今日认领已达上限")

    if is_virtual_weekly_task_id(task_id):
        claimed = await materialize_weekly_profile_task(
            db,
            scp_id=scp_id_from_virtual_weekly_task_id(task_id),
            sales_wechat_id=sw,
            ref_date=ref,
            user_id=current_user.id,
            status="pending",
            due_date=ref,
            pool_meta={
                "tier": "profile_weekly",
                "claimed": True,
                "claimed_by": int(current_user.id),
                "claimed_on": ref.isoformat(),
            },
        )
        if not claimed:
            raise HTTPException(status_code=404, detail="该周任务不可认领")
        await db.commit()
        await db.refresh(claimed)
        row_res = await db.execute(
            select(ContactTask, SalesCustomerProfile, RawCustomer)
            .outerjoin(SalesCustomerProfile, SalesCustomerProfile.id == ContactTask.scp_id)
            .outerjoin(RawCustomer, RawCustomer.id == ContactTask.raw_customer_id)
            .where(ContactTask.id == claimed.id)
        )
        row = row_res.first()
        scp = row[1] if row else None
        rc = row[2] if row else None
        return {
            "code": 200,
            "message": "认领成功",
            "data": schemas.ContactTaskOut(**_task_to_out(claimed, scp, rc)),
        }

    res = await db.execute(select(ContactTask).where(ContactTask.id == task_id))
    task = res.scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if str(task.status or "") != "reserve":
        raise HTTPException(status_code=409, detail="该任务不可认领")
    if (task.sales_wechat_id or "").strip() != sw:
        raise HTTPException(status_code=403, detail="无权认领该任务")
    claimed = await claim_reserve_task(
        db,
        task_id=task_id,
        sales_wechat_id=sw,
        user_id=current_user.id,
        ref_date=ref,
    )
    if not claimed:
        await db.rollback()
        raise HTTPException(status_code=409, detail="该任务已被认领")
    await db.commit()
    await db.refresh(claimed)
    row_res = await db.execute(
        select(ContactTask, SalesCustomerProfile, RawCustomer)
        .outerjoin(SalesCustomerProfile, SalesCustomerProfile.id == ContactTask.scp_id)
        .outerjoin(RawCustomer, RawCustomer.id == ContactTask.raw_customer_id)
        .where(ContactTask.id == task_id)
    )
    row = row_res.first()
    scp = row[1] if row else None
    rc = row[2] if row else None
    return {
        "code": 200,
        "message": "认领成功",
        "data": schemas.ContactTaskOut(**_task_to_out(claimed, scp, rc)),
    }


@router.get("/calendar")
async def task_calendar(
    month: str = Query(..., description="YYYY-MM"),
    sales_wechat_id: Optional[str] = Query(None),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        y, m = map(int, month.split("-")[:2])
        month_start = date(y, m, 1)
    except (ValueError, TypeError):
        return {"code": 400, "message": "month 格式须为 YYYY-MM", "data": None}
    if m == 12:
        month_end = date(y + 1, 1, 1)
    else:
        month_end = date(y, m + 1, 1)
    from datetime import timedelta

    month_end = month_end - timedelta(days=1)

    sw = await _resolve_sales_wechat_id(db, current_user, sales_wechat_id)
    res = await db.execute(
        select(ContactTask.due_date, ContactTask.status)
        .join(TaskAllocationBatch, TaskAllocationBatch.id == ContactTask.batch_id)
        .where(ContactTask.sales_wechat_id == sw)
        .where(TaskAllocationBatch.status == "published")
        .where(ContactTask.due_date >= month_start)
        .where(ContactTask.due_date <= month_end)
        .where(ContactTask.status != "reserve")
    )
    by_day: dict[date, dict[str, int]] = {}
    for due, st in res.all():
        d = due
        bucket = by_day.setdefault(d, {"total": 0, "done": 0, "pending": 0, "overdue": 0})
        bucket["total"] += 1
        s = str(st or "pending")
        if s == "done":
            bucket["done"] += 1
        elif s == "overdue":
            bucket["overdue"] += 1
        elif s in ("pending", "in_progress"):
            bucket["pending"] += 1

    days = []
    cur = month_start
    while cur <= month_end:
        b = by_day.get(cur, {"total": 0, "done": 0, "pending": 0, "overdue": 0})
        days.append(schemas.TaskCalendarDayOut(date=cur, **b))
        cur += timedelta(days=1)

    return {"code": 200, "message": "ok", "data": days}


@router.post("/{task_id}/complete")
async def complete_task(
    task_id: int,
    body: schemas.TaskCompleteIn | None = None,
    sales_wechat_id: Optional[str] = Query(None),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_or_materialize_task(
        db,
        task_id,
        user=current_user,
        sales_wechat_id=sales_wechat_id,
    )
    await _resolve_sales_wechat_id(db, current_user, task.sales_wechat_id)
    task.status = "done"
    task.completed_at = datetime.now()
    task.completed_by_user_id = current_user.id
    if body and body.note:
        task.completion_note = body.note.strip()[:500]
    await db.commit()
    return {"code": 200, "message": "已完成", "data": {"id": task.id, "status": task.status}}


@router.post("/{task_id}/skip")
async def skip_task(
    task_id: int,
    body: schemas.TaskSkipIn | None = None,
    sales_wechat_id: Optional[str] = Query(None),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_or_materialize_task(
        db,
        task_id,
        user=current_user,
        sales_wechat_id=sales_wechat_id,
    )
    await _resolve_sales_wechat_id(db, current_user, task.sales_wechat_id)
    task.status = "skipped"
    if body and body.note:
        task.completion_note = body.note.strip()[:500]
    await db.commit()
    await safe_trigger_profile_for_contact_task(task, reason="task_skip")
    return {"code": 200, "message": "已跳过", "data": {"id": task.id, "status": task.status}}


@router.post("/{task_id}/appeal")
async def appeal_task(
    task_id: int,
    body: schemas.TaskAppealIn,
    sales_wechat_id: Optional[str] = Query(None),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """申诉任务：采集原因用于优化任务分配（状态置为 skipped，note 保存原因）。"""
    task = await _load_or_materialize_task(
        db,
        task_id,
        user=current_user,
        sales_wechat_id=sales_wechat_id,
    )
    await _resolve_sales_wechat_id(db, current_user, task.sales_wechat_id)
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason 不能为空")
    task.status = "skipped"
    task.completed_at = datetime.now()
    task.completed_by_user_id = current_user.id
    task.completion_note = ("appeal: " + reason)[:500]
    await db.commit()
    await safe_trigger_profile_for_contact_task(task, reason="task_appeal")
    return {"code": 200, "message": "已申诉", "data": {"id": task.id, "status": task.status}}


@router.get("/appeals/reasons")
async def appeal_reason_stats(
    period: str = Query(PERIOD_DAILY, description="daily|weekly|monthly"),
    date_str: Optional[str] = Query(None, alias="date"),
    sales_wechat_id: Optional[str] = Query(None),
    top: int = Query(20, ge=1, le=200),
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """申诉原因统计（仅统计 completion_note 以 'appeal:' 开头的 skipped 任务）。"""
    if period not in (PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY):
        return {"code": 400, "message": "period 须为 daily|weekly|monthly", "data": None}
    ref = today_shanghai()
    if date_str:
        try:
            ref = date.fromisoformat(date_str[:10])
        except ValueError:
            return {"code": 400, "message": "date 格式须为 YYYY-MM-DD", "data": None}
    sw = await _resolve_sales_wechat_id(db, current_user, sales_wechat_id)
    p_start, p_end = period_bounds(period, ref)

    stmt = (
        select(ContactTask.completion_note, func.count(ContactTask.id))
        .where(ContactTask.sales_wechat_id == sw)
        .where(ContactTask.due_date >= p_start)
        .where(ContactTask.due_date <= p_end)
        .where(ContactTask.status == "skipped")
        .where(ContactTask.completion_note.isnot(None))
        .where(ContactTask.completion_note.like("appeal:%"))
        .group_by(ContactTask.completion_note)
        .order_by(desc(func.count(ContactTask.id)))
        .limit(int(top))
    )
    rows = (await db.execute(stmt)).all()
    out = [
        schemas.TaskAppealReasonStatOut(reason=str(note or "")[7:].strip(), count=int(cnt or 0))
        for note, cnt in rows
        if str(note or "").strip()
    ]
    return {"code": 200, "message": "ok", "data": out}


@router.post("/{task_id}/restore")
async def restore_task(
    task_id: int,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """将已完成 / 已跳过的任务恢复为待办。"""
    res = await db.execute(select(ContactTask).where(ContactTask.id == task_id))
    task = res.scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    await _resolve_sales_wechat_id(db, current_user, task.sales_wechat_id)
    task.status = "pending"
    task.completed_at = None
    task.completed_by_user_id = None
    task.completion_note = None
    await db.commit()
    return {"code": 200, "message": "已恢复待办", "data": {"id": task.id, "status": task.status}}
