"""管理后台：任务分配总览与批次操作。"""
from __future__ import annotations

import asyncio
import html
import json
from datetime import date, datetime

from markupsafe import Markup
from sqladmin import BaseView, ModelView, expose
from core.admin_sort import AdminModelView
from admin_views import (
    LocalizedStaticValuesFilter,
    SalesWechatIdFilter,
    _TASK_BATCH_STATUS_VALUES,
    _TASK_PERIOD_VALUES,
    _fmt_sales_wechat_column,
)
from sqlalchemy.future import select
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from ai.task_allocation import (
    PERIOD_DAILY,
    PERIOD_MONTHLY,
    PERIOD_WEEKLY,
    batch_stats,
    create_generating_batch,
    find_generating_batch,
    generate_allocation_batch,
    get_task_allocation_auto_allowlist,
    is_task_allocation_auto_enabled,
    period_bounds,
    publish_batch,
    set_task_allocation_auto_allowlist,
    set_task_allocation_auto_enabled,
    today_shanghai,
)
from ai.task_allocation_limits import get_task_allocation_limits, set_task_allocation_limits
from ai.task_month_progress import query_month_progress_rows, stats_from_task_dicts
from database import AsyncSessionLocal
from models import ContactTask, RawCustomer, SalesCustomerProfile, SalesWechatAccount, TaskAllocationBatch
from core.logger import logger

ADMIN_CAT_TASKS = "任务管理"

def _sales_wechat_option_label(
    sales_wechat_id: str,
    *,
    nickname: str | None = None,
    alias_name: str | None = None,
    account_code: str | None = None,
) -> str:
    sw = (sales_wechat_id or "").strip()
    display = (
        (nickname or "").strip()
        or (alias_name or "").strip()
        or (account_code or "").strip()
    )
    if display and display != sw:
        return f"{display}（{sw}）"
    return display or sw


TASK_KIND_LABELS: dict[str, str] = {
    "contact": "联系",
    "follow_up": "跟进",
    "close_deal": "促单",
    "revisit": "回访",
    "icebreaker": "破冰",
}

CONTACT_CHANNEL_LABELS: dict[str, str] = {
    "wechat": "微信",
    "phone": "电话",
}

_BATCH_STATUS_QUERY_VALUES = frozenset({"active", "all", "draft", "published", "archived"})


def _resolve_batch_statuses(batch_status: str) -> tuple[str, ...]:
    """解析 batch_status 查询参数为 SQL IN 可用状态集合。"""
    s = (batch_status or "").strip().lower()
    if s in ("", "active", "current"):
        return ("draft", "published")
    if s == "all":
        return ("draft", "published", "archived")
    if s in ("draft", "published", "archived"):
        return (s,)
    return ("draft", "published")


def _is_current_period(period_type: str, period_start: date) -> bool:
    cur_start, _ = period_bounds(period_type, today_shanghai())
    return period_start == cur_start


def _parse_ref_date(raw: str) -> date:
    ref = today_shanghai()
    raw = (raw or "").strip()
    if raw:
        try:
            ref = date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return ref


async def _load_batch_for_overview(
    db,
    *,
    sales_wechat_id: str,
    period_type: str,
    period_start: date,
    batch_status: str,
    batch_id: int | None = None,
) -> TaskAllocationBatch | None:
    """按销售/周期/发布状态选取展示批次；当前周期仍优先 generating。"""
    is_current = _is_current_period(period_type, period_start)
    if batch_id:
        res = await db.execute(
            select(TaskAllocationBatch).where(TaskAllocationBatch.id == batch_id)
        )
        batch = res.scalars().first()
        if batch and batch.sales_wechat_id == sales_wechat_id and batch.period_type == period_type:
            return batch
        return None
    gen_batch = None
    if is_current:
        gen_batch = await find_generating_batch(db, sales_wechat_id, period_type, period_start)
    statuses = _resolve_batch_statuses(batch_status)
    res = await db.execute(
        select(TaskAllocationBatch)
        .where(TaskAllocationBatch.sales_wechat_id == sales_wechat_id)
        .where(TaskAllocationBatch.period_type == period_type)
        .where(TaskAllocationBatch.period_start == period_start)
        .where(TaskAllocationBatch.status.in_(statuses))
        .order_by(TaskAllocationBatch.id.desc())
        .limit(1)
    )
    return gen_batch or res.scalars().first()


def _fmt_task_allocation_contact_tasks_detail(m: TaskAllocationBatch, _prop: str) -> list[str]:
    """详情页：contact_tasks 为一对多，sqladmin 会将本返回值与任务列表 zip，故须返回等长的短标签列表。"""
    tasks = list(getattr(m, "contact_tasks", None) or [])
    tasks.sort(key=lambda t: ((t.priority_rank or 0), (t.id or 0)))
    out: list[str] = []
    for t in tasks:
        kind = TASK_KIND_LABELS.get((t.task_kind or "").strip(), (t.task_kind or "").strip() or "—")
        ch = CONTACT_CHANNEL_LABELS.get((t.contact_channel or "wechat").strip(), (t.contact_channel or "wechat").strip())
        title = (t.title or "").strip() or "—"
        if len(title) > 48:
            title = title[:48] + "…"
        out.append(f"#{t.id} 序{t.priority_rank} {ch}·{kind} 客户{t.raw_customer_id} {title}")
    return out


