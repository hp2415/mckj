"""夜间增量画像 - 预览页（BaseView，与 dashboard 同模式）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqladmin import BaseView, expose
from sqlalchemy.future import select
from starlette.requests import Request
from starlette.responses import JSONResponse

from database import AsyncSessionLocal
from models import RawCustomer, SalesWechatAccount, User, UserSalesWechat
from ai.profile_nightly import (
    SHANGHAI_TZ,
    NightlyCandidate,
    calendar_day_window_ms,
    collect_nightly_candidates,
    enqueue_candidates,
)


class NightlyProfilePreviewView(BaseView):
    name = "夜间增量画像预览"
    icon = "fa-solid fa-moon"
    category = "客户管理"

    # 单一 expose：GET 页面 + GET ?format=json 轮询 + POST ?action=enqueue 入队。
    # 与 ProfilingProgressView 同形——sqladmin 侧栏 url_for 才能稳定命中页面入口。
    @expose("/profile-nightly", methods=["GET", "POST"])
    async def nightly_profile_preview_page(self, request: Request):
        if request.method == "POST":
            action = (request.query_params.get("action") or "").strip()
            if action == "enqueue":
                return await self._enqueue(request)
            return JSONResponse(
                {"ok": False, "message": f"未知动作: {action or '(空)'}"}, status_code=400
            )

        if (request.query_params.get("format") or "").strip().lower() == "json":
            return await self._json(request)
        from core.admin_pages import render_admin_page

        return await render_admin_page(
            request,
            "admin/profile_nightly.html",
            title="夜间增量画像 · 预览",
            subtitle="默认 = 今日 00:00 至当前；选历史日期则为该日全天",
        )

    async def _enqueue(self, request: Request) -> JSONResponse:
        params = await _read_params(request)
        cands = await collect_nightly_candidates(
            params["since_ms"],
            params["until_ms"],
            sales_wechat_ids=params["sw_filter"] or None,
            respect_watermark=params["respect_watermark"],
        )
        if not cands:
            return JSONResponse({"enqueued": 0, "message": "无候选可入队"})
        label = (
            f"夜间增量画像 {params['day'].strftime('%Y-%m-%d')}"
            + (" · 强制重跑" if not params["respect_watermark"] else "")
            + (f" · 仅{','.join(params['sw_filter'])}" if params["sw_filter"] else "")
            + f"（手动触发，共{len(cands)}对）"
        )
        n = await enqueue_candidates(cands, label=label)
        return JSONResponse({"enqueued": n, "label": label})

    async def _json(self, request: Request) -> JSONResponse:
        params = await _read_params(request)
        try:
            cands = await collect_nightly_candidates(
                params["since_ms"],
                params["until_ms"],
                sales_wechat_ids=params["sw_filter"] or None,
                respect_watermark=params["respect_watermark"],
            )
            sw_ids = {c.sales_wechat_id for c in cands}
            sw_map, staff_map = await _load_sales_maps(sw_ids)
            by_sales: dict[str, dict[str, Any]] = {}
            for c in cands:
                sw = c.sales_wechat_id
                agg = by_sales.setdefault(
                    sw,
                    {
                        "sales_wechat_id": sw,
                        "sales_label": sw_map.get(sw, sw),
                        "staff_name": staff_map.get(sw, ""),
                        "pair_count": 0,
                        "total_chats": 0,
                    },
                )
                agg["pair_count"] += 1
                agg["total_chats"] += c.chat_count
            by_sales_list = sorted(
                by_sales.values(), key=lambda x: x["pair_count"], reverse=True
            )
            row_cands = cands[:500]
            rows = await _enrich_rows(row_cands, sw_map=sw_map, staff_map=staff_map)
            return JSONResponse(
                {
                    "window": {
                        "day": params["day"].strftime("%Y-%m-%d"),
                        "since_ms": params["since_ms"],
                        "until_ms": params["until_ms"],
                        "respect_watermark": params["respect_watermark"],
                        "sw_filter": params["sw_filter"],
                    },
                    "summary": {
                        "total_pairs": len(cands),
                        "total_chats": sum(c.chat_count for c in cands),
                        "by_sales": by_sales_list,
                    },
                    "rows": rows,
                    "rows_truncated": len(cands) > 500,
                }
            )
        except Exception as exc:
            from core.logger import logger

            logger.exception("[Nightly Profile Preview] 加载失败")
            return JSONResponse(
                {"ok": False, "message": f"加载失败: {exc}"},
                status_code=500,
            )


async def _read_params(request: Request) -> dict[str, Any]:
    q = request.query_params
    if request.method == "POST":
        form = await request.form()
        q = {**dict(q), **dict(form)}
    day_str = (q.get("day") or "").strip()
    if day_str:
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=SHANGHAI_TZ)
        except ValueError:
            day = datetime.now(SHANGHAI_TZ)
    else:
        day = datetime.now(SHANGHAI_TZ)
    since_ms, until_ms = calendar_day_window_ms(day)
    today_start, _ = calendar_day_window_ms(datetime.now(SHANGHAI_TZ))
    if since_ms == today_start:
        now_ms = int(datetime.now(SHANGHAI_TZ).timestamp() * 1000)
        until_ms = min(until_ms, now_ms)
    sw_filter_raw = (q.get("sales_wechat_id") or "").strip()
    sw_filter = [s for s in (sw_filter_raw.split(",") if sw_filter_raw else []) if s]
    respect_wm = str(q.get("force") or "").strip().lower() not in ("1", "true", "yes", "on")
    return {
        "day": day,
        "since_ms": since_ms,
        "until_ms": until_ms,
        "sw_filter": sw_filter,
        "respect_watermark": respect_wm,
    }


async def _load_sales_maps(
    sw_ids: set[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """销售号展示名与绑定业务员（预览聚合用）。"""
    if not sw_ids:
        return {}, {}
    async with AsyncSessionLocal() as db:
        sw_rows = (
            await db.execute(
                select(
                    SalesWechatAccount.sales_wechat_id,
                    SalesWechatAccount.nickname,
                    SalesWechatAccount.alias_name,
                ).where(SalesWechatAccount.sales_wechat_id.in_(sw_ids))
            )
        ).all()
        sw_map = {
            swid: (nick or alias or swid).strip() for swid, nick, alias in sw_rows
        }
        usw_rows = (
            await db.execute(
                select(UserSalesWechat.sales_wechat_id, User.real_name)
                .join(User, User.id == UserSalesWechat.user_id)
                .where(UserSalesWechat.sales_wechat_id.in_(sw_ids))
            )
        ).all()
        staff_map: dict[str, str] = {}
        for swid, name in usw_rows:
            if swid and name and swid not in staff_map:
                staff_map[swid] = name
    return sw_map, staff_map


async def _enrich_rows(
    cands: list[NightlyCandidate],
    *,
    sw_map: dict[str, str] | None = None,
    staff_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """补展示字段：customer_name / sales_label / staff_name / latest_chat_str。"""
    if not cands:
        return []
    raw_ids = {c.raw_customer_id for c in cands}
    sw_ids = {c.sales_wechat_id for c in cands}
    if sw_map is None or staff_map is None:
        sw_map, staff_map = await _load_sales_maps(sw_ids)
    async with AsyncSessionLocal() as db:
        rc_rows = (
            await db.execute(
                select(
                    RawCustomer.id,
                    RawCustomer.customer_name,
                    RawCustomer.remark,
                    RawCustomer.name,
                ).where(RawCustomer.id.in_(raw_ids))
            )
        ).all()
        rc_map = {
            rid: (cname or remark or nname or "").strip() or rid
            for rid, cname, remark, nname in rc_rows
        }
    out = []
    for c in cands:
        latest_dt = (
            datetime.fromtimestamp(c.latest_chat_ms / 1000) if c.latest_chat_ms else None
        )
        out.append(
            {
                "raw_customer_id": c.raw_customer_id,
                "sales_wechat_id": c.sales_wechat_id,
                "customer_name": rc_map.get(c.raw_customer_id, c.raw_customer_id),
                "sales_label": sw_map.get(c.sales_wechat_id, c.sales_wechat_id),
                "staff_name": staff_map.get(c.sales_wechat_id, ""),
                "latest_chat_at": latest_dt.strftime("%Y-%m-%d %H:%M") if latest_dt else "",
                "latest_chat_ms": c.latest_chat_ms,
                "chat_count": c.chat_count,
                "profiled_at": c.profiled_at.strftime("%Y-%m-%d %H:%M") if c.profiled_at else "",
            }
        )
    return out
