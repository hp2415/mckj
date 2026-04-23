from sqladmin import BaseView, ModelView, action, expose
from sqladmin.filters import StaticValuesFilter, get_column_obj, get_parameter_name
from sqlalchemy import or_
from sqlalchemy.sql.expression import Select
from typing import Any, Callable, List, Tuple
from wtforms import SelectField
from models import (
    User,
    Customer,
    Order,
    UserCustomerRelation,
    ChatMessage,
    Product,
    SystemConfig,
    BusinessTransfer,
    SyncFailure,
    RawCustomer,
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
        document.getElementById("counts").textContent =
          d.total ? (d.processed + " / " + d.total + "（成功 " + d.done + "，失败 " + d.failed + "）") : "—";
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
        User.id, User.username, User.real_name, User.wechat_id, User.role, User.is_active, 
        "relations_links", "chat_links"
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
    form_excluded_columns = ["relations", "chat_messages"]
    
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

    # 隐藏只读审计字段
    form_excluded_columns = ["assigned_at"]

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
                ("sync_failed_suppliers", "系统内部：当前待修复的供货商清单")
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
    column_searchable_list = [RawCustomer.id, RawCustomer.name, RawCustomer.remark, RawCustomer.phone]
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
        confirmation_message="确定要开始分析所有尚未进行画像的原始客户吗？这可能会消耗较多 API 额度并在后台持续运行一段时间。可在侧栏「AI 画像任务进度」查看实时进度。",
        add_in_detail=False,
        add_in_list=True,
    )
    async def run_ai_profile_all(self, request):
        from ai.raw_profiling import schedule_profile_all_unprofiled

        schedule_profile_all_unprofiled()
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

admin_views = [
    UserAdmin,
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
]