def _fmt_task_allocation_snapshot_json_detail(m: TaskAllocationBatch, _prop: str):
    raw = m.input_snapshot_json
    if raw is None:
        return "—"
    try:
        text = json.dumps(raw, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(raw)
    return Markup(f'<pre class="admin-json-pre">{html.escape(text)}</pre>')


async def _admin_task_quick_action(request: Request) -> JSONResponse:
    """总览页快捷改状态：完成 / 跳过 / 待办（需已登录管理后台）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "请求体需为 JSON"}, status_code=400)
    raw_id = body.get("task_id")
    op = (body.get("op") or "").strip().lower()
    try:
        tid = int(raw_id)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "message": "task_id 无效"}, status_code=400)
    if op not in ("done", "skip", "pending"):
        return JSONResponse({"ok": False, "message": "op 须为 done | skip | pending"}, status_code=400)
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(ContactTask).where(ContactTask.id == tid))
        task = res.scalars().first()
        if not task:
            return JSONResponse({"ok": False, "message": "任务不存在"}, status_code=404)
        if op == "done":
            task.status = "done"
            task.completed_at = datetime.now()
            out_status = "done"
        elif op == "skip":
            task.status = "skipped"
            task.completed_at = None
            out_status = "skipped"
        else:
            task.status = "pending"
            task.completed_at = None
            task.completed_by_user_id = None
            out_status = "pending"
        await db.commit()
    return JSONResponse({"ok": True, "task_id": tid, "status": out_status})


async def _run_bg_allocation_job(job_id: str, sw: str, period: str) -> None:
    """入队 DB 队列并由 worker 执行，同步进度到内存 job 供前端轮询。"""
    from ai.task_allocation_jobs import update_job
    from ai.task_allocation_queue import (
        QueueTableMissingError,
        enqueue_single_or_get_active,
        wait_and_sync_memory_job,
    )

    await update_job(job_id, status="running", phase="入队中", detail=sw, pct=0.01)
    try:
        qid = await enqueue_single_or_get_active(
            sw,
            period,
            source="manual_regen",
            auto_publish=False,
            batch_label=f"手动分配 {sw}",
        )
        if not qid:
            await update_job(
                job_id,
                status="error",
                phase="入队失败",
                error="无法入队（销售号无效或周期不支持）",
                pct=1.0,
            )
            return
        await update_job(
            job_id,
            status="queued",
            phase="已入队",
            detail=f"queue#{qid}",
            pct=0.03,
        )
        await wait_and_sync_memory_job(job_id, qid, sw, period)
    except QueueTableMissingError as e:
        await update_job(job_id, status="error", phase="队列表未就绪", error=str(e), pct=1.0)
    except Exception as e:
        logger.exception("后台任务分配失败 job_id={} sw={}", job_id, sw)
        await update_job(job_id, status="error", phase="失败", error=str(e), pct=1.0)


class TaskAllocationOverviewView(BaseView):
    name = "任务分配总览"
    category = ADMIN_CAT_TASKS

    @expose("/task-allocation", methods=["GET", "POST"])
    async def task_allocation_page(self, request: Request):
        if request.method == "POST" and request.query_params.get("format") == "task_action":
            return await _admin_task_quick_action(request)

        if request.query_params.get("format") == "settings":
            if request.method == "GET":
                from ai.task_allocation_queue import get_worker_concurrency, snapshot_queue

                async with AsyncSessionLocal() as db:
                    enabled = await is_task_allocation_auto_enabled(db)
                    allowlist = await get_task_allocation_auto_allowlist(db)
                    limits = await get_task_allocation_limits(db)
                queue = await snapshot_queue()
                return JSONResponse(
                    {
                        "ok": True,
                        "auto_enabled": enabled,
                        "auto_sales_ids": allowlist,
                        "auto_sales_count": len(allowlist),
                        "limits": limits,
                        "worker_concurrency": await get_worker_concurrency(),
                        "worker_concurrency_max": queue.get("worker_concurrency_max", 16),
                        "queue": queue,
                    }
                )
            if request.method == "POST":
                try:
                    body = await request.json()
                except Exception:
                    return JSONResponse({"ok": False, "message": "请求体需为 JSON"}, status_code=400)
                from ai.task_allocation_queue import get_worker_concurrency, set_worker_concurrency, snapshot_queue

                async with AsyncSessionLocal() as db:
                    if "auto_enabled" in body:
                        await set_task_allocation_auto_enabled(db, bool(body.get("auto_enabled")))
                    if "auto_sales_ids" in body:
                        raw_ids = body.get("auto_sales_ids")
                        if not isinstance(raw_ids, list):
                            return JSONResponse(
                                {"ok": False, "message": "auto_sales_ids 须为字符串数组"},
                                status_code=400,
                            )
                        await set_task_allocation_auto_allowlist(db, raw_ids)
                    if "limits" in body and isinstance(body.get("limits"), dict):
                        await set_task_allocation_limits(db, body["limits"])
                    enabled = await is_task_allocation_auto_enabled(db)
                    allowlist = await get_task_allocation_auto_allowlist(db)
                    limits = await get_task_allocation_limits(db)
                if "worker_concurrency" in body:
                    try:
                        await set_worker_concurrency(int(body.get("worker_concurrency")))
                    except (TypeError, ValueError):
                        return JSONResponse(
                            {"ok": False, "message": "worker_concurrency 须为整数"},
                            status_code=400,
                        )
                queue = await snapshot_queue()
                return JSONResponse(
                    {
                        "ok": True,
                        "auto_enabled": enabled,
                        "auto_sales_ids": allowlist,
                        "auto_sales_count": len(allowlist),
                        "limits": limits,
                        "worker_concurrency": await get_worker_concurrency(),
                        "worker_concurrency_max": queue.get("worker_concurrency_max", 16),
                        "queue": queue,
                    }
                )

        if request.query_params.get("format") == "queue":
            from ai.task_allocation_queue import snapshot_queue

            return JSONResponse({"ok": True, "queue": await snapshot_queue()})

        action = (request.query_params.get("action") or "").strip()
        if action == "publish":
            bid = request.query_params.get("batch_id")
            if bid and bid.isdigit():
                async with AsyncSessionLocal() as db:
                    await publish_batch(db, int(bid))
            sw = (request.query_params.get("sales_wechat_id") or "").strip()
            period = (request.query_params.get("period") or PERIOD_DAILY).strip()
            url = "/admin/task-allocation"
            if sw:
                url += f"?sales_wechat_id={sw}&period={period}"
            return RedirectResponse(url=url, status_code=303)

        if request.method == "POST":
            action = (request.query_params.get("action") or "").strip()
            sw = (request.query_params.get("sales_wechat_id") or "").strip()
            period = (request.query_params.get("period") or PERIOD_DAILY).strip()
            if action == "generate" and sw:
                if period == PERIOD_MONTHLY:
                    return JSONResponse(
                        {
                            "ok": False,
                            "message": "月任务分配已停用；「月」视图仅作本月任务进度统计，请使用日/周任务分配",
                        },
                        status_code=400,
                    )
                if request.query_params.get("async") == "1":
                    from ai.task_allocation_jobs import try_acquire_job
                    from ai.task_allocation_queue import QueueTableMissingError, ensure_queue_table

                    try:
                        await ensure_queue_table()
                    except QueueTableMissingError as e:
                        return JSONResponse(
                            {"ok": False, "message": str(e)},
                            status_code=503,
                        )
                    p_start, _ = period_bounds(period, today_shanghai())
                    async with AsyncSessionLocal() as db:
                        if await find_generating_batch(db, sw, period, p_start):
                            return JSONResponse(
                                {
                                    "ok": False,
                                    "message": "该销售本周期已有模型分配进行中（数据库批次 generating），请稍后再试或刷新页面查看进度",
                                },
                                status_code=409,
                            )
                    jid, err = try_acquire_job(sw, period)
                    if err:
                        return JSONResponse({"ok": False, "message": err}, status_code=409)
                    asyncio.create_task(_run_bg_allocation_job(jid, sw, period))
                    return JSONResponse({"ok": True, "job_id": jid})
                async with AsyncSessionLocal() as db:
                    await generate_allocation_batch(
                        db,
                        sw,
                        period,
                        source="manual_regen",
                        auto_publish=False,
                    )
                return RedirectResponse(
                    url=f"/admin/task-allocation?sales_wechat_id={sw}&period={period}",
                    status_code=303,
                )
        if request.query_params.get("format") == "sales":
            async with AsyncSessionLocal() as db:
                res = await db.execute(
                    select(SalesWechatAccount)
                    .where(SalesWechatAccount.sales_wechat_id.isnot(None))
                    .order_by(SalesWechatAccount.nickname, SalesWechatAccount.sales_wechat_id)
                    .limit(500)
                )
                items: list[dict] = []
                for acc in res.scalars().all():
                    sw = (acc.sales_wechat_id or "").strip()
                    if not sw:
                        continue
                    label = _sales_wechat_option_label(
                        sw,
                        nickname=acc.nickname,
                        alias_name=acc.alias_name,
                        account_code=acc.account_code,
                    )
                    items.append(
                        {
                            "sales_wechat_id": sw,
                            "nickname": (acc.nickname or "").strip() or None,
                            "label": label,
                        }
                    )
            return JSONResponse({"ok": True, "items": items})

        if request.query_params.get("format") == "job":
            jid = (request.query_params.get("job_id") or "").strip()
            from ai.task_allocation_jobs import get_job, find_active_job

            row = await get_job(jid) if jid else None
            if not row:
                return JSONResponse({"ok": False, "message": "job 不存在或已过期"}, status_code=404)
            return JSONResponse({"ok": True, "job": row})

        if request.query_params.get("format") == "active_job":
            sw = (request.query_params.get("sales_wechat_id") or "").strip()
            period = (request.query_params.get("period") or PERIOD_DAILY).strip()
            from ai.task_allocation_jobs import find_active_job

            mem = find_active_job(sw, period) if sw else None
            batch_prog = None
            batch_id = None
            if sw:
                ref = today_shanghai()
                p_start, p_end = period_bounds(period, ref)
                async with AsyncSessionLocal() as db:
                    gen = await find_generating_batch(db, sw, period, p_start)
                    if gen:
                        batch_id = gen.id
                        snap = gen.input_snapshot_json if isinstance(gen.input_snapshot_json, dict) else {}
                        batch_prog = snap.get("progress")
            return JSONResponse(
                {
                    "ok": True,
                    "job": mem,
                    "batch_id": batch_id,
                    "batch_progress": batch_prog,
                }
            )
        if request.query_params.get("format") == "batches":
            sw = (request.query_params.get("sales_wechat_id") or "").strip()
            period = (request.query_params.get("period") or PERIOD_DAILY).strip()
            ref = _parse_ref_date(request.query_params.get("date") or "")
            batch_status = (request.query_params.get("batch_status") or "all").strip().lower()
            if batch_status not in _BATCH_STATUS_QUERY_VALUES:
                batch_status = "all"
            p_start, p_end = period_bounds(period, ref)
            items: list[dict] = []
            if sw and period != PERIOD_MONTHLY:
                statuses = _resolve_batch_statuses(batch_status)
                async with AsyncSessionLocal() as db:
                    res = await db.execute(
                        select(TaskAllocationBatch)
                        .where(TaskAllocationBatch.sales_wechat_id == sw)
                        .where(TaskAllocationBatch.period_type == period)
                        .where(TaskAllocationBatch.period_start == p_start)
                        .where(TaskAllocationBatch.status.in_(statuses))
                        .order_by(TaskAllocationBatch.id.desc())
                        .limit(30)
                    )
                    for b in res.scalars().all():
                        pub = b.published_at.isoformat(sep=" ", timespec="seconds") if b.published_at else ""
                        items.append(
                            {
                                "id": b.id,
                                "status": b.status,
                                "task_count": b.task_count,
                                "published_at": pub,
                                "created_at": b.created_at.isoformat(sep=" ", timespec="seconds")
                                if b.created_at
                                else "",
                            }
                        )
            return JSONResponse(
                {
                    "ok": True,
                    "period_start": p_start.isoformat(),
                    "period_end": p_end.isoformat(),
                    "items": items,
                }
            )
        if request.query_params.get("format") == "json":
            sw = (request.query_params.get("sales_wechat_id") or "").strip()
            period = (request.query_params.get("period") or PERIOD_DAILY).strip()
            ref_s = (request.query_params.get("date") or "").strip()
            ref = _parse_ref_date(ref_s) if ref_s else today_shanghai()
            batch_status = (request.query_params.get("batch_status") or "active").strip().lower()
            if batch_status not in _BATCH_STATUS_QUERY_VALUES:
                batch_status = "active"
            raw_batch_id = (request.query_params.get("batch_id") or "").strip()
            batch_id: int | None = None
            if raw_batch_id:
                try:
                    batch_id = int(raw_batch_id)
                except ValueError:
                    batch_id = None
            p_start, p_end = period_bounds(period, ref)
            is_historical = bool(ref_s) and not _is_current_period(period, p_start)
            async with AsyncSessionLocal() as db:
                batch = None
                items: list[dict] = []
                snap_json: dict = {}
                if sw and period == PERIOD_MONTHLY:
                    rows, _ = await query_month_progress_rows(
                        db,
                        sales_wechat_id=sw,
                        month_start=p_start,
                        month_end=p_end,
                    )
                    for t, scp, rc in rows:
                        kind = (t.task_kind or "contact").strip()
                        ch = (t.contact_channel or "wechat").strip()
                        cust_name = (rc.customer_name if rc else "") or ""
                        unit_name = (rc.unit_name if rc else "") or ""
                        remark = ""
                        if scp and (scp.wechat_remark or "").strip():
                            remark = (scp.wechat_remark or "").strip()
                        instr = (t.instruction or "").strip()
                        items.append(
                            {
                                "id": t.id,
                                "title": t.title,
                                "instruction": instr,
                                "instruction_preview": (instr[:120] + "…") if len(instr) > 120 else instr,
                                "status": t.status,
                                "priority_rank": t.priority_rank,
                                "priority_score": float(t.priority_score) if t.priority_score else None,
                                "due_date": t.due_date.isoformat(),
                                "raw_customer_id": t.raw_customer_id,
                                "task_kind": kind,
                                "task_kind_label": TASK_KIND_LABELS.get(kind, kind),
                                "contact_channel": ch,
                                "contact_channel_label": CONTACT_CHANNEL_LABELS.get(ch, ch),
                                "customer_name": cust_name.strip(),
                                "unit_name": unit_name.strip(),
                                "wechat_remark": remark,
                                "sales_wechat_id": t.sales_wechat_id,
                            }
                        )
                    stats = stats_from_task_dicts(items)
                    return JSONResponse(
                        {
                            "period_type": period,
                            "period_start": p_start.isoformat(),
                            "period_end": p_end.isoformat(),
                            "ref_date": ref.isoformat(),
                            "is_historical": is_historical,
                            "batch_id": None,
                            "batch_status": None,
                            "batch_status_filter": batch_status,
                            "view_mode": "month_progress",
                            "snapshot": None,
                            "stats": stats,
                            "items": items,
                        }
                    )
                if sw:
                    batch = await _load_batch_for_overview(
                        db,
                        sales_wechat_id=sw,
                        period_type=period,
                        period_start=p_start,
                        batch_status=batch_status,
                        batch_id=batch_id,
                    )
                    if batch and batch.status == "generating":
                        snap_json = batch.input_snapshot_json or {}
                        if not isinstance(snap_json, dict):
                            snap_json = {}
                        prog = snap_json.get("progress") or {}
                        return JSONResponse(
                            {
                                "period_type": period,
                                "period_start": p_start.isoformat(),
                                "period_end": p_end.isoformat(),
                                "batch_id": batch.id,
                                "batch_status": batch.status,
                                "view_mode": "generating",
                                "allocation_progress": prog,
                                "snapshot": None,
                                "stats": {
                                    "total": 0,
                                    "done": 0,
                                    "pending": 0,
                                    "overdue": 0,
                                    "completion_rate": 0,
                                },
                                "items": [],
                            }
                        )
                    if batch:
                        tres = await db.execute(
                            select(ContactTask, RawCustomer, SalesCustomerProfile)
                            .outerjoin(RawCustomer, RawCustomer.id == ContactTask.raw_customer_id)
                            .outerjoin(SalesCustomerProfile, SalesCustomerProfile.id == ContactTask.scp_id)
                            .where(ContactTask.batch_id == batch.id)
                            .order_by(ContactTask.priority_rank)
                        )
                        snap_json = batch.input_snapshot_json or {}
                        if not isinstance(snap_json, dict):
                            snap_json = {}
                        for t, rc, scp in tres.all():
                            kind = (t.task_kind or "contact").strip()
                            ch = (t.contact_channel or "wechat").strip()
                            cust_name = (rc.customer_name if rc else "") or ""
                            unit_name = (rc.unit_name if rc else "") or ""
                            remark = ""
                            if scp and (scp.wechat_remark or "").strip():
                                remark = (scp.wechat_remark or "").strip()
                            instr = (t.instruction or "").strip()
                            items.append(
                                {
                                    "id": t.id,
                                    "title": t.title,
                                    "instruction": instr,
                                    "instruction_preview": (instr[:120] + "…") if len(instr) > 120 else instr,
                                    "status": t.status,
                                    "priority_rank": t.priority_rank,
                                    "priority_score": float(t.priority_score) if t.priority_score else None,
                                    "due_date": t.due_date.isoformat(),
                                    "raw_customer_id": t.raw_customer_id,
                                    "task_kind": kind,
                                    "task_kind_label": TASK_KIND_LABELS.get(kind, kind),
                                    "contact_channel": ch,
                                    "contact_channel_label": CONTACT_CHANNEL_LABELS.get(ch, ch),
                                    "customer_name": cust_name.strip(),
                                    "unit_name": unit_name.strip(),
                                    "wechat_remark": remark,
                                    "sales_wechat_id": t.sales_wechat_id,
                                }
                            )
                        stats = await batch_stats(db, batch.id)
                    else:
                        stats = {
                            "total": 0,
                            "done": 0,
                            "pending": 0,
                            "overdue": 0,
                            "completion_rate": 0,
                        }
                else:
                    stats = {
                        "total": 0,
                        "done": 0,
                        "pending": 0,
                        "overdue": 0,
                        "completion_rate": 0,
                    }
            view_mode = "generating" if batch and batch.status == "generating" else (
                "historical" if is_historical else "current"
            )
            return JSONResponse(
                {
                    "period_type": period,
                    "period_start": p_start.isoformat(),
                    "period_end": p_end.isoformat(),
                    "ref_date": ref.isoformat(),
                    "is_historical": is_historical,
                    "batch_id": batch.id if batch else None,
                    "batch_status": batch.status if batch else None,
                    "batch_status_filter": batch_status,
                    "view_mode": view_mode,
                    "snapshot": {
                        "main_task_count": snap_json.get("main_task_count"),
                        "main_wechat_count": snap_json.get("main_wechat_count"),
                        "main_phone_count": snap_json.get("main_phone_count"),
                        "channel_caps": snap_json.get("channel_caps"),
                        "icebreaker_task_count": snap_json.get("icebreaker_task_count"),
                        "candidate_count": snap_json.get("candidate_count"),
                    }
                    if batch
                    else None,
                    "stats": stats,
                    "items": items,
                }
            )

        sw_raw = (request.query_params.get("sales_wechat_id") or "").strip()
        period_raw = (request.query_params.get("period") or PERIOD_DAILY).strip()
        if period_raw not in (PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY):
            period_raw = PERIOD_DAILY
        date_raw = (request.query_params.get("date") or "").strip()
        batch_status_raw = (request.query_params.get("batch_status") or "active").strip().lower()
        if batch_status_raw not in _BATCH_STATUS_QUERY_VALUES:
            batch_status_raw = "active"
        from core.admin_pages import render_admin_page

        period_js = json.dumps(period_raw)
        sw_param_js = json.dumps(sw_raw)
        date_js = json.dumps(date_raw)
        batch_status_js = json.dumps(batch_status_raw)
        history_js = json.dumps(bool(date_raw))
        page_html = f"""<link rel="stylesheet" href="/admin-static/pages/task-allocation.css">
<section class="admin-task-page">
    <p class="admin-muted mb-3">任务数量与刷新策略在下方配置（存数据库）。定时需开总开关并勾选销售；<strong>仅在工作日</strong>自动分配（周六日及法定节假日跳过，调休上班日仍会分配）。工作日 06:00 将销售批次<strong>入队</strong>，由后台 worker 并行执行（默认并发 4，可调）。周计划每日滚动刷新可在夜间画像后每日重算当周计划。「月」视图仅作本月任务进度统计，不再分配月任务。</p>
    <div id="allocProgressPanel" class="alloc-progress-panel" style="display:none">
      <div class="alloc-progress-head">
        <strong id="allocProgressTitle">模型分配进行中</strong>
        <span id="allocProgressPct" class="alloc-progress-pct">0%</span>
      </div>
      <div class="alloc-progress-track"><div class="alloc-progress-fill" id="allocProgressFill"></div></div>
      <div class="alloc-progress-detail" id="allocProgressDetail">—</div>
    </div>
    <p id="jobLine"></p>
    <p id="toast"></p>

    <div class="card mb-3" id="limitsPanel">
      <div class="card-body">
      <h3 class="card-title">任务数量与刷新策略</h3>
      <div class="limits-grid">
        <label>日任务·微信<input type="number" id="lim-daily-wechat" min="0" max="200" title="日任务微信触达上限"/></label>
        <label>日任务·电话<input type="number" id="lim-daily-phone" min="0" max="100" title="日任务电话触达上限"/></label>
        <label>周任务·微信<input type="number" id="lim-weekly-wechat" min="0" max="300" title="周任务微信触达上限"/></label>
        <label>周任务·电话<input type="number" id="lim-weekly-phone" min="0" max="150" title="周任务电话触达上限"/></label>
        <label>破冰产出上限<input type="number" id="lim-ice" min="0" max="200"/></label>
        <label>主线 LLM 候选数<input type="number" id="lim-max-cust" min="20" max="500" title="参与打分的已分析客户上限"/></label>
        <label>破冰 LLM 候选数<input type="number" id="lim-ice-fetch" min="20" max="800"/></label>
      </div>
      <div class="limits-checks">
        <label><input type="checkbox" id="lim-ice-on"/> 日任务含破冰</label>
        <label><input type="checkbox" id="lim-weekly-daily"/> 周计划每日滚动刷新（建议开）</label>
      </div>
      <div class="limits-foot">
        <span class="hint" id="limitsHint">修改后请点击保存；手动「生成草稿」与定时均使用此处配置。</span>
        <button type="button" class="btn btn-sm btn-primary" id="btn-save-limits">保存数量与策略</button>
      </div>
      <div class="limits-worker-row mt-3 pt-2 border-top">
        <label class="me-2">分配并发上限
          <input type="number" id="lim-worker-concurrency" min="1" max="16" style="width:4.5rem"/>
        </label>
        <button type="button" class="btn btn-sm btn-outline-primary" id="btn-save-worker-concurrency">保存并发</button>
        <span class="admin-muted small ms-2" id="workerConcurrencyHint">定时 06:00 入队后由后台 worker 并行执行；默认 4，可在运行中调整。</span>
      </div>
      </div>
    </div>

    <div class="card mb-3" id="allocQueuePanel">
      <div class="card-body py-2">
        <strong>分配队列</strong>
        <span class="admin-muted small ms-2" id="allocQueueLine">加载中…</span>
      </div>
    </div>

    <div class="card mb-3" id="autoSettings">
      <div class="card-body">
        <div class="form-check mb-2">
          <input type="checkbox" class="form-check-input" id="chk-auto"/>
          <label class="form-check-label" for="chk-auto">启用定时自动任务分配</label>
        </div>
        <span class="admin-muted small" id="autoSettingsHint">加载中…</span>
      </div>
    </div>

    <div class="card mb-3 auto-sales-panel" id="autoSalesPanel">
      <div class="card-body">
      <div class="auto-sales-head">
        <span>定时参与的销售</span>
        <span class="sub-h" id="autoSalesSub">仅勾选的账号会在工作日定时自动分配并发布</span>
        <button type="button" class="btn btn-sm btn-outline-secondary" id="btn-allow-all">全选</button>
        <button type="button" class="btn btn-sm btn-outline-secondary" id="btn-allow-none">清空</button>
        <button type="button" class="btn btn-sm btn-primary" id="btn-save-allow">保存勾选范围</button>
      </div>
      <div class="auto-sales-list" id="autoSalesList"></div>
      </div>
    </div>

    <div class="card mb-3">
      <div class="card-body">
        <form class="row g-3 align-items-end at-toolbar-form" method="get" action="/admin/task-allocation" id="toolbar-form">
          <div class="col-md-auto">
            <label class="form-label mb-1">销售</label>
            <select class="form-select form-select-sm" name="sales_wechat_id" id="sw">
              <option value="">— 请选择销售 —</option>
            </select>
          </div>
          <div class="col-md-auto">
            <label class="form-label mb-1">周期</label>
            <select class="form-select form-select-sm" name="period" id="period">
              <option value="daily">日任务（含破冰）</option>
              <option value="weekly">周任务</option>
              <option value="monthly">月进度（统计）</option>
            </select>
          </div>
          <div class="col-md-auto">
            <label class="form-label mb-1 d-block">历史任务</label>
            <div class="form-check mb-0 at-history-check">
              <input type="checkbox" class="form-check-input" id="chk-history" name="history" value="1"/>
              <label class="form-check-label" for="chk-history">查看历史</label>
            </div>
          </div>
          <div class="col-md-auto at-history-field" id="historyDateWrap" style="display:none">
            <label class="form-label mb-1" for="refDate">参考日期</label>
            <input type="date" class="form-control form-control-sm" name="date" id="refDate"/>
            <div class="form-text at-history-hint">按该日期所在日/周/月定位历史周期</div>
          </div>
          <div class="col-md-auto">
            <label class="form-label mb-1" for="batchStatus">发布状态</label>
            <select class="form-select form-select-sm" name="batch_status" id="batchStatus">
              <option value="active">当前批次（草稿/已发布）</option>
              <option value="published">已发布</option>
              <option value="draft">草稿</option>
              <option value="archived">已归档</option>
              <option value="all">全部状态</option>
            </select>
          </div>
          <div class="col-md-auto at-history-field" id="historyBatchWrap" style="display:none">
            <label class="form-label mb-1" for="batchPick">历史批次</label>
            <select class="form-select form-select-sm" id="batchPick">
              <option value="">— 自动匹配 —</option>
            </select>
          </div>
          <div class="col-md-auto d-flex flex-wrap gap-2 align-items-end">
            <button type="submit" class="btn btn-primary btn-sm">查询</button>
            <button type="button" class="btn btn-primary btn-sm" id="btn-gen">生成本周期草稿</button>
            <button type="button" class="btn btn-sm btn-secondary" id="btn-pub" style="display:none">发布草稿批次</button>
            <button type="button" class="btn btn-sm btn-outline-secondary" id="btn-list" title="打开联系任务列表（可筛选）">联系任务列表</button>
          </div>
        </form>
      </div>
    </div>

    <div class="at-stat-grid" id="cards">
      <div class="at-stat-card"><div class="v" id="c-total">—</div><div class="k" id="c-total-label">本批任务</div></div>
      <div class="at-stat-card"><div class="v" id="c-main-wechat">—</div><div class="k">微信主线</div></div>
      <div class="at-stat-card"><div class="v" id="c-main-phone">—</div><div class="k">电话主线</div></div>
      <div class="at-stat-card ice"><div class="v" id="c-ice">—</div><div class="k">破冰</div></div>
      <div class="at-stat-card"><div class="v" id="c-pend">—</div><div class="k">待办</div></div>
      <div class="at-stat-card"><div class="v" id="c-rate">—</div><div class="k">完成率</div></div>
    </div>
    <div class="at-progress"><div class="at-progress-inner" id="bar" style="width:0%"></div></div>
    <p class="text-muted small mb-2" id="metaLine">请选择销售后自动加载任务列表。</p>

    <div class="at-filters">
      <span class="lab">筛选</span>
      <button type="button" class="chip-f active" data-filter="all">全部</button>
      <button type="button" class="chip-f" data-filter="main">仅主线</button>
      <button type="button" class="chip-f" data-filter="wechat">微信主线</button>
      <button type="button" class="chip-f" data-filter="phone">电话主线</button>
      <button type="button" class="chip-f" data-filter="ice">仅破冰</button>
      <button type="button" class="chip-f" data-filter="pending">仅待办</button>
      <button type="button" class="chip-f" data-filter="done">已完成</button>
      <button type="button" class="chip-f" data-filter="skipped">已跳过</button>
    </div>

    <div class="card admin-task-table-wrap"><div class="card-body p-0"><div class="table-responsive">
      <table class="table table-vcenter table-sm mb-0">
        <thead>
          <tr>
            <th style="width:3rem">#</th>
            <th style="width:5rem">类型</th>
            <th style="width:11rem">客户</th>
            <th style="width:14rem">任务</th>
            <th>执行说明</th>
            <th style="width:6.5rem">截止</th>
            <th style="width:5rem">状态</th>
            <th style="width:11rem">快捷操作</th>
          </tr>
        </thead>
        <tbody id="tbody"><tr><td colspan="8" style="text-align:center;color:var(--muted);padding:1.5rem">—</td></tr></tbody>
      </table></div></div></div>
  <script>
    const periodEl = document.getElementById('period');
    periodEl.value = {period_js};
    const chkHistory = document.getElementById('chk-history');
    const refDateEl = document.getElementById('refDate');
    const batchStatusEl = document.getElementById('batchStatus');
    const batchPickEl = document.getElementById('batchPick');
    const historyDateWrap = document.getElementById('historyDateWrap');
    const historyBatchWrap = document.getElementById('historyBatchWrap');
    chkHistory.checked = {history_js};
    if ({history_js}) {{
      refDateEl.value = {date_js};
    }}
    batchStatusEl.value = {batch_status_js};
    let lastData = null;
    let filterMode = 'all';
    let jobPoll = null;
    let activeJobId = null;

    const BATCH_STATUS_LABELS = {{
      draft: '草稿',
      published: '已发布',
      archived: '已归档',
      generating: '生成中',
      failed: '失败',
    }};

    function isHistoryMode() {{
      return !!chkHistory.checked;
    }}

    function syncHistoryUi() {{
      const show = isHistoryMode();
      historyDateWrap.style.display = show ? '' : 'none';
      historyBatchWrap.style.display = show ? '' : 'none';
      document.getElementById('btn-gen').style.display = show ? 'none' : '';
      if (!show) {{
        refDateEl.value = '';
        batchPickEl.innerHTML = '<option value="">— 自动匹配 —</option>';
      }}
    }}

    function overviewQueryParams() {{
      const sw = document.getElementById('sw').value.trim();
      const period = periodEl.value;
      const params = new URLSearchParams();
      params.set('format', 'json');
      params.set('sales_wechat_id', sw);
      params.set('period', period);
      if (isHistoryMode() && refDateEl.value) {{
        params.set('date', refDateEl.value);
      }}
      const bs = batchStatusEl.value || 'active';
      params.set('batch_status', bs);
      if (isHistoryMode() && batchPickEl.value) {{
        params.set('batch_id', batchPickEl.value);
      }}
      return params;
    }}

    async function loadBatchOptions() {{
      if (!isHistoryMode()) {{
        batchPickEl.innerHTML = '<option value="">— 自动匹配 —</option>';
        return;
      }}
      const sw = document.getElementById('sw').value.trim();
      const period = periodEl.value;
      if (!sw || !refDateEl.value || period === 'monthly') {{
        batchPickEl.innerHTML = '<option value="">— 自动匹配 —</option>';
        return;
      }}
      const params = new URLSearchParams();
      params.set('format', 'batches');
      params.set('sales_wechat_id', sw);
      params.set('period', period);
      params.set('date', refDateEl.value);
      params.set('batch_status', batchStatusEl.value || 'all');
      const r = await fetch('/admin/task-allocation?' + params.toString(), {{ credentials: 'same-origin' }});
      const d = await r.json().catch(() => ({{}}));
      const cur = batchPickEl.value;
      const items = (d && d.items) ? d.items : [];
      if (!items.length) {{
        batchPickEl.innerHTML = '<option value="">— 无匹配批次 —</option>';
        return;
      }}
      batchPickEl.innerHTML = '<option value="">— 自动匹配 —</option>' + items.map(it => {{
        const lab = '#' + it.id + ' · ' + (BATCH_STATUS_LABELS[it.status] || it.status)
          + ' · ' + (it.task_count || 0) + ' 条';
        return '<option value="' + it.id + '">' + escapeHtml(lab) + '</option>';
      }}).join('');
      if (cur && items.some(it => String(it.id) === cur)) {{
        batchPickEl.value = cur;
      }}
    }}

    function jobStorageKey(sw, period) {{
      return 'taskAllocJob:' + (sw || '') + ':' + (period || '');
    }}

    function saveActiveJob(sw, period, jobId) {{
      if (!sw || !jobId) return;
      try {{ localStorage.setItem(jobStorageKey(sw, period), jobId); }} catch (e) {{}}
    }}

    function loadStoredJob(sw, period) {{
      try {{ return localStorage.getItem(jobStorageKey(sw, period)) || ''; }} catch (e) {{ return ''; }}
    }}

    function clearStoredJob(sw, period) {{
      try {{ localStorage.removeItem(jobStorageKey(sw, period)); }} catch (e) {{}}
    }}

    function setGenButtonRunning(running) {{
      const btn = document.getElementById('btn-gen');
      btn.disabled = !!running;
      btn.textContent = running ? '分配进行中…' : '生成本周期草稿';
    }}

    function renderAllocProgressPanel(opts) {{
      const panel = document.getElementById('allocProgressPanel');
      const fill = document.getElementById('allocProgressFill');
      const pctEl = document.getElementById('allocProgressPct');
      const detail = document.getElementById('allocProgressDetail');
      const title = document.getElementById('allocProgressTitle');
      if (!opts || !opts.visible) {{
        panel.style.display = 'none';
        panel.className = 'alloc-progress-panel';
        return;
      }}
      panel.style.display = 'block';
      panel.className = 'alloc-progress-panel'
        + (opts.isError ? ' is-error' : (opts.done ? ' is-done' : ''));
      const pct = Math.max(0, Math.min(100, Math.round((opts.pct || 0) * 100)));
      fill.style.width = pct + '%';
      pctEl.textContent = pct + '%';
      title.textContent = opts.isError ? '模型分配失败' : (opts.done ? '模型分配完成' : '模型分配进行中');
      const parts = [];
      if (opts.phase) parts.push(opts.phase);
      if (opts.detail) parts.push(opts.detail);
      if (opts.errorMsg) parts.push('错误: ' + opts.errorMsg);
      if (opts.batchId) parts.push('batch #' + opts.batchId);
      detail.textContent = parts.join(' · ') || '—';
    }}

    function applyJobState(job) {{
      if (!job) return false;
      const running = job.status === 'queued' || job.status === 'running';
      const done = job.status === 'done';
      const err = job.status === 'error';
      renderAllocProgressPanel({{
        visible: running || done || err,
        pct: job.pct || (done || err ? 1 : 0),
        phase: job.phase || '',
        detail: job.detail || '',
        errorMsg: job.error || '',
        batchId: job.batch_id || '',
        done: done,
        isError: err,
      }});
      setGenButtonRunning(running);
      return running;
    }}

    function applyBatchProgress(prog, batchId) {{
      if (!prog) return false;
      const st = (prog.status || '').toLowerCase();
      const running = st === 'generating' || (!st && (prog.pct || 0) < 1 && !prog.error);
      renderAllocProgressPanel({{
        visible: true,
        pct: prog.pct || 0,
        phase: prog.phase || '模型分配',
        detail: prog.detail || '',
        errorMsg: prog.error || '',
        batchId: batchId || '',
        done: false,
        isError: !!prog.error,
      }});
      setGenButtonRunning(running);
      return running;
    }}

    async function pollJobOnce(jobId, sw, period) {{
      const pr = await fetch('/admin/task-allocation?format=job&job_id=' + encodeURIComponent(jobId), {{ credentials: 'same-origin' }});
      const d = await pr.json().catch(() => ({{}}));
      if (!pr.ok || !d.ok || !d.job) return null;
      const job = d.job;
      applyJobState(job);
      const line = document.getElementById('jobLine');
      line.style.display = 'block';
      const pct = Math.round((job.pct || 0) * 100);
      line.textContent = (job.phase || '') + (job.detail ? ' · ' + job.detail : '') + ' — ' + pct + '%';
      if (job.status === 'done' || job.status === 'error') {{
        if (jobPoll) {{ clearInterval(jobPoll); jobPoll = null; }}
        activeJobId = null;
        clearStoredJob(sw, period);
        setGenButtonRunning(false);
        if (job.status === 'done') {{
          line.textContent += ' | batch_id=' + (job.batch_id || '') + ' 任务数=' + (job.task_count || 0);
          renderAllocProgressPanel({{ visible: true, pct: 1, phase: '完成', detail: line.textContent, done: true, batchId: job.batch_id }});
          setTimeout(() => renderAllocProgressPanel({{ visible: false }}), 8000);
        }}
        refresh();
      }}
      return job;
    }}

    function startJobPolling(jobId, sw, period) {{
      activeJobId = jobId;
      saveActiveJob(sw, period, jobId);
      if (jobPoll) clearInterval(jobPoll);
      const tick = () => pollJobOnce(jobId, sw, period).catch(() => {{}});
      tick();
      jobPoll = setInterval(tick, 1200);
    }}

    async function resumeAllocationProgress() {{
      const sw = document.getElementById('sw').value.trim();
      const period = periodEl.value;
      if (!sw || period === 'monthly') {{
        renderAllocProgressPanel({{ visible: false }});
        setGenButtonRunning(false);
        return;
      }}
      if (lastData && lastData.view_mode === 'generating' && lastData.allocation_progress) {{
        applyBatchProgress(lastData.allocation_progress, lastData.batch_id);
        return;
      }}
      const stored = loadStoredJob(sw, period);
      if (stored) {{
        const job = await pollJobOnce(stored, sw, period);
        if (job && (job.status === 'queued' || job.status === 'running')) {{
          startJobPolling(stored, sw, period);
          return;
        }}
      }}
      try {{
        const r = await fetch('/admin/task-allocation?format=active_job&sales_wechat_id=' + encodeURIComponent(sw) + '&period=' + period, {{ credentials: 'same-origin' }});
        const d = await r.json().catch(() => ({{}}));
        if (d.ok && d.batch_progress) {{
          applyBatchProgress(d.batch_progress, d.batch_id);
          if (d.job && (d.job.status === 'queued' || d.job.status === 'running')) {{
            startJobPolling(d.job.job_id, sw, period);
          }}
          return;
        }}
      }} catch (e) {{}}
      if (!activeJobId) {{
        renderAllocProgressPanel({{ visible: false }});
        setGenButtonRunning(false);
      }}
    }}

    function escapeHtml(s) {{
      return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }}

    const initialSw = {sw_param_js};
    let salesCatalog = [];

    function fillSalesSelect(items) {{
      const sel = document.getElementById('sw');
      const cur = initialSw || sel.value || '';
      sel.innerHTML = '<option value="">— 请选择销售 —</option>';
      items.forEach(it => {{
        const o = document.createElement('option');
        o.value = it.sales_wechat_id;
        o.textContent = it.label || it.sales_wechat_id;
        o.title = it.sales_wechat_id;
        if (it.sales_wechat_id === cur) o.selected = true;
        sel.appendChild(o);
      }});
    }}

    function renderAutoSalesAllowlist(items, selectedIds) {{
      const box = document.getElementById('autoSalesList');
      const selected = new Set(selectedIds || []);
      if (!items.length) {{
        box.innerHTML = '<span class="muted" style="font-size:.8rem">暂无销售主数据</span>';
        return;
      }}
      box.innerHTML = items.map(it => {{
        const id = it.sales_wechat_id;
        const chk = selected.has(id) ? ' checked' : '';
        return '<label class="auto-sales-item"><input type="checkbox" class="auto-sales-chk" value="' +
          escapeHtml(id) + '"' + chk + '><span title="' + escapeHtml(id) + '">' +
          escapeHtml(it.label || id) + '</span></label>';
      }}).join('');
    }}

    function collectAllowlistChecked() {{
      return Array.from(document.querySelectorAll('.auto-sales-chk:checked'))
        .map(el => el.value)
        .filter(Boolean);
    }}

    function fillLimitsForm(lim) {{
      if (!lim) return;
      document.getElementById('lim-daily-wechat').value = lim.daily_wechat_cap;
      document.getElementById('lim-daily-phone').value = lim.daily_phone_cap;
      document.getElementById('lim-weekly-wechat').value = lim.weekly_wechat_cap;
      document.getElementById('lim-weekly-phone').value = lim.weekly_phone_cap;
      document.getElementById('lim-ice').value = lim.icebreaker_cap;
      document.getElementById('lim-max-cust').value = lim.max_customers_main;
      document.getElementById('lim-ice-fetch').value = lim.icebreaker_max_candidates;
      document.getElementById('lim-ice-on').checked = !!lim.icebreaker_enabled;
      document.getElementById('lim-weekly-daily').checked = !!lim.weekly_refresh_daily;
    }}

    function fillWorkerConcurrency(v, maxV) {{
      const el = document.getElementById('lim-worker-concurrency');
      if (!el) return;
      const n = parseInt(v, 10);
      el.max = String(maxV || 16);
      el.value = isNaN(n) ? 4 : n;
    }}

    function renderAllocQueueLine(q) {{
      const el = document.getElementById('allocQueueLine');
      if (!el) return;
      if (!q) {{
        el.textContent = '—';
        return;
      }}
      const st = q.status || 'idle';
      if (q.table_missing) {{
        el.textContent = '队列表未创建：请在 backend 目录执行 alembic upgrade head 后重启服务';
        el.classList.add('text-danger');
        return;
      }}
      el.classList.remove('text-danger');
      const parts = [
        '状态 ' + st,
        '待办 ' + (q.pending || 0),
        '执行中 ' + (q.running || 0),
        '已完成 ' + (q.succeeded || 0),
        '失败 ' + (q.failed || 0),
      ];
      if (q.current_batch_label) parts.push('当前批次「' + q.current_batch_label + '」');
      el.textContent = parts.join(' · ');
    }}

    async function refreshAllocQueue() {{
      try {{
        const r = await fetch('/admin/task-allocation?format=queue', {{ credentials: 'same-origin' }});
        const d = await r.json();
        if (d.ok) renderAllocQueueLine(d.queue);
      }} catch (e) {{ /* ignore */ }}
    }}

    function collectLimitsPayload() {{
      return {{
        daily_wechat_cap: parseInt(document.getElementById('lim-daily-wechat').value, 10),
        daily_phone_cap: parseInt(document.getElementById('lim-daily-phone').value, 10),
        weekly_wechat_cap: parseInt(document.getElementById('lim-weekly-wechat').value, 10),
        weekly_phone_cap: parseInt(document.getElementById('lim-weekly-phone').value, 10),
        icebreaker_cap: parseInt(document.getElementById('lim-ice').value, 10),
        max_customers_main: parseInt(document.getElementById('lim-max-cust').value, 10),
        icebreaker_max_candidates: parseInt(document.getElementById('lim-ice-fetch').value, 10),
        icebreaker_enabled: document.getElementById('lim-ice-on').checked,
        weekly_refresh_daily: document.getElementById('lim-weekly-daily').checked,
      }};
    }}

    function updateAutoSettingsHint(enabled, count, lim) {{
      const hint = document.getElementById('autoSettingsHint');
      const panel = document.getElementById('autoSalesPanel');
      const sub = document.getElementById('autoSalesSub');
      const sched = (lim && lim.weekly_refresh_daily)
        ? '工作日 06:00 含日+周滚动'
        : '工作日 06:00 / 周一 06:30';
      if (!enabled) {{
        panel.classList.remove('visible');
        hint.textContent = '已关闭：仅本页「生成草稿」会分配；开启后可勾选参与定时的销售';
        return;
      }}
      panel.classList.add('visible');
      if (count > 0) {{
        hint.textContent = '已开启：定时仅对勾选的 ' + count + ' 个销售（' + sched + '）';
        sub.textContent = '已勾选 ' + count + ' 个 · 修改后请点击「保存勾选范围」';
      }} else {{
        hint.textContent = '已开启但未勾选销售：定时将跳过（请勾选并保存）';
        sub.textContent = '请勾选参与灰度的销售并保存';
      }}
    }}

    async function loadSalesCatalog() {{
      const r = await fetch('/admin/task-allocation?format=sales', {{ credentials: 'same-origin' }});
      const d = await r.json();
      if (!d.ok || !d.items) return [];
      salesCatalog = d.items;
      fillSalesSelect(salesCatalog);
      return salesCatalog;
    }}

    async function loadAutoSettings() {{
      const chk = document.getElementById('chk-auto');
      try {{
        const r = await fetch('/admin/task-allocation?format=settings', {{ credentials: 'same-origin' }});
        const d = await r.json();
        if (!d.ok) {{
          document.getElementById('autoSettingsHint').textContent = '无法读取自动分配设置';
          return;
        }}
        chk.checked = !!d.auto_enabled;
        fillLimitsForm(d.limits || {{}});
        fillWorkerConcurrency(d.worker_concurrency, d.worker_concurrency_max);
        renderAllocQueueLine(d.queue);
        renderAutoSalesAllowlist(salesCatalog, d.auto_sales_ids || []);
        updateAutoSettingsHint(!!d.auto_enabled, d.auto_sales_count || 0, d.limits);
      }} catch (e) {{
        document.getElementById('autoSettingsHint').textContent = '读取设置失败: ' + e;
      }}
    }}

    async function postAutoSettings(payload) {{
      const r = await fetch('/admin/task-allocation?format=settings', {{
        method: 'POST',
        credentials: 'same-origin',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const d = await r.json().catch(() => ({{}}));
      if (!r.ok || !d.ok) {{
        throw new Error((d && d.message) ? d.message : ('HTTP ' + r.status));
      }}
      return d;
    }}

    document.getElementById('chk-auto').addEventListener('change', async (ev) => {{
      const hint = document.getElementById('autoSettingsHint');
      const enabled = ev.target.checked;
      hint.textContent = '保存中…';
      try {{
        await postAutoSettings({{ auto_enabled: enabled }});
        await loadAutoSettings();
      }} catch (e) {{
        ev.target.checked = !enabled;
        hint.textContent = '保存异常: ' + e;
      }}
    }});

    document.getElementById('btn-save-allow').addEventListener('click', async () => {{
      const hint = document.getElementById('autoSettingsHint');
      const ids = collectAllowlistChecked();
      hint.textContent = '保存勾选范围中…';
      try {{
        await postAutoSettings({{ auto_sales_ids: ids }});
        await loadAutoSettings();
      }} catch (e) {{
        hint.textContent = '保存失败: ' + e;
      }}
    }});

    document.getElementById('btn-allow-all').addEventListener('click', () => {{
      document.querySelectorAll('.auto-sales-chk').forEach(el => {{ el.checked = true; }});
    }});
    document.getElementById('btn-allow-none').addEventListener('click', () => {{
      document.querySelectorAll('.auto-sales-chk').forEach(el => {{ el.checked = false; }});
    }});

    document.getElementById('btn-save-limits').addEventListener('click', async () => {{
      const hint = document.getElementById('limitsHint');
      hint.textContent = '保存中…';
      try {{
        const d = await postAutoSettings({{ limits: collectLimitsPayload() }});
        fillLimitsForm(d.limits || {{}});
        hint.textContent = '已保存 · 下次生成/定时将使用新配置';
        await loadAutoSettings();
      }} catch (e) {{
        hint.textContent = '保存失败: ' + e;
      }}
    }});

    document.getElementById('btn-save-worker-concurrency').addEventListener('click', async () => {{
      const hint = document.getElementById('workerConcurrencyHint');
      const n = parseInt(document.getElementById('lim-worker-concurrency').value, 10);
      hint.textContent = '保存中…';
      try {{
        const d = await postAutoSettings({{ worker_concurrency: n }});
        fillWorkerConcurrency(d.worker_concurrency, d.worker_concurrency_max);
        hint.textContent = '已保存并发 ' + d.worker_concurrency + '（约 2 秒内生效）';
        renderAllocQueueLine(d.queue);
      }} catch (e) {{
        hint.textContent = '保存失败: ' + e;
      }}
    }});

    function countMain(items) {{
      return items.filter(it => (it.task_kind || 'contact') !== 'icebreaker').length;
    }}
    function countMainWechat(items) {{
      return items.filter(it => (it.task_kind || 'contact') !== 'icebreaker' && (it.contact_channel || 'wechat') !== 'phone').length;
    }}
    function countMainPhone(items) {{
      return items.filter(it => (it.task_kind || 'contact') !== 'icebreaker' && (it.contact_channel || '') === 'phone').length;
    }}
    function countIce(items) {{
      return items.filter(it => (it.task_kind || '') === 'icebreaker').length;
    }}
    function countPend(items) {{
      return items.filter(it => ['pending','in_progress'].indexOf(it.status) >= 0).length;
    }}

    function applyFilter(items) {{
      return items.filter(it => {{
        const kind = it.task_kind || 'contact';
        const ch = it.contact_channel || 'wechat';
        if (filterMode === 'ice' && kind !== 'icebreaker') return false;
        if (filterMode === 'main' && kind === 'icebreaker') return false;
        if (filterMode === 'wechat' && (kind === 'icebreaker' || ch === 'phone')) return false;
        if (filterMode === 'phone' && (kind === 'icebreaker' || ch !== 'phone')) return false;
        if (filterMode === 'pending' && ['pending','in_progress'].indexOf(it.status) < 0) return false;
        if (filterMode === 'done' && it.status !== 'done') return false;
        if (filterMode === 'skipped' && it.status !== 'skipped') return false;
        return true;
      }});
    }}

    function channelBadge(it) {{
      const kind = (it.task_kind || 'contact').trim();
      if (kind === 'icebreaker') return '';
      const ch = (it.contact_channel || 'wechat').trim();
      const lab = it.contact_channel_label || (ch === 'phone' ? '电话' : '微信');
      const cls = ch === 'phone' ? 'at-channel-phone' : 'at-channel-wechat';
      return '<span class="at-channel-badge badge ' + cls + '">' + escapeHtml(lab) + '</span> ';
    }}

    function kindBadge(it) {{
      const k = (it.task_kind || 'contact').trim();
      const lab = it.task_kind_label || k;
      const kindClass =
        k === 'icebreaker' ? 'at-kind-icebreaker'
        : k === 'follow_up' ? 'at-kind-follow_up'
        : k === 'close_deal' ? 'at-kind-close_deal'
        : k === 'revisit' ? 'at-kind-revisit'
        : 'at-kind-contact';
      return '<span class="at-kind-badge badge ' + kindClass + '">' + escapeHtml(lab) + '</span>';
    }}

    function statusCell(st) {{
      let cls = 'st-pending';
      if (st === 'done') cls = 'st-done';
      else if (st === 'overdue') cls = 'st-overdue';
      else if (st === 'skipped') cls = 'st-skip';
      else if (st === 'in_progress') cls = 'st-progress';
      return '<span class="at-status-badge badge st ' + cls + '">' + escapeHtml(st) + '</span>';
    }}

    function renderTable(items) {{
      const body = document.getElementById('tbody');
      if (!items.length) {{
        body.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:1.25rem">暂无任务</td></tr>';
        return;
      }}
      const rows = items.map(it => {{
        const cust = (it.customer_name || '（未登记姓名）');
        const sub = [it.unit_name, it.wechat_remark].filter(Boolean).join(' · ');
        const ins = it.instruction_preview || '';
        const fullIns = it.instruction || '';
        const editUrl = '/admin/contact-task/edit/' + it.id;
        const detUrl = '/admin/contact-task/details/' + it.id;
        const canQuick = ['pending','in_progress','overdue'].indexOf(it.status) >= 0;
        const ops = canQuick
          ? '<button type="button" class="btn btn-sm btn-outline-secondary" data-op="done" data-id="' + it.id + '">完成</button> '
            + '<button type="button" class="btn btn-sm btn-outline-secondary" data-op="skip" data-id="' + it.id + '">跳过</button> '
            + '<a class="btn btn-sm btn-outline-secondary" href="' + editUrl + '" target="_blank" rel="noopener">编辑</a>'
          : (it.status === 'skipped'
              ? '<button type="button" class="btn btn-sm btn-outline-secondary" data-op="pending" data-id="' + it.id + '">恢复待办</button> '
              : '<button type="button" class="btn btn-sm btn-outline-secondary" data-op="pending" data-id="' + it.id + '">改待办</button> ')
            + '<a class="btn btn-sm btn-outline-secondary" href="' + detUrl + '">详情</a>';
        const kindSafe = /^[a-z0-9_]+$/i.test(String(it.task_kind || '').trim())
          ? String(it.task_kind || 'contact').trim()
          : 'contact';
        return '<tr data-task-kind="' + kindSafe + '" data-contact-channel="' + escapeHtml((it.contact_channel || 'wechat').trim()) + '">'
          + '<td>' + it.priority_rank + '</td>'
          + '<td>' + channelBadge(it) + kindBadge(it) + '</td>'
          + '<td><div class="cust">' + escapeHtml(cust) + '</div>'
          + (sub ? '<div style="font-size:.75rem;color:var(--muted)">' + escapeHtml(sub) + '</div>' : '')
          + '<div class="rid">' + escapeHtml(it.raw_customer_id || '') + '</div></td>'
          + '<td>' + escapeHtml(it.title || '') + '</td>'
          + '<td class="instr" title="' + escapeHtml(fullIns) + '">' + escapeHtml(ins) + '</td>'
          + '<td>' + escapeHtml(it.due_date || '') + '</td>'
          + '<td>' + statusCell(it.status) + '</td>'
          + '<td class="ops">' + ops + '</td>'
          + '</tr>';
      }}).join('');
      body.innerHTML = rows;
      body.querySelectorAll('button[data-op]').forEach(btn => {{
        btn.addEventListener('click', () => taskOp(btn.getAttribute('data-id'), btn.getAttribute('data-op')));
      }});
    }}

    async function taskOp(id, op) {{
      const toast = document.getElementById('toast');
      toast.className = '';
      toast.style.display = 'none';
      try {{
        const r = await fetch('/admin/task-allocation?format=task_action', {{
          method: 'POST',
          credentials: 'same-origin',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ task_id: parseInt(id, 10), op }})
        }});
        const j = await r.json().catch(() => ({{}}));
        if (!r.ok || !j.ok) {{
          toast.textContent = (j && j.message) ? j.message : ('操作失败 HTTP ' + r.status);
          toast.className = 'err';
          return;
        }}
        toast.textContent = '已更新任务 #' + id + ' → ' + j.status;
        toast.className = 'ok';
        await refresh();
      }} catch (e) {{
        toast.textContent = '请求异常: ' + e;
        toast.className = 'err';
      }}
    }}

    async function refresh() {{
      const sel = document.getElementById('sw');
      const sw = sel.value.trim();
      const period = periodEl.value;
      if (!sw) return;
      if (isHistoryMode() && !refDateEl.value) {{
        document.getElementById('metaLine').textContent = '请勾选「查看历史」并选择参考日期';
        document.getElementById('tbody').innerHTML =
          '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:1.25rem">请选择参考日期</td></tr>';
        return;
      }}
      const salesLabel = sel.options[sel.selectedIndex]
        ? sel.options[sel.selectedIndex].textContent
        : sw;
      const r = await fetch('/admin/task-allocation?' + overviewQueryParams().toString(), {{ credentials: 'same-origin' }});
      const d = await r.json();
      lastData = d;
      const st = d.stats || {{}};
      const rate = Math.round((st.completion_rate || 0) * 100);
      const items = d.items || [];
      const isMonthProgress = d.view_mode === 'month_progress' || period === 'monthly';
      const isHistorical = !!d.is_historical || isHistoryMode();
      document.getElementById('c-total-label').textContent = isMonthProgress ? '本月任务' : '本批任务';
      document.getElementById('c-total').textContent = isMonthProgress ? (st.total || items.length) : items.length;
      document.getElementById('c-main-wechat').textContent = countMainWechat(items);
      document.getElementById('c-main-phone').textContent = countMainPhone(items);
      document.getElementById('c-ice').textContent = countIce(items);
      document.getElementById('c-pend').textContent = countPend(items);
      document.getElementById('c-rate').textContent = rate + '%';
      document.getElementById('bar').style.width = rate + '%';
      const snap = d.snapshot || {{}};
      let meta = '销售 <strong>' + escapeHtml(salesLabel) + '</strong> · 周期 <strong>' + d.period_start + '</strong> ~ <strong>' + d.period_end + '</strong>';
      if (isHistorical) {{
        meta += ' · <span class="at-history-badge">历史查看</span>';
        if (d.ref_date) meta += ' · 参考日 <strong>' + escapeHtml(d.ref_date) + '</strong>';
      }}
      if (isMonthProgress) {{
        meta += ' · <span style="color:var(--muted)">汇总本月日/周任务（按截止日）</span>';
      }} else if (d.batch_id) {{
        const stLab = BATCH_STATUS_LABELS[d.batch_status] || d.batch_status || '';
        meta += ' · 批次 <strong>#' + d.batch_id + '</strong> <span class="at-batch-status st-' + escapeHtml(d.batch_status||'') + '">' + escapeHtml(stLab) + '</span>';
      }}
      if (snap.main_task_count != null) meta += ' · 快照主线 <strong>' + snap.main_task_count + '</strong>';
      if (snap.main_wechat_count != null) meta += '（微信 <strong>' + snap.main_wechat_count + '</strong>';
      if (snap.main_phone_count != null) meta += ' / 电话 <strong>' + snap.main_phone_count + '</strong>）';
      if (snap.channel_caps) {{
        meta += ' · 策略上限 微信 <strong>' + (snap.channel_caps.wechat ?? '—') + '</strong> / 电话 <strong>' + (snap.channel_caps.phone ?? '—') + '</strong>';
      }}
      if (snap.icebreaker_task_count != null) meta += ' · 破冰 <strong>' + snap.icebreaker_task_count + '</strong>';
      meta += ' · 应办 ' + (st.total||0) + ' / 完成 ' + (st.done||0) + ' / 逾期 ' + (st.overdue||0);
      document.getElementById('metaLine').innerHTML = meta;
      const pub = document.getElementById('btn-pub');
      pub.style.display = (!isMonthProgress && !isHistorical && d.batch_status === 'draft' && d.batch_id) ? 'inline-block' : 'none';
      pub.onclick = () => {{
        location.href = '/admin/task-allocation?action=publish&batch_id=' + d.batch_id +
          '&sales_wechat_id=' + encodeURIComponent(sw) + '&period=' + period;
      }};
      renderTable(applyFilter(items));
      if (isHistorical) {{
        renderAllocProgressPanel({{ visible: false }});
        setGenButtonRunning(false);
        return;
      }}
      if (d.view_mode === 'generating' && d.allocation_progress) {{
        applyBatchProgress(d.allocation_progress, d.batch_id);
      }} else if (!activeJobId) {{
        resumeAllocationProgress();
      }}
    }}

    document.querySelectorAll('.chip-f').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.chip-f').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        filterMode = btn.getAttribute('data-filter') || 'all';
        if (lastData && lastData.items) renderTable(applyFilter(lastData.items));
      }});
    }});

    document.getElementById('btn-list').onclick = () => {{
      window.open('/admin/contact-task/list', '_blank');
    }};

    document.getElementById('btn-gen').onclick = async () => {{
      const sw = document.getElementById('sw').value.trim();
      if (!sw) {{ alert('请选择销售'); return; }}
      const period = periodEl.value;
      if (period === 'monthly') {{
        alert('月视图仅作进度统计，请切换到日/周任务后再生成');
        return;
      }}
      const line = document.getElementById('jobLine');
      const btn = document.getElementById('btn-gen');
      line.style.display = 'block';
      line.textContent = '正在提交后台分配任务…';
      renderAllocProgressPanel({{ visible: true, pct: 0.02, phase: '提交中', detail: sw }});
      setGenButtonRunning(true);
      if (jobPoll) {{ clearInterval(jobPoll); jobPoll = null; }}
      try {{
        const postUrl = '/admin/task-allocation?action=generate&async=1&sales_wechat_id=' +
          encodeURIComponent(sw) + '&period=' + period;
        const resp = await fetch(postUrl, {{ method: 'POST', credentials: 'same-origin' }});
        const j = await resp.json().catch(() => ({{}}));
        if (!resp.ok || !j.ok || !j.job_id) {{
          const msg = (j && j.message) ? j.message : ('HTTP ' + resp.status);
          line.textContent = '提交失败: ' + msg;
          renderAllocProgressPanel({{ visible: true, pct: 1, phase: '提交失败', errorMsg: msg, isError: true }});
          setGenButtonRunning(false);
          return;
        }}
        startJobPolling(j.job_id, sw, period);
      }} catch (e) {{
        line.textContent = '请求异常: ' + e;
        renderAllocProgressPanel({{ visible: true, pct: 1, phase: '请求异常', errorMsg: String(e), isError: true }});
        setGenButtonRunning(false);
      }}
    }};

    document.getElementById('sw').addEventListener('change', () => {{
      if (jobPoll) {{ clearInterval(jobPoll); jobPoll = null; }}
      activeJobId = null;
      loadBatchOptions().then(() => refresh()).then(() => resumeAllocationProgress());
    }});
    periodEl.addEventListener('change', () => {{
      if (jobPoll) {{ clearInterval(jobPoll); jobPoll = null; }}
      activeJobId = null;
      loadBatchOptions().then(() => refresh()).then(() => resumeAllocationProgress());
    }});
    chkHistory.addEventListener('change', () => {{
      syncHistoryUi();
      if (isHistoryMode()) {{
        loadBatchOptions().then(() => refresh());
      }} else {{
        refresh().then(() => resumeAllocationProgress());
      }}
    }});
    refDateEl.addEventListener('change', () => {{
      if (!isHistoryMode()) return;
      loadBatchOptions().then(() => refresh());
    }});
    batchStatusEl.addEventListener('change', () => {{
      loadBatchOptions().then(() => refresh());
    }});
    batchPickEl.addEventListener('change', () => {{
      if (isHistoryMode()) refresh();
    }});

    syncHistoryUi();
    loadSalesCatalog().then(() => loadAutoSettings()).then(() => {{
      if (document.getElementById('sw').value.trim()) {{
        loadBatchOptions().then(() => refresh()).then(() => resumeAllocationProgress());
      }}
    }});
    refreshAllocQueue();
    setInterval(refreshAllocQueue, 5000);
    setInterval(() => {{
      if (isHistoryMode()) return;
      refresh().then(() => {{
        if (lastData && lastData.view_mode === 'generating') resumeAllocationProgress();
      }});
    }}, 8000);
  </script>
