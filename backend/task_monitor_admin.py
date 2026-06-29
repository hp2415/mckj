"""管理后台：任务监测（数据看板分组）。"""
from __future__ import annotations

from datetime import date

from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import JSONResponse

from ai.task_allocation import PERIOD_DAILY, PERIOD_MONTHLY, PERIOD_WEEKLY, today_shanghai
from ai.task_monitor import _BATCH_STATUS_QUERY_VALUES, query_task_monitor
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


class TaskMonitorView(BaseView):
    name = "任务监测"
    category = ADMIN_CAT_DASHBOARD

    @expose("/task-monitor", methods=["GET"])
    async def task_monitor_page(self, request: Request):
        if (request.query_params.get("format") or "").strip().lower() == "json":
            period = (request.query_params.get("period") or PERIOD_DAILY).strip()
            if period not in (PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY):
                period = PERIOD_DAILY
            ref_s = (request.query_params.get("date") or "").strip()
            ref = _parse_ref_date(ref_s) if ref_s else today_shanghai()
            batch_status = (request.query_params.get("batch_status") or "active").strip().lower()
            if batch_status not in _BATCH_STATUS_QUERY_VALUES:
                batch_status = "active"
            async with AsyncSessionLocal() as db:
                data = await query_task_monitor(
                    db,
                    period=period,
                    ref_date=ref,
                    batch_status=batch_status,
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
