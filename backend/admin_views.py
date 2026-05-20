from sqladmin import BaseView, ModelView, action, expose
from core.admin_sort import AdminModelView
from sqladmin.filters import StaticValuesFilter, get_column_obj, get_parameter_name
from sqlalchemy import or_, and_, select
from sqlalchemy.sql.expression import Select
from typing import Any, Callable, List, Tuple
from wtforms import (
    SelectField,
    StringField,
    TextAreaField,
    SelectMultipleField,
    BooleanField,
    IntegerField,
)
from wtforms.validators import InputRequired, Optional as WTFOptional, NumberRange
from ai.router_prompt import ROUTER_PROMPT_VARIABLE_CHOICES, ROUTER_PROMPT_VARIABLE_TITLES
from models import (
    User,
    UserSalesWechat,
    SalesWechatAccount,
    SalesCustomerProfile,
    ChatMessage,
    Product,
    SystemConfig,
    BusinessTransfer,
    SyncFailure,
    RawCustomerSalesWechat,
    RawCustomer,
    PromptScenario,
    PromptVersion,
    PromptDoc,
    PromptDocVersion,
    PromptAuditLog,
    ProfileTagDefinition,
    WechatOutboundAction,
)
from database import AsyncSessionLocal
import asyncio


class LocalizedStaticValuesFilter(StaticValuesFilter):
    """与 StaticValuesFilter 相同，首项为中文「全部」。"""

    async def lookups(
        self,
        request: Any,
        model: Any,
        run_query: Callable[[Select], Any],
    ) -> List[Tuple[str, str]]:
        return [("", "全部")] + self.values


class LocalizedBooleanFilter:
    """布尔列筛选，选项为中文。"""

    has_operator = False

    def __init__(
        self,
        column: Any,
        title: str,
        *,
        true_label: str = "是",
        false_label: str = "否",
        parameter_name: str | None = None,
    ):
        self.column = column
        self.title = title
        self.true_label = true_label
        self.false_label = false_label
        self.parameter_name = parameter_name or (
            f"{get_parameter_name(column)}_bool"
        )

    async def lookups(
        self,
        request: Any,
        model: Any,
        run_query: Callable[[Select], Any],
    ) -> List[Tuple[str, str]]:
        return [
            ("", "全部"),
            ("true", self.true_label),
            ("false", self.false_label),
        ]

    async def get_filtered_query(
        self, query: Select, value: Any, model: Any
    ) -> Select:
        col = get_column_obj(self.column, model)
        if value == "true":
            return query.filter(col.is_(True))
        if value == "false":
            return query.filter(col.is_(False))
        return query


class PhonePresenceFilter:
    """筛选有电话 / 无电话（NULL 或空串视为无电话）。"""

    has_operator = False

    def __init__(
        self,
        column: Any,
        title: str = "电话情况",
        parameter_name: str | None = None,
    ):
        self.column = column
        self.title = title
        self.parameter_name = parameter_name or (
            f"{get_parameter_name(column)}_presence"
        )

    async def lookups(
        self,
        request: Any,
        model: Any,
        run_query: Callable[[Select], Any],
    ) -> List[Tuple[str, str]]:
        return [
            ("", "全部"),
            ("has", "有电话"),
            ("empty", "无电话"),
        ]

    async def get_filtered_query(
        self, query: Select, value: Any, model: Any
    ) -> Select:
        col = get_column_obj(self.column, model)
        if value == "has":
            return query.filter(col.isnot(None), col != "")
        if value == "empty":
            return query.filter(or_(col.is_(None), col == ""))
        return query


class ScpProfileStatusFilter:
    """按 per-sales 画像状态筛选：基于 SalesCustomerProfile.profile_status（无 SCP 视为未分析）。"""

    has_operator = False

    def __init__(
        self,
        title: str = "画像状态",
        parameter_name: str = "scp_profile_status",
    ):
        self.title = title
        self.parameter_name = parameter_name

    async def lookups(
        self,
        request: Any,
        model: Any,
        run_query: Callable[[Select], Any],
    ) -> List[Tuple[str, str]]:
        return [("", "全部"), ("0", "未分析"), ("1", "已分析")]

    async def get_filtered_query(self, query: Select, value: Any, model: Any) -> Select:
        v = (value or "").strip()
        if v not in ("0", "1"):
            return query
        target = int(v)
        query = query.outerjoin(
            SalesCustomerProfile,
            and_(
                SalesCustomerProfile.raw_customer_id == RawCustomerSalesWechat.raw_customer_id,
                SalesCustomerProfile.sales_wechat_id == RawCustomerSalesWechat.sales_wechat_id,
            ),
        )
        # 无 SCP 的行应被视为“未分析”
        if target == 1:
            return query.filter(SalesCustomerProfile.profile_status == 1)
        return query.filter(or_(SalesCustomerProfile.id.is_(None), SalesCustomerProfile.profile_status == 0))


from crud import transfer_user_customers
from markupsafe import Markup
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from urllib.parse import parse_qs, unquote, urlparse
import os

PAGE_SIZE = 50

_PROMPT_STATUS_FILTER_VALUES = [
    ("draft", "draft（草稿）"),
    ("published", "published（已发布）"),
    ("archived", "archived（已归档）"),
]

_OUTBOUND_ACTION_TYPE_VALUES = [
    ("send", "发送"),
    ("edit_send", "编辑后发送"),
]

_OUTBOUND_STATUS_VALUES = [
    ("pending", "待处理"),
    ("sent", "已发送"),
    ("failed", "失败"),
    ("blocked", "已拦截"),
]

_TASK_PERIOD_VALUES = [
    ("daily", "日"),
    ("weekly", "周"),
    ("monthly", "月"),
]

_TASK_BATCH_STATUS_VALUES = [
    ("draft", "草稿"),
    ("published", "已发布"),
    ("archived", "已归档"),
]


def _sales_wechat_label(acc: SalesWechatAccount | None, sw_id: str | None = None) -> str:
    sw_id = (sw_id or "").strip()
    if not acc:
        return sw_id or "—"
    nick = (acc.nickname or "").strip()
    alias = (acc.alias_name or "").strip()
    main = nick or alias or sw_id
    if main and sw_id and main != sw_id:
        return f"{main}（{sw_id}）"
    return main or sw_id or "—"


def _fmt_sales_wechat_column(m: Any, _a: Any) -> str:
    sw = getattr(m, "sales_wechat_id", "") or ""
    acc = getattr(m, "sales_wechat_account", None)
    return Markup.escape(_sales_wechat_label(acc, sw))


class SalesWechatIdFilter:
    """按销售微信号筛选，下拉展示昵称。"""

    has_operator = False

    def __init__(self, column: Any, title: str = "销售微信"):
        self.column = column
        self.title = title
        self.parameter_name = get_parameter_name(column)

    async def lookups(
        self,
        request: Any,
        model: Any,
        run_query: Callable[[Select], Any],
    ) -> List[Tuple[str, str]]:
        # run_query 返回 Row，不能当 ORM 实体用属性访问
        rows = await run_query(
            select(
                SalesWechatAccount.sales_wechat_id,
                SalesWechatAccount.nickname,
                SalesWechatAccount.alias_name,
            ).order_by(
                SalesWechatAccount.nickname, SalesWechatAccount.sales_wechat_id
            )
        )
        out: List[Tuple[str, str]] = [("", "全部")]
        for row in rows:
            sw = (row[0] or "").strip()
            if not sw:
                continue
            nick = (row[1] or "").strip()
            alias = (row[2] or "").strip()
            main = nick or alias or sw
            label = f"{main}（{sw}）" if main != sw else main
            out.append((sw, label))
        return out

    async def get_filtered_query(
        self, query: Select, value: Any, model: Any
    ) -> Select:
        if value in ("", None):
            return query
        return query.filter(get_column_obj(self.column, model) == value)


class UserIdLabelFilter:
    """按用户 ID 筛选，下拉展示真实姓名。"""

    has_operator = False

    def __init__(self, column: Any, title: str = "用户"):
        self.column = column
        self.title = title
        self.parameter_name = get_parameter_name(column)

    async def lookups(
        self,
        request: Any,
        model: Any,
        run_query: Callable[[Select], Any],
    ) -> List[Tuple[str, str]]:
        rows = await run_query(
            select(User.id, User.real_name, User.username).order_by(User.real_name)
        )
        out: List[Tuple[str, str]] = [("", "全部")]
        for row in rows:
            uid, real_name, username = row[0], row[1], row[2]
            if uid is None:
                continue
            name = (real_name or username or "").strip() or str(uid)
            uname = (username or "").strip()
            label = f"{name}（{uname}）" if uname and name != uname else name
            out.append((str(uid), label))
        return out

    async def get_filtered_query(
        self, query: Select, value: Any, model: Any
    ) -> Select:
        if value in ("", None):
            return query
        return query.filter(get_column_obj(self.column, model) == int(value))


class ChatCustomerIdFilter:
    """对话列表：按客户筛选（展示客观库姓名）。"""

    has_operator = False

    def __init__(self, title: str = "客户对象"):
        self.title = title
        self.parameter_name = "chat_raw_customer_id"

    async def lookups(
        self,
        request: Any,
        model: Any,
        run_query: Callable[[Select], Any],
    ) -> List[Tuple[str, str]]:
        stmt = (
            select(RawCustomer.id, RawCustomer.customer_name)
            .join(ChatMessage, ChatMessage.raw_customer_id == RawCustomer.id)
            .distinct()
            .order_by(RawCustomer.customer_name)
            .limit(300)
        )
        rows = await run_query(stmt)
        out: List[Tuple[str, str]] = [("", "全部")]
        for cid, cname in rows:
            if not cid:
                continue
            label = (cname or cid).strip() or cid
            out.append((cid, label))
        return out

    async def get_filtered_query(
        self, query: Select, value: Any, model: Any
    ) -> Select:
        if value in ("", None):
            return query
        return query.filter(ChatMessage.raw_customer_id == value)


class DistinctColumnValuesFilter:
    """列 distinct 值筛选（中文「全部」）。"""

    has_operator = False

    def __init__(self, column: Any, title: str):
        self.column = column
        self.title = title
        self.parameter_name = get_parameter_name(column)

    async def lookups(
        self,
        request: Any,
        model: Any,
        run_query: Callable[[Select], Any],
    ) -> List[Tuple[str, str]]:
        col = get_column_obj(self.column, model)
        rows = await run_query(
            select(col)
            .where(col.isnot(None), col != "")
            .distinct()
            .order_by(col)
            .limit(200)
        )
        return [("", "全部")] + [
            (str(row[0]), str(row[0])) for row in rows if row[0] is not None
        ]

    async def get_filtered_query(
        self, query: Select, value: Any, model: Any
    ) -> Select:
        if value in ("", None):
            return query
        return query.filter(get_column_obj(self.column, model) == value)


ADMIN_CAT_USERS = "用户管理"
ADMIN_CAT_CUSTOMERS = "客户管理"
ADMIN_CAT_MARKETING = "营销策略管理"
ADMIN_CAT_PROMPTS = "提示词管理"
ADMIN_CAT_SYNC = "数据同步"
ADMIN_CAT_SYSTEM = "系统设置"
ADMIN_CAT_DASHBOARD = "数据看板"

# 数据看板开关：便于快速隐藏侧栏入口（支持环境变量覆盖）
# - ADMIN_DASHBOARD_ENABLED=0/false 可直接关闭
ENABLE_DASHBOARD = (os.getenv("ADMIN_DASHBOARD_ENABLED") or "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

try:
    from dashboard import DataDashboardView
except Exception:  # pragma: no cover
    DataDashboardView = None  # type: ignore


async def resolve_sales_wechat_id_for_rcsw_batch(request: Request) -> str:
    """
    原始客户池列表上的批量操作请求里，sqladmin 生成的 action URL 通常不带列表筛选参数，
    导致仅依赖 request.query_params['sales_wechat_id'] 会为空。依次尝试：
    query → Referer 中的 sales_wechat_id → Referer 中列表页 search=（与库中 sales_wechat_id 精确匹配）
    → 选中行 pks 对应的唯一 sales_wechat_id。

    说明：用户在列表上用搜索框按「销售企微ID」筛选时，URL 多为 ?search=wxid_xxx 而非
    ?sales_wechat_id=...；此前无法解析 sw，导致「分析指定企微ID的全部客户」不入队。
    """
    sw = (request.query_params.get("sales_wechat_id") or "").strip()
    if sw:
        return sw
    ref = (request.headers.get("referer") or "").strip()
    if ref:
        try:
            parsed = urlparse(ref)
            path = (parsed.path or "").rstrip("/")
            qs = parse_qs(parsed.query)
            vals = qs.get("sales_wechat_id") or []
            if vals and (vals[0] or "").strip():
                return unquote(str(vals[0]).strip())
            if path.endswith("/raw-customer-sales-wechat/list"):
                svals = qs.get("search") or []
                if svals:
                    st = unquote(str(svals[0] or "").strip())
                    if st:
                        async with AsyncSessionLocal() as db:
                            r = await db.execute(
                                select(RawCustomerSalesWechat.sales_wechat_id)
                                .where(RawCustomerSalesWechat.sales_wechat_id == st)
                                .limit(1)
                            )
                            row = r.first()
                            if row and row[0]:
                                return str(row[0]).strip()
        except Exception:
            pass
    pks = [p.strip() for p in (request.query_params.get("pks") or "").split(",") if p.strip()]
    ids = [int(x) for x in pks if x.isdigit()]
    if not ids:
        return ""
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(RawCustomerSalesWechat.sales_wechat_id).where(
                RawCustomerSalesWechat.id.in_(ids)
            )
        )
        uniq = {(r[0] or "").strip() for r in res.all() if r and r[0]}
    if len(uniq) == 1:
        return next(iter(uniq))
    return ""