</section>"""
        return await render_admin_page(
            request,
            "admin/raw_content.html",
            title="任务分配总览",
            subtitle="按销售与周期查看任务批次",
            raw_html=Markup(page_html),
        )


class TaskAllocationBatchAdmin(AdminModelView, model=TaskAllocationBatch):
    name = "分配批次"
    name_plural = "分配批次"
    category = ADMIN_CAT_TASKS
    column_list = [
        TaskAllocationBatch.id,
        TaskAllocationBatch.sales_wechat_id,
        TaskAllocationBatch.period_type,
        TaskAllocationBatch.period_start,
        TaskAllocationBatch.period_end,
        TaskAllocationBatch.status,
        TaskAllocationBatch.task_count,
        TaskAllocationBatch.source,
        TaskAllocationBatch.published_at,
        TaskAllocationBatch.created_at,
    ]
    column_labels = {
        TaskAllocationBatch.id: "批次 ID",
        TaskAllocationBatch.sales_wechat_id: "销售微信",
        TaskAllocationBatch.period_type: "周期类型",
        TaskAllocationBatch.period_start: "周期开始",
        TaskAllocationBatch.period_end: "周期结束",
        TaskAllocationBatch.status: "状态",
        TaskAllocationBatch.task_count: "任务数",
        TaskAllocationBatch.source: "来源",
        TaskAllocationBatch.published_at: "发布时间",
        TaskAllocationBatch.created_at: "创建时间",
        TaskAllocationBatch.user_id: "用户 ID",
        TaskAllocationBatch.input_snapshot_json: "分配输入快照 (JSON)",
        "contact_tasks": "联系任务",
    }
    column_formatters = {
        TaskAllocationBatch.sales_wechat_id: _fmt_sales_wechat_column,
    }
    column_details_list = [
        TaskAllocationBatch.id,
        TaskAllocationBatch.sales_wechat_id,
        TaskAllocationBatch.user_id,
        TaskAllocationBatch.period_type,
        TaskAllocationBatch.period_start,
        TaskAllocationBatch.period_end,
        TaskAllocationBatch.source,
        TaskAllocationBatch.status,
        TaskAllocationBatch.task_count,
        TaskAllocationBatch.input_snapshot_json,
        TaskAllocationBatch.created_at,
        TaskAllocationBatch.published_at,
        "contact_tasks",
    ]
    column_formatters_detail = {
        TaskAllocationBatch.sales_wechat_id: _fmt_sales_wechat_column,
        TaskAllocationBatch.input_snapshot_json: _fmt_task_allocation_snapshot_json_detail,
        "contact_tasks": _fmt_task_allocation_contact_tasks_detail,
    }
    form_columns = [
        TaskAllocationBatch.sales_wechat_id,
        TaskAllocationBatch.user_id,
        TaskAllocationBatch.period_type,
        TaskAllocationBatch.period_start,
        TaskAllocationBatch.period_end,
        TaskAllocationBatch.source,
        TaskAllocationBatch.status,
        TaskAllocationBatch.task_count,
    ]
    column_sortable_list = [
        TaskAllocationBatch.id,
        TaskAllocationBatch.period_start,
        TaskAllocationBatch.period_end,
        TaskAllocationBatch.task_count,
        TaskAllocationBatch.published_at,
        TaskAllocationBatch.created_at,
    ]
    column_default_sort = [(TaskAllocationBatch.id, True)]
    column_filters = [
        SalesWechatIdFilter(TaskAllocationBatch.sales_wechat_id, title="销售微信"),
        LocalizedStaticValuesFilter(
            TaskAllocationBatch.period_type,
            title="周期类型",
            values=_TASK_PERIOD_VALUES,
        ),
        LocalizedStaticValuesFilter(
            TaskAllocationBatch.status,
            title="状态",
            values=_TASK_BATCH_STATUS_VALUES,
        ),
    ]

    def list_query(self, request):
        from sqlalchemy.orm import selectinload

        return super().list_query(request).options(
            selectinload(TaskAllocationBatch.sales_wechat_account),
        )

    def form_edit_query(self, request):
        from sqlalchemy.orm import selectinload

        stmt = super().form_edit_query(request)
        return stmt.options(selectinload(TaskAllocationBatch.sales_wechat_account))

    def details_query(self, request):
        from sqlalchemy.orm import selectinload

        stmt = super().details_query(request)
        return stmt.options(selectinload(TaskAllocationBatch.contact_tasks))

    can_create = False
    can_delete = True
    page_size = 50
    show_compact_lists = False

class ContactTaskAdmin(AdminModelView, model=ContactTask):
    name = "联系任务"
    name_plural = "联系任务"
    category = ADMIN_CAT_TASKS
    column_list = [
        ContactTask.id,
        ContactTask.batch_id,
        ContactTask.sales_wechat_id,
        ContactTask.raw_customer_id,
        ContactTask.task_kind,
        ContactTask.contact_channel,
        ContactTask.period_type,
        ContactTask.due_date,
        ContactTask.priority_rank,
        ContactTask.priority_score,
        ContactTask.title,
        ContactTask.instruction,
        ContactTask.status,
        ContactTask.completed_at,
    ]
    column_labels = {
        ContactTask.id: "任务 ID",
        ContactTask.batch_id: "批次 ID",
        ContactTask.sales_wechat_id: "销售微信",
        ContactTask.raw_customer_id: "客户 ID",
        ContactTask.task_kind: "类型",
        ContactTask.contact_channel: "渠道",
        ContactTask.period_type: "周期类型",
        ContactTask.due_date: "截止日期",
        ContactTask.priority_rank: "优先级序号",
        ContactTask.priority_score: "优先级分数",
        ContactTask.title: "任务标题",
        ContactTask.instruction: "执行说明",
        ContactTask.status: "状态",
        ContactTask.completed_at: "完成时间",
    }
    column_formatters = {
        ContactTask.sales_wechat_id: _fmt_sales_wechat_column,
        ContactTask.instruction: lambda m, a: (
            ((m.instruction or "")[:80] + "…")
            if (m.instruction and len(m.instruction) > 80)
            else (m.instruction or "")
        ),
    }
    column_sortable_list = [
        ContactTask.id,
        ContactTask.batch_id,
        ContactTask.due_date,
        ContactTask.priority_rank,
        ContactTask.priority_score,
        ContactTask.completed_at,
    ]
    column_default_sort = [(ContactTask.priority_rank, False)]
    column_filters = [
        SalesWechatIdFilter(ContactTask.sales_wechat_id, title="销售微信"),
        LocalizedStaticValuesFilter(
            ContactTask.task_kind,
            title="类型",
            values=[
                ("contact", "联系"),
                ("follow_up", "跟进"),
                ("close_deal", "促单"),
                ("revisit", "回访"),
                ("icebreaker", "破冰"),
            ],
        ),
        LocalizedStaticValuesFilter(
            ContactTask.period_type,
            title="周期类型",
            values=_TASK_PERIOD_VALUES,
        ),
        LocalizedStaticValuesFilter(
            ContactTask.status,
            title="状态",
            values=[
                ("pending", "待办"),
                ("in_progress", "进行中"),
                ("done", "完成"),
                ("skipped", "跳过"),
                ("overdue", "逾期"),
            ],
        ),
    ]

    def list_query(self, request):
        from sqlalchemy.orm import selectinload

        return super().list_query(request).options(
            selectinload(ContactTask.sales_wechat_account),
        )
    column_searchable_list = [
        ContactTask.sales_wechat_id,
        ContactTask.raw_customer_id,
        ContactTask.title,
    ]
    form_columns = [
        ContactTask.status,
        ContactTask.due_date,
        ContactTask.title,
        ContactTask.instruction,
        ContactTask.completion_note,
    ]
    can_create = False
    page_size = 50
