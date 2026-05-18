"""夜间增量画像 - 预览页（BaseView，与 dashboard 同模式）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqladmin import BaseView, expose
from sqlalchemy.future import select
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

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
        return HTMLResponse(_HTML)

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
        cands = await collect_nightly_candidates(
            params["since_ms"],
            params["until_ms"],
            sales_wechat_ids=params["sw_filter"] or None,
            respect_watermark=params["respect_watermark"],
        )
        enriched = await _enrich(cands)
        # 按销售号聚合
        by_sales: dict[str, dict[str, Any]] = {}
        for row in enriched:
            sw = row["sales_wechat_id"]
            agg = by_sales.setdefault(
                sw,
                {
                    "sales_wechat_id": sw,
                    "sales_label": row.get("sales_label") or sw,
                    "staff_name": row.get("staff_name") or "",
                    "pair_count": 0,
                    "total_chats": 0,
                },
            )
            agg["pair_count"] += 1
            agg["total_chats"] += row["chat_count"]
        by_sales_list = sorted(by_sales.values(), key=lambda x: x["pair_count"], reverse=True)
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
                    "total_pairs": len(enriched),
                    "total_chats": sum(r["chat_count"] for r in enriched),
                    "by_sales": by_sales_list,
                },
                "rows": enriched[:500],
                "rows_truncated": len(enriched) > 500,
            }
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


async def _enrich(cands: list[NightlyCandidate]) -> list[dict[str, Any]]:
    """补展示字段：customer_name / sales_label / staff_name / latest_chat_str。"""
    if not cands:
        return []
    raw_ids = {c.raw_customer_id for c in cands}
    sw_ids = {c.sales_wechat_id for c in cands}
    async with AsyncSessionLocal() as db:
        rc_rows = (
            await db.execute(
                select(RawCustomer.id, RawCustomer.customer_name, RawCustomer.remark, RawCustomer.name).where(
                    RawCustomer.id.in_(raw_ids)
                )
            )
        ).all()
        rc_map = {
            rid: (cname or remark or nname or "").strip() or rid
            for rid, cname, remark, nname in rc_rows
        }
        sw_rows = (
            await db.execute(
                select(SalesWechatAccount.sales_wechat_id, SalesWechatAccount.nickname, SalesWechatAccount.alias_name).where(
                    SalesWechatAccount.sales_wechat_id.in_(sw_ids)
                )
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


_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>夜间增量画像预览</title>
  <style>
    body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
           max-width: 90rem; margin: 1.5rem auto; padding: 0 1rem; color: #1f2937; }
    h1 { font-size: 1.35rem; margin: 0 0 1rem 0; }
    .toolbar { display: flex; gap: .75rem; align-items: center; flex-wrap: wrap; margin-bottom: 1rem; }
    .toolbar input, .toolbar select { padding: .35rem .55rem; border: 1px solid #cbd5e1; border-radius: .35rem; }
    .toolbar button { padding: .4rem .75rem; border: 1px solid #1d4ed8; background: #2563eb; color: #fff;
                      border-radius: .35rem; cursor: pointer; }
    .toolbar button.secondary { background: #fff; color: #1d4ed8; }
    .toolbar .hint { color: #64748b; font-size: .85rem; }
    .kpis { display: grid; grid-template-columns: repeat(auto-fill, minmax(13rem, 1fr)); gap: .75rem;
            margin-bottom: 1rem; }
    .kpi { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: .5rem; padding: .75rem 1rem; }
    .kpi .v { font-size: 1.55rem; font-weight: 600; }
    .kpi .k { color: #475569; font-size: .85rem; }
    table { width: 100%; border-collapse: collapse; font-size: .88rem; }
    th, td { border-bottom: 1px solid #e2e8f0; padding: .45rem .55rem; text-align: left; }
    th { background: #f1f5f9; }
    tbody tr:hover { background: #f8fafc; }
    .pill { display: inline-block; padding: .1rem .5rem; background: #e0e7ff; color: #1e40af;
            border-radius: 99px; font-size: .75rem; }
    .muted { color: #64748b; }
    .danger { color: #b91c1c; }
  </style>
</head>
<body>
  <h1>夜间增量画像 · 预览</h1>
  <div class="toolbar">
    <label>日期 <input type="date" id="day"/></label>
    <label>销售号 <input id="sw" placeholder="wxid_xxx，多选用英文逗号分隔"/></label>
    <label><input type="checkbox" id="force"/> 忽略水位（强制重跑）</label>
    <button type="button" id="btn-refresh">刷新预览</button>
    <button type="button" class="secondary" id="btn-enqueue">全部入队 → profile_jobs</button>
    <span class="hint" id="hint">默认 = 今日 00:00 至当前（与看板「今晚待画像」一致）；选历史日期则为该日全天</span>
  </div>
  <div class="kpis" id="kpis"></div>
  <h2 style="font-size:1rem;">按销售号分布</h2>
  <table id="bySalesTable">
    <thead><tr><th>销售号</th><th>业务员</th><th>对数</th><th>聊天条数（窗口内）</th></tr></thead>
    <tbody></tbody>
  </table>
  <h2 style="font-size:1rem; margin-top:1rem;">候选明细（最多 500 条）</h2>
  <table id="rowsTable">
    <thead><tr>
      <th>客户</th><th>销售号</th><th>业务员</th>
      <th>窗口内最近聊天</th><th>窗口内条数</th><th>上次画像</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <script>
    function qs(obj) {
      const u = new URLSearchParams();
      for (const k in obj) if (obj[k] !== "" && obj[k] != null) u.set(k, obj[k]);
      return u.toString();
    }
    function readForm() {
      return {
        day: document.getElementById('day').value || '',
        sales_wechat_id: document.getElementById('sw').value.trim(),
        force: document.getElementById('force').checked ? '1' : '',
      };
    }
    async function refresh() {
      const btn = document.getElementById('btn-refresh');
      btn.disabled = true;
      try {
        const params = readForm();
        params.format = 'json';
        const r = await fetch('/admin/profile-nightly?' + qs(params));
        const data = await r.json();
        renderKpis(data); renderBySales(data); renderRows(data);
      } catch (e) {
        console.error(e);
        alert('刷新失败');
      } finally {
        btn.disabled = false;
      }
    }
    function renderKpis(d) {
      const el = document.getElementById('kpis');
      el.innerHTML = '';
      const items = [
        { k: '候选客户对', v: d.summary.total_pairs },
        { k: '窗口内聊天总条数', v: d.summary.total_chats },
        { k: '涉及销售号', v: d.summary.by_sales.length },
        { k: '窗口', v: d.window.day + (d.window.respect_watermark ? '' : ' · 强制') },
      ];
      for (const it of items) {
        const div = document.createElement('div');
        div.className = 'kpi';
        div.innerHTML = `<div class="v">${it.v}</div><div class="k">${it.k}</div>`;
        el.appendChild(div);
      }
    }
    function renderBySales(d) {
      const tb = document.querySelector('#bySalesTable tbody');
      tb.innerHTML = '';
      for (const r of d.summary.by_sales) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td><code>${r.sales_label}</code><div class="muted" style="font-size:.75rem">${r.sales_wechat_id}</div></td>
          <td>${r.staff_name || '<span class="muted">未绑定</span>'}</td>
          <td>${r.pair_count}</td>
          <td>${r.total_chats}</td>`;
        tb.appendChild(tr);
      }
    }
    function renderRows(d) {
      const tb = document.querySelector('#rowsTable tbody');
      tb.innerHTML = '';
      for (const r of d.rows) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${r.customer_name}<div class="muted" style="font-size:.75rem">${r.raw_customer_id}</div></td>
          <td><code>${r.sales_label}</code></td>
          <td>${r.staff_name || '<span class="muted">未绑定</span>'}</td>
          <td>${r.latest_chat_at}</td>
          <td>${r.chat_count}</td>
          <td>${r.profiled_at || ''}</td>`;
        tb.appendChild(tr);
      }
      if (d.rows_truncated) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td colspan="6" class="muted">… 候选超过 500 条，仅展示前 500 条，入队会处理全部 ${d.summary.total_pairs} 条。</td>`;
        tb.appendChild(tr);
      }
    }
    async function doEnqueue() {
      if (!confirm('确认按当前过滤条件，把候选全部入队 profile_jobs？')) return;
      const btn = document.getElementById('btn-enqueue');
      btn.disabled = true;
      try {
        const params = readForm();
        params.action = 'enqueue';
        const r = await fetch('/admin/profile-nightly?' + qs(params), { method: 'POST' });
        const data = await r.json();
        alert(`已入队 ${data.enqueued} 对\n${data.label || data.message || ''}`);
      } catch (e) {
        console.error(e);
        alert('入队失败，请查看控制台');
      } finally {
        btn.disabled = false;
      }
    }
    document.getElementById('btn-refresh').addEventListener('click', refresh);
    document.getElementById('btn-enqueue').addEventListener('click', doEnqueue);
    document.getElementById('day').value = new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' });
    refresh();
  </script>
</body>
</html>
"""
