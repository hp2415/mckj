from sqladmin import BaseView, ModelView, action, expose
from sqladmin.filters import StaticValuesFilter, get_column_obj, get_parameter_name
from sqlalchemy import or_
from sqlalchemy.sql.expression import Select
from typing import Any, Callable, List, Tuple
from wtforms import SelectField, StringField, TextAreaField, SelectMultipleField
from wtforms.validators import InputRequired, Optional as WTFOptional
from models import (
    User,
    UserSalesWechat,
    SalesWechatAccount,
    Customer,
    Order,
    UserCustomerRelation,
    ChatMessage,
    Product,
    SystemConfig,
    BusinessTransfer,
    SyncFailure,
    RawCustomer,
    RawCustomerSalesWechat,
    PromptScenario,
    PromptVersion,
    PromptDoc,
    PromptDocVersion,
    PromptAuditLog,
    ProfileTagDefinition,
)
from database import AsyncSessionLocal


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
    icon = "fa-solid fa-bars-progress"
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
            f'<a href="/admin/user-customer-relation/list?search=user:{m.username}">👥 {len(m.relations)} 条关联</a>'
        ) if m.relations else "空",
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
            selectinload(User.relations),
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


class CustomerAdmin(ModelView, model=Customer):
    column_list = [
        "id", "customer_name", "phone", "unit_name", "unit_type", "profile_status", "quick_action",
        "relations_links", "chat_links", "orders_links"
    ]
    column_searchable_list = ["phone", "customer_name", "unit_name"]
    page_size = PAGE_SIZE
    
    column_formatters = {
        "profile_status": lambda m, a: Markup(
            '<span class="badge bg-success">已分析</span>'
            if (m.profile_status or 0) == 1
            else '<span class="badge bg-secondary">未分析</span>'
        ),
        "quick_action": lambda m, a: Markup(
            f'<a class="btn btn-sm btn-outline-primary" '
            f'href="/admin/customer/action/run_ai_profile_cust?pks={m.id}">'
            f'🔍 分析画像</a>'
        ),
        "relations_links": lambda m, a: Markup(
            f'<a href="/admin/user-customer-relation/list?search=phone:{m.phone}">👥 {len(m.relations)} 条归属</a>'
        ) if m.relations else "公选池",
        "chat_links": lambda m, a: Markup(
            f'<a href="/admin/chat-message/list?search=phone:{m.phone}">📊 {len(m.chat_messages)} 条轨迹</a>'
        ) if m.chat_messages else "未采集",
        "orders_links": lambda m, a: Markup(
            f'<a href="/admin/order/list?search={m.phone}">🛒 {len(m.orders)} 条订单</a>'
        ) if m.orders else "无订单"
    }
    
    # 修复：修改页面中屏蔽内部关联项
    form_excluded_columns = ["relations", "chat_messages", "orders"]
    
    column_labels = {
        "id": "ID",
        Customer.phone: "手机号(唯一实体)",
        Customer.customer_name: "客户真名",
        Customer.unit_name: "收货单位名称",
        Customer.unit_type: "单位类型",
        Customer.admin_division: "行政划区",
        Customer.external_id: "外部关联ID",
        Customer.purchase_months: "采购月份",
        "profile_status": "画像状态",
        "quick_action": "快捷操作",
        "relations_links": "当前归属(穿透查询)",
        "chat_links": "沟通足迹",
        "orders_links": "客户订单"
    }
    column_filters = [
        LocalizedStaticValuesFilter(
            Customer.profile_status,
            values=[("0", "未分析"), ("1", "已分析")],
            title="画像状态",
        ),
        PhonePresenceFilter(Customer.phone),
    ]
    
    # 强制预加载关联数据，杜绝 DetachedInstanceError 并支持列表计数显示
    column_select_related_list = ["relations", "chat_messages", "orders"]
    
    category = "2. 业务审计中心"
    name = "客观客户库"
    name_plural = "客观客户库"
    
    # 启用内联及标签定义
    inline_models = [UserCustomerRelation]

    @action(
        name="run_ai_profile_cust",
        label="重新画像（选中）",
        confirmation_message="确定对选中的客户执行重新画像吗？程序将查找原始记录重新分析。任务在后台执行，可在侧栏「AI 画像任务进度」查看进度。",
        add_in_detail=True,
        add_in_list=True,
    )
    async def run_ai_profile_cust(self, request):
        pks = request.query_params.get("pks", "").split(",")
        pks = [p.strip() for p in pks if p.strip()]
        if pks:
            async with AsyncSessionLocal() as db:
                stmt = select(Customer.external_id).where(Customer.id.in_([int(pk) for pk in pks]))
                res = await db.execute(stmt)
                ext_ids = [eid for eid in res.scalars().all() if eid]
                if ext_ids:
                    from ai.raw_profiling import schedule_profile_raw_customers

                    schedule_profile_raw_customers(ext_ids)
        from starlette.responses import RedirectResponse

        return RedirectResponse(url=request.url_for("admin:list", identity=self.identity))

    def list_query(self, request):
        from sqlalchemy.orm import selectinload
        return super().list_query(request).options(
            selectinload(Customer.relations),
            selectinload(Customer.chat_messages),
            selectinload(Customer.orders)
        )
