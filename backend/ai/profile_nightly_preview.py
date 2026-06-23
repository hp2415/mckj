"""夜间增量画像 - 预览页（BaseView，与 dashboard 同模式）。"""
from __future__ import annotations

import time
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
    get_cached_nightly_candidates,
)
from ai.profile_nightly_cache import get_or_compute, preview_cache_key


_BOUND_SALES_MAPS: tuple[dict[str, str], dict[str, str], float] | None = None
_BOUND_SALES_MAPS_TTL = 300.0


class NightlyProfilePreviewView(BaseView):
    name = "夜间增量画像预览"
    icon = "fa-solid fa-moon"
    category = "客户管理"

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
        nocache = str(request.query_params.get("nocache") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        today_start, _ = calendar_day_window_ms(datetime.now(SHANGHAI_TZ))
        is_today = params["since_ms"] == today_start
        cache_key = preview_cache_key(
            day_str=params["day"].strftime("%Y-%m-%d"),
            is_today=is_today,
            sw_filter=params["sw_filter"],
            respect_watermark=params["respect_watermark"],
        )
        try:
            if nocache:
                payload = await _build_preview_payload(params, use_candidates_cache=False)
                payload["cached"] = False
                return JSONResponse(payload)

            payload, from_cache = await get_or_compute(
                cache_key,
                is_today=is_today,
                compute=lambda: _build_preview_payload(params, use_candidates_cache=True),
            )
            payload["cached"] = from_cache
            return JSONResponse(payload)
        except Exception as exc:
            from core.logger import logger

            logger.exception("[Nightly Profile Preview] 加载失败")
            return JSONResponse(
                {"ok": False, "message": f"加载失败: {exc}"},
                status_code=500,
            )


async def warm_nightly_preview_cache() -> None:
    """后台预热「今日」预览缓存，使管理端打开页面直接命中缓存（秒开）。

    与 _json 的默认参数（今日全窗口、无销售号过滤、尊重水位）完全一致，
    会顺带填充候选两级缓存。由调度器周期性调用（计算成本移出 HTTP 请求）。
    """
    from core.logger import logger

    try:
        day = datetime.now(SHANGHAI_TZ)
        since_ms, until_ms = calendar_day_window_ms(day)
        now_ms = int(datetime.now(SHANGHAI_TZ).timestamp() * 1000)
        until_ms = min(until_ms, now_ms)
        params: dict[str, Any] = {
            "day": day,
            "since_ms": since_ms,
            "until_ms": until_ms,
            "sw_filter": [],
            "respect_watermark": True,
        }
        cache_key = preview_cache_key(
            day_str=day.strftime("%Y-%m-%d"),
            is_today=True,
            sw_filter=[],
            respect_watermark=True,
        )
        _, from_cache = await get_or_compute(
            cache_key,
            is_today=True,
            compute=lambda: _build_preview_payload(params, use_candidates_cache=True),
        )
        logger.info(
            "[Nightly Profile Preview] 预热完成 from_cache={} key={}",
            from_cache,
            cache_key,
        )
    except Exception:
        logger.exception("[Nightly Profile Preview] 预热失败")


async def _build_preview_payload(
    params: dict[str, Any],
    *,
    use_candidates_cache: bool = True,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    if use_candidates_cache:
        cands, _ = await get_cached_nightly_candidates(
            params["since_ms"],
            params["until_ms"],
            sales_wechat_ids=params["sw_filter"] or None,
            respect_watermark=params["respect_watermark"],
        )
    else:
        cands = await collect_nightly_candidates(
            params["since_ms"],
            params["until_ms"],
            sales_wechat_ids=params["sw_filter"] or None,
            respect_watermark=params["respect_watermark"],
        )
    sw_map, staff_map = await _load_all_bound_sales_maps()
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
    by_sales_list = sorted(by_sales.values(), key=lambda x: x["pair_count"], reverse=True)
    row_cands = cands[:500]
    rows = await _enrich_rows(row_cands, sw_map=sw_map, staff_map=staff_map)
    return {
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
        "query_ms": int((time.perf_counter() - t0) * 1000),
        "cached_at": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


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


async def _load_all_bound_sales_maps() -> tuple[dict[str, str], dict[str, str]]:
    """已绑定销售号的全量展示名（数量很少，可长期缓存）。"""
    global _BOUND_SALES_MAPS
    now = time.time()
    if _BOUND_SALES_MAPS and _BOUND_SALES_MAPS[2] > now:
        return _BOUND_SALES_MAPS[0], _BOUND_SALES_MAPS[1]

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(
                    UserSalesWechat.sales_wechat_id,
                    SalesWechatAccount.nickname,
                    SalesWechatAccount.alias_name,
                    User.real_name,
                )
                .outerjoin(
                    SalesWechatAccount,
                    SalesWechatAccount.sales_wechat_id == UserSalesWechat.sales_wechat_id,
                )
                .outerjoin(User, User.id == UserSalesWechat.user_id)
            )
        ).all()
    sw_map: dict[str, str] = {}
    staff_map: dict[str, str] = {}
    for swid, nick, alias, name in rows:
        if not swid:
            continue
        sw_map[str(swid)] = (nick or alias or swid).strip()
        if name and swid not in staff_map:
            staff_map[str(swid)] = name
    _BOUND_SALES_MAPS = (sw_map, staff_map, now + _BOUND_SALES_MAPS_TTL)
    return sw_map, staff_map


async def _enrich_rows(
    cands: list[NightlyCandidate],
    *,
    sw_map: dict[str, str] | None = None,
    staff_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    if not cands:
        return []
    raw_ids = {c.raw_customer_id for c in cands}
    if sw_map is None or staff_map is None:
        sw_map, staff_map = await _load_all_bound_sales_maps()
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
