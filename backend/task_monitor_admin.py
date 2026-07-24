"""管理后台：任务监测（数据看板分组）。"""
from __future__ import annotations

from datetime import date

from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ai.task_allocation import PERIOD_DAILY, PERIOD_MONTHLY, PERIOD_WEEKLY, today_shanghai
from ai.task_monitor import (
    EXPORT_MAX_RANGE_DAYS,
    _BATCH_STATUS_QUERY_VALUES,
    _TASK_CATEGORY_QUERY_VALUES,
    build_task_monitor_csv,
    query_task_monitor,
    query_task_monitor_range,
)
from database import AsyncSessionLocal

ADMIN_CAT_DASHBOARD = "数据看板"


def _parse_ref_date(raw: str) -> date:
    ref = today_shanghai()
    raw = (raw or "").strip()
    if raw:
        try:
            ref = date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return ref


def _parse_iso_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


class TaskMonitorView(BaseView):
    name = "任务监测"
    category = ADMIN_CAT_DASHBOARD

    @expose("/task-monitor", methods=["GET"])
    async def task_monitor_page(self, request: Request):
        fmt = (request.query_params.get("format") or "").strip().lower()
        if fmt == "csv":
            return await self._export_csv(request)
        if fmt == "json":
            period = (request.query_params.get("period") or PERIOD_DAILY).strip()
            if period not in (PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY):
                period = PERIOD_DAILY
            ref_s = (request.query_params.get("date") or "").strip()
            ref = _parse_ref_date(ref_s) if ref_s else today_shanghai()
            batch_status = (request.query_params.get("batch_status") or "active").strip().lower()
            if batch_status not in _BATCH_STATUS_QUERY_VALUES:
                batch_status = "active"
            task_category = (request.query_params.get("task_category") or "all").strip().lower()
            if task_category not in _TASK_CATEGORY_QUERY_VALUES:
                task_category = "all"
            async with AsyncSessionLocal() as db:
                data = await query_task_monitor(
                    db,
                    period=period,
                    ref_date=ref,
                    batch_status=batch_status,
                    task_category=task_category,
                    ref_date_explicit=bool(ref_s),
                )
            return JSONResponse({"ok": True, **data})

        from core.admin_pages import render_admin_page

        return await render_admin_page(
            request,
            "admin/task_monitor.html",
            title="任务监测",
            subtitle="各销售微信号任务完成情况一览",
        )

    async def _export_csv(self, request: Request) -> Response:
        today = today_shanghai()
        date_from = _parse_iso_date(request.query_params.get("date_from") or "")
        date_to = _parse_iso_date(request.query_params.get("date_to") or "")
        if date_from is None and date_to is None:
            date_from = today.replace(day=1)
            date_to = today
        elif date_from is None:
            date_from = date_to
        elif date_to is None:
            date_to = date_from
        assert date_from is not None and date_to is not None
        if date_to < date_from:
            date_from, date_to = date_to, date_from
        if (date_to - date_from).days > EXPORT_MAX_RANGE_DAYS:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"导出区间不能超过 {EXPORT_MAX_RANGE_DAYS} 天",
                },
                status_code=400,
            )

        task_category = (request.query_params.get("task_category") or "all").strip().lower()
        if task_category not in _TASK_CATEGORY_QUERY_VALUES:
            task_category = "all"

        async with AsyncSessionLocal() as db:
            data = await query_task_monitor_range(
                db,
                date_from=date_from,
                date_to=date_to,
                task_category=task_category,
            )
        payload = build_task_monitor_csv(data)
        filename = f"task_monitor_{date_from.isoformat()}_{date_to.isoformat()}.csv"
        return Response(
            content=payload,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