class OrderAdmin(ModelView, model=Order):
    column_list = [
        "id", "dddh", "consignee", "consignee_phone", 
        "pay_amount", "status_name", "order_time"
    ]
    column_searchable_list = ["dddh", "consignee_phone", "consignee", "buyer_name"]
    page_size = PAGE_SIZE
    # column_filters = ["status_name", "store", "order_time"]
    
    category = "2. 业务审计中心"
    name = "全量订单审计"
    name_plural = "客户订单"
    
    # 已平滑迁移至代理键绑定，恢复标准的 Object 关联
    form_excluded_columns = []
    
    column_labels = {
        "customer":"对应客户",
        "id": "序号",
        "customer_id": "客户ID",
        "dddh": "订单号",
        "order_id": "子系统ID",
        "store": "店铺编码",
        "consignee": "收货人",
        "consignee_phone": "收货电话",
        "consignee_address": "详细地址",
        "province_code": "省份码",
        "city_code": "城市码",
        "district_code": "区县码",
        "pay_amount": "订单总额",
        "freight": "含运费",
        "pay_type_name": "支付渠道",
        "status_name": "订单状态",
        "order_time": "下单日期",
        "update_time": "最后更新",
        "remark": "客户备注",
        "buyer_id": "单位编码",
        "buyer_name": "采购单位名称",
        "buyer_phone": "单位联系电话",
        "product_title": "商品摘要内容",
        "purchase_type": "采购类型码",
        "user_id": "负责员工ID"
    }

class RelationAdmin(ModelView, model=UserCustomerRelation):
    column_list = [
        "id", "user", "customer", "view_chats",
        "relation_type", "budget_amount", "contact_date"
    ]
    
    def search_query(self, stmt, term):
        from sqlalchemy import or_
        # 显式执行一次性关联
        stmt = stmt.outerjoin(User, UserCustomerRelation.user_id == User.id)
        stmt = stmt.outerjoin(Customer, UserCustomerRelation.customer_id == Customer.id)
        
        # 精准路由：处理带有特定前缀的下钻链接 (来自员工表或客户表)
        if term.startswith("user:"):
            target_user = term[len("user:"):]
            return stmt.filter(User.username == target_user)
        
        if term.startswith("phone:"):
            target_phone = term[len("phone:"):]
            return stmt.filter(Customer.phone == target_phone)
            
        # 模糊搜寻：支持对员工名、客户名的实时关联搜寻
        search_term = f"%{term}%"
        return stmt.filter(
            or_(
                User.real_name.ilike(search_term),
                User.username.ilike(search_term),
                Customer.customer_name.ilike(search_term),
                Customer.phone.ilike(search_term),
                UserCustomerRelation.title.ilike(search_term)
            )
        )

    # 必须保留一个字段以开启前端搜索框
    column_searchable_list = ["title"]
    page_size = PAGE_SIZE
    column_filters = []
    
    category = "1. 人员与组织"
    name = "销售主观跟进卡"
    name_plural = "销售跟进关系线"

    # 隐藏只读审计字段；动态标签由画像任务写入，不在此手工维护
    form_excluded_columns = ["assigned_at", "profile_tags"]

    # 核心钻取逻辑：下钻至对话审计列表，带上 user: 和 phone: 前缀确保 100% 精准
    column_formatters = {
        "view_chats": lambda m, a: Markup(
            f'<a class="btn btn-sm btn-outline-primary" style="padding: 2px 5px; font-size: 11px;" '
            f'href="/admin/chat-message/list?search=user:{m.user.username if m.user else "NULL"}_phone:{m.customer.phone if m.customer else "NULL"}">'
            f'💬 ai对话记录</a>'
        )
    }
    
    form_overrides = {"relation_type": SelectField}
    form_args = {
        "relation_type": {
            "choices": [("active", "活跃跟进中(含已成交)"), ("inactive", "暂无意向休眠")],
            "label": "跟进阶段"
        }
    }
    column_labels = {
        "id": "序号",
        "username": "负责员工账号",
        "customer_phone": "目标客户手机",
        "relation_type": "跟进状态",
        "title": "专有称呼(例如: 李局长)",
        "budget_amount": "预计单笔预算",
        "ai_profile": "AI生成的客户画像与战术",
        "contact_date": "首次建联日",
        "assigned_at": "系统分配时间"
    }

