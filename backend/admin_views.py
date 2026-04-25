from sqladmin import BaseView, ModelView, action, expose
from sqladmin.filters import StaticValuesFilter, get_column_obj, get_parameter_name
from sqlalchemy import or_, and_
from sqlalchemy.sql.expression import Select
from typing import Any, Callable, List, Tuple
from wtforms import SelectField, StringField, TextAreaField, SelectMultipleField
from wtforms.validators import InputRequired, Optional as WTFOptional
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
        title: str = "画像状态(per-sales)",
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


from sqlalchemy.future import select
from crud import transfer_user_customers
from markupsafe import Markup
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

PAGE_SIZE = 25


class ProfilingProgressView(BaseView):
    """后台 AI 画像批任务进度（内存状态，页面内自动刷新）。"""

    name = "AI 画像任务进度"
    category = "2. 业务审计中心"

    @expose("/profiling-progress", methods=["GET"])
    async def progress_page(self, request: Request):
        from ai.profiling_progress import snapshot

        if request.query_params.get("format") == "json":
            return JSONResponse(snapshot())
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>AI 画像任务进度</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; }
    .bar { height: 1.25rem; background: #e9ecef; border-radius: .25rem; overflow: hidden; margin: 1rem 0; }
    .fill { height: 100%; background: #206bc4; transition: width .3s ease; }
    .muted { color: #626976; font-size: .875rem; }
    .row { margin: .5rem 0; }
    code { background: #f1f5f9; padding: .1rem .35rem; border-radius: .2rem; font-size: .85em; }
  </style>
</head>
<body>
  <h1>AI 画像任务进度</h1>
  <p class="muted">页面每 2 秒自动更新。关闭本页不影响后台任务。</p>
  <div class="row"><strong>状态：</strong> <span id="status">—</span></div>
  <div class="row"><strong>进度：</strong> <span id="counts">—</span></div>
  <div class="bar"><div class="fill" id="fill" style="width:0%"></div></div>
  <div class="row"><strong>当前处理：</strong> <code id="current">—</code></div>
  <div class="row muted" id="msg"></div>
  <script>
    function fmt(ts) {
      if (ts == null) return "";
      const d = new Date(ts * 1000);
      return isNaN(d) ? "" : d.toLocaleString();
    }
    async function tick() {
      try {
        const u = new URL(window.location.href);
        u.searchParams.set("format", "json");
        const r = await fetch(u.toString(), { credentials: "same-origin" });
        const d = await r.json();
        const st = { idle: "空闲", running: "运行中", completed: "已完成", failed: "失败" };
        document.getElementById("status").textContent = (st[d.status] || d.status);
        const sk = d.skipped != null ? d.skipped : 0;
        document.getElementById("counts").textContent =
          d.total ? (d.processed + " / " + d.total + "（成功 " + d.done + "，失败 " + d.failed + "，跳过 " + sk + "）") : "—";
        document.getElementById("fill").style.width = (d.percent || 0) + "%";
        document.getElementById("current").textContent = d.current_raw_id || "—";
        let extra = "";
        if (d.started_at) extra += "开始：" + fmt(d.started_at) + " ";
        if (d.finished_at) extra += "结束：" + fmt(d.finished_at);
        document.getElementById("msg").textContent = (d.message || "") + (extra ? " · " + extra : "");
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

class UserAdmin(ModelView, model=User):
    column_list = [
        User.id,
        User.username,
        User.real_name,
        User.wechat_remark_for_prompt,
        User.wechat_id,
        User.role,
        User.is_active,
        "relations_links",
        "chat_links",
    ]
    column_searchable_list = [User.username, User.real_name]
    page_size = PAGE_SIZE
    
    category = "1. 人员与组织"
    name = "系统登录账号"
    name_plural = "系统登录账号"
    
    column_formatters = {
        "relations_links": lambda m, a: Markup(
            f'<a href="/admin/sales-customer-profile/list?search={m.username}">👥 {len(m.sales_customer_profiles)} 条关联</a>'
        ) if m.sales_customer_profiles else "空",
        "chat_links": lambda m, a: Markup(
            f'<a href="/admin/chat-message/list?search=user:{m.username}">💬 {len(m.chat_messages)} 条对话</a>'
        ) if m.chat_messages else "暂无"
    }
    

    # 安全增强：查看详情页时排除密码哈希字段
    column_details_exclude_list = [User.password_hash]
    
    # 修复：修改页面中屏蔽 Relations 和 Chat Messages
    form_excluded_columns = ["relations", "chat_messages", "sales_wechat_bindings"]
    
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
        User.wechat_remark_for_prompt: "用户级备注(可选，画像以客户关系微信备注为准)",
        User.wechat_id: "微信号绑定",
        User.role: "系统权限角色",
        User.is_active: "账号状态(是否停用)",
        "relations_links": "管辖客户",
        "chat_links": "对话记录"
    }

    def list_query(self, request):
        from sqlalchemy.orm import selectinload
        return super().list_query(request).options(
            selectinload(User.sales_customer_profiles),
            selectinload(User.chat_messages)
        )

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


class UserSalesWechatAdmin(ModelView, model=UserSalesWechat):
    category = "1. 人员与组织"
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
    form_columns = [
        UserSalesWechat.user_id,
        UserSalesWechat.sales_wechat_id,
        UserSalesWechat.label,
        UserSalesWechat.is_primary,
        UserSalesWechat.verified_at,
    ]


class SalesWechatAccountAdmin(ModelView, model=SalesWechatAccount):
    """销售业务微信主数据（与云客 wxid 对齐；由 accounts.xlsx 或接口同步）。"""

    category = "1. 人员与组织"
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


class SalesWechatAccountSyncView(BaseView):
    """从 accounts.xlsx 批量导入/更新 sales_wechat_accounts（幂等）。"""

    name = "销售微信 XLSX 导入"
    icon = "fa-solid fa-file-excel"
    category = "1. 人员与组织"

    @expose("/sales-wechat-accounts/import-xlsx", methods=["GET", "POST"])
    async def import_xlsx_page(self, request: Request):
        from sync.sales_wechat_accounts import default_accounts_xlsx_path, sync_from_path

        msg = ""
        if request.method == "POST":
            form = await request.form()
            raw = (form.get("path") or "").strip()
            try:
                p = Path(raw).expanduser().resolve() if raw else default_accounts_xlsx_path()
                st = await sync_from_path(p)
                msg = (
                    f"成功：已 upsert {st.get('upserted')} 条，"
                    f"文件中有效行 {st.get('rows_in_file')}。路径：{st.get('path')}"
                )
            except Exception as e:
                msg = f"失败：{e}"

        default_p = str(default_accounts_xlsx_path())
        safe_msg = Markup.escape(msg) if msg else ""
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>销售微信主数据 XLSX 导入</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 36rem; margin: 2rem auto; padding: 0 1rem; }}
    label {{ display: block; margin: .75rem 0 .25rem; font-weight: 600; }}
    input[type=text] {{ width: 100%; box-sizing: border-box; padding: .5rem; }}
    button {{ margin-top: 1rem; padding: .5rem 1rem; }}
    .muted {{ color: #64748b; font-size: .875rem; margin-top: 1.5rem; }}
    .msg {{ margin-top: 1rem; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <h1>销售微信主数据 · XLSX 导入</h1>
  <p>与云客导出 <code>accounts.xlsx</code> 表头一致（含「微信ID (wechatId)」「昵称」「别名」等列）。</p>
  <form method="post">
    <label for="path">文件路径（留空则使用默认：项目根目录 accounts.xlsx 或环境变量 ACCOUNTS_XLSX）</label>
    <input type="text" id="path" name="path" value="" placeholder="{Markup.escape(default_p)}"/>
    <button type="submit">开始导入</button>
  </form>
  <p class="muted">默认路径当前解析为：<code>{Markup.escape(default_p)}</code></p>
  <p class="muted">亦可在服务器执行：<code>cd backend &amp;&amp; python -m sync.sales_wechat_accounts [路径]</code></p>
  {f'<p class="msg">{safe_msg}</p>' if safe_msg else ''}
</body>
</html>"""
        return HTMLResponse(html)


class RawWechatPoolSyncView(BaseView):
    """开放平台 getAllFriendsIncrement：按自然日写入 raw_customers / raw_customer_sales_wechats。"""

    name = "原始客户池·微信增量同步"
    icon = "fa-solid fa-cloud-arrow-down"
    category = "2. 业务审计中心"

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
        yday = (datetime.now(sh).date() - timedelta(days=1)).isoformat()
        day_default = (cfg.get(CFG_TARGET_DAY) or "").strip() or yday
        partner_default = (cfg.get(CFG_PARTNER) or "").strip()
        st = (cfg.get("wechat_friends_sync_status") or "").strip() or "—"
        last_msg = Markup.escape((cfg.get("wechat_friends_sync_last_message") or "").strip() or "—")
        last_ok = Markup.escape((cfg.get("wechat_friends_sync_last_success") or "").strip() or "—")
        qmode = Markup.escape((cfg.get("wechat_friends_query_mode") or "updateTime").strip())

        safe_msg = Markup.escape(msg) if msg else ""
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
    icon = "fa-solid fa-comments"
    category = "2. 业务审计中心"

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


class SalesCustomerProfileAdmin(ModelView, model=SalesCustomerProfile):
    category = "2. 业务审计中心"
    name = "私域画像与跟进"
    name_plural = "私域画像与跟进（per-sales）"
    page_size = PAGE_SIZE

    column_list = [
        SalesCustomerProfile.id,
        SalesCustomerProfile.raw_customer_id,
        SalesCustomerProfile.sales_wechat_id,
        SalesCustomerProfile.user_id,
        SalesCustomerProfile.relation_type,
        SalesCustomerProfile.title,
        SalesCustomerProfile.budget_amount,
        SalesCustomerProfile.purchase_type,
        SalesCustomerProfile.wechat_remark,
        SalesCustomerProfile.suggested_followup_date,
        SalesCustomerProfile.updated_at,
    ]
    column_searchable_list = [
        "raw_customer_id",
        "sales_wechat_id",
        "wechat_remark",
        "ai_profile",
        "title",
    ]
    # 编辑页默认会渲染大文本字段，数据量大时容易卡顿；先隐藏（画像建议在专用页面/只读查看）。
    form_excluded_columns = ["created_at", "updated_at", "ai_profile", "dify_conversation_id"]

class ChatAdmin(ModelView, model=ChatMessage):
    column_list = [
        "id", "user", "raw_customer", "role", "content", 
        "rating", "is_copied", "created_at"
    ]
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
    
    category = "2. 业务审计中心"
    name = "AI对话快调"
    name_plural = "AI对话历史"
    page_size = PAGE_SIZE
    
    # 彻底移除过滤器，改用 URL 搜索穿透逻辑
    column_filters = []
    
    can_export = True
    export_columns = ["id", "user.username", "raw_customer.phone", "role", "content", "rating", "is_copied", "created_at"]
    
    # 再次缩减宽度，限额 30 字符
    column_formatters = {
        "content": lambda m, a: (m.content[:30] + "...") if m.content and len(m.content) > 30 else m.content,
        "rating": lambda m, a: {1: "👍 赞", -1: "👎 踩", 0: "➖ 未评"}.get(m.rating, "➖"),
        "is_copied": lambda m, a: "✅ 已采纳" if m.is_copied else "⚪ 未复制"
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

class ProductAdmin(ModelView, model=Product):
    column_list = [Product.id, Product.product_name, Product.product_id, Product.price, Product.supplier_name]
    column_searchable_list = [Product.product_name, Product.product_id]
    page_size = PAGE_SIZE
    category = "3. 基础资源库"
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


class ProfileTagDefinitionAdmin(ModelView, model=ProfileTagDefinition):
    category = "2. 业务审计中心"
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


class ConfigAdmin(ModelView, model=SystemConfig):
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
    category = "3. 基础资源库"
    name = "系统配置项"
    name_plural = "环境控制变量"
    page_size = PAGE_SIZE
    
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
                ("wechat_friends_sync_target_day", "微信原始池：定时与手动共用的目标自然日 (YYYY-MM-DD，上海时区)"),
                ("wechat_open_partner_id", "微信原始池：开放平台 partnerId（空则使用环境变量 WECHAT_OPEN_ADMIN_PARTNER_ID）"),
                ("wechat_friends_query_mode", "微信原始池：增量接口 queryMode，填 updateTime 或 createTime"),
                ("llm_api_url", "AI（对话默认）：兼容 OpenAI 的 API Base URL（未给单模型配置 url 时使用）"),
                ("llm_api_key", "AI（对话默认）：API Key（未给单模型配置 key 时使用）"),
                ("llm_chat_model", "AI（对话）：桌面/API 默认对话模型（须出现在 llm_chat_models_list 中，可被请求体 chat_model 覆盖）"),
                (
                    "desktop_default_chat_models",
                    "桌面端：默认勾选模型（逗号分隔；如 deepseek-v3.2,qwen3.5-plus；本机未固定偏好时生效）",
                ),
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
                elif key.startswith("profile_") or key.startswith("llm_") or key.startswith("use_db_prompts"):
                    data["config_group"] = "ai"
                elif key.startswith("desktop_"):
                    data["config_group"] = "desktop"
        except Exception:
            pass

    # 编辑时 config_key 设为只读，防止 MySQL 报 Duplicate entry 错误
    form_widget_args = {
        "config_key": {"readonly": True}
    }
    
    column_labels = {
        SystemConfig.config_key: "内部指令通道",
        SystemConfig.config_value: "在此输入对应指令生效的具体值",
        SystemConfig.config_group: "作用域隔离保护伞(general即代表根环境)",
        SystemConfig.description: "备注说明",
        SystemConfig.updated_at: "最后修改时间"
    }

class TransferAdmin(ModelView, model=BusinessTransfer):
    column_list = [BusinessTransfer.id, BusinessTransfer.from_user, BusinessTransfer.to_user, BusinessTransfer.transferred_count, BusinessTransfer.transfer_time]
    category = "1. 人员与组织"
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

class RawCustomerAdmin(ModelView, model=RawCustomerSalesWechat):
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

    category = "2. 业务审计中心"
    name = "原始客户池(同步,per-sales)"
    name_plural = "原始客户池(同步,per-sales)"

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

    column_filters = [
        ScpProfileStatusFilter(title="画像状态"),
        PhonePresenceFilter(RawCustomerSalesWechat.phone),
    ]

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
        confirmation_message="确定对选中的原始客户执行画像并同步到「客观客户库 / 销售跟进」吗？任务在后台执行，可在侧栏「AI 画像任务进度」查看进度；也可稍后刷新本列表查看状态。",
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
                pairs = [(r[0], r[1]) for r in res.all() if r and r[0] and r[1]]
            from ai.raw_profiling import schedule_profile_raw_customer_sales_pairs

            schedule_profile_raw_customer_sales_pairs(pairs)
        from starlette.responses import RedirectResponse

        return RedirectResponse(url=request.url_for("admin:list", identity=self.identity))

    @action(
        name="run_ai_profile_all",
        label="分析所有未画像客户",
        confirmation_message=(
            "确定要开始分析尚未进行画像的原始客户吗？若需在 URL 后附加 "
            "`?sales_wechat_id=你的销售微信号` 可仅处理该号下数据；留空则处理全库符合条件的记录。"
            "任务消耗 API 额度，可在侧栏「AI 画像任务进度」查看进度。"
        ),
        add_in_detail=False,
        add_in_list=True,
    )
    async def run_ai_profile_all(self, request):
        from ai.raw_profiling import schedule_profile_all_unprofiled

        sw = (request.query_params.get("sales_wechat_id") or "").strip()
        filt = [sw] if sw else None
        schedule_profile_all_unprofiled(sales_wechat_ids=filt)
        from starlette.responses import RedirectResponse

        return RedirectResponse(url=request.url_for("admin:list", identity=self.identity))


class SyncFailureAdmin(ModelView, model=SyncFailure):
    name = "数据同步异常监控"
    name_plural = "数据同步异常监控"
    category = "2. 业务审计中心"
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


class PromptScenarioAdmin(ModelView, model=PromptScenario):
    """业务"场景"：每个 scenario_key 对应一套 system prompt 版本序列。

    新增流程：
    1) 在本页『新增』→填写英文 key（如 custom_scene）+ 中文名；
    2) 到『提示词版本』新增一行：所属场景选这条，版本号=1，状态 draft；
       在『System 模板』里写好提示词正文并勾选要注入的文档；
    3) 用 /api/prompt/versions/{id}/publish 把版本发布成 published；
    4) 代码侧需要把 gateway/scenario 路由指向这个 key（自由对话/推品已自动接入）。
    """
    name = "提示词场景"
    name_plural = "提示词场景"
    category = "4. 提示词管理中心"
    page_size = PAGE_SIZE

    column_list = [
        PromptScenario.id,
        PromptScenario.scenario_key,
        PromptScenario.name,
        PromptScenario.ui_category,
        PromptScenario.enabled,
        PromptScenario.tools_enabled,
        "published_version",
        PromptScenario.updated_at,
    ]
    column_searchable_list = [PromptScenario.scenario_key, PromptScenario.name]
    column_labels = {
        PromptScenario.id: "ID",
        PromptScenario.scenario_key: "场景 Key",
        PromptScenario.name: "名称",
        PromptScenario.description: "描述",
        PromptScenario.enabled: "启用",
        PromptScenario.tools_enabled: "允许 Function Call",
        PromptScenario.ui_category: "界面分类",
        PromptScenario.created_at: "创建时间",
        PromptScenario.updated_at: "更新时间",
        "published_version": "当前线上版本",
    }

    column_formatters = {
        "published_version": lambda m, a: Markup(
            "<span class='text-muted'>（待运行期计算）</span>"
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
        "description": {"label": "描述", "description": "供运营/产品查看的说明。"},
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
    form_excluded_columns = ["versions", "created_at", "updated_at"]

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
]

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
}


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


class PromptVersionAdmin(ModelView, model=PromptVersion):
    """场景的 prompt 版本：draft / published / archived。

    编辑要点：
    - "System 模板" 直接写纯文本 / markdown，占位符用 {{customer_card}} 这样的形式；
    - "参考话术文档注入" 用复选框勾选即可，保存时自动转成 doc_refs_json；
    - "快捷插入变量" 用复选框，对模板里还未出现的占位符会自动追加一个标题块；
    - 发布/回滚请走 /api/prompt 管理 API（会归档上一个 published 并失效缓存）。
    """
    name = "提示词版本"
    name_plural = "提示词版本"
    category = "4. 提示词管理中心"
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
                    "客户画像场景（customer_profile）请在此填写任务与 {{basic_info}} / {{chat_context}} / {{order_context}}。"
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


class PromptDocAdmin(ModelView, model=PromptDoc):
    """参考话术文档主表：doc_key 要与 PromptVersion.doc_refs_json 中的 key 对齐。"""
    name = "参考话术文档"
    name_plural = "参考话术文档"
    category = "4. 提示词管理中心"
    page_size = PAGE_SIZE

    column_list = [
        PromptDoc.id,
        PromptDoc.doc_key,
        PromptDoc.name,
        PromptDoc.description,
        PromptDoc.created_at,
    ]
    column_searchable_list = [PromptDoc.doc_key, PromptDoc.name]
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


class PromptDocVersionAdmin(ModelView, model=PromptDocVersion):
    """参考话术版本内容；发布/回滚建议走 /api/prompt/doc-versions/* 管理 API。"""
    name = "话术版本内容"
    name_plural = "话术版本内容"
    category = "4. 提示词管理中心"
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


class PromptAuditLogAdmin(ModelView, model=PromptAuditLog):
    name = "Prompt 审计日志"
    name_plural = "Prompt 审计日志"
    category = "4. 提示词管理中心"
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


admin_views = [
    UserAdmin,
    UserSalesWechatAdmin,
    SalesWechatAccountAdmin,
    SalesWechatAccountSyncView,
    ProfileTagDefinitionAdmin,
    SalesCustomerProfileAdmin,
    ChatAdmin,
    ProductAdmin,
    ConfigAdmin,
    TransferAdmin,
    ProfilingProgressView,
    RawCustomerAdmin,
    RawWechatChatSyncView,
    RawWechatPoolSyncView,
    SyncFailureAdmin,
    PromptScenarioAdmin,
    PromptVersionAdmin,
    PromptDocAdmin,
    PromptDocVersionAdmin,
    PromptAuditLogAdmin,
]
