from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from markupsafe import Markup
from sqladmin import BaseView, expose
from sqlalchemy import case, func, select
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from database import AsyncSessionLocal
from models import (
    ChatMessage,
    RawChatLog,
    RawCustomer,
    RawCustomerSalesWechat,
    RawOrder,
    SalesCustomerProfile,
    SalesWechatAccount,
    User,
    WechatOutboundAction,
)


def _parse_days(raw: Optional[str], *, default: int = 7) -> int:
    s = (raw or "").strip()
    if not s:
        return default
    if not s.isdigit():
        return default
    v = int(s)
    # 防止误传极大范围拖垮 DB
    return max(1, min(365, v))


def _now_utc() -> datetime:
    # created_at 多为 naive datetime，这里统一用 utcnow 做相对窗口
    return datetime.utcnow()


def _safe_div(n: float, d: float) -> float:
    return (n / d) if d else 0.0


@dataclass
class _Kpi:
    key: str
    title: str
    value: str
    hint: str = ""


class DataDashboardView(BaseView):
    name = "数据看板"
    category = "数据看板"

    @expose("/dashboard", methods=["GET"])
    async def dashboard_page(self, request: Request):
        # 统一入口：侧栏无论指到哪个 expose，都能正确渲染页面
        # - /admin/dashboard               -> HTML 看板
        # - /admin/dashboard?format=json   -> JSON 数据（供前端轮询）
        if (request.query_params.get("format") or "").strip().lower() == "json":
            return await self._dashboard_json(request)

        # UI：纯 HTML + Chart.js CDN（与现有 BaseView 进度页保持一致）
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>数据看板</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; max-width: 78rem; margin: 1.5rem auto; padding: 0 1rem; }
    h1 { font-size: 1.35rem; margin: 0 0 1rem 0; }
    h2 { font-size: 1.05rem; margin: 1.35rem 0 .75rem; }
    .toolbar { display: flex; gap: .75rem; align-items: center; flex-wrap: wrap; margin-bottom: 1rem; }
    .toolbar .muted { color: #64748b; font-size: .875rem; }
    select, button { padding: .45rem .65rem; border-radius: .4rem; border: 1px solid #e2e8f0; background: #fff; }
    button { cursor: pointer; }
    button.primary { border-color: #1d4ed8; background: #1d4ed8; color: #fff; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: .75rem; }
    .card { border: 1px solid #e2e8f0; border-radius: .6rem; padding: .85rem; background: #fff; }
    .kpis { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(5, 1fr); gap: .75rem; }
    .kpi .title { color: #64748b; font-size: .8rem; }
    .kpi .value { font-size: 1.25rem; font-weight: 700; margin-top: .15rem; }
    .kpi .hint { color: #94a3b8; font-size: .75rem; margin-top: .25rem; }
    .chart-box { height: 320px; position: relative; }
    .chart-box canvas { display: block; width: 100% !important; height: 100% !important; }
    .col-6 { grid-column: span 6; }
    .col-4 { grid-column: span 4; }
    .col-8 { grid-column: span 8; }
    .col-12 { grid-column: span 12; }
    table { width: 100%; border-collapse: collapse; font-size: .875rem; }
    th, td { border: 1px solid #e2e8f0; padding: .35rem .5rem; text-align: left; }
    th { background: #f8fafc; }
    .right { text-align: right; }
    .pill { display:inline-block; padding:.1rem .45rem; border-radius:999px; font-size:.75rem; background:#f1f5f9; color:#0f172a; }
    @media (max-width: 1100px) { .kpis { grid-template-columns: repeat(3, 1fr); } }
    @media (max-width: 720px) { .kpis { grid-template-columns: repeat(2, 1fr); } .col-6,.col-4,.col-8{ grid-column: span 12; } }
  </style>
</head>
<body>
  <h1>数据看板</h1>
  <div class="toolbar">
    <label>时间范围：
      <select id="days">
        <option value="1">今天</option>
        <option value="7" selected>最近 7 天</option>
        <option value="30">最近 30 天</option>
      </select>
    </label>
    <button class="primary" id="btn-refresh" type="button">刷新</button>
    <span class="muted">自动刷新：每 60 秒</span>
    <span class="muted" id="last-updated"></span>
  </div>

  <div class="grid">
    <div class="kpis" id="kpis"></div>

    <div class="card col-6">
      <h2>AI 对话趋势</h2>
      <div class="chart-box"><canvas id="chatTrend"></canvas></div>
    </div>
    <div class="card col-6">
      <h2>微信外发趋势</h2>
      <div class="chart-box"><canvas id="outTrend"></canvas></div>
    </div>

    <div class="card col-4">
      <h2>外发方式占比</h2>
      <div class="chart-box"><canvas id="outboundTypePie"></canvas></div>
    </div>
    <div class="card col-4">
      <h2>AI 反馈概览</h2>
      <div class="chart-box"><canvas id="ratingPie"></canvas></div>
    </div>
    <div class="card col-8">
      <h2>按模型统计（最近窗口）</h2>
      <div class="chart-box"><canvas id="modelBar"></canvas></div>
    </div>

    <div class="card col-12">
      <h2>员工表现（最近窗口 Top 20）</h2>
      <div class="muted" style="margin-bottom:.5rem">按对话量排序，展示好评率/采纳率（仅统计 assistant 回复）</div>
      <div style="overflow:auto">
        <table>
          <thead>
            <tr>
              <th>员工</th>
              <th class="right">消息数</th>
              <th class="right">AI 回复</th>
              <th class="right">👍</th>
              <th class="right">👎</th>
              <th class="right">好评率</th>
              <th class="right">采纳</th>
              <th class="right">采纳率</th>
              <th class="right">外发</th>
            </tr>
          </thead>
          <tbody id="staffRows">
            <tr><td colspan="9" class="muted">加载中…</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    let charts = {};
    function pct(v){ return (v*100).toFixed(1) + "%"; }
    function fmtInt(n){ try { return new Intl.NumberFormat().format(n); } catch(e){ return String(n); } }
    function fmtMoney(n){ try { return "¥" + new Intl.NumberFormat(undefined,{maximumFractionDigits:2}).format(n); } catch(e){ return "¥" + String(n); } }
    function setLastUpdated(){
      const el = document.getElementById("last-updated");
      const d = new Date();
      el.textContent = "最近更新：" + d.toLocaleString();
    }
    async function load(){
      const days = document.getElementById("days").value || "7";
      const u = new URL(window.location.href);
      u.searchParams.set("format", "json");
      u.searchParams.set("days", days);
      const r = await fetch(u.toString(), { credentials: "same-origin" });
      const data = await r.json();
      render(data);
      setLastUpdated();
    }
    function renderKpis(items){
      const wrap = document.getElementById("kpis");
      wrap.innerHTML = "";
      for (const k of (items || [])){
        const div = document.createElement("div");
        div.className = "card kpi";
        div.innerHTML = '<div class="title">' + (k.title||k.key) + '</div>' +
                        '<div class="value">' + (k.value||"—") + '</div>' +
                        (k.hint ? '<div class="hint">' + k.hint + '</div>' : "");
        wrap.appendChild(div);
      }
    }
    function upsertLineChart(id, labels, datasets){
      if (charts[id]) charts[id].destroy();
      const ctx = document.getElementById(id);
      charts[id] = new Chart(ctx, {
        type: "line",
        data: { labels, datasets },
        options: { responsive: true, maintainAspectRatio: false }
      });
    }
    function upsertPieChart(id, labels, data){
      if (charts[id]) charts[id].destroy();
      const ctx = document.getElementById(id);
      charts[id] = new Chart(ctx, {
        type: "doughnut",
        data: { labels, datasets: [{ data }] },
        options: { responsive: true, maintainAspectRatio: false }
      });
    }
    function upsertBarChart(id, labels, datasets){
      if (charts[id]) charts[id].destroy();
      const ctx = document.getElementById(id);
      charts[id] = new Chart(ctx, {
        type: "bar",
        data: { labels, datasets },
        options: { responsive: true, maintainAspectRatio: false }
      });
    }
    function renderStaff(rows){
      const body = document.getElementById("staffRows");
      const items = rows || [];
      if (!items.length){
        body.innerHTML = '<tr><td colspan="9" class="muted">无数据</td></tr>';
        return;
      }
      body.innerHTML = items.map(r => {
        const goodRate = r.good_rate != null ? pct(r.good_rate) : "—";
        const adoptRate = r.adopt_rate != null ? pct(r.adopt_rate) : "—";
        return "<tr>" +
          "<td>" + (r.name || r.username || ("user#" + r.user_id)) + "</td>" +
          "<td class='right'>" + fmtInt(r.total_msgs||0) + "</td>" +
          "<td class='right'>" + fmtInt(r.ai_replies||0) + "</td>" +
          "<td class='right'>" + fmtInt(r.good||0) + "</td>" +
          "<td class='right'>" + fmtInt(r.bad||0) + "</td>" +
          "<td class='right'><span class='pill'>" + goodRate + "</span></td>" +
          "<td class='right'>" + fmtInt(r.adopted||0) + "</td>" +
          "<td class='right'><span class='pill'>" + adoptRate + "</span></td>" +
          "<td class='right'>" + fmtInt(r.outbound||0) + "</td>" +
        "</tr>";
      }).join("");
    }
    function render(d){
      renderKpis(d.kpis);
      const ct = d.chat_trend || { labels: [], total: [], assistant: [] };
      upsertLineChart("chatTrend", ct.labels || [], [
        { label: "消息数", data: ct.total || [], borderWidth: 2, tension: .25 },
        { label: "AI 回复", data: ct.assistant || [], borderWidth: 2, tension: .25 },
      ]);
      const ot = d.outbound_trend || { labels: [], total: [], sent: [], failed: [], blocked: [] };
      upsertLineChart("outTrend", ot.labels || [], [
        { label: "总外发", data: ot.total || [], borderWidth: 2, tension: .25 },
        { label: "成功", data: ot.sent || [], borderWidth: 2, tension: .25 },
        { label: "失败", data: ot.failed || [], borderWidth: 2, tension: .25 },
        { label: "拦截", data: ot.blocked || [], borderWidth: 2, tension: .25 },
      ]);
      const oa = d.outbound_action || { direct_send: 0, edit_send: 0 };
      upsertPieChart("outboundTypePie", ["直发(send)", "编辑后发送(edit_send)"], [oa.direct_send||0, oa.edit_send||0]);
      const rp = d.rating || { good: 0, bad: 0, none: 0 };
      upsertPieChart("ratingPie", ["👍 好评", "👎 差评", "未评"], [rp.good||0, rp.bad||0, rp.none||0]);
      const mb = d.model_stats || { labels: [], good_rate: [], adopt_rate: [] };
      upsertBarChart("modelBar", mb.labels || [], [
        { label: "好评率", data: (mb.good_rate || []).map(x => (x*100).toFixed(1)), },
        { label: "采纳率", data: (mb.adopt_rate || []).map(x => (x*100).toFixed(1)), },
      ]);
      renderStaff(d.staff || []);
    }
    document.getElementById("btn-refresh").addEventListener("click", load);
    document.getElementById("days").addEventListener("change", load);
    load();
    setInterval(load, 60 * 1000);
  </script>
</body>
</html>"""
        return HTMLResponse(html)

    # 兼容旧路径：如果侧栏/书签指到了 /dashboard/data，强制跳回 HTML 页面
    @expose("/dashboard/data", methods=["GET"])
    async def dashboard_data_compat(self, request: Request):
        return RedirectResponse(url=str(request.url_for("admin:dashboard_page")))

    async def _dashboard_json(self, request: Request):
        days = _parse_days(request.query_params.get("days"), default=7)
        since = _now_utc() - timedelta(days=days)

        async with AsyncSessionLocal() as db:
            chat = await _aggregate_chat(db, since=since)
            outbound = await _aggregate_outbound(db, since=since)
            base = await _aggregate_base(db)
            staff = await _aggregate_staff(db, since=since)
            model_stats = await _aggregate_models(db, since=since)
            chat_trend = await _trend_chat_daily(db, since=since, days=days)
            outbound_trend = await _trend_outbound_daily(db, since=since, days=days)

        kpis = _compose_kpis(base=base, chat=chat, outbound=outbound, days=days)

        return JSONResponse(
            {
                "days": days,
                "kpis": [k.__dict__ for k in kpis],
                "rating": chat.get("rating", {}),
                "outbound_action": {
                    "direct_send": int(outbound.get("direct_send") or 0),
                    "edit_send": int(outbound.get("edit_send") or 0),
                    "typed_total": int(outbound.get("typed_total") or 0),
                    "edit_rate": float(outbound.get("edit_rate") or 0.0),
                },
                "chat_trend": chat_trend,
                "outbound_trend": outbound_trend,
                "staff": staff,
                "model_stats": model_stats,
            }
        )


def _compose_kpis(*, base: Dict[str, Any], chat: Dict[str, Any], outbound: Dict[str, Any], days: int) -> List[_Kpi]:
    users_total = int(base.get("users_total") or 0)
    customers_total = int(base.get("raw_customers_total") or 0)
    chat_total = int(chat.get("total_msgs") or 0)
    out_total = int(outbound.get("total") or 0)
    orders_total = int(base.get("orders_total") or 0)
    orders_amount = float(base.get("orders_amount") or 0.0)

    rating = chat.get("rating") or {}
    good = int(rating.get("good") or 0)
    bad = int(rating.get("bad") or 0)
    good_rate = _safe_div(float(good), float(good + bad))

    ai_replies = int(chat.get("ai_replies") or 0)
    adopted = int(chat.get("adopted") or 0)
    adopt_rate = _safe_div(float(adopted), float(ai_replies))

    sent = int(outbound.get("sent") or 0)
    failed = int(outbound.get("failed") or 0)
    blocked = int(outbound.get("blocked") or 0)
    edit_rate = float(outbound.get("edit_rate") or 0.0)

    hint = f"窗口：最近 {days} 天"
    return [
        _Kpi("users_total", "系统用户数", str(users_total), hint),
        _Kpi("raw_customers_total", "原始客户数", str(customers_total), hint="历史累计"),
        _Kpi("chat_total", "对话消息数", str(chat_total), hint),
        _Kpi("outbound_total", "外发次数", str(out_total), hint),
        _Kpi("orders_amount", "订单金额", f"{orders_amount:.2f}", hint="历史累计"),
        _Kpi("good_rate", "好评率", f"{good_rate*100:.1f}%", hint),
        _Kpi("adopt_rate", "采纳率", f"{adopt_rate*100:.1f}%", hint),
        _Kpi("outbound_edit_rate", "编辑外发占比", f"{edit_rate*100:.1f}%", hint="edit_send / (send+edit_send)"),
        _Kpi("outbound_breakdown", "外发(成/败/拦)", f"{sent}/{failed}/{blocked}", hint),
        _Kpi("orders_total", "订单数", str(orders_total), hint="历史累计"),
    ]


async def _aggregate_chat(db, *, since: datetime) -> Dict[str, Any]:
    # 总消息数 / AI 回复数 / 采纳 / rating
    stmt = select(
        func.count(ChatMessage.id),
        func.sum(case((ChatMessage.role == "assistant", 1), else_=0)),
        func.sum(case((ChatMessage.is_copied.is_(True), 1), else_=0)),
        func.sum(case((ChatMessage.rating == 1, 1), else_=0)),
        func.sum(case((ChatMessage.rating == -1, 1), else_=0)),
        func.sum(case((ChatMessage.rating == 0, 1), else_=0)),
    ).where(ChatMessage.created_at >= since)
    res = await db.execute(stmt)
    row = res.first() or (0, 0, 0, 0, 0, 0)
    total, ai_replies, adopted, good, bad, none = [int(x or 0) for x in row]
    return {
        "total_msgs": total,
        "ai_replies": ai_replies,
        "adopted": adopted,
        "rating": {"good": good, "bad": bad, "none": none},
    }


async def _aggregate_outbound(db, *, since: datetime) -> Dict[str, Any]:
    stmt = select(
        func.count(WechatOutboundAction.id),
        func.sum(case((WechatOutboundAction.status == "sent", 1), else_=0)),
        func.sum(case((WechatOutboundAction.status == "failed", 1), else_=0)),
        func.sum(case((WechatOutboundAction.status == "blocked", 1), else_=0)),
        func.sum(case((WechatOutboundAction.action_type == "send", 1), else_=0)),
        func.sum(case((WechatOutboundAction.action_type == "edit_send", 1), else_=0)),
    ).where(WechatOutboundAction.created_at >= since)
    res = await db.execute(stmt)
    row = res.first() or (0, 0, 0, 0, 0, 0)
    total, sent, failed, blocked, direct_send, edit_send = [int(x or 0) for x in row]
    typed_total = int(direct_send + edit_send)
    edit_rate = _safe_div(float(edit_send), float(typed_total))
    return {
        "total": total,
        "sent": sent,
        "failed": failed,
        "blocked": blocked,
        "direct_send": direct_send,
        "edit_send": edit_send,
        "typed_total": typed_total,
        "edit_rate": edit_rate,
    }


async def _aggregate_base(db) -> Dict[str, Any]:
    users_total = int((await db.execute(select(func.count(User.id)))).scalar() or 0)
    users_active = int(
        (await db.execute(select(func.count(User.id)).where(User.is_active.is_(True)))).scalar()
        or 0
    )
    raw_customers_total = int((await db.execute(select(func.count(RawCustomer.id)))).scalar() or 0)
    relations_total = int(
        (await db.execute(select(func.count(RawCustomerSalesWechat.id)))).scalar() or 0
    )
    scp_total = int(
        (await db.execute(select(func.count(SalesCustomerProfile.id)))).scalar() or 0
    )
    scp_profiled = int(
        (
            await db.execute(
                select(func.count(SalesCustomerProfile.id)).where(
                    SalesCustomerProfile.profile_status == 1
                )
            )
        ).scalar()
        or 0
    )
    sales_wechats_total = int(
        (await db.execute(select(func.count(SalesWechatAccount.sales_wechat_id)))).scalar() or 0
    )
    orders_total = int((await db.execute(select(func.count(RawOrder.id)))).scalar() or 0)
    orders_amount = float((await db.execute(select(func.coalesce(func.sum(RawOrder.pay_amount), 0)))).scalar() or 0)
    raw_chat_logs_total = int((await db.execute(select(func.count(RawChatLog.id)))).scalar() or 0)

    return {
        "users_total": users_total,
        "users_active": users_active,
        "raw_customers_total": raw_customers_total,
        "relations_total": relations_total,
        "scp_total": scp_total,
        "scp_profiled": scp_profiled,
        "scp_unprofiled": max(0, scp_total - scp_profiled),
        "sales_wechats_total": sales_wechats_total,
        "orders_total": orders_total,
        "orders_amount": orders_amount,
        "raw_chat_logs_total": raw_chat_logs_total,
    }


async def _aggregate_staff(db, *, since: datetime) -> List[Dict[str, Any]]:
    # ChatMessage 按 user_id 分组
    chat_stmt = (
        select(
            ChatMessage.user_id.label("user_id"),
            func.count(ChatMessage.id).label("total_msgs"),
            func.sum(case((ChatMessage.role == "assistant", 1), else_=0)).label("ai_replies"),
            func.sum(case((ChatMessage.rating == 1, 1), else_=0)).label("good"),
            func.sum(case((ChatMessage.rating == -1, 1), else_=0)).label("bad"),
            func.sum(case((ChatMessage.is_copied.is_(True), 1), else_=0)).label("adopted"),
        )
        .where(ChatMessage.created_at >= since)
        .group_by(ChatMessage.user_id)
    )
    chat_res = await db.execute(chat_stmt)
    chat_rows = [dict(r._mapping) for r in chat_res.all()]

    user_ids = [int(r["user_id"]) for r in chat_rows if r.get("user_id") is not None]
    users_map: Dict[int, Dict[str, str]] = {}
    if user_ids:
        u_res = await db.execute(select(User.id, User.username, User.real_name).where(User.id.in_(user_ids)))
        for uid, username, real_name in u_res.all():
            users_map[int(uid)] = {"username": username or "", "name": real_name or username or ""}

    # Outbound 按 actor_user_id 分组
    out_stmt = (
        select(
            WechatOutboundAction.actor_user_id.label("user_id"),
            func.count(WechatOutboundAction.id).label("outbound"),
        )
        .where(WechatOutboundAction.created_at >= since)
        .group_by(WechatOutboundAction.actor_user_id)
    )
    out_res = await db.execute(out_stmt)
    out_map = {int(r[0]): int(r[1] or 0) for r in out_res.all() if r and r[0] is not None}

    out: List[Dict[str, Any]] = []
    for r in chat_rows:
        uid = r.get("user_id")
        if uid is None:
            continue
        uid = int(uid)
        good = int(r.get("good") or 0)
        bad = int(r.get("bad") or 0)
        ai = int(r.get("ai_replies") or 0)
        adopted = int(r.get("adopted") or 0)
        out.append(
            {
                "user_id": uid,
                "username": users_map.get(uid, {}).get("username", ""),
                "name": users_map.get(uid, {}).get("name", "") or f"user#{uid}",
                "total_msgs": int(r.get("total_msgs") or 0),
                "ai_replies": ai,
                "good": good,
                "bad": bad,
                "adopted": adopted,
                "good_rate": _safe_div(float(good), float(good + bad)),
                "adopt_rate": _safe_div(float(adopted), float(ai)),
                "outbound": int(out_map.get(uid, 0)),
            }
        )

    out.sort(key=lambda x: (x.get("total_msgs", 0), x.get("ai_replies", 0)), reverse=True)
    return out[:20]


async def _aggregate_models(db, *, since: datetime) -> Dict[str, Any]:
    stmt = (
        select(
            ChatMessage.chat_model.label("model"),
            func.count(ChatMessage.id).label("total"),
            func.sum(case((ChatMessage.role == "assistant", 1), else_=0)).label("assistant"),
            func.sum(case((ChatMessage.rating == 1, 1), else_=0)).label("good"),
            func.sum(case((ChatMessage.rating == -1, 1), else_=0)).label("bad"),
            func.sum(case((ChatMessage.is_copied.is_(True), 1), else_=0)).label("adopted"),
        )
        .where(ChatMessage.created_at >= since)
        .group_by(ChatMessage.chat_model)
    )
    res = await db.execute(stmt)
    rows = []
    for m, total, assistant, good, bad, adopted in res.all():
        model = (m or "").strip() or "unknown"
        assistant = int(assistant or 0)
        good = int(good or 0)
        bad = int(bad or 0)
        adopted = int(adopted or 0)
        rows.append(
            (
                model,
                int(total or 0),
                _safe_div(float(good), float(good + bad)),
                _safe_div(float(adopted), float(assistant)),
            )
        )
    rows.sort(key=lambda x: x[1], reverse=True)
    rows = rows[:12]
    return {
        "labels": [r[0] for r in rows],
        "total": [r[1] for r in rows],
        "good_rate": [r[2] for r in rows],
        "adopt_rate": [r[3] for r in rows],
    }


async def _trend_chat_daily(db, *, since: datetime, days: int) -> Dict[str, Any]:
    day_col = func.date(ChatMessage.created_at)
    stmt = (
        select(
            day_col.label("d"),
            func.count(ChatMessage.id).label("total"),
            func.sum(case((ChatMessage.role == "assistant", 1), else_=0)).label("assistant"),
        )
        .where(ChatMessage.created_at >= since)
        .group_by(day_col)
        .order_by(day_col.asc())
    )
    res = await db.execute(stmt)
    raw = {str(d): (int(total or 0), int(assistant or 0)) for d, total, assistant in res.all() if d}

    labels: List[str] = []
    total_arr: List[int] = []
    assistant_arr: List[int] = []
    # 生成完整日期序列，避免图表断点
    start = (_now_utc() - timedelta(days=days - 1)).date()
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        labels.append(day)
        t, a = raw.get(day, (0, 0))
        total_arr.append(t)
        assistant_arr.append(a)
    return {"labels": labels, "total": total_arr, "assistant": assistant_arr}


async def _trend_outbound_daily(db, *, since: datetime, days: int) -> Dict[str, Any]:
    day_col = func.date(WechatOutboundAction.created_at)
    stmt = (
        select(
            day_col.label("d"),
            func.count(WechatOutboundAction.id).label("total"),
            func.sum(case((WechatOutboundAction.status == "sent", 1), else_=0)).label("sent"),
            func.sum(case((WechatOutboundAction.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((WechatOutboundAction.status == "blocked", 1), else_=0)).label("blocked"),
        )
        .where(WechatOutboundAction.created_at >= since)
        .group_by(day_col)
        .order_by(day_col.asc())
    )
    res = await db.execute(stmt)
    raw = {
        str(d): (
            int(total or 0),
            int(sent or 0),
            int(failed or 0),
            int(blocked or 0),
        )
        for d, total, sent, failed, blocked in res.all()
        if d
    }

    labels: List[str] = []
    total_arr: List[int] = []
    sent_arr: List[int] = []
    failed_arr: List[int] = []
    blocked_arr: List[int] = []
    start = (_now_utc() - timedelta(days=days - 1)).date()
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        labels.append(day)
        t, s, f, b = raw.get(day, (0, 0, 0, 0))
        total_arr.append(t)
        sent_arr.append(s)
        failed_arr.append(f)
        blocked_arr.append(b)
    return {
        "labels": labels,
        "total": total_arr,
        "sent": sent_arr,
        "failed": failed_arr,
        "blocked": blocked_arr,
    }