class ChatAdmin(ModelView, model=ChatMessage):
    column_list = [
        "id", "user", "customer", "role", "content", 
        "rating", "is_copied", "created_at"
    ]
    # 重写搜寻引擎逻辑，支持精确身份路由与多字段合并模糊搜索
    def search_query(self, stmt, term):
        from sqlalchemy import or_, func
        
        # 1. 显式执行表关联
        stmt = stmt.outerjoin(User, ChatMessage.user_id == User.id)
        stmt = stmt.outerjoin(Customer, ChatMessage.customer_id == Customer.id)
        
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
                    filters.append(Customer.phone == p_part)
            
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
                Customer.phone.ilike(search_term),
                Customer.customer_name.ilike(search_term)
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
    export_columns = ["id", "user.username", "customer.phone", "role", "content", "rating", "is_copied", "created_at"]
    
    # 再次缩减宽度，限额 30 字符
    column_formatters = {
        "content": lambda m, a: (m.content[:30] + "...") if m.content and len(m.content) > 30 else m.content,
        "rating": lambda m, a: {1: "👍 赞", -1: "👎 踩", 0: "➖ 未评"}.get(m.rating, "➖"),
        "is_copied": lambda m, a: "✅ 已采纳" if m.is_copied else "⚪ 未复制"
    }
    column_labels = {
        "user": "发起员工",
        "customer": "客户对象",
        "user_id": "员工实体ID",
        "customer_id": "客户实体ID",
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
    icon = "fa-solid fa-tags"
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
                ("dify_api_key", "大脑中枢：Dify API开放授权秘钥"), 
                ("dify_base_url", "大脑中枢：Dify 核心请求网关 URL"),
                ("unit_type_choices", "字典：单位类型下拉项 (逗号相隔)"),
                ("admin_division_choices", "字典：行政区划下拉项 (逗号相隔)"),
                ("purchase_type_choices", "字典：采购类型下拉项 (逗号相隔)"),
                ("sync_status", "系统内部：当前同步状态 (running/success/error)"),
                ("sync_last_message", "系统内部：商品同步汇总消息"),
                ("sync_last_success", "系统内部：商品同步成功时间"),
                ("sync_failed_suppliers", "系统内部：当前待修复的供货商清单"),
                ("llm_api_url", "AI：兼容 OpenAI 的 API 根 URL"),
                ("llm_api_key", "AI：API Key（对话与画像共用）"),
                ("llm_model", "AI：客户画像分析用模型名（勿与对话模型混用）"),
                ("llm_chat_model", "AI：桌面/API 对话默认模型（须出现在 llm_chat_models_list 中），可被请求体覆盖"),
                ("llm_chat_models_list", "AI：桌面可选对话模型列表，格式 模型ID:显示名;模型ID2:显示名2（建议分号分隔；画像仍用 llm_model）"),
                ("use_db_prompts", "Prompt：是否启用数据库化提示词（1 启用 / 0 回退旧 prompts.py）"),
            ],
            "label": "选择要定义的全局控制键"
        }
    }

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