class ProfilingProgressView(BaseView):
    """后台 AI 画像批任务进度（统一队列、待处理批次、错误列表、可请求中断）。"""

    name = "AI 画像任务进度"
    category = ADMIN_CAT_CUSTOMERS

    # 单一 expose：GET 页面 + JSON 轮询 + POST 中断。路由名默认为函数名（与侧栏 url_for 一致）。
    @expose("/profiling-progress", methods=["GET", "POST"])
    async def ai_profiling_progress_page(self, request: Request):
        from ai.profiling_progress import request_cancel, snapshot

        if request.method == "POST":
            action = (request.query_params.get("action") or "").strip()
            if action in (
                "pause",
                "resume",
                "cancel_all_pending",
                "cancel_batch",
                "clear_cancel",
                "reclaim_stale",
            ):
                from ai import profile_queue

                if action == "pause":
                    await profile_queue.pause_workers_db()
                    return JSONResponse({"ok": True, "message": "已暂停抢任务（进行中的单条仍会跑完）"})
                if action == "resume":
                    await profile_queue.resume_workers_db()
                    return JSONResponse({"ok": True, "message": "已恢复抢任务"})
                if action == "cancel_all_pending":
                    n = await profile_queue.cancel_all_pending()
                    return JSONResponse({"ok": True, "message": f"已取消全部排队任务：{n} 条"})
                if action == "cancel_batch":
                    bid = (request.query_params.get("batch_id") or "").strip()
                    n = await profile_queue.cancel_pending_batch(bid)
                    return JSONResponse({"ok": True, "message": f"已取消批次 {bid} 的排队任务：{n} 条"})
                if action == "clear_cancel":
                    await profile_queue.clear_cancel_db()
                    return JSONResponse({"ok": True, "message": "已清除中断标记（允许继续抢任务）"})
                if action == "reclaim_stale":
                    raw_m = (request.query_params.get("stale_minutes") or "30").strip()
                    try:
                        stale_m = max(1, int(raw_m))
                    except ValueError:
                        stale_m = 30
                    n = await profile_queue.reclaim_stale_running(stale_minutes=stale_m)
                    return JSONResponse(
                        {
                            "ok": True,
                            "message": f"已回收卡死(>{stale_m}min)的运行中任务：{n} 条 → pending",
                        }
                    )
            request_cancel()
            return JSONResponse({"ok": True, "message": "已发送中断请求（将停止继续抢任务；正在跑的单条会跑完）"})
        if request.query_params.get("format") == "json":
            from ai.raw_profiling import get_profile_llm_display_for_progress

            data = await snapshot()
            async with AsyncSessionLocal() as db:
                data["profile_llm"] = await get_profile_llm_display_for_progress(db)
            return JSONResponse(data)
        from core.admin_pages import render_admin_page

        return await render_admin_page(
            request,
            "admin/profiling_progress.html",
            title="AI 画像任务进度",
            subtitle="后台批任务排队与执行状态",
        )
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>AI 画像任务进度</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 52rem; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; }
    h2 { font-size: 1rem; margin-top: 1.5rem; }
    .bar { height: 1.25rem; background: #e9ecef; border-radius: .25rem; overflow: hidden; margin: 1rem 0; display:flex; }
    .seg { height:100%; transition: width .3s ease; }
    .seg.done { background:#2fb344; }
    .seg.running { background:#206bc4; }
    .seg.pending { background:#adb5bd; }
    .seg.failed { background:#d63939; }
    .seg.cancelled { background:#f59f00; }
    .muted { color: #626976; font-size: .875rem; }
    .row { margin: .5rem 0; }
    code { background: #f1f5f9; padding: .1rem .35rem; border-radius: .2rem; font-size: .85em; }
    table { width: 100%; border-collapse: collapse; font-size: .875rem; }
    th, td { border: 1px solid #e9ecef; padding: .35rem .5rem; text-align: left; }
    th { background: #f8f9fa; }
    pre.err { background: #fff5f5; border: 1px solid #fecaca; border-radius: .25rem; padding: .75rem;
      max-height: 14rem; overflow: auto; white-space: pre-wrap; word-break: break-word; font-size: .8rem; }
    .btn { padding: .4rem .75rem; border-radius: .25rem; border: 1px solid #ced4da; background: #fff;
      color: #212529; cursor: pointer; font-size: .875rem; margin-right:.5rem; }
    .btn.danger { border-color:#c92a2a; background:#fff5f5; color:#c92a2a; }
    .btn.warn { border-color:#f59f00; background:#fff9db; color:#8a5b00; }
    .btn.primary { border-color:#206bc4; background:#e7f0ff; color:#0b3d91; }
    button:disabled { opacity: .5; cursor: not-allowed; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:.75rem; }
    .card { border:1px solid #e9ecef; border-radius:.35rem; padding:.75rem; background:#fff; }
    .chip { display:inline-block; padding:.1rem .45rem; border-radius:999px; background:#f1f5f9; font-size:.8rem; }
  </style>
</head>
<body>
  <h1>AI 画像任务进度</h1>
  <p class="muted">每 2 秒自动刷新。并行执行时，running 表示当前并发中的任务数；取消/暂停只会影响未开始(pending)的任务。</p>

  <div class="row">
    <button class="btn primary" type="button" id="btn-pause">暂停</button>
    <button class="btn primary" type="button" id="btn-resume">继续</button>
    <button class="btn danger" type="button" id="btn-cancel" title="停止继续抢任务（进行中的单条会跑完）">中断（停止抢任务）</button>
    <button class="btn warn" type="button" id="btn-clear-cancel" title="清除中断标记，允许继续抢任务">清除中断</button>
    <button class="btn danger" type="button" id="btn-cancel-all" title="取消全部排队(pending)任务">取消全部排队</button>
    <button class="btn warn" type="button" id="btn-reclaim-stale" title="把锁定超过 30 分钟仍 running 的任务回滚为 pending（worker 重启后忘记回收孤儿时使用）">重置卡死任务</button>
    <span class="muted" id="cancel-hint"></span>
  </div>

  <div class="grid">
    <div class="card">
      <div class="row"><strong>状态：</strong> <span id="status">—</span> <span class="chip" id="running-chip">running=0</span></div>
      <div class="row"><strong>画像模型：</strong> <span id="llm-info">—</span></div>
      <div class="row"><strong>进度：</strong> <span id="counts">—</span></div>
      <div class="bar">
        <div class="seg done" id="seg-done" style="width:0%"></div>
        <div class="seg running" id="seg-running" style="width:0%"></div>
        <div class="seg pending" id="seg-pending" style="width:0%"></div>
        <div class="seg failed" id="seg-failed" style="width:0%"></div>
        <div class="seg cancelled" id="seg-cancelled" style="width:0%"></div>
      </div>
      <div class="row muted" id="msg"></div>
    </div>
    <div class="card">
      <div class="row"><strong>当前并发中的任务</strong> <span class="muted">（最多显示 12 条）</span></div>
      <div id="running-wrap"><p class="muted">—</p></div>
    </div>
  </div>

  <h2>排队中的批次（尚未开始）</h2>
  <div id="pending-wrap"><p class="muted">—</p></div>

  <h2>最近错误（含堆栈摘要）</h2>
  <pre class="err" id="errors">—</pre>

  <script>
    function fmt(ts) {
      if (ts == null) return "";
      const d = new Date(ts * 1000);
      return isNaN(d) ? "" : d.toLocaleString();
    }
    async function postAction(action, params) {
      const u = new URL(window.location.href);
      u.searchParams.delete("format");
      u.searchParams.set("action", action);
      if (params) {
        Object.keys(params).forEach(k => u.searchParams.set(k, params[k]));
      }
      const r = await fetch(u.pathname + "?" + u.searchParams.toString(), { method: "POST", credentials: "same-origin" });
      return await r.json();
    }
    function bindBtn(id, action, paramsFn) {
      document.getElementById(id).addEventListener("click", async function() {
        const btn = this;
        btn.disabled = true;
        try {
          const j = await postAction(action, paramsFn ? paramsFn() : null);
          document.getElementById("cancel-hint").textContent = (j && j.message) ? j.message : "已提交";
        } catch (e) {
          document.getElementById("cancel-hint").textContent = "操作失败（请确认已登录后台）";
        }
        setTimeout(function() { btn.disabled = false; }, 1200);
      });
    }
    bindBtn("btn-pause", "pause");
    bindBtn("btn-resume", "resume");
    bindBtn("btn-cancel", "cancel");
    bindBtn("btn-clear-cancel", "clear_cancel");
    bindBtn("btn-cancel-all", "cancel_all_pending");
    bindBtn("btn-reclaim-stale", "reclaim_stale", function() { return { stale_minutes: 30 }; });
    async function tick() {
      try {
        const u = new URL(window.location.href);
        u.searchParams.set("format", "json");
        const r = await fetch(u.toString(), { credentials: "same-origin" });
        const d = await r.json();
        const st = { idle: "空闲", running: "运行中", paused: "已暂停", completed: "已完成", failed: "失败", cancelled: "已中断" };
        document.getElementById("status").textContent = (st[d.status] || d.status);
        let llmEl = document.getElementById("llm-info");
        if (llmEl) {
          const pl = d.profile_llm || {};
          llmEl.textContent = pl.model
            ? (pl.model + (pl.api_host ? " · API: " + pl.api_host : ""))
            : "—";
        }
        const cur = d.current_batch || {};
        const cbs = cur.counts_by_status || {};
        const total = cur.total || 0;
        const done = cbs.succeeded || cur.done || 0;
        const running = cbs.running || cur.running || 0;
        const pending = cbs.pending || cur.pending || 0;
        const failed = cbs.failed || cur.failed || 0;
        const cancelled = cbs.cancelled || cur.cancelled || 0;
        document.getElementById("running-chip").textContent = "running=" + running;
        let binfo = "—";
        if (cur.batch_label || cur.batch_id) {
          binfo = (cur.batch_label || "") + (cur.batch_id ? " · id=" + cur.batch_id : "");
        }
        document.getElementById("counts").textContent = total
          ? ("本次 " + binfo + " · 总 " + total + "（成功 " + done + "，运行中 " + running + "，排队 " + pending + "，失败 " + failed + "，取消 " + cancelled + "）")
          : "—";
        function pct(x) { return total ? (100.0 * x / total) : 0; }
        document.getElementById("seg-done").style.width = pct(done) + "%";
        document.getElementById("seg-running").style.width = pct(running) + "%";
        document.getElementById("seg-pending").style.width = pct(pending) + "%";
        document.getElementById("seg-failed").style.width = pct(failed) + "%";
        document.getElementById("seg-cancelled").style.width = pct(cancelled) + "%";
        let extra = "";
        if (pending > 0) extra += "排队任务：" + pending;
        const flags = [];
        if (d.paused) flags.push("已暂停");
        if (d.cancel_requested) flags.push("已中断(停止抢任务)");
        document.getElementById("msg").textContent = (flags.length ? flags.join(" · ") : "") + (extra ? (" · " + extra) : "");

        const rj = d.running_jobs || [];
        const rw = document.getElementById("running-wrap");
        if (!rj.length) {
          rw.innerHTML = '<p class="muted">无</p>';
        } else {
          let rows = rj.map(function(p, i) {
            return "<tr><td>" + (i+1) + "</td><td><code>" + (p.target || "") + "</code></td><td>" +
              (p.locked_by || "") + "</td><td>" + (p.locked_at || "") + "</td></tr>";
          }).join("");
          rw.innerHTML = "<table><thead><tr><th>#</th><th>任务</th><th>worker</th><th>锁定时间</th></tr></thead><tbody>" + rows + "</tbody></table>";
        }

        const pend = d.pending_batches || [];
        const pw = document.getElementById("pending-wrap");
        if (!pend.length) {
          pw.innerHTML = '<p class="muted">无</p>';
        } else {
          let rows = pend.map(function(p, i) {
            const bid = (p.batch_id || "");
            const btn = bid ? ('<button class="btn warn" data-batch="' + bid + '">取消该批次排队</button>') : "";
            return "<tr><td>" + (i+1) + "</td><td>" + (p.label || "") + "</td><td>" + (p.count != null ? p.count : "—") +
              "</td><td>" + fmt(p.enqueued_at) + "</td><td><code>" + bid + "</code></td><td>" + btn + "</td></tr>";
          }).join("");
          pw.innerHTML = "<table><thead><tr><th>#</th><th>说明</th><th>条数</th><th>入队时间</th><th>batch_id</th><th>操作</th></tr></thead><tbody>" +
            rows + "</tbody></table>";
          pw.querySelectorAll("button[data-batch]").forEach(function(btn) {
            btn.addEventListener("click", async function() {
              const bid = this.getAttribute("data-batch");
              this.disabled = true;
              try {
                const j = await postAction("cancel_batch", { batch_id: bid });
                document.getElementById("cancel-hint").textContent = (j && j.message) ? j.message : "已提交";
              } catch (e) {
                document.getElementById("cancel-hint").textContent = "取消批次失败";
              }
              setTimeout(() => { this.disabled = false; }, 1200);
            });
          });
        }

        const errs = d.recent_errors || [];
        const ep = document.getElementById("errors");
        if (!errs.length) {
          ep.textContent = "无";
        } else {
          ep.textContent = errs.map(function(e) {
            return fmt(e.at) + "  [" + (e.target || "") + "]\\n" + (e.message || "") + "\\n---\\n";
          }).join("");
        }
      } catch (e) {
        document.getElementById("msg").textContent = "无法拉取状态（请保持管理后台已登录）";
      }
    }
    tick();
    setInterval(tick, 2000);
  </script>
</body>
</html>"""
        return HTMLResponse(html)

class UserAdmin(AdminModelView, model=User):
    column_list = [
        User.id,
        User.username,
        User.real_name,
        User.wechat_remark_for_prompt,
        User.role,
        User.is_active,
        "sales_wechat_bindings_count",
        "relations_links",
        "chat_links",
    ]
    column_searchable_list = [User.username, User.real_name]
    page_size = PAGE_SIZE

    # 工号/姓名/备注靠搜索即可；仅 ID 可排序
    column_sortable_list = [User.id]
    column_default_sort = [(User.id, True)]

    column_filters = [
        LocalizedStaticValuesFilter(
            User.role,
            title="系统权限角色",
            values=[
                ("staff", "普通业务员"),
                ("admin", "超级系统管理员"),
            ],
        ),
        LocalizedBooleanFilter(
            User.is_active,
            title="账号状态",
            true_label="正常",
            false_label="已停用",
        ),
    ]

    category = ADMIN_CAT_USERS
    name = "系统登录账号"
    name_plural = "系统登录账号"
    
    column_formatters = {
        # 列表页：只显示数量，避免把绑定项展开成碎片文本
        "sales_wechat_bindings_count": lambda m, a: (
            f"{len(m.sales_wechat_bindings or [])} 个"
            if getattr(m, "sales_wechat_bindings", None)
            else "0 个"
        ),
        "relations_links": lambda m, a: Markup(
            f'<a href="/admin/sales-customer-profile/list?search=wechat:{",".join([b.sales_wechat_id for b in m.sales_wechat_bindings])}">👥 {len(m.sales_customer_profiles or [])} 条关联</a>'
        ) if m.sales_wechat_bindings else "—",
        "chat_links": lambda m, a: Markup(
            f'<a href="/admin/chat-message/list?search=user:{m.username}">💬 {len(m.chat_messages)} 条对话</a>'
        ) if m.chat_messages else "暂无"
    }
    

    # 编辑页仅屏蔽关系型大字段（避免误编辑/加载卡顿）
    form_excluded_columns = [
        "wechat_id",
        "sales_customer_profiles",
        "chat_messages",
        "sales_wechat_bindings", # 排除直接对绑定中间表的编辑，改为通过 wechat_accounts 直接关联主数据
    ]
    
    form_ajax_refs = {
        "wechat_accounts": {
            "fields": ("sales_wechat_id", "nickname", "alias_name"),
            "order_by": "sales_wechat_id",
        }
    }
    
    # 强制让 role 变成下拉项
    form_overrides = {"role": SelectField}

    form_args = {
        "role": {
            "choices": [("staff", "普通业务员"), ("admin", "超级系统管理员")],
            "label": "系统权限角色"
        },
        "password_hash": {
            "label": "登录密码（新创建时必填；修改时留空则保持原密码）"
        }
    }
    column_labels = {
        User.id: "ID",
        User.username: "登录系统工号",
        User.password_hash: "登录密码",
        User.real_name: "真实姓名",
        User.wechat_remark_for_prompt: "用户备注",
        User.wechat_id: "微信号绑定（旧字段，已废弃）",
        User.role: "系统权限角色",
        User.is_active: "账号状态(是否停用)",
        "sales_wechat_bindings_count": "绑定微信数",
        "sales_wechat_bindings": "微信号绑定明细(旧)",
        "wechat_accounts": "微信号绑定明细（销售微信号绑定）",
        "relations_links": "管辖客户",
        "chat_links": "对话记录"
    }

    def list_query(self, request):
        from sqlalchemy.orm import selectinload
        return super().list_query(request).options(
            selectinload(User.sales_customer_profiles),
            selectinload(User.chat_messages),
            selectinload(User.sales_wechat_bindings),
        )

    # 详情页：展示绑定明细（多行）
    column_details_list = [
        User.id,
        User.username,
        User.real_name,
        User.wechat_remark_for_prompt,
        User.role,
        User.is_active,
        "sales_wechat_bindings",
        "relations_links",
        "chat_links",
    ]
    column_formatters_detail = {
        "sales_wechat_bindings": lambda m, a: Markup("<br/>".join(
            [
                Markup.escape(
                    (
                        f"{(b.sales_wechat_id or '').strip()}"
                        + (f"（{(b.label or '').strip()}）" if (b.label or '').strip() else "")
                        + (" · 主号" if getattr(b, "is_primary", False) else "")
                        + (f" · 审核:{b.verified_at}" if getattr(b, "verified_at", None) else "")
                    ).strip()
                )
                for b in (m.sales_wechat_bindings or [])
            ]
        )) if getattr(m, "sales_wechat_bindings", None) else "空",
    }

    async def on_model_change(self, data: dict, model: any, is_created: bool, request: any) -> None:
        """
        在保存员工信息前进行拦截：
        如果提供了密码字段，且它不是哈希格式，则自动进行 Bcrypt 哈希加密。
        """
        if "password_hash" in data:
            pwd = data["password_hash"]
            if pwd:
                # 简单校验：如果不是以 $2b$ (Bcrypt) 开头，则认为需要加密
                if not pwd.startswith("$2b$"):
                    from core.security import get_password_hash
                    data["password_hash"] = get_password_hash(pwd)
            else:
                # 如果是修改操作且密码为空，则透传，不更新密码字段（从 data 中移除）
                if not is_created:
                    data.pop("password_hash")


class UserSalesWechatAdmin(AdminModelView, model=UserSalesWechat):
    category = ADMIN_CAT_USERS
    name = "销售微信号绑定"
    name_plural = "销售微信号绑定"
    page_size = PAGE_SIZE

    column_list = [
        UserSalesWechat.id,
        UserSalesWechat.user_id,
        UserSalesWechat.sales_wechat_id,
        UserSalesWechat.label,
        UserSalesWechat.is_primary,
        UserSalesWechat.created_at,
        UserSalesWechat.verified_at,
    ]
    column_labels = {
        UserSalesWechat.id: "ID",
        UserSalesWechat.user_id: "用户 ID",
        UserSalesWechat.sales_wechat_id: "销售微信号",
        UserSalesWechat.label: "备注",
        UserSalesWechat.is_primary: "主号",
        UserSalesWechat.created_at: "创建时间",
        UserSalesWechat.verified_at: "审核时间",
    }
    column_searchable_list = [UserSalesWechat.sales_wechat_id, UserSalesWechat.label]
    column_sortable_list = [UserSalesWechat.id, UserSalesWechat.user_id, UserSalesWechat.created_at, UserSalesWechat.verified_at]
    column_default_sort = [(UserSalesWechat.id, True)]
    column_formatters = {
        UserSalesWechat.sales_wechat_id: _fmt_sales_wechat_column,
    }
    column_filters = [
        LocalizedBooleanFilter(
            UserSalesWechat.is_primary,
            title="主号",
            true_label="是",
            false_label="否",
        ),
    ]

    def list_query(self, request):
        from sqlalchemy.orm import selectinload

        return super().list_query(request).options(
            selectinload(UserSalesWechat.sales_wechat_account),
        )

    form_columns = [
        UserSalesWechat.user_id,
        UserSalesWechat.sales_wechat_id,
        UserSalesWechat.label,
        UserSalesWechat.is_primary,
        UserSalesWechat.verified_at,
    ]


class SalesWechatAccountAdmin(AdminModelView, model=SalesWechatAccount):
    """销售业务微信主数据（与云客 wxid 对齐；默认从开放平台 companyAccounts 同步，可选 XLSX 备用）。"""

    category = ADMIN_CAT_USERS
    name = "销售微信主数据"
    name_plural = "销售微信主数据"
    page_size = PAGE_SIZE

    column_list = [
        SalesWechatAccount.sales_wechat_id,
        SalesWechatAccount.nickname,
        SalesWechatAccount.alias_name,
        SalesWechatAccount.account_code,
        SalesWechatAccount.phone,
        SalesWechatAccount.source,
        SalesWechatAccount.updated_at,
    ]
    column_labels = {
        SalesWechatAccount.sales_wechat_id: "微信ID (sales_wechat_id)",
        SalesWechatAccount.nickname: "昵称",
        SalesWechatAccount.alias_name: "别名/备注",
        SalesWechatAccount.account_code: "云客账号",
        SalesWechatAccount.phone: "号上手机号",
        SalesWechatAccount.source: "来源",
        SalesWechatAccount.updated_at: "更新时间",
    }
    column_searchable_list = [
        SalesWechatAccount.sales_wechat_id,
        SalesWechatAccount.nickname,
        SalesWechatAccount.alias_name,
    ]
    form_columns = [
        SalesWechatAccount.sales_wechat_id,
        SalesWechatAccount.nickname,
        SalesWechatAccount.alias_name,
        SalesWechatAccount.account_code,
        SalesWechatAccount.phone,
        SalesWechatAccount.source,
    ]
    column_sortable_list = [SalesWechatAccount.updated_at]
    column_default_sort = [(SalesWechatAccount.updated_at, True)]


class SalesWechatAccountSyncView(BaseView):
    """从开放平台 /open/wechat/companyAccounts 分页同步；可选 XLSX 备用导入。"""

    name = "销售微信·开放平台同步"
    category = ADMIN_CAT_SYNC

    @expose("/sales-wechat-accounts/import-xlsx", methods=["GET", "POST"])
    async def import_xlsx_page(self, request: Request):
        from sync.company_accounts_open import sync_from_open_api
        from sync.sales_wechat_accounts import default_accounts_xlsx_path, sync_from_path

        msg = ""
        if request.method == "POST":
            form = await request.form()
            mode = (form.get("sync_mode") or "").strip()
            try:
                if mode == "open_api":
                    partner = (form.get("partner_id") or "").strip() or None
                    psize_raw = (form.get("page_size") or "200").strip()
                    page_size = int(psize_raw) if psize_raw.isdigit() else 200
                    page_size = max(1, min(400, page_size))
                    uts = (form.get("update_time_start") or "").strip() or None
                    ute = (form.get("update_time_end") or "").strip() or None
                    st = await sync_from_open_api(
                        partner_id=partner,
                        page_size=page_size,
                        update_time_start=uts,
                        update_time_end=ute,
                    )
                    msg = (
                        f"开放平台同步成功：已 upsert {st.get('upserted')} 条，"
                        f"接口 totalCount≈{st.get('total_count_api')}，"
                        f"展开行数 {st.get('flattened_rows')}，共 {st.get('pages_fetched')} 页。"
                    )
                else:
                    raw = (form.get("path") or "").strip()
                    p = Path(raw).expanduser().resolve() if raw else default_accounts_xlsx_path()
                    st = await sync_from_path(p)
                    msg = (
                        f"XLSX 成功：已 upsert {st.get('upserted')} 条，"
                        f"文件中有效行 {st.get('rows_in_file')}。路径：{st.get('path')}"
                    )
            except Exception as e:
                msg = f"失败：{e}"

        default_p = str(default_accounts_xlsx_path())
        safe_msg = Markup.escape(msg) if msg else ""
        from core.admin_pages import render_admin_page

        return await render_admin_page(
            request,
            "admin/sync_sales_wechat.html",
            title="销售微信·开放平台同步",
            subtitle="主数据同步与 XLSX 备用导入",
            default_path=default_p,
            message=msg,
        )
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>销售微信主数据 · 开放平台同步</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem; }}
    label {{ display: block; margin: .75rem 0 .25rem; font-weight: 600; }}
    input[type=text], input[type=number] {{ width: 100%; box-sizing: border-box; padding: .5rem; }}
    button {{ margin-top: 1rem; padding: .5rem 1rem; }}
    .muted {{ color: #64748b; font-size: .875rem; margin-top: 1.25rem; line-height: 1.5; }}
    .msg {{ margin-top: 1rem; white-space: pre-wrap; }}
    hr {{ margin: 2rem 0; border: none; border-top: 1px solid #e2e8f0; }}
    h2 {{ font-size: 1.1rem; margin-top: 0; }}
  </style>
</head>
<body>
  <h1>销售微信主数据 · 开放平台同步</h1>
  <p class="muted">接口 <code>/open/wechat/companyAccounts</code>，与好友/聊天同步共用环境变量
  <code>WECHAT_OPEN_*</code>；partnerId 优先读表单，否则与「原始客户池」一致（系统配置或环境变量）。</p>

  <h2>1. 从开放平台全量同步（推荐）</h2>
  <form method="post">
    <input type="hidden" name="sync_mode" value="open_api"/>
    <label for="partner_id">开放平台 partnerId（可选，留空则读系统配置 / 环境变量）</label>
    <input type="text" id="partner_id" name="partner_id" value="" placeholder="管理员或员工 ID" autocomplete="off"/>
    <label for="page_size">每页条数（1–400，默认 200）</label>
    <input type="number" id="page_size" name="page_size" value="200" min="1" max="400"/>
    <label for="update_time_start">更新时间起（可选，格式 yyyy-MM-dd HH:mm:ss；与止期跨度≤31 天）</label>
    <input type="text" id="update_time_start" name="update_time_start" value="" placeholder="留空表示不按时间筛选"/>
    <label for="update_time_end">更新时间止（可选）</label>
    <input type="text" id="update_time_end" name="update_time_end" value="" placeholder="留空表示不按时间筛选"/>
    <button type="submit">开始从开放平台同步</button>
  </form>
  <p class="muted">联调可先执行：<code>cd backend &amp;&amp; python scripts/test_company_accounts.py</code>（默认只拉第 1 页预览）；加 <code>--write</code> 再写入数据库。</p>

  <hr/>
  <h2>2. 备用：XLSX 导入</h2>
  <p class="muted">与云客导出 <code>accounts.xlsx</code> 表头一致。</p>
  <form method="post">
    <input type="hidden" name="sync_mode" value="xlsx"/>
    <label for="path">文件路径（留空则默认项目根 <code>accounts.xlsx</code> 或环境变量 ACCOUNTS_XLSX）</label>
    <input type="text" id="path" name="path" value="" placeholder="{Markup.escape(default_p)}"/>
    <button type="submit">开始 XLSX 导入</button>
  </form>
  <p class="muted">命令行：<code>cd backend &amp;&amp; python -m sync.sales_wechat_accounts [路径]</code></p>
  {f'<p class="msg">{safe_msg}</p>' if safe_msg else ''}
</body>
</html>"""
        return HTMLResponse(html)


class RawWechatPoolSyncView(BaseView):
    """开放平台 getAllFriendsIncrement：按自然日写入 raw_customers / raw_customer_sales_wechats。"""

    name = "原始客户池·微信增量同步"
    category = ADMIN_CAT_SYNC

    @expose("/raw-customer-wechat-sync", methods=["GET", "POST"])
    async def wechat_increment_sync_page(self, request: Request):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from core.wechat_friends_sync import (
            CFG_PARTNER,
            CFG_TARGET_DAY,
            persist_wechat_sync_prefs,
            read_wechat_sync_ui_settings,
            sync_wechat_friends_for_calendar_day,
        )

        msg = ""
        if request.method == "POST":
            form = await request.form()
            day = (form.get("calendar_day") or "").strip()
            partner = (form.get("partner_id") or "").strip()
            include_groups = form.get("include_groups") == "on"
            types = (1, 2) if include_groups else (1,)
            try:
                async with AsyncSessionLocal() as db:
                    await persist_wechat_sync_prefs(db, calendar_day=day, partner_field=partner)
                asyncio.create_task(
                    sync_wechat_friends_for_calendar_day(day, partner_id=None, types=types)
                )
                msg = (
                    f"已提交后台任务：目标日 {day}，"
                    f"{'含群(type=2)' if include_groups else '仅好友(type=1)'}。"
                    "开放平台限频 5 秒/次，请稍后到「环境控制变量」查看 wechat_friends_sync_* 状态。"
                )
            except Exception as e:
                msg = f"失败：{e}"

        async with AsyncSessionLocal() as db:
            cfg = await read_wechat_sync_ui_settings(db)
        if request.query_params.get("format") == "json":
            return JSONResponse(
                {
                    "status": (cfg.get("wechat_friends_sync_status") or "").strip() or "idle",
                    "query_mode": (cfg.get("wechat_friends_query_mode") or "updateTime").strip(),
                    "last_message": (cfg.get("wechat_friends_sync_last_message") or "").strip(),
                    "last_success": (cfg.get("wechat_friends_sync_last_success") or "").strip(),
                    "target_day": (cfg.get(CFG_TARGET_DAY) or "").strip(),
                    "partner_id_override": (cfg.get(CFG_PARTNER) or "").strip(),
                }
            )
        sh = ZoneInfo("Asia/Shanghai")
        today = datetime.now(sh).date().isoformat()
        day_default = (cfg.get(CFG_TARGET_DAY) or "").strip() or today
        partner_default = (cfg.get(CFG_PARTNER) or "").strip()
        st = (cfg.get("wechat_friends_sync_status") or "").strip() or "—"
        last_msg = Markup.escape((cfg.get("wechat_friends_sync_last_message") or "").strip() or "—")
        last_ok = Markup.escape((cfg.get("wechat_friends_sync_last_success") or "").strip() or "—")
        qmode = Markup.escape((cfg.get("wechat_friends_query_mode") or "updateTime").strip())

        safe_msg = Markup.escape(msg) if msg else ""
        from core.admin_pages import render_admin_page

        return await render_admin_page(
            request,
            "admin/sync_raw_pool.html",
            title="原始客户池·微信增量同步",
            subtitle="按自然日增量同步好友快照",
            day_default=day_default,
            partner_default=partner_default,
            sync_status=st,
            query_mode=(cfg.get("wechat_friends_query_mode") or "updateTime").strip(),
            last_message=(cfg.get("wechat_friends_sync_last_message") or "").strip() or "—",
            last_success=(cfg.get("wechat_friends_sync_last_success") or "").strip() or "—",
            message=msg,
        )
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>原始客户池 · 微信增量同步</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem; }}
    label {{ display: block; margin: .75rem 0 .25rem; font-weight: 600; }}
    input[type=date], input[type=text] {{ width: 100%; box-sizing: border-box; padding: .5rem; }}
    .row {{ margin: .5rem 0; }}
    button {{ margin-top: 1rem; padding: .5rem 1rem; }}
    .muted {{ color: #64748b; font-size: .875rem; margin-top: 1.25rem; line-height: 1.5; }}
    .msg {{ margin-top: 1rem; white-space: pre-wrap; }}
    .panel {{ background: #f8fafc; border-radius: .5rem; padding: 1rem; margin-top: 1.5rem; }}
  </style>
</head>
<body>
  <h1>原始客户池 · 微信增量同步</h1>
  <p class="muted">仅同步所选<strong>自然日（上海时区）</strong>内变更的记录；自动任务与手动任务共用下方保存的「目标日」与 partner 覆盖。</p>
  <div class="panel">
    <div class="row"><strong>当前状态：</strong> <span id="st">{Markup.escape(st)}</span></div>
    <div class="row"><strong>查询模式：</strong> <span id="qmode">{qmode}</span>（可在「环境控制变量」修改 wechat_friends_query_mode）</div>
    <div class="row"><strong>上次摘要：</strong> <span id="last_msg">{last_msg}</span></div>
    <div class="row"><strong>上次成功时间：</strong> <span id="last_ok">{last_ok}</span></div>
    <div class="row muted" id="poll_tip">页面每 2 秒自动刷新状态。</div>
  </div>
  <form method="post">
    <label for="calendar_day">目标自然日</label>
    <input type="date" id="calendar_day" name="calendar_day" value="{Markup.escape(day_default)}" required />
    <label for="partner_id">开放平台 partnerId（可选，留空则读环境变量 WECHAT_OPEN_ADMIN_PARTNER_ID）</label>
    <input type="text" id="partner_id" name="partner_id" value="{Markup.escape(partner_default)}" placeholder="管理员或员工 ID" autocomplete="off" />
    <label class="row"><input type="checkbox" name="include_groups" checked/> 同时同步微信群 (type=2)</label>
    <button type="submit">保存配置并后台同步</button>
  </form>
  <p class="muted">说明：提交后会写入 system_configs 的 <code>wechat_friends_sync_target_day</code> 与 <code>wechat_open_partner_id</code>，
  每日 04:20 定时任务将<strong>仅同步该目标日</strong>。开放平台限频 5 秒/次，大日期可能需数分钟。</p>
  {f'<p class="msg">{safe_msg}</p>' if safe_msg else ''}
  <script>
    async function tick() {{
      try {{
        const u = new URL(window.location.href);
        u.searchParams.set("format", "json");
        const r = await fetch(u.toString(), {{ credentials: "same-origin" }});
        const d = await r.json();
        document.getElementById("st").textContent = d.status || "—";
        document.getElementById("qmode").textContent = d.query_mode || "—";
        document.getElementById("last_msg").textContent = d.last_message || "—";
        document.getElementById("last_ok").textContent = d.last_success || "—";
      }} catch (e) {{
        document.getElementById("poll_tip").textContent = "无法拉取状态（请保持管理后台已登录）";
      }}
    }}
    tick();
    setInterval(tick, 2000);
  </script>
</body>
</html>"""
        return HTMLResponse(html)


class RawWechatChatSyncView(BaseView):
    """开放平台 allRecords：增量同步聊天到 raw_chat_logs。"""

    name = "原始聊天·微信增量同步"
    category = ADMIN_CAT_SYNC

    @expose("/raw-chat-wechat-sync", methods=["GET", "POST"])
    async def wechat_chat_sync_page(self, request: Request):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from core.wechat_chat_sync import (
            CFG_CHAT_CURSOR_CREATE,
            CFG_CHAT_CURSOR_TIME,
            CFG_CHAT_LAST_MSG,
            CFG_CHAT_LAST_OK,
            CFG_CHAT_STATUS,
            sync_wechat_chat_increment,
        )

        msg = ""
        if request.method == "POST":
            form = await request.form()
            start_dt = (form.get("start_dt") or "").strip()
            hours = int((form.get("hours") or "6").strip() or "6")
            partner = (form.get("partner_id") or "").strip()
            persist = form.get("persist_cursor") == "on"

            try:
                # datetime-local: "YYYY-MM-DDTHH:MM"
                if "T" not in start_dt:
                    raise ValueError("开始时间格式不正确")
                dt = datetime.fromisoformat(start_dt)
                start_ms = int(dt.timestamp() * 1000)
                max_calls = max(1, min(24, hours))
                asyncio.create_task(
                    sync_wechat_chat_increment(
                        start_time_ms=start_ms,
                        max_calls=max_calls,
                        partner_id=(partner or None),
                        persist_cursor=persist,
                    )
                )
                msg = (
                    f"已提交后台任务：start={start_dt} hours≈{max_calls}，"
                    f"{'写回游标' if persist else '不写回游标'}。"
                    "接口限频 5 秒/次，且 timestamp 必须早于当前约30分钟以上。"
                )
            except Exception as e:
                msg = f"失败：{e}"

        async with AsyncSessionLocal() as db:
            keys = [
                CFG_CHAT_CURSOR_TIME,
                CFG_CHAT_CURSOR_CREATE,
                CFG_CHAT_STATUS,
                CFG_CHAT_LAST_MSG,
                CFG_CHAT_LAST_OK,
            ]
            stmt = select(SystemConfig).where(SystemConfig.config_key.in_(keys))
            res = await db.execute(stmt)
            rows = {c.config_key: (c.config_value or "") for c in res.scalars().all()}
        if request.query_params.get("format") == "json":
            return JSONResponse(
                {
                    "status": (rows.get(CFG_CHAT_STATUS, "") or "").strip() or "idle",
                    "cursor_time_ms": (rows.get(CFG_CHAT_CURSOR_TIME, "") or "").strip(),
                    "cursor_create_ts_ms": (rows.get(CFG_CHAT_CURSOR_CREATE, "") or "").strip(),
                    "last_message": (rows.get(CFG_CHAT_LAST_MSG, "") or "").strip(),
                    "last_success": (rows.get(CFG_CHAT_LAST_OK, "") or "").strip(),
                }
            )

        sh = ZoneInfo("Asia/Shanghai")
        # 默认：昨天 15:00
        default_dt = datetime.now(sh).replace(hour=15, minute=0, second=0, microsecond=0) - timedelta(days=1)
        default_start = default_dt.strftime("%Y-%m-%dT%H:%M")

        st = (rows.get(CFG_CHAT_STATUS, "") or "").strip() or "—"
        last_msg = Markup.escape((rows.get(CFG_CHAT_LAST_MSG, "") or "").strip() or "—")
        last_ok = Markup.escape((rows.get(CFG_CHAT_LAST_OK, "") or "").strip() or "—")
        cur_time = Markup.escape((rows.get(CFG_CHAT_CURSOR_TIME, "") or "").strip() or "—")
        cur_create = Markup.escape((rows.get(CFG_CHAT_CURSOR_CREATE, "") or "").strip() or "0")

        safe_msg = Markup.escape(msg) if msg else ""
        from core.admin_pages import render_admin_page

        return await render_admin_page(
            request,
            "admin/sync_raw_chat.html",
            title="原始聊天·微信增量同步",
            subtitle="allRecords 按时间窗口增量同步",
            default_start=default_start,
            sync_status=st,
            cursor_time=(rows.get(CFG_CHAT_CURSOR_TIME, "") or "").strip() or "—",
            cursor_create=(rows.get(CFG_CHAT_CURSOR_CREATE, "") or "").strip() or "0",
            last_message=(rows.get(CFG_CHAT_LAST_MSG, "") or "").strip() or "—",
            last_success=(rows.get(CFG_CHAT_LAST_OK, "") or "").strip() or "—",
            message=msg,
        )
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>原始聊天 · 微信增量同步</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; }}
    label {{ display: block; margin: .75rem 0 .25rem; font-weight: 600; }}
    input[type=datetime-local], input[type=number], input[type=text] {{ width: 100%; box-sizing: border-box; padding: .5rem; }}
    button {{ margin-top: 1rem; padding: .5rem 1rem; }}
    .muted {{ color: #64748b; font-size: .875rem; margin-top: 1.25rem; line-height: 1.5; }}
    .msg {{ margin-top: 1rem; white-space: pre-wrap; }}
    .panel {{ background: #f8fafc; border-radius: .5rem; padding: 1rem; margin-top: 1.5rem; }}
  </style>
</head>
<body>
  <h1>原始聊天 · 微信增量同步</h1>
  <p class="muted">接口按 <code>timestamp(time)</code> 每次拉 1 小时窗口，限频 5 秒/次，且入参必须早于当前约 30 分钟以上（建议留 40 分钟）。</p>
  <div class="panel">
    <div><strong>当前状态：</strong> <span id="st">{Markup.escape(st)}</span></div>
    <div><strong>游标 time(ms)：</strong> <span id="cur_time">{cur_time}</span></div>
    <div><strong>游标 createTimestamp(ms)：</strong> <span id="cur_create">{cur_create}</span></div>
    <div><strong>上次摘要：</strong> <span id="last_msg">{last_msg}</span></div>
    <div><strong>上次成功时间：</strong> <span id="last_ok">{last_ok}</span></div>
    <div class="muted" id="poll_tip">页面每 2 秒自动刷新状态。</div>
  </div>
  <form method="post">
    <label for="start_dt">开始时间（上海时区）</label>
    <input type="datetime-local" id="start_dt" name="start_dt" value="{Markup.escape(default_start)}" required />
    <label for="hours">拉取小时数（每小时=一次请求窗口）</label>
    <input type="number" id="hours" name="hours" value="6" min="1" max="24" />
    <label for="partner_id">开放平台 partnerId（可选，留空走配置/环境变量）</label>
    <input type="text" id="partner_id" name="partner_id" value="" placeholder="管理员或员工 ID" autocomplete="off" />
    <label class="row"><input type="checkbox" name="persist_cursor" checked/> 写回游标（下次自动从 end 继续）</label>
    <button type="submit">后台开始同步</button>
  </form>
  {f'<p class="msg">{safe_msg}</p>' if safe_msg else ''}
  <script>
    async function tick() {{
      try {{
        const u = new URL(window.location.href);
        u.searchParams.set("format", "json");
        const r = await fetch(u.toString(), {{ credentials: "same-origin" }});
        const d = await r.json();
        document.getElementById("st").textContent = d.status || "—";
        document.getElementById("cur_time").textContent = d.cursor_time_ms || "—";
        document.getElementById("cur_create").textContent = d.cursor_create_ts_ms || "0";
        document.getElementById("last_msg").textContent = d.last_message || "—";
        document.getElementById("last_ok").textContent = d.last_success || "—";
      }} catch (e) {{
        document.getElementById("poll_tip").textContent = "无法拉取状态（请保持管理后台已登录）";
      }}
    }}
    tick();
    setInterval(tick, 2000);
  </script>
</body>
</html>"""
        return HTMLResponse(html)


def _scp_fmt_purchase_months(m: Any, _prop: str) -> str:
    rc = getattr(m, "raw_customer", None)
    val = getattr(rc, "purchase_months", None) if rc else None
    if val is None:
        return "—"
    if isinstance(val, list):
        parts = [str(x).strip() for x in val if str(x).strip()]
        return ", ".join(parts) if parts else "—"
    s = str(val).strip()
    return s if s else "—"


def _scp_fmt_profile_tags(m: Any, _prop: str):
    tags = getattr(m, "profile_tags", None) or []
    if not tags:
        return "—"
    return Markup(", ".join(Markup.escape(getattr(t, "name", str(t))) for t in tags))


def _scp_fmt_profile_status_badge(m: Any, _prop: str):
    ok = (getattr(m, "profile_status", 0) or 0) == 1
    return Markup(
        '<span class="badge bg-success">已分析</span>'
        if ok
        else '<span class="badge bg-secondary">未分析</span>'
    )


class SalesCustomerProfileAdmin(AdminModelView, model=SalesCustomerProfile):
    category = ADMIN_CAT_CUSTOMERS
    name = "私域画像与跟进"
    name_plural = "私域画像与跟进"
    page_size = PAGE_SIZE

    column_list = [
        SalesCustomerProfile.id,
        "raw_customer.customer_name",
        "raw_customer.phone",
        SalesCustomerProfile.sales_wechat_id,
        "raw_customer.unit_name",
        SalesCustomerProfile.wechat_remark,
        SalesCustomerProfile.raw_customer_id,
        SalesCustomerProfile.budget_amount,
        SalesCustomerProfile.purchase_type,
        SalesCustomerProfile.contact_date,
        "profile_tags",
        SalesCustomerProfile.profile_status,
        SalesCustomerProfile.updated_at,
    ]
    column_details_list = [
        SalesCustomerProfile.id,
        SalesCustomerProfile.raw_customer_id,
        SalesCustomerProfile.sales_wechat_id,
        SalesCustomerProfile.user_id,
        "raw_customer.customer_name",
        "raw_customer.phone",
        "raw_customer.phone_normalized",
        "raw_customer.unit_type",
        "raw_customer.unit_name",
        "raw_customer.admin_division",
        "raw_customer.purchase_months",
        SalesCustomerProfile.relation_type,
        SalesCustomerProfile.contact_date,
        SalesCustomerProfile.suggested_followup_date,
        SalesCustomerProfile.wechat_remark,
        SalesCustomerProfile.title,
        SalesCustomerProfile.budget_amount,
        SalesCustomerProfile.purchase_type,
        "profile_tags",
        SalesCustomerProfile.ai_profile,
        SalesCustomerProfile.profile_status,
        SalesCustomerProfile.profiled_at,
        SalesCustomerProfile.created_at,
        SalesCustomerProfile.updated_at,
    ]
    column_labels = {
        "raw_customer.customer_name": "真实姓名（客观库）",
        "raw_customer.phone": "联系电话",
        "raw_customer.phone_normalized": "规范化电话",
        "raw_customer.unit_type": "所属单位（类型）",
        "raw_customer.unit_name": "所属单位",
        "raw_customer.admin_division": "行政区划",
        "raw_customer.purchase_months": "采货月份",
        SalesCustomerProfile.sales_wechat_id: "销售昵称",
        SalesCustomerProfile.raw_customer_id: "客户 ID",
        "profile_tags": "动态标签（画像）",
        SalesCustomerProfile.contact_date: "建联日期",
        SalesCustomerProfile.suggested_followup_date: "建议跟进日",
        SalesCustomerProfile.wechat_remark: "客户微信备注",
        SalesCustomerProfile.title: "当前称呼",
        SalesCustomerProfile.budget_amount: "采购预算",
        SalesCustomerProfile.purchase_type: "采购类型",
        SalesCustomerProfile.ai_profile: "私域画像（全文）",
        SalesCustomerProfile.profile_status: "画像状态",
        SalesCustomerProfile.profiled_at: "画像完成时间",
    }
    column_formatters = {
        SalesCustomerProfile.sales_wechat_id: _fmt_sales_wechat_column,
        SalesCustomerProfile.wechat_remark: lambda m, a: (
            ((m.wechat_remark or "")[:28] + "…")
            if m.wechat_remark and len(m.wechat_remark) > 28
            else (m.wechat_remark or "—")
        ),
        SalesCustomerProfile.raw_customer_id: lambda m, a: Markup(
            f'<span class="text-truncate d-inline-block" style="max-width:9rem" title="{Markup.escape(m.raw_customer_id or "")}">'
            f"{Markup.escape(m.raw_customer_id or "—")}</span>"
        ),
        SalesCustomerProfile.budget_amount: lambda m, a: (
            f"{float(m.budget_amount):,.2f}" if m.budget_amount is not None else "—"
        ),
        "raw_customer.unit_name": lambda m, a: (
            (getattr(getattr(m, "raw_customer", None), "unit_name", None) or "—").strip() or "—"
        ),
        "profile_tags": _scp_fmt_profile_tags,
        SalesCustomerProfile.profile_status: _scp_fmt_profile_status_badge,
    }
    column_formatters_detail = {
        "raw_customer.purchase_months": _scp_fmt_purchase_months,
        "profile_tags": _scp_fmt_profile_tags,
        SalesCustomerProfile.profile_status: _scp_fmt_profile_status_badge,
    }
    column_searchable_list = [
        "raw_customer_id",
        "sales_wechat_id",
        "wechat_remark",
        "ai_profile",
        "title",
    ]
    column_sortable_list = [
        SalesCustomerProfile.id,
        SalesCustomerProfile.contact_date,
        SalesCustomerProfile.budget_amount,
        SalesCustomerProfile.updated_at,
    ]
    column_default_sort = [(SalesCustomerProfile.updated_at, True)]
    column_filters = [
        DistinctColumnValuesFilter(
            SalesCustomerProfile.purchase_type,
            title="采购类型",
        ),
    ]
    # 编辑页默认会渲染大文本字段，数据量大时容易卡顿；先隐藏（画像建议在专用页面/只读查看）。
    form_excluded_columns = ["created_at", "updated_at", "ai_profile", "dify_conversation_id"]

    form_ajax_refs = {
        # raw_customers 数量很大：编辑页如果渲染为普通下拉会一次性加载全量选项导致卡顿；
        # 改为“输入搜索后再下拉”的异步选择（select2 ajax）。
        "raw_customer": {
            "fields": (
                "id",
                "customer_name",
                "phone",
                "phone_normalized",
                "unit_name",
                "unit_type",
            ),
            "order_by": "id",
        },
        "profile_tags": {
            "fields": ("name",),
            "order_by": "sort_order",
        }
    }

    def list_query(self, request):
        from sqlalchemy.orm import selectinload

        # 列表使用了 raw_customer.* 穿透字段，需预加载，否则渲染列表时会触发懒加载/游离实例错误
        return super().list_query(request).options(
            selectinload(SalesCustomerProfile.raw_customer),
            selectinload(SalesCustomerProfile.sales_wechat_account),
        )

    def search_query(self, stmt, term):
        """同时按客观库姓名/电话检索，避免 sqladmin 对多段 raw_customer.* 搜索字段重复 JOIN。"""
        if "wechat:" in term:
            w_part = term.split("wechat:")[1].strip()
            if w_part:
                wechat_ids = [w.strip() for w in w_part.split(",") if w.strip()]
                if wechat_ids:
                    return stmt.filter(SalesCustomerProfile.sales_wechat_id.in_(wechat_ids))
        
        pat = f"%{term}%"
        stmt = stmt.outerjoin(
            RawCustomer, SalesCustomerProfile.raw_customer_id == RawCustomer.id
        )
        return stmt.filter(
            or_(
                SalesCustomerProfile.raw_customer_id.ilike(pat),
                SalesCustomerProfile.sales_wechat_id.ilike(pat),
                SalesCustomerProfile.wechat_remark.ilike(pat),
                SalesCustomerProfile.ai_profile.ilike(pat),
                SalesCustomerProfile.title.ilike(pat),
                RawCustomer.customer_name.ilike(pat),
                RawCustomer.phone.ilike(pat),
                RawCustomer.phone_normalized.ilike(pat),
            )
        )

class ChatAdmin(AdminModelView, model=ChatMessage):
    column_list = [
        "id", "user", "raw_customer", "role", "content", 
        "rating", "is_copied", "created_at"
    ]

    column_default_sort = [(ChatMessage.created_at, True)]
    column_sortable_list = [ChatMessage.id, ChatMessage.created_at]

    column_filters = [
        UserIdLabelFilter(ChatMessage.user_id, title="发起员工"),
        LocalizedStaticValuesFilter(
            ChatMessage.role,
            title="身份",
            values=[
                ("user", "员工"),
                ("assistant", "AI"),
                ("system", "系统"),
            ],
        ),
        LocalizedStaticValuesFilter(
            ChatMessage.rating,
            title="质量反馈",
            values=[("1", "👍 赞"), ("-1", "👎 踩"), ("0", "➖ 未评")],
        ),
        LocalizedBooleanFilter(
            ChatMessage.is_copied,
            title="采纳状态",
            true_label="已采纳",
            false_label="未复制",
        ),
    ]

    def list_query(self, request):
        from sqlalchemy.orm import selectinload

        return super().list_query(request).options(
            selectinload(ChatMessage.user),
            selectinload(ChatMessage.raw_customer),
        )

    # 重写搜寻引擎逻辑，支持精确身份路由与多字段合并模糊搜索
    def search_query(self, stmt, term):
        from sqlalchemy import or_, func
        
        # 1. 显式执行表关联
        stmt = stmt.outerjoin(User, ChatMessage.user_id == User.id)
        stmt = stmt.outerjoin(RawCustomer, ChatMessage.raw_customer_id == RawCustomer.id)
        
        # 2. 精准路由：处理带有特定前缀的下钻链接 (来自关系表穿透)
        # 支持 user:{username}_phone:{phone} 格式或单字段格式
        if "user:" in term or "phone:" in term:
            filters = []
            if "user:" in term:
                # 提取 user 标识，可能是 user:01_phone:... 或仅 user:01
                u_part = term.split("user:")[1].split("_")[0]
                if u_part and u_part != "NULL":
                    filters.append(User.username == u_part)
            if "phone:" in term:
                p_part = term.split("phone:")[1].split("_")[0]
                if p_part and p_part != "NULL":
                    filters.append(RawCustomer.phone == p_part)
            
            if filters:
                from sqlalchemy import and_
                return stmt.filter(and_(*filters))
        
        # 3. 模糊搜寻逻辑
        search_term = f"%{term}%"
        return stmt.filter(
            or_(
                ChatMessage.content.ilike(search_term),
                User.username.ilike(search_term),
                User.real_name.ilike(search_term),
                RawCustomer.phone.ilike(search_term),
                RawCustomer.customer_name.ilike(search_term),
                RawCustomer.name.ilike(search_term),
                RawCustomer.remark.ilike(search_term),
            )
        )

    # 必须保留至少一个搜索项，否则 sqladmin 不会显示前端搜索框
    # 实际搜索逻辑由下方的 search_query 完全接管
    column_searchable_list = ["content"]
    
    category = ADMIN_CAT_CUSTOMERS
    name = "AI对话快调"
    name_plural = "AI对话历史"
    page_size = 100

    can_export = True
    column_export_list = ["id", "user.username", "raw_customer.phone", "role", "content", "rating", "is_copied", "created_at"]
    
    # 再次缩减宽度，限额 30 字符
    column_formatters = {
        "content": lambda m, a: (m.content[:30] + "...") if m.content and len(m.content) > 30 else m.content,
        "rating": lambda m, a: {1: "👍 赞", -1: "👎 踩", 0: "➖ 未评"}.get(m.rating, "➖"),
        "is_copied": lambda m, a: "✅ 已采纳" if m.is_copied else "⚪ 未复制",
        "raw_customer": lambda m, a: Markup(f'<a href="/admin/raw-customer-sales-wechat/list?search={m.raw_customer_id}">{m.raw_customer}</a>') if m.raw_customer else "—"
    }
    column_labels = {
        "user": "发起员工",
        "raw_customer": "客户对象",
        "user_id": "员工实体ID",
        "raw_customer_id": "客户实体ID",
        "role": "身份",
        "content": "对话内容抄录",
        "dify_conv_id": "对话ID",
        "rating": "质量反馈",
        "is_regenerated": "是否重发",
        "is_copied": "采纳状态",
        "feedback_at": "评价时间",
        "copied_at": "采纳时间",
        "created_at": "记录时间"
    }
    
    # raw_customers 数量很大：编辑/新建时若渲染 raw_customer 为普通下拉会卡顿；
    # 统一改为“输入搜索后再下拉”的异步选择。
    form_ajax_refs = {
        "raw_customer": {
            "fields": (
                "id",
                "customer_name",
                "phone",
                "phone_normalized",
                "unit_name",
                "unit_type",
            ),
            "order_by": "id",
        },
        "user": {
            "fields": ("username", "real_name"),
            "order_by": "username",
        },
    }

    async def _export_csv(self, data):
        from starlette.responses import StreamingResponse
        response = await super()._export_csv(data)
        
        async def wrap_content():
            yield b"\xef\xbb\xbf" # UTF-8 BOM
            async for chunk in response.body_iterator:
                if isinstance(chunk, str):
                    yield chunk.encode("utf-8")
                else:
                    yield chunk
                    
        return StreamingResponse(
            content=wrap_content(),
            media_type="text/csv; charset=utf-8",
            headers=dict(response.headers)
        )

class ProductAdmin(AdminModelView, model=Product):
    column_list = [Product.id, Product.product_name, Product.product_id, Product.price, Product.supplier_name]
    column_searchable_list = [Product.product_name, Product.product_id]
    column_sortable_list = [Product.id, Product.price]
    column_default_sort = [(Product.id, True)]
    column_filters = [
        DistinctColumnValuesFilter(Product.supplier_name, title="独家渠道商字号"),
    ]
    page_size = PAGE_SIZE
    category = ADMIN_CAT_MARKETING
    name = "公共商品池"
    name_plural = "商品资源管理"
    column_labels = {
        Product.uuid: "平台原生UUID",
        Product.product_id: "平台内部商品序列号",
        Product.product_name: "商品营销全名",
        Product.price: "爬取售价(元)",
        Product.cover_img: "CDN图床链接",
        Product.product_url: "官方购买详情页",
        Product.unit: "打包单位",
        Product.supplier_name: "独家渠道商字号",
        Product.supplier_id:"独家渠道商ID"
    }


class ProfileTagDefinitionAdmin(AdminModelView, model=ProfileTagDefinition):
    category = ADMIN_CAT_MARKETING
    name = "客户动态标签"
    name_plural = "客户动态标签（画像）"
    page_size = PAGE_SIZE

    column_list = [
        ProfileTagDefinition.id,
        ProfileTagDefinition.name,
        ProfileTagDefinition.is_active,
        ProfileTagDefinition.sort_order,
        ProfileTagDefinition.created_at,
    ]
    column_sortable_list = [ProfileTagDefinition.id, ProfileTagDefinition.sort_order, ProfileTagDefinition.created_at]
    column_searchable_list = [ProfileTagDefinition.name]
    column_default_sort = [(ProfileTagDefinition.sort_order, False), (ProfileTagDefinition.id, False)]
    column_labels = {
        ProfileTagDefinition.id: "ID",
        ProfileTagDefinition.name: "标签名称",
        ProfileTagDefinition.feature_note: "特征说明（给模型与销售参考）",
        ProfileTagDefinition.strategy_note: "策略说明（跟进/话术要点）",
        ProfileTagDefinition.sort_order: "排序（小在前）",
        ProfileTagDefinition.is_active: "启用（仅启用项参与画像）",
        ProfileTagDefinition.created_at: "创建时间",
    }


class ConfigAdmin(AdminModelView, model=SystemConfig):
    """
    可人工维护的配置项见 form_args「config_key」下拉里列出。

    未列入下拉的键（如 sync_*/wechat_*_status 等由定时任务/同步服务自动写入的
    状态、游标）不会出现在「新建」选项中，但数据库中若已存在，仍会在列表中展示
    并可按行编辑，避免把运行态行误当废弃数据清空。
    """

    column_list = [
        SystemConfig.id, 
        SystemConfig.config_key, 
        SystemConfig.config_value, 
        SystemConfig.description, 
        SystemConfig.config_group, 
        SystemConfig.updated_at
    ]
    category = ADMIN_CAT_SYSTEM
    name = "系统配置项"
    name_plural = "环境控制变量"
    page_size = PAGE_SIZE
    
    column_sortable_list = [
        SystemConfig.id,
        SystemConfig.updated_at,
    ]
    column_default_sort = [(SystemConfig.updated_at, True)]

    column_formatters = {
        "config_value": lambda m, a: (m.config_value[:50] + "...") if m.config_value and len(m.config_value) > 50 else m.config_value,
        "description": lambda m, a: m.description or ""  
    }
    
    # 彻底改写本表的行为逻辑
    form_overrides = {"config_key": SelectField}
    form_excluded_columns = [] # 允许修改备注
    
    form_args = {
        "config_key": {
            "choices": [
                ("supplier_ids", "832爬虫：配置商品货源铺子ID (多店用逗号相隔)"), 
                ("unit_type_choices", "字典：单位类型下拉项 (逗号相隔)"),
                ("admin_division_choices", "字典：行政区划下拉项 (逗号相隔)"),
                ("purchase_type_choices", "字典：采购类型下拉项 (逗号相隔)"),
                ("wechat_friends_sync_target_day", "微信原始池：手动同步默认/上次保存的目标自然日 (YYYY-MM-DD，上海时区)；定时任务固定今天+04:20补昨天"),
                ("wechat_open_partner_id", "微信原始池：开放平台 partnerId（空则使用环境变量 WECHAT_OPEN_ADMIN_PARTNER_ID）"),
                ("wechat_friends_query_mode", "微信原始池：增量接口 queryMode，填 updateTime 或 createTime"),
                ("llm_api_url", "AI（对话默认）：兼容 OpenAI 的 API Base URL（未给单模型配置 url 时使用）"),
                ("llm_api_key", "AI（对话默认）：API Key（未给单模型配置 key 时使用）"),
                ("llm_chat_model", "AI（对话）：桌面/API 默认对话模型（须出现在 llm_chat_models_list 中，可被请求体 chat_model 覆盖）"),
                (
                    "desktop_default_chat_models",
                    "桌面端：默认勾选模型（逗号分隔；如 deepseek-v3.2,qwen3.5-plus；本机未固定偏好时生效）",
                ),
                ("desktop_latest_version", "桌面端更新：最新版本号（例如 1.0.2）"),
                ("desktop_installer_url", "桌面端更新：安装包下载相对路径（例如 /downloads/WeChatAI_Assistant_Setup.exe）"),
                ("desktop_force_update", "桌面端更新：是否强制更新（true/false，默认 true）"),
                ("desktop_release_notes", "桌面端更新：版本更新说明/日志"),
                (
                    "llm_chat_models_list",
                    "AI（对话）：可选模型清单（推荐 JSON；支持为每个模型单独配置 api_url/api_key）"
                ),
                ("profile_llm_api_url", "AI（画像分析）：API Base URL（不配则回退 llm_api_url）"),
                ("profile_llm_api_key", "AI（画像分析）：API Key（不配则回退 llm_api_key）"),
                (
                    "profile_llm_model",
                    "AI（画像分析）：模型名；未配时回退旧键 llm_model（仅兼容存量）再回退 qwen-max",
                ),
                ("profile_audit_log", "AI（画像分析）：请求审计写日志（1/true 开启，默认关；日志体积与隐私风险大）"),
                ("use_db_prompts", "Prompt：是否启用数据库化提示词（1 启用 / 0 回退旧 prompts.py）"),
                ("llm_router_enabled", "AI（场景路由）：是否启用小模型分类（1 启用 / 0 仅 hint+兜底，默认 1）"),
                ("llm_router_model", "AI（场景路由）：分类用的小模型名（如 qwen-turbo / deepseek-chat；为空回退 llm_chat_model）"),
                ("llm_router_api_url", "AI（场景路由）：API Base URL（为空回退 llm_api_url）"),
                ("llm_router_api_key", "AI（场景路由）：API Key（为空回退 llm_api_key）"),
                ("ai_router_debug_log", "AI（场景路由）：测试期详细日志（1 开启 / 0 关闭，默认关）"),
                ("order_api_token", "画像分析：832订单同步接口 Token凭据 (有效期通常为30天)"),
            ],
            "label": "选择要定义的全局控制键"
        },
        # 允许提交空字符串：用于“清空覆盖项/回退环境变量”，例如 wechat_open_partner_id。
        # MySQL 列为 NOT NULL，但空串仍然是合法值。
        "config_value": {
            "validators": [WTFOptional()],
        },
    }

    # 作用域分组：按 config_group 筛选（越多配置越需要）
    column_filters = [
        LocalizedStaticValuesFilter(
            SystemConfig.config_group,
            title="作用域",
            values=[
                ("general", "general"),
                ("ai", "ai"),
                ("sync", "sync"),
                ("desktop", "desktop"),
                ("dict", "dict"),
                ("prompt", "prompt"),
                ("task", "task"),
            ],
        )
    ]

    async def on_model_change(self, data: dict, model: any, is_created: bool, request: any) -> None:
        """
        统一为常用配置项自动归类作用域（config_group），减少维护时的心智负担。
        仍允许人工在编辑页改 group（如有特殊需求）。
        """
        try:
            key = (data.get("config_key") or getattr(model, "config_key", "") or "").strip()
            grp = (data.get("config_group") or getattr(model, "config_group", "") or "").strip()
            if not key:
                return
            if not grp or grp == "general":
                if key in ("unit_type_choices", "admin_division_choices", "purchase_type_choices"):
                    data["config_group"] = "dict"
                elif key.startswith("wechat_") or key.startswith("sync_"):
                    data["config_group"] = "sync"
                elif key.startswith("profile_") or key.startswith("llm_") or key.startswith("use_db_prompts") or key.startswith("ai_router_"):
                    data["config_group"] = "ai"
                elif key.startswith("task_"):
                    data["config_group"] = "task"
                elif key.startswith("desktop_"):
                    data["config_group"] = "desktop"
        except Exception:
            pass

    # 编辑时 config_key 设为只读，防止 MySQL 报 Duplicate entry 错误
    form_widget_args = {
        "config_key": {"readonly": True}
    }
    
    column_labels = {
        SystemConfig.id: "内部序号（业务以左侧「内部指令通道」为准；勿依赖 id 连续性）",
        SystemConfig.config_key: "内部指令通道",
        SystemConfig.config_value: "在此输入对应指令生效的具体值",
        SystemConfig.config_group: "作用域隔离保护伞(general即代表根环境)",
        SystemConfig.description: "备注说明",
        SystemConfig.updated_at: "最后修改时间"
    }

class TransferAdmin(AdminModelView, model=BusinessTransfer):
    column_list = [BusinessTransfer.id, BusinessTransfer.from_user, BusinessTransfer.to_user, BusinessTransfer.transferred_count, BusinessTransfer.transfer_time]
    category = ADMIN_CAT_USERS
    name = "业务移交历史"
    name_plural = "客源流转记录"
    page_size = PAGE_SIZE
    
    column_labels = {
        BusinessTransfer.from_user: "我要交出人(From)",
        BusinessTransfer.to_user: "我要接收人(To)",
        BusinessTransfer.from_user_id: "交出人ID",
        BusinessTransfer.to_user_id: "接收人ID",
        BusinessTransfer.transferred_count: "移交客户成功数",
        BusinessTransfer.transfer_time: "操作发生时间",
        BusinessTransfer.operator: "操作人",
    }
    
    # 隐藏不应该由于人工干预填写的只读审计字段
    form_excluded_columns = [BusinessTransfer.transferred_count, BusinessTransfer.transfer_time, BusinessTransfer.operator]

    async def on_model_change(self, data: dict, model: any, is_created: bool, request: any) -> None:
        """
        拦截新增移交记录的行为，提取两方员工的 ID，进行业务客户移交转换
        """
        if is_created:
            # sqladmin 会把选择框结果以字段名为 key 的对象或者 fk_id 传回
            from_user_id = data.get("from_user_id") 
            to_user_id = data.get("to_user_id")
            
            if not from_user_id and "from_user" in data:
                from_user_id = data["from_user"].id if hasattr(data["from_user"], "id") else data["from_user"]
                
            if not to_user_id and "to_user" in data:
                to_user_id = data["to_user"].id if hasattr(data["to_user"], "id") else data["to_user"]

            if from_user_id and to_user_id and from_user_id != to_user_id:
                async with AsyncSessionLocal() as db:
                    u1_r = await db.execute(select(User).where(User.id == int(from_user_id)))
                    u1 = u1_r.scalars().first()
                    u2_r = await db.execute(select(User).where(User.id == int(to_user_id)))
                    u2 = u2_r.scalars().first()
                    
                    if u1 and u2:
                        count = await transfer_user_customers(db, u1.username, u2.username)
                        data["transferred_count"] = count
                        # 记录操作者身份
                        token = request.cookies.get("admin_token")
                        data["operator"] = "admin" # TODO 解析具体管理员 token，目前默认系统级别操作

class RawCustomerAdmin(AdminModelView, model=RawCustomerSalesWechat):
    """原始客户池（per-sales）：每行 = (raw_customer_id, sales_wechat_id) 好友快照。"""

    column_list = [
        RawCustomerSalesWechat.raw_customer_id,
        RawCustomerSalesWechat.sales_wechat_id,
        RawCustomerSalesWechat.remark,
        RawCustomerSalesWechat.name,
        RawCustomerSalesWechat.phone,
        "profile_status",
        "quick_action",
        RawCustomerSalesWechat.label,
        RawCustomerSalesWechat.last_chat_time,
        RawCustomerSalesWechat.synced_at,
    ]
    column_searchable_list = [
        RawCustomerSalesWechat.raw_customer_id,
        RawCustomerSalesWechat.sales_wechat_id,
        RawCustomerSalesWechat.name,
        RawCustomerSalesWechat.remark,
        RawCustomerSalesWechat.phone,
    ]
    page_size = PAGE_SIZE
    can_create = False
    can_delete = False

    category = ADMIN_CAT_CUSTOMERS
    name = "原始客户池"
    name_plural = "原始客户池"

    column_formatters = {
        "profile_status": lambda m, a: Markup(
            '<span class="badge bg-success">已分析</span>'
            if (getattr(getattr(m, "sales_profile", None), "profile_status", 0) or 0) == 1
            else '<span class="badge bg-secondary">未分析</span>'
        ),
        "quick_action": lambda m, a: Markup(
            f'<a class="btn btn-sm btn-outline-primary" '
            f'href="/admin/raw-customer-sales-wechat/action/run-ai-profile?pks={m.id}">'
            f'🔍 分析画像</a>'
        ),
    }

    column_labels = {
        RawCustomerSalesWechat.raw_customer_id: "客户ID(raw_customer_id)",
        RawCustomerSalesWechat.sales_wechat_id: "销售企微ID",
        RawCustomerSalesWechat.remark: "客户备注",
        RawCustomerSalesWechat.name: "昵称",
        RawCustomerSalesWechat.phone: "预存电话",
        "quick_action": "快捷操作",
        RawCustomerSalesWechat.label: "标签",
        RawCustomerSalesWechat.synced_at: "同步时间",
    }

    column_sortable_list = [
        RawCustomerSalesWechat.last_chat_time,
        RawCustomerSalesWechat.synced_at,
    ]
    column_default_sort = [(RawCustomerSalesWechat.synced_at, True)]

    column_filters = [
        ScpProfileStatusFilter(title="画像状态"),
        PhonePresenceFilter(RawCustomerSalesWechat.phone),
    ]

    # raw_customers 数量很大：编辑页若让 raw_customer 关系变成普通下拉会加载全量导致卡顿；
    # 使用 ajax refs 改为输入搜索后再下拉。
    form_ajax_refs = {
        "raw_customer": {
            "fields": (
                "id",
                "customer_name",
                "phone",
                "phone_normalized",
                "unit_name",
                "unit_type",
            ),
            "order_by": "id",
        }
    }

    def list_query(self, request):
        from sqlalchemy.orm import selectinload

        # 预加载 raw_customer，避免模板渲染时触发 lazy load 导致 DetachedInstanceError
        stmt = super().list_query(request).options(
            selectinload(RawCustomerSalesWechat.raw_customer),
            selectinload(RawCustomerSalesWechat.sales_profile),
        )
        sw = (getattr(request, "query_params", {}) or {}).get("sales_wechat_id")
        sw = (sw or "").strip()
        if not sw:
            return stmt
        return stmt.where(RawCustomerSalesWechat.sales_wechat_id == sw)

    @action(
        name="run_ai_profile",
        label="开始 AI 画像（选中）",
        confirmation_message="确定对选中的原始客户执行画像并同步到「客观客户库 / 销售跟进」吗？任务在后台排队执行，侧栏「AI 画像任务进度」可查看排队批次、实时进度、失败详情，并可在运行中中断当前批次（已在跑的单条会跑完）。",
        add_in_detail=True,
        add_in_list=True,
    )
    async def run_ai_profile(self, request):
        pks = request.query_params.get("pks", "").split(",")
        pks = [p.strip() for p in pks if p.strip()]
        if pks:
            async with AsyncSessionLocal() as db:
                # 关键修复：必须把 sales_wechat_id 一起传入画像任务，否则会丢“归属销售号”上下文，
                # 导致画像落到错误销售号/或 sales_wechat_id=NULL，进而桌面端列表 join 不上。
                stmt = select(
                    RawCustomerSalesWechat.raw_customer_id,
                    RawCustomerSalesWechat.sales_wechat_id,
                ).where(
                    RawCustomerSalesWechat.id.in_([int(pk) for pk in pks if pk.isdigit()])
                )
                res = await db.execute(stmt)
                from ai.raw_profiling import is_group_chat_customer

                pairs = [
                    (r[0], r[1])
                    for r in res.all()
                    if r and r[0] and r[1] and not is_group_chat_customer(r[0])
                ]
            from ai.raw_profiling import enqueue_profile_sales_pairs

            if pairs:
                await enqueue_profile_sales_pairs(pairs, "选中客户画像")
        from starlette.responses import RedirectResponse

        return RedirectResponse(url=request.url_for("admin:list", identity=self.identity))

    @action(
        name="run_ai_profile_all",
        label="分析所有未画像客户",
        confirmation_message=(
            "确定要开始分析尚未进行画像的记录吗？按每条 (客户, 销售企微) 判断："
            "无 sales_customer_profiles 或 profile_status=0 的才会入队（与列表「画像状态」一致）；"
            "不再按 raw_customers 全局去重。可在 URL 后附加 `?sales_wechat_id=wxid_xxx` 仅处理该号。"
            "任务消耗 API 额度；侧栏「AI 画像任务进度」可查看排队、进度、错误详情并中断当前批次。"
        ),
        add_in_detail=False,
        add_in_list=True,
    )
    async def run_ai_profile_all(self, request):
        from ai.raw_profiling import run_profile_all_unprofiled

        sw = await resolve_sales_wechat_id_for_rcsw_batch(request)
        filt = [sw] if sw else None
        await run_profile_all_unprofiled(sales_wechat_ids=filt)
        from starlette.responses import RedirectResponse

        return RedirectResponse(url=request.url_for("admin:list", identity=self.identity))

    @action(
        name="run_ai_profile_sales_all",
        label="分析指定企微ID的全部客户（含已分析）",
        confirmation_message=(
            "确定要对该销售企微ID下的【全部客户】重新执行画像吗？\n\n"
            "指定企微方式（任选其一即可）：① 列表 URL 带 `?sales_wechat_id=wxid_xxx`；"
            "② 先按该企微筛出列表再点本按钮（会从来源页解析）；"
            "③ 勾选若干行且这些行属同一 `sales_wechat_id`。\n\n"
            "注意：会覆盖已有 per-sales 画像并消耗 API；进度见侧栏「AI 画像任务进度」。"
        ),
        add_in_detail=False,
        add_in_list=True,
    )
    async def run_ai_profile_sales_all(self, request):
        """
        批任务：按 sales_wechat_id 重新画像其名下全部 per-sales 快照。
        与 run_ai_profile_all 的区别：
        - run_ai_profile_all 仅处理“未画像”（全库或指定号下每条 rcsw：基于 SCP 缺失或 profile_status=0）。
        - 本动作强制处理该 sales_wechat_id 下的所有客户（含已画像），用于“重跑/重置画像”。
        """
        sw = await resolve_sales_wechat_id_for_rcsw_batch(request)
        if not sw:
            from starlette.responses import RedirectResponse

            return RedirectResponse(url=request.url_for("admin:list", identity=self.identity))

        async with AsyncSessionLocal() as db:
            from ai.raw_profiling import rcsw_active_for_profile_where

            # 取该 sales_wechat_id 下的所有 (raw_customer_id, sales_wechat_id) pair（过滤已删好友与群聊）
            stmt = (
                select(
                    RawCustomerSalesWechat.raw_customer_id,
                    RawCustomerSalesWechat.sales_wechat_id,
                )
                .join(RawCustomer, RawCustomer.id == RawCustomerSalesWechat.raw_customer_id)
                .where(
                    RawCustomerSalesWechat.sales_wechat_id == sw,
                    rcsw_active_for_profile_where(),
                )
                .distinct()
            )
            res = await db.execute(stmt)
            pairs = [(r[0], r[1]) for r in res.all() if r and r[0] and r[1]]

        from ai.raw_profiling import enqueue_profile_sales_pairs

        if pairs:
            await enqueue_profile_sales_pairs(
                pairs,
                "指定企微ID全部客户（含已画像）",
            )

        from starlette.responses import RedirectResponse
        return RedirectResponse(url=request.url_for("admin:list", identity=self.identity))


class SyncFailureAdmin(AdminModelView, model=SyncFailure):
    name = "数据同步异常监控"
    name_plural = "数据同步异常监控"
    category = ADMIN_CAT_SYNC
    column_list = ["supplier_id", "last_error", "updated_at", "retry_action"]
    column_labels = {
        "supplier_id": "抓取失败的供货商 ID",
        "last_error": "具体报错详情",
        "updated_at": "异常发生时间",
        "retry_action": "快捷操作"
    }

    column_formatters = {
        "retry_action": lambda m, a: Markup(
            f'<a class="btn btn-sm btn-outline-success" '
            f'href="/admin/sync-failure/action/retry-sync?pks={m.id}">'
            f'🔄 立即重试</a>'
        )
    }
    
    # 增加自定义操作按钮 (保留批量操作能力)
    @action(
        name="retry_sync",
        label="立刻重试此供货商同步",
        confirmation_message="确定要为选中的供货商重新抓取数据吗？",
        add_in_detail=True,
        add_in_list=True,
    )
    async def retry_sync(self, request):
        pks = request.query_params.get("pks", "").split(",")
        if pks:
            from core.tasks import fetch_and_sync_832_products
            async with AsyncSessionLocal() as db:
                for pk in pks:
                    res = await db.execute(select(SyncFailure).where(SyncFailure.id == int(pk)))
                    item = res.scalars().first()
                    if item:
                        import asyncio
                        asyncio.create_task(fetch_and_sync_832_products(item.supplier_id))
            
        # 使用重定向返回列表页
        from starlette.responses import RedirectResponse
        return RedirectResponse(url=request.url_for("admin:list", identity=self.identity))

# ============ 提示词场景管理（DB 化 system prompt 与参考话术文档） ============


def _router_lifecycle_from_hints(hints: dict) -> str:
    conds = hints.get("customer_conditions") if isinstance(hints, dict) else None
    if not isinstance(conds, dict):
        return "any"
    for cond in conds.get("all") or []:
        if isinstance(cond, dict) and cond.get("field") == "customer_lifecycle":
            return str(cond.get("value") or "any")
    return "any"


def _router_intent_from_hints(hints: dict) -> str:
    conds = hints.get("customer_conditions") if isinstance(hints, dict) else None
    if not isinstance(conds, dict):
        return "any"
    for cond in conds.get("all") or []:
        if isinstance(cond, dict) and cond.get("field") == "intent_band":
            return str(cond.get("value") or "any")
    return "any"


def _build_router_customer_conditions(lifecycle: str, intent: str) -> dict | None:
    all_conds: list[dict] = []
    if lifecycle and lifecycle not in ("", "any"):
        all_conds.append({"field": "customer_lifecycle", "op": "eq", "value": lifecycle})
    if intent and intent not in ("", "any"):
        all_conds.append({"field": "intent_band", "op": "eq", "value": intent})
    if not all_conds:
        return None
    return {"all": all_conds}


class PromptScenarioAdmin(AdminModelView, model=PromptScenario):
    """业务"场景"：每个 scenario_key 对应一套 system prompt 版本序列。

    新增流程：
    1) 在本页『新增』→填写英文 key（如 custom_scene）+ 中文名 + 描述；
    2) 在底部『路由命中规则』中填关键词/示例/反例等（留空即不参与路由器自动分流，
       只能通过桌面端下拉显式选中）；
    3) 到『提示词版本』新增一行：所属场景选这条，版本号=1，状态 draft；
       在『System 模板』里写好提示词正文并勾选要注入的文档；
    4) 用 /api/prompt/versions/{id}/publish 把版本发布成 published；
    5) 完成。新场景一旦 enabled + 有 published 版本，SceneRouter 立即把它纳入候选，
       无需改任何代码。
    """
    name = "提示词场景"
    name_plural = "提示词场景"
    category = ADMIN_CAT_PROMPTS
    page_size = PAGE_SIZE

    column_list = [
        PromptScenario.id,
        PromptScenario.scenario_key,
        PromptScenario.name,
        PromptScenario.ui_category,
        PromptScenario.enabled,
        PromptScenario.tools_enabled,
        "router_status",
        "published_version",
        PromptScenario.updated_at,
    ]
    column_searchable_list = [PromptScenario.scenario_key, PromptScenario.name]
    column_sortable_list = [PromptScenario.id, PromptScenario.updated_at]
    column_default_sort = [(PromptScenario.updated_at, True)]
    column_filters = [
        LocalizedStaticValuesFilter(
            PromptScenario.ui_category,
            title="界面分类",
            values=[
                ("free_chat", "自由对话"),
                ("customer_chat", "客户对话"),
                ("backend_only", "仅后台"),
            ],
        ),
        LocalizedBooleanFilter(
            PromptScenario.enabled,
            title="启用",
            true_label="已启用",
            false_label="已关闭",
        ),
    ]
    column_labels = {
        PromptScenario.id: "ID",
        PromptScenario.scenario_key: "场景 Key",
        PromptScenario.name: "名称",
        PromptScenario.description: "描述",
        PromptScenario.enabled: "启用",
        PromptScenario.tools_enabled: "允许 Function Call",
        PromptScenario.ui_category: "界面分类",
        PromptScenario.router_hints_json: "路由候选约束（自动组装）",
        PromptScenario.created_at: "创建时间",
        PromptScenario.updated_at: "更新时间",
        "published_version": "当前线上版本",
        "router_status": "路由候选约束",
    }

    column_formatters = {
        "published_version": lambda m, a: Markup(
            "<span class='text-muted'>（待运行期计算）</span>"
        ),
        "router_status": lambda m, a: Markup(
            '<span class="badge bg-success">已配置</span>'
            if isinstance(m.router_hints_json, dict) and (
                m.router_hints_json.get("examples")
                or m.router_hints_json.get("customer_conditions")
                or m.router_hints_json.get("auxiliary_scenarios")
            )
            else '<span class="badge bg-secondary">未配置</span>'
        ),
        PromptScenario.enabled: lambda m, a: "✅" if m.enabled else "⛔️",
        PromptScenario.tools_enabled: lambda m, a: "🛠️" if m.tools_enabled else "—",
    }

    form_args = {
        "scenario_key": {
            "label": "场景 Key（英文，唯一）",
            "description": "示例：general_chat / product_recommend / custom_xxx。建站用，不建议改。",
        },
        "name": {"label": "场景名称（中文）", "description": "在前端/后台列表展示。"},
        "description": {"label": "描述", "description": "供运营/产品查看的说明；也会喂给小模型分类器作为场景说明。"},
        "enabled": {"label": "是否启用", "description": "关闭后，该场景的所有请求会 fallback（若仍配置了回退路径）。"},
        "tools_enabled": {
            "label": "允许 Function Call",
            "description": "开启后这个场景的 AI 可以触发后端工具（修改客户资料、记录采购计划等）。",
        },
        "ui_category": {
            "label": "界面分类",
            "description": "free_chat=桌面自由对话；customer_chat=桌面客户对话；backend_only=仅后台（如画像分析）。",
        },
    }
    # router_hints_json 由下方虚拟字段组装，不直接暴露原始 JSON
    form_excluded_columns = ["versions", "created_at", "updated_at", "router_hints_json"]

    async def scaffold_form(self, rules=None):
        form_class = await super().scaffold_form(rules)

        _MONO_STYLE = (
            "width:100%; display:block; resize:vertical; "
            "font-family: ui-monospace, Menlo, Consolas, monospace; "
            "font-size: 13px; line-height: 1.5;"
        )

        class ExtendedForm(form_class):  # type: ignore[misc, valid-type]
            router_examples = TextAreaField(
                label="路由 · 正例发言（每行一个，用于小模型 few-shot）",
                validators=[WTFOptional()],
                description="销售员最像本场景的发言示范。会作为 few-shot 喂给小模型分类器。",
                render_kw={
                    "rows": 4,
                    "class": "form-control",
                    "style": _MONO_STYLE,
                    "placeholder": "帮我给客户推几款符合预算的茶叶礼盒\n客户问哪个型号好",
                },
            )
            router_anti_examples = TextAreaField(
                label="路由 · 反例发言（每行一个，用于小模型 few-shot）",
                validators=[WTFOptional()],
                description="销售员不应该匹配本场景的发言示范。",
                render_kw={
                    "rows": 3,
                    "class": "form-control",
                    "style": _MONO_STYLE,
                    "placeholder": "这个怎么退货",
                },
            )
            router_requires_customer = SelectField(
                label="路由 · 是否要求当前已选客户",
                choices=[
                    ("none", "不限制（默认）"),
                    ("true", "必须已选客户（否则跳过本场景）"),
                    ("false", "必须未选客户（自由对话专用）"),
                ],
                default="none",
                description="桌面端按 ui_category 已做一层分流；此项可在场景内再加一道保险。",
            )
            router_priority = IntegerField(
                label="路由 · 优先级",
                validators=[WTFOptional(), NumberRange(min=-100, max=1000)],
                default=0,
                description="同分场景按本字段降序裁决；通常 0 即可，需要『兜底场景』时设为负数。",
                render_kw={"class": "form-control"},
            )
            router_customer_lifecycle = SelectField(
                label="路由 · 客户生命周期",
                choices=[
                    ("any", "不限制"),
                    ("new_friend", "新好友/新客"),
                    ("active_old", "活跃老客户"),
                    ("dormant_old", "沉睡老客户"),
                    ("unknown", "未知"),
                ],
                default="any",
                description="写入 customer_conditions；仅当 RouteContext 判定一致时才参与本场景候选。",
            )
            router_intent_band = SelectField(
                label="路由 · 意向档位",
                choices=[
                    ("any", "不限制"),
                    ("20", "20 分冷线索"),
                    ("30", "30 分意向"),
                    ("40", "40 分高意向"),
                    ("unknown", "未知"),
                ],
                default="any",
                description="写入 customer_conditions；与生命周期条件为 AND 关系。",
            )
            router_auxiliary_scenarios = TextAreaField(
                label="路由 · 默认辅场景 Key（每行一个）",
                validators=[WTFOptional()],
                description="命中本场景为主场景时默认叠加的其它 scenario_key，例如 order_guide。",
                render_kw={
                    "rows": 3,
                    "class": "form-control",
                    "style": _MONO_STYLE,
                    "placeholder": "order_guide",
                },
            )

            def __init__(self, formdata=None, obj=None, prefix="", data=None, meta=None, **kwargs):
                """编辑场景时把 router_hints_json 反向拆到 6 个虚拟字段。

                sqladmin 0.24 在渲染编辑页时调用 Form(obj=model, data={...})，formdata 为 None；
                WTForms 仅按字段名从 obj 同名属性取值，而 router_keywords 等虚拟字段在 model 上
                根本不存在，所以原本一直显示为空（旧版用 edit_form override 试图反填，但 sqladmin
                0.24 没有这个 hook，等于死代码）。
                现在在 form 自己的 __init__ 里做这件事：只在初次渲染（formdata is None）时回填，
                POST 提交（formdata 非 None）时不再覆盖用户输入。
                """
                super().__init__(formdata, obj, prefix, data, meta, **kwargs)
                if formdata is not None or obj is None:
                    return
                hints = getattr(obj, "router_hints_json", None)
                if not isinstance(hints, dict):
                    return
                self.router_examples.data = "\n".join(
                    [str(x) for x in (hints.get("examples") or [])]
                )
                self.router_anti_examples.data = "\n".join(
                    [str(x) for x in (hints.get("anti_examples") or [])]
                )
                req = hints.get("requires_customer")
                self.router_requires_customer.data = (
                    "true" if req is True else ("false" if req is False else "none")
                )
                try:
                    self.router_priority.data = int(hints.get("priority") or 0)
                except (TypeError, ValueError):
                    self.router_priority.data = 0
                self.router_customer_lifecycle.data = _router_lifecycle_from_hints(hints)
                self.router_intent_band.data = _router_intent_from_hints(hints)
                self.router_auxiliary_scenarios.data = "\n".join(
                    [str(x) for x in (hints.get("auxiliary_scenarios") or []) if str(x).strip()]
                )

        return ExtendedForm

    async def on_model_change(self, data: dict, model, is_created, request) -> None:
        ex_raw = data.pop("router_examples", "") or ""
        anti_ex_raw = data.pop("router_anti_examples", "") or ""
        req_cust_raw = (data.pop("router_requires_customer", "none") or "none").strip()
        prio_raw = data.pop("router_priority", None)
        lifecycle_raw = (data.pop("router_customer_lifecycle", "any") or "any").strip()
        intent_raw = (data.pop("router_intent_band", "any") or "any").strip()
        aux_raw = data.pop("router_auxiliary_scenarios", "") or ""
        data.pop("router_keywords", None)
        data.pop("router_keyword_refs", None)
        data.pop("router_anti_keywords", None)

        def _split_lines(text: str) -> list[str]:
            return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

        hints: dict = {}
        exs = _split_lines(ex_raw)
        anti_exs = _split_lines(anti_ex_raw)
        if exs:
            hints["examples"] = exs
        if anti_exs:
            hints["anti_examples"] = anti_exs
        if req_cust_raw == "true":
            hints["requires_customer"] = True
        elif req_cust_raw == "false":
            hints["requires_customer"] = False
        try:
            prio = int(prio_raw) if prio_raw not in (None, "") else 0
        except (TypeError, ValueError):
            prio = 0
        if prio:
            hints["priority"] = prio
        conds = _build_router_customer_conditions(lifecycle_raw, intent_raw)
        if conds:
            hints["customer_conditions"] = conds
        aux_keys = _split_lines(aux_raw)
        if aux_keys:
            hints["auxiliary_scenarios"] = aux_keys
        # 关键：必须 dict(...) 浅拷贝出新引用——若直接拿 model.router_hints_json 原地 pop/update，
        # SQLAlchemy 的 JSON 列默认不监听 dict in-place 变化，会判定"未变化"而跳过 UPDATE，
        # 表现为"表单看似改了，刷新后仍是默认值"。
        existing_raw = (
            model.router_hints_json
            if isinstance(getattr(model, "router_hints_json", None), dict)
            else {}
        )
        existing = dict(existing_raw)
        for k in (
            "keywords",
            "keyword_refs",
            "anti_keywords",
            "examples",
            "anti_examples",
            "requires_customer",
            "priority",
            "customer_conditions",
            "auxiliary_scenarios",
        ):
            existing.pop(k, None)
        existing.update(hints)
        # 完全空时落 None，便于和"未配置"区分（DB 列允许 NULL）
        new_hints = existing or None
        data["router_hints_json"] = new_hints
        # 双保险：sqladmin 不同版本对 form_excluded_columns 字段的 data→model 同步行为不一致，
        # 直接把新值写到 model 上，保证 commit 阶段 SQLAlchemy 能看到属性变更。
        model.router_hints_json = new_hints

    async def after_model_change(self, data: dict, model, is_created, request) -> None:
        try:
            from ai.prompt_store import get_prompt_store
            await get_prompt_store().invalidate_scenario(model.scenario_key)
        except Exception:
            pass


# ---- 提示词版本表单辅助：可注入变量清单（与 prompt_renderer 对齐） ----

# 这些变量由 PromptService/Renderer 在运行期从 ctx 里填入，
# 模板中使用 {{var}} 占位；勾选"快捷插入"后，若模板未出现该占位符，
# 保存时会在 system 末尾自动追加一个 "## 标题\n{{var}}\n" 块，免得写错变量名。
PROMPT_VARIABLE_CHOICES: list[tuple[str, str]] = [
    ("doc_block", "参考话术文档块（会被勾选的文档替换）"),
    ("current_date", "当前日期（系统注入：如 2026年04月23日）"),
    ("customer_card", "当前客户信息（customer_card）"),
    ("ai_profile", "客户 AI 画像（ai_profile）"),
    ("order_summary", "历史订单摘要（order_summary）"),
    ("chat_summary", "近期微信沟通记录（chat_summary）"),
    ("budget_amount", "预计单笔预算（budget_amount）"),
    ("purchase_type", "采购类型（purchase_type）"),
    ("basic_info", "画像：客户基础信息（basic_info）"),
    ("chat_context", "画像：最近聊天记录原文（chat_context）"),
    ("order_context", "画像：订单历史拼接文本（order_context）"),
    ("profile_tags_detail", "客户动态标签及跟进策略（profile_tags_detail）"),
]
PROMPT_VARIABLE_CHOICES.extend(ROUTER_PROMPT_VARIABLE_CHOICES)

PROMPT_VARIABLE_TITLES: dict[str, str] = {
    "doc_block": "参考话术",
    "current_date": "当前日期",
    "customer_card": "当前客户信息",
    "ai_profile": "客户 AI 画像",
    "order_summary": "历史订单记录",
    "chat_summary": "近期微信沟通记录",
    "budget_amount": "预计单笔预算",
    "purchase_type": "采购类型",
    "basic_info": "客户基础信息",
    "chat_context": "最近聊天记录",
    "order_context": "订单历史记录",
    "profile_tags_detail": "客户动态标签及跟进策略",
}
PROMPT_VARIABLE_TITLES.update(ROUTER_PROMPT_VARIABLE_TITLES)


class BootstrapCheckboxListWidget:
    """把 SelectMultipleField 渲染成 Bootstrap 风格的竖排复选框列表。

    WTForms 默认的 ListWidget 只吐 <ul><li> 而不带任何 Bootstrap 类，渲染出来
    会看起来全挤在一起（复选框贴标签、没有间距）。这里直接包一层
    .form-check 容器并给 <input>/<label> 补上标准类，让它在 sqladmin 里和其它
    布尔字段的观感一致。
    """

    def __call__(self, field, **kwargs):
        """
        注意：SelectMultipleField 的迭代对象不是 checkbox subfield，而是 option。
        所以这里必须用 field.iter_choices() 自己拼 <input type="checkbox">，否则会渲染错控件。
        """
        from markupsafe import Markup, escape

        container_cls = "sa-checkbox-list"
        html: list[str] = [f'<div class="{container_cls}" id="{escape(field.id)}">']

        i = 0
        for choice in field.iter_choices():
            # SelectMultipleField.iter_choices() 在 WTForms 里可能返回 3/4 元组：
            # (value, label, selected) 或 (value, label, selected, render_kw)
            value = choice[0]
            label = choice[1]
            checked = bool(choice[2])
            # 生成稳定的 id，便于 label 的 for 指向
            opt_id = f"{field.id}-{i}"
            i += 1

            checked_attr = " checked" if checked else ""
            value_attr = escape(value)
            label_text = escape(label)

            html.append('<div class="form-check" style="margin-bottom:.25rem;">')
            html.append(
                f'<input class="form-check-input" type="checkbox"'
                f' name="{escape(field.name)}" id="{escape(opt_id)}" value="{value_attr}"{checked_attr}>'
            )
            html.append(
                f'<label class="form-check-label" for="{escape(opt_id)}">{label_text}</label>'
            )
            html.append("</div>")

        html.append("</div>")
        return Markup("".join(html))


class MultiCheckboxField(SelectMultipleField):
    """渲染为"竖排复选框"的多选字段；供文档注入与变量插入共用。"""
    widget = BootstrapCheckboxListWidget()


class PromptVersionAdmin(AdminModelView, model=PromptVersion):
    """场景的 prompt 版本：draft / published / archived。

    编辑要点：
    - "System 模板" 直接写纯文本 / markdown，占位符用 {{customer_card}} 这样的形式；
    - "参考话术文档注入" 用复选框勾选即可，保存时自动转成 doc_refs_json；
    - "快捷插入变量" 用复选框，对模板里还未出现的占位符会自动追加一个标题块；
    - 发布/回滚请走 /api/prompt 管理 API（会归档上一个 published 并失效缓存）。
    """
    name = "提示词版本"
    name_plural = "提示词版本"
    category = ADMIN_CAT_PROMPTS
    page_size = PAGE_SIZE

    column_list = [
        PromptVersion.id,
        "scenario",
        PromptVersion.version,
        PromptVersion.status,
        "template_preview",
        PromptVersion.created_at,
        PromptVersion.published_at,
    ]
    column_searchable_list = [PromptVersion.status]
    column_sortable_list = [
        PromptVersion.id,
        PromptVersion.version,
        PromptVersion.created_at,
        PromptVersion.published_at,
    ]
    column_default_sort = [(PromptVersion.created_at, True)]
    column_filters = [
        LocalizedStaticValuesFilter(
            PromptVersion.status,
            title="状态",
            values=_PROMPT_STATUS_FILTER_VALUES,
        ),
    ]
    column_labels = {
        PromptVersion.id: "ID",
        "scenario": "所属场景",
        PromptVersion.version: "版本号",
        PromptVersion.status: "状态",
        PromptVersion.template_json: "系统模板（自动组装）",
        PromptVersion.doc_refs_json: "文档注入（自动组装）",
        PromptVersion.params_json: "参数覆盖 (JSON)",
        PromptVersion.rollout_json: "灰度策略 (JSON，Phase3)",
        PromptVersion.notes: "备注",
        PromptVersion.created_by: "创建人 ID",
        PromptVersion.created_at: "创建时间",
        PromptVersion.published_at: "发布时间",
        "template_preview": "模板预览",
    }
    column_formatters = {
        "template_preview": lambda m, a: Markup(
            f"<code>{(m.template_json or {}).get('system','')[:80]}…</code>"
        ) if m.template_json else "",
        PromptVersion.status: lambda m, a: Markup({
            "draft": '<span class="badge bg-secondary">draft</span>',
            "published": '<span class="badge bg-success">published</span>',
            "archived": '<span class="badge bg-dark">archived</span>',
        }.get(m.status, m.status)),
    }

    # 不再直接暴露 JSON 字段，由下方自定义字段替代
    form_excluded_columns = [
        "created_at",
        "published_at",
        "template_json",
        "doc_refs_json",
    ]

    form_args = {
        "scenario": {"label": "所属场景", "description": "每个场景同时只能有一条 published 版本。"},
        "version": {"label": "版本号", "description": "同场景下唯一；克隆当前线上版本建议使用 /api/prompt 接口。"},
        "status": {"label": "状态（draft/published/archived）", "description": "新建请保持 draft；发布走 /api/prompt/versions/{id}/publish。"},
        "params_json": {"label": "参数覆盖 (JSON)", "description": '可选，示例：{"temperature":0.6,"max_tokens":1024}'},
        "rollout_json": {"label": "灰度策略 (JSON)", "description": "Phase3 用，MVP 留空即可。"},
        "notes": {"label": "备注", "description": "例如：修订原因、上线时间等。"},
        "created_by": {"label": "创建人 ID"},
    }

    async def _publish_like(self, version_id: int, *, action: str) -> tuple[bool, str]:
        """
        action: "publish" | "rollback"
        在管理后台直接把某个版本置为 published，并归档同场景旧 published。
        """
        from datetime import datetime
        from sqlalchemy import update
        try:
            async with AsyncSessionLocal() as db:
                v_res = await db.execute(select(PromptVersion).where(PromptVersion.id == int(version_id)))
                v = v_res.scalars().first()
                if not v:
                    return False, "版本不存在"

                sc_res = await db.execute(select(PromptScenario).where(PromptScenario.id == v.scenario_id))
                sc = sc_res.scalars().first()
                if not sc:
                    return False, "场景不存在"

                # 归档同场景当前 published
                await db.execute(
                    update(PromptVersion)
                    .where(PromptVersion.scenario_id == v.scenario_id)
                    .where(PromptVersion.status == "published")
                    .values(status="archived")
                )
                v.status = "published"
                v.published_at = datetime.now()

                # 审计（actor_id 在 sqladmin 侧不好取，先留空）
                try:
                    db.add(PromptAuditLog(
                        actor_id=None,
                        action=f"admin_ui.version.{action}",
                        target_type="version",
                        target_id=v.id,
                        payload_json={"scenario_id": v.scenario_id, "version": v.version},
                    ))
                except Exception:
                    pass

                await db.commit()

            # 失效缓存
            try:
                from ai.prompt_store import get_prompt_store
                await get_prompt_store().invalidate_scenario(sc.scenario_key)
            except Exception:
                pass

            return True, f"已将 V{v.version} 设为 published"
        except Exception as e:
            return False, f"操作失败：{e}"

    @action(
        name="publish_version",
        label="发布为线上版本（选中 1 条）",
        confirmation_message="确定发布选中的版本吗？同场景当前线上版本将自动归档，并立即生效。",
        add_in_list=True,
        add_in_detail=True,
    )
    async def publish_version(self, request):
        from starlette.responses import RedirectResponse
        pks = request.query_params.get("pks", "").split(",")
        pks = [p.strip() for p in pks if p.strip()]
        if not pks:
            return RedirectResponse(url=request.url_for("admin:list", identity=self.identity))
        ok, _msg = await self._publish_like(int(pks[0]), action="publish")
        return RedirectResponse(url=request.url_for("admin:list", identity=self.identity))

    @action(
        name="rollback_version",
        label="回滚到该版本（选中 1 条）",
        confirmation_message="确定把选中的历史版本回滚为线上版本吗？同场景当前线上版本将自动归档，并立即生效。",
        add_in_list=True,
        add_in_detail=True,
    )
    async def rollback_version(self, request):
        from starlette.responses import RedirectResponse
        pks = request.query_params.get("pks", "").split(",")
        pks = [p.strip() for p in pks if p.strip()]
        if not pks:
            return RedirectResponse(url=request.url_for("admin:list", identity=self.identity))
        ok, _msg = await self._publish_like(int(pks[0]), action="rollback")
        return RedirectResponse(url=request.url_for("admin:list", identity=self.identity))

    async def scaffold_form(self, rules=None):
        form_class = await super().scaffold_form(rules)

        # 去数据库拉最新文档清单，作为"文档注入"的复选框选项
        doc_choices: list[tuple[str, str]] = []
        try:
            async with AsyncSessionLocal() as db:
                res = await db.execute(select(PromptDoc).order_by(PromptDoc.id.asc()))
                for d in res.scalars().all():
                    doc_choices.append((d.doc_key, f"{d.name}（key={d.doc_key}）"))
        except Exception:
            # 表不存在时也不要阻塞表单
            doc_choices = []

        # sqladmin 的 _macros.html 只在"字段有校验错误"时给 <textarea>/<input> 补
        # class="form-control"；无错误时直接调用 field()。所以自定义字段必须自己带
        # form-control 才能拿到 Bootstrap 的 width:100%，否则会塌成默认小宽度，
        # 视觉上就像被后面的 description 挤窄。
        class ExtendedForm(form_class):  # type: ignore[misc, valid-type]
            template_system = TextAreaField(
                label="System 模板（纯文本 / markdown，占位符用 {{var}}）",
                validators=[InputRequired(message="请填写 system 模板正文")],
                description=(
                    "直接写纯文本或 markdown；可用占位符见下方"
                    "「快捷插入变量」清单，运行时会自动替换，缺失走默认兜底。"
                ),
                render_kw={
                    "rows": 22,
                    "class": "form-control",
                    "style": (
                        "width:100%; display:block; resize:vertical; "
                        "font-family: ui-monospace, Menlo, Consolas, monospace; "
                        "font-size: 13px; line-height: 1.5;"
                    ),
                    "placeholder": (
                        "例如：\n"
                        "你是一位经验丰富的农产品销售顾问...\n"
                        "{{doc_block}}\n"
                        "## 当前日期\n{{current_date}}\n"
                        "## 当前客户信息\n{{customer_card}}\n"
                        "..."
                    ),
                },
            )
            template_notes = StringField(
                label="模板备注（存入 template_json.notes）",
                validators=[WTFOptional()],
                description="记录本模板的调整思路，不会发给模型。",
                render_kw={"class": "form-control"},
            )
            template_user = TextAreaField(
                label="User 模板（可选，如客户画像场景）",
                validators=[WTFOptional()],
                description=(
                    "若填写，将作为第二条 user 消息发送（在 system 之后）。"
                    "客户画像（customer_profile）与场景路由分类器（ai_scene_router）请在此填写任务与变量块。"
                ),
                render_kw={
                    "rows": 18,
                    "class": "form-control",
                    "style": (
                        "width:100%; display:block; resize:vertical; "
                        "font-family: ui-monospace, Menlo, Consolas, monospace; "
                        "font-size: 13px; line-height: 1.5;"
                    ),
                },
            )
            doc_refs_keys = MultiCheckboxField(
                label="参考话术文档注入",
                choices=doc_choices,
                validators=[WTFOptional()],
                description=(
                    "勾选后会把文档 published 版本拼进 {{doc_block}} 位置；"
                    "没有 {{doc_block}} 则追加到 system 末尾。细粒度（title/"
                    "max_chars/required）请用 /api/prompt 接口维护。"
                ),
            )
            insert_variables = MultiCheckboxField(
                label="快捷插入变量",
                choices=PROMPT_VARIABLE_CHOICES,
                validators=[WTFOptional()],
                description=(
                    "保存时若模板中未出现对应 {{var}}，将自动追加一个『## 标题』块；"
                    "已存在的占位符会跳过，不会删除或覆盖你已写的内容。"
                ),
            )

            def __init__(self, formdata=None, obj=None, prefix="", data=None, meta=None, **kwargs):
                """编辑已发布版本时，把 template_json / doc_refs_json 反向拆到虚拟字段。

                sqladmin 0.24 实例化编辑表单时只能按字段名从 model 同名属性取值，
                但 template_system / template_user / template_notes / doc_refs_keys
                在 PromptVersion 上根本不存在。若不在此处补一刀，运营点开"编辑版本"
                会看到一片空白；直接保存就会把整段 template_json 清空——风险很大。

                注意：insert_variables 是『动作命令』而非『存储状态』（保存时把未出现的
                占位符追加到 system/user 末尾），所以编辑表单上每次默认不勾选任何一项，
                不做反填。
                """
                super().__init__(formdata, obj, prefix, data, meta, **kwargs)
                if formdata is not None or obj is None:
                    return

                tj = getattr(obj, "template_json", None)
                if isinstance(tj, dict):
                    self.template_system.data = str(tj.get("system") or "")
                    self.template_user.data = str(tj.get("user") or "")
                    self.template_notes.data = str(tj.get("notes") or "")

                refs = getattr(obj, "doc_refs_json", None)
                if isinstance(refs, list):
                    self.doc_refs_keys.data = [
                        item.get("doc_key")
                        for item in refs
                        if isinstance(item, dict) and item.get("doc_key")
                    ]

        return ExtendedForm

    async def on_model_change(self, data: dict, model, is_created, request) -> None:
        # 1) 取出虚拟字段（它们不是 ORM 列，必须在 setattr 阶段前 pop 掉）
        system_text = (data.pop("template_system", "") or "").rstrip()
        notes = (data.pop("template_notes", "") or "").strip()
        user_text = (data.pop("template_user", "") or "").rstrip()
        doc_keys: list[str] = list(data.pop("doc_refs_keys", []) or [])
        insert_vars: list[str] = list(data.pop("insert_variables", []) or [])

        # 2) 按勾选自动追加还未出现的变量块（仅追加，不删除）
        for var_name in insert_vars:
            placeholder = "{{" + var_name + "}}"
            if placeholder in system_text:
                continue
            title = PROMPT_VARIABLE_TITLES.get(var_name, var_name)
            if var_name == "doc_block":
                # doc_block 约定放在第一行之后
                system_text = (placeholder + "\n" + system_text).strip("\n")
            else:
                system_text = system_text.rstrip() + f"\n\n## {title}\n{placeholder}\n"

        for var_name in insert_vars:
            placeholder = "{{" + var_name + "}}"
            if placeholder in user_text:
                continue
            title = PROMPT_VARIABLE_TITLES.get(var_name, var_name)
            if var_name == "doc_block":
                user_text = (placeholder + "\n" + user_text).strip("\n")
            else:
                user_text = user_text.rstrip() + f"\n\n## {title}\n{placeholder}\n"

        # 3) 组装 template_json：保留现有其它键（如未来扩展的字段）
        base_tj = dict(model.template_json) if isinstance(getattr(model, "template_json", None), dict) else {}
        base_tj["system"] = system_text or ""
        if user_text:
            base_tj["user"] = user_text
        else:
            base_tj.pop("user", None)
        if notes:
            base_tj["notes"] = notes
        else:
            base_tj.pop("notes", None)
        data["template_json"] = base_tj

        # 4) 组装 doc_refs_json：按选中的 key 去 PromptDoc 查名字作为 title
        if doc_keys:
            try:
                async with AsyncSessionLocal() as db:
                    res = await db.execute(
                        select(PromptDoc).where(PromptDoc.doc_key.in_(doc_keys))
                    )
                    docs_by_key = {d.doc_key: d for d in res.scalars().all()}
            except Exception:
                docs_by_key = {}
            existing_map = {}
            if isinstance(getattr(model, "doc_refs_json", None), list):
                for item in model.doc_refs_json:
                    if isinstance(item, dict) and item.get("doc_key"):
                        existing_map[item["doc_key"]] = item
            refs: list[dict] = []
            for k in doc_keys:
                prev = existing_map.get(k) or {}
                d_obj = docs_by_key.get(k)
                refs.append({
                    "doc_key": k,
                    "title": prev.get("title") or (d_obj.name if d_obj else k),
                    "required": bool(prev.get("required", False)),
                    "max_chars": prev.get("max_chars"),
                    "doc_version_id": prev.get("doc_version_id"),
                })
            data["doc_refs_json"] = refs
        else:
            data["doc_refs_json"] = []

    async def after_model_change(self, data: dict, model, is_created, request) -> None:
        try:
            from ai.prompt_store import get_prompt_store
            sc = model.scenario
            if sc:
                await get_prompt_store().invalidate_scenario(sc.scenario_key)
        except Exception:
            pass


class PromptDocAdmin(AdminModelView, model=PromptDoc):
    """参考话术文档主表：doc_key 要与 PromptVersion.doc_refs_json 中的 key 对齐。"""
    name = "参考话术文档"
    name_plural = "参考话术文档"
    category = ADMIN_CAT_PROMPTS
    page_size = PAGE_SIZE

    column_list = [
        PromptDoc.id,
        PromptDoc.doc_key,
        PromptDoc.name,
        PromptDoc.description,
        PromptDoc.created_at,
    ]
    column_searchable_list = [PromptDoc.doc_key, PromptDoc.name]
    column_sortable_list = [PromptDoc.id, PromptDoc.created_at]
    column_default_sort = [(PromptDoc.created_at, True)]
    column_labels = {
        PromptDoc.id: "ID",
        PromptDoc.doc_key: "文档 Key（例：ai_guide / opening / closing）",
        PromptDoc.name: "文档名称",
        PromptDoc.description: "描述",
        PromptDoc.created_at: "创建时间",
    }
    form_args = {
        "doc_key": {
            "label": "文档 Key（唯一，英文小写+下划线）",
            "description": "创建后会被场景版本的『文档注入』复选框引用，请勿随意改动。",
        },
        "name": {"label": "文档名称", "description": "复选框里展示用；可中文。"},
        "description": {"label": "描述", "description": "供后台维护者查看的说明。"},
    }
    form_excluded_columns = ["versions", "created_at"]


class PromptDocVersionAdmin(AdminModelView, model=PromptDocVersion):
    """参考话术版本内容；发布/回滚建议走 /api/prompt/doc-versions/* 管理 API。"""
    name = "话术版本内容"
    name_plural = "话术版本内容"
    category = ADMIN_CAT_PROMPTS
    page_size = PAGE_SIZE

    column_list = [
        PromptDocVersion.id,
        "doc",
        PromptDocVersion.version,
        PromptDocVersion.status,
        "content_len",
        PromptDocVersion.source_filename,
        PromptDocVersion.created_at,
        PromptDocVersion.published_at,
    ]
    column_labels = {
        PromptDocVersion.id: "ID",
        "doc": "所属文档",
        PromptDocVersion.version: "版本号",
        PromptDocVersion.status: "状态",
        PromptDocVersion.content: "正文（纯文本 / markdown）",
        PromptDocVersion.source_filename: "来源文件名（可空）",
        PromptDocVersion.created_by: "创建人 ID",
        PromptDocVersion.created_at: "创建时间",
        PromptDocVersion.published_at: "发布时间",
        "content_len": "内容字符数",
    }
    column_sortable_list = [
        PromptDocVersion.id,
        PromptDocVersion.version,
        PromptDocVersion.created_at,
        PromptDocVersion.published_at,
    ]
    column_default_sort = [(PromptDocVersion.created_at, True)]
    column_filters = [
        LocalizedStaticValuesFilter(
            PromptDocVersion.status,
            title="状态",
            values=_PROMPT_STATUS_FILTER_VALUES,
        ),
    ]
    column_formatters = {
        "content_len": lambda m, a: f"{len(m.content or '')} 字",
        PromptDocVersion.status: lambda m, a: Markup({
            "draft": '<span class="badge bg-secondary">draft</span>',
            "published": '<span class="badge bg-success">published</span>',
            "archived": '<span class="badge bg-dark">archived</span>',
        }.get(m.status, m.status)),
    }
    form_excluded_columns = ["created_at", "published_at"]

    form_overrides = {"status": SelectField}
    form_args = {
        "doc": {"label": "所属文档", "description": "选择已在『参考话术文档』中创建的条目。"},
        "version": {"label": "版本号", "description": "同文档内唯一，新增草稿时请 +1。"},
        "status": {
            "label": "状态",
            "choices": [
                ("draft", "draft（草稿，线上不会读到）"),
                ("published", "published（当前生效，同文档只保留一条）"),
                ("archived", "archived（历史归档）"),
            ],
            "description": "直接置 published 会让缓存失效并立刻对所有场景生效；建议通过 /api/prompt/doc-versions/{id}/publish 发布（会自动归档上一版）。",
        },
        "content": {
            "label": "正文（纯文本 / markdown）",
            "description": "直接粘贴话术正文，支持 markdown；保存后由 /prompt/doc-versions/{id}/publish 发布生效。",
            "render_kw": {
                "rows": 24,
                "class": "form-control",
                "style": (
                    "width:100%; display:block; resize:vertical; "
                    "font-family: ui-monospace, Menlo, Consolas, monospace; "
                    "font-size: 13px; line-height: 1.55;"
                ),
                "placeholder": "直接粘贴话术内容，例如：\n## 开场 1：先价值后询问\n您好！我们最近在...",
            },
        },
        "source_filename": {"label": "来源文件名（可空）", "description": "仅作溯源记录，如从 docx 迁移而来。"},
        "notes": {"label": "备注"},
    }

    async def after_model_change(self, data: dict, model, is_created, request) -> None:
        try:
            from ai.prompt_store import get_prompt_store
            d = model.doc
            if d:
                await get_prompt_store().invalidate_doc(d.doc_key)
        except Exception:
            pass


class PromptAuditLogAdmin(AdminModelView, model=PromptAuditLog):
    name = "Prompt 审计日志"
    name_plural = "Prompt 审计日志"
    category = ADMIN_CAT_PROMPTS
    page_size = PAGE_SIZE

    column_list = [
        PromptAuditLog.id,
        PromptAuditLog.action,
        PromptAuditLog.target_type,
        PromptAuditLog.target_id,
        PromptAuditLog.actor_id,
        PromptAuditLog.created_at,
    ]
    column_labels = {
        PromptAuditLog.id: "ID",
        PromptAuditLog.action: "动作",
        PromptAuditLog.target_type: "目标类型",
        PromptAuditLog.target_id: "目标 ID",
        PromptAuditLog.actor_id: "操作人 ID",
        PromptAuditLog.payload_json: "载荷",
        PromptAuditLog.created_at: "发生时间",
    }
    can_create = False
    can_edit = False
    can_delete = False


class WechatOutboundActionAdmin(AdminModelView, model=WechatOutboundAction):
    name = "微信外发记录"
    name_plural = "微信外发记录"
    category = ADMIN_CAT_CUSTOMERS
    page_size = PAGE_SIZE
    column_default_sort = [(WechatOutboundAction.completed_at, True)]
    column_sortable_list = [
        WechatOutboundAction.id,
        WechatOutboundAction.completed_at,
    ]

    column_list = [
        WechatOutboundAction.id,
        WechatOutboundAction.completed_at,
        WechatOutboundAction.actor_user_id,
        WechatOutboundAction.raw_customer_id,
        WechatOutboundAction.sales_wechat_id,
        WechatOutboundAction.action_type,
        WechatOutboundAction.status,
        WechatOutboundAction.receiver,
        WechatOutboundAction.block_reason,
        WechatOutboundAction.error,
    ]
    column_labels = {
        WechatOutboundAction.id: "ID",
        WechatOutboundAction.completed_at: "完成时间",
        WechatOutboundAction.actor_user_id: "真实姓名",
        WechatOutboundAction.raw_customer_id: "客户 ID",
        WechatOutboundAction.sales_wechat_id: "销售微信号",
        WechatOutboundAction.action_type: "动作类型",
        WechatOutboundAction.status: "状态",
        WechatOutboundAction.block_reason: "拦截原因",
        WechatOutboundAction.receiver_source: "接收方来源",
        WechatOutboundAction.receiver: "接收方(搜索词)",
        WechatOutboundAction.source_chat_message_id: "来源消息ID",
        WechatOutboundAction.claimed_local_sales_wechat_id: "本机声明销售微信",
        WechatOutboundAction.error: "错误",
    }
    column_formatters = {
        WechatOutboundAction.actor_user_id: lambda m, a: (
            (m.actor.real_name or m.actor.username)
            if getattr(m, "actor", None)
            else (str(m.actor_user_id) if m.actor_user_id else "—")
        ),
        WechatOutboundAction.sales_wechat_id: _fmt_sales_wechat_column,
        WechatOutboundAction.raw_customer_id: lambda m, a: Markup(
            f'<span class="text-truncate d-inline-block" style="max-width:8rem" title="{Markup.escape(m.raw_customer_id or "")}">'
            f"{Markup.escape(m.raw_customer_id or "—")}</span>"
        ),
        WechatOutboundAction.receiver: lambda m, a: (
            ((m.receiver or "")[:24] + "…")
            if m.receiver and len(m.receiver) > 24
            else (m.receiver or "—")
        ),
        WechatOutboundAction.block_reason: lambda m, a: (m.block_reason or "—"),
        WechatOutboundAction.error: lambda m, a: (
            ((m.error or "")[:40] + "…")
            if m.error and len(m.error) > 40
            else (m.error or "—")
        ),
        WechatOutboundAction.action_type: lambda m, a: {
            "send": "发送",
            "edit_send": "编辑后发送",
        }.get(m.action_type, m.action_type or "—"),
        WechatOutboundAction.status: lambda m, a: Markup({
            "pending": '<span class="badge bg-secondary-lt">待处理</span>',
            "sent": '<span class="badge bg-success-lt">已发送</span>',
            "failed": '<span class="badge bg-danger-lt">失败</span>',
            "blocked": '<span class="badge bg-warning-lt">已拦截</span>',
        }.get(m.status, f'<span class="badge bg-secondary-lt">{Markup.escape(m.status or "—")}</span>')),
    }
    column_type_formatters = {type(None): lambda v: "—"}
    column_filters = [
        UserIdLabelFilter(WechatOutboundAction.actor_user_id, title="真实姓名"),
        LocalizedStaticValuesFilter(
            WechatOutboundAction.action_type,
            title="动作类型",
            values=_OUTBOUND_ACTION_TYPE_VALUES,
        ),
        LocalizedStaticValuesFilter(
            WechatOutboundAction.status,
            title="状态",
            values=_OUTBOUND_STATUS_VALUES,
        ),
    ]

    def list_query(self, request):
        from sqlalchemy.orm import selectinload

        return super().list_query(request).options(
            selectinload(WechatOutboundAction.actor),
            selectinload(WechatOutboundAction.sales_wechat_account),
        )

    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True


admin_views = [
    # 数据看板（可通过上方开关快速隐藏）
    *([DataDashboardView] if (ENABLE_DASHBOARD and DataDashboardView) else []),
    # 用户管理
    UserAdmin,
    UserSalesWechatAdmin,
    SalesWechatAccountAdmin,
    # TransferAdmin,
    # 客户管理
    ProfilingProgressView,
    SalesCustomerProfileAdmin,
    ChatAdmin,
    WechatOutboundActionAdmin,
    RawCustomerAdmin,
    # 营销策略管理
    ProfileTagDefinitionAdmin,
    ProductAdmin,
    # 提示词管理
    PromptScenarioAdmin,
    PromptVersionAdmin,
    PromptDocAdmin,
    PromptDocVersionAdmin,
    PromptAuditLogAdmin,
    # 数据同步
    SalesWechatAccountSyncView,
    RawWechatPoolSyncView,
    RawWechatChatSyncView,
    SyncFailureAdmin,
    # 系统设置
    ConfigAdmin,
]
