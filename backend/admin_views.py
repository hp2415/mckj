from sqladmin import ModelView
from wtforms import SelectField
from models import User, Customer, Order, UserCustomerRelation, ChatMessage, Product, SystemConfig, BusinessTransfer
from database import AsyncSessionLocal
from sqlalchemy.future import select
from crud import transfer_user_customers

class UserAdmin(ModelView, model=User):
    column_list = [User.id, User.username, User.real_name, User.wechat_id, User.role, User.is_active]
    column_searchable_list = [User.username, User.real_name]
    name = "员工账号"
    name_plural = "员工管理"
    
    # 不再排除密码字段，而是允许管理员输入
    # form_excluded_columns = [User.password_hash]
    
    # 强制让 role 变成下拉项
    form_overrides = {"role": SelectField}
    form_args = {
        "role": {
            "choices": [("staff", "普通业务员"), ("admin", "超级系统管理员")],
            "label": "系统权限角色"
        }
    }
    column_labels = {
        User.id: "ID",
        User.username: "登录系统账号(工号)",
        User.password_hash: "登录密码(由系统自动加密)",
        User.real_name: "真实姓名",
        User.wechat_id: "微信号绑定",
        User.role: "系统权限角色",
        User.is_active: "账号状态(是否停用)",
    }

    async def on_model_change(self, data: dict, model: any, is_created: bool, request: any) -> None:
        """
        在保存员工信息前进行拦截：
        如果提供了密码字段，且它不是哈希格式，则自动进行 Bcrypt 哈希加密。
        """
        if "password_hash" in data and data["password_hash"]:
            pwd = data["password_hash"]
            # 简单校验：如果不是以 $2b$ (Bcrypt) 开头，则认为需要加密
            if not pwd.startswith("$2b$"):
                from core.security import get_password_hash
                data["password_hash"] = get_password_hash(pwd)

class CustomerAdmin(ModelView, model=Customer):
    column_list = [Customer.id, Customer.customer_name, Customer.phone, Customer.unit_name, Customer.unit_type]
    column_searchable_list = [Customer.phone, Customer.customer_name, Customer.unit_name]
    name = "核心客户实体记录"
    name_plural = "客观客户库"
    column_labels = {
        Customer.phone: "手机号(唯一实体)",
        Customer.customer_name: "客户真名",
        Customer.unit_name: "收货单位名称",
        Customer.unit_type: "单位类型",
        Customer.admin_division: "行政划区",
        Customer.external_id: "外部关联ID"
    }

class OrderAdmin(ModelView, model=Order):
    column_list = [
        "id", "dddh", "consignee", "consignee_phone", 
        "pay_amount", "status_name", "order_time"
    ]
    column_searchable_list = ["dddh", "consignee_phone", "consignee", "buyer_name"]
    # column_filters = ["status_name", "store", "order_time"]
    
    name = "业务流水大表"
    name_plural = "全量订单记录"
    
    column_labels = {
        "id": "序号",
        "dddh": "订单号",
        "order_id": "子系统ID",
        "store": "店铺编码",
        "consignee": "收货人",
        "consignee_phone": "收货电话",
        "consignee_address": "详细地址",
        "province_code": "省份码",
        "city_code": "城市码",
        "district_code": "区县码",
        "pay_amount": "实付总额",
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
        "id", "username", "customer_phone", 
        "relation_type", "budget_amount", "contact_date"
    ]
    column_searchable_list = ["username", "customer_phone", "title"]
    # column_filters = ["relation_type", "contact_date"]
    
    name = "销售主观跟进卡"
    name_plural = "销售跟进关系线"
    
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
    column_list = [ChatMessage.id, ChatMessage.role, ChatMessage.content, ChatMessage.customer_id]
    column_searchable_list = [ChatMessage.content]
    name = "微信对话流存档"
    name_plural = "大模型语料"
    column_labels = {
        "user": "发起员工",
        "customer": "客户对象",
        ChatMessage.role: "发言人身份(user代表人工/ai代表机器人)",
        ChatMessage.content: "原始消息体",
        ChatMessage.dify_conv_id: "Dify大脑追溯生命线",
        ChatMessage.created_at: "收发录入时间"
    }

class ProductAdmin(ModelView, model=Product):
    column_list = [Product.id, Product.product_name, Product.product_id, Product.price, Product.supplier_name]
    column_searchable_list = [Product.product_name, Product.product_id]
    name = "外部采集商品库"
    name_plural = "通用商品库"
    column_labels = {
        Product.uuid: "平台原生UUID",
        Product.product_id: "平台内部商品序列号",
        Product.product_name: "商品营销全名",
        Product.price: "爬取售价(元)",
        Product.cover_img: "CDN图床链接",
        Product.product_url: "官方购买详情页",
        Product.unit: "打包单位",
        Product.supplier_name: "独家渠道商字号"
    }

class ConfigAdmin(ModelView, model=SystemConfig):
    column_list = [SystemConfig.id, SystemConfig.config_key, SystemConfig.config_group, SystemConfig.updated_at]
    name = "最高控制参数"
    name_plural = "系统调度级配置"
    
    # 彻底改写本表的行为逻辑
    form_overrides = {"config_key": SelectField}
    form_args = {
        "config_key": {
            "choices": [
                ("supplier_ids", "832爬虫：配置商品货源铺子ID (多店用逗号相隔)"), 
                ("dify_api_key", "大脑中枢：Dify API开放授权秘钥"), 
                ("dify_base_url", "大脑中枢：Dify 核心请求网关 URL"),
                ("unit_type_choices", "字典：单位类型下拉项 (逗号相隔)"),
                ("admin_division_choices", "字典：行政区划下拉项 (逗号相隔)"),
                ("purchase_type_choices", "字典：采购类型下拉项 (逗号相隔)")
            ],
            "label": "选择要定义的全局控制键"
        }
    }
    
    column_labels = {
        SystemConfig.config_key: "内部指令通道",
        SystemConfig.config_value: "在此输入对应指令生效的具体值",
        SystemConfig.config_group: "作用域隔离保护伞(general即代表根环境)",
        SystemConfig.description: "备注",
        SystemConfig.updated_at: "最后修改时间"
    }

class TransferAdmin(ModelView, model=BusinessTransfer):
    column_list = [BusinessTransfer.id, BusinessTransfer.from_user, BusinessTransfer.to_user, BusinessTransfer.transferred_count, BusinessTransfer.transfer_time]
    name = "业务移交操作"
    name_plural = "业务强制移交"
    
    column_labels = {
        BusinessTransfer.from_user: "我要交出人(From)",
        BusinessTransfer.to_user: "我要接收人(To)",
        BusinessTransfer.transferred_count: "移交客户成功数",
        BusinessTransfer.transfer_time: "操作发生时间",
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

admin_views = [UserAdmin, CustomerAdmin, OrderAdmin, RelationAdmin, ChatAdmin, ProductAdmin, ConfigAdmin, TransferAdmin]