class RawCustomerAdmin(ModelView, model=RawCustomer):
    """业务同步原始客户（微信侧）；画像状态与批量分析。"""

    column_list = [
        RawCustomer.id,
        RawCustomer.remark,
        RawCustomer.name,
        RawCustomer.phone,
        RawCustomer.profile_status,
        "quick_action",
        RawCustomer.sales_wechat_id,
        RawCustomer.label,
        RawCustomer.synced_at,
    ]
    column_searchable_list = [
        RawCustomer.id,
        RawCustomer.name,
        RawCustomer.remark,
        RawCustomer.phone,
        RawCustomer.sales_wechat_id,
    ]
    page_size = PAGE_SIZE
    can_create = False
    can_delete = False

    category = "2. 业务审计中心"
    name = "原始客户池(同步)"
    name_plural = "原始客户池(同步)"

    column_formatters = {
        "profile_status": lambda m, a: Markup(
            '<span class="badge bg-success">已分析</span>'
            if (m.profile_status or 0) == 1
            else '<span class="badge bg-secondary">未分析</span>'
        ),
        "quick_action": lambda m, a: Markup(
            f'<a class="btn btn-sm btn-outline-primary" '
            f'href="/admin/raw-customer/action/run-ai-profile?pks={m.id}">'
            f'🔍 分析画像</a>'
        ),
    }

    column_labels = {
        RawCustomer.id: "微信侧ID",
        RawCustomer.remark: "备注",
        RawCustomer.name: "昵称",
        RawCustomer.phone: "预存电话",
        RawCustomer.profile_status: "画像状态",
        "quick_action": "快捷操作",
        RawCustomer.sales_wechat_id: "销售企微ID",
        RawCustomer.label: "标签",
        RawCustomer.synced_at: "同步时间",
        RawCustomer.type: "类型",
        RawCustomer.from_type: "来源",
        RawCustomer.region: "地区",
        RawCustomer.note_des: "描述",
    }

    column_filters = [
        LocalizedStaticValuesFilter(
            RawCustomer.profile_status,
            values=[("0", "未分析"), ("1", "已分析")],
            title="画像状态",
        ),
        PhonePresenceFilter(RawCustomer.phone),
    ]

    def list_query(self, request):
        """
        关键修复：同一 raw_customer_id 可能对应多个 sales_wechat_id。
        当通过 URL 参数 ?sales_wechat_id=xxx 过滤时，必须以 raw_customer_sales_wechats 映射表为准，
        否则仅靠 raw_customers.sales_wechat_id（快照）会导致某些销售的客户“看不见”。
        """
        stmt = super().list_query(request)
        sw = (getattr(request, "query_params", {}) or {}).get("sales_wechat_id")
        sw = (sw or "").strip()
        if not sw:
            return stmt
        # JOIN 映射表进行筛选；返回仍是 RawCustomer 实体（不新增页面）
        return (
            stmt.join(
                RawCustomerSalesWechat,
                RawCustomerSalesWechat.raw_customer_id == RawCustomer.id,
            ).where(RawCustomerSalesWechat.sales_wechat_id == sw)
        )

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
            from ai.raw_profiling import schedule_profile_raw_customers

            schedule_profile_raw_customers(pks)
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
    icon = "fa-solid fa-sitemap"
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
    icon = "fa-solid fa-code-branch"
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
    icon = "fa-solid fa-book"
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
    icon = "fa-solid fa-file-lines"
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
    icon = "fa-solid fa-clipboard-list"
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
    CustomerAdmin,
    OrderAdmin,
    RelationAdmin,
    ChatAdmin,
    ProductAdmin,
    ConfigAdmin,
    TransferAdmin,
    ProfilingProgressView,
    RawCustomerAdmin,
    SyncFailureAdmin,
    PromptScenarioAdmin,
    PromptVersionAdmin,
    PromptDocAdmin,
    PromptDocVersionAdmin,
    PromptAuditLogAdmin,
]
