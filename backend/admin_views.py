from sqladmin import ModelView
from wtforms import SelectField
from models import User, Customer, Order, UserCustomerRelation, ChatMessage, Product, SystemConfig

class UserAdmin(ModelView, model=User):
    column_list = [User.id, User.username, User.real_name, User.wechat_id, User.role, User.is_active]
    column_searchable_list = [User.username, User.real_name]
    name = "员工账号"
    name_plural = "员工管理"
    form_excluded_columns = [User.password_hash]
    
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
        User.username: "登录系统账号",
        User.real_name: "真实姓名",
        User.wechat_id: "微信号绑定",
        User.role: "系统权限角色",
        User.is_active: "账号状态(是否停用)",
    }

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
    column_list = [Order.id, Order.product_title, Order.amount, Order.order_date, Order.customer_id, Order.user_id]
    name = "历史物理订单"
    name_plural = "流水订单统计"
    column_labels = {
        Order.product_title: "成单商品名",
        Order.amount: "订单金额(元)",
        Order.order_date: "下单时间",
        Order.category_name: "分类标签",
        Order.external_order_id: "原始凭证订单号",
        "customer": "所属客户",
        "user": "销售归属"
    }

class RelationAdmin(ModelView, model=UserCustomerRelation):
    column_list = [UserCustomerRelation.id, UserCustomerRelation.user_id, UserCustomerRelation.customer_id, UserCustomerRelation.relation_type, UserCustomerRelation.budget_amount]
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
        "user": "负责销售员工",
        "customer": "目标客户",
        UserCustomerRelation.relation_type: "跟进状态",
        UserCustomerRelation.title: "专有称呼(例如: 李局长)",
        UserCustomerRelation.budget_amount: "掌握的客户单笔预算",
        UserCustomerRelation.ai_profile: "AI生成的客户画像与战术",
        UserCustomerRelation.contact_date: "首次建联日",
        UserCustomerRelation.assigned_at: "系统分配下沉日"
    }

class ChatAdmin(ModelView, model=ChatMessage):
    column_list = [ChatMessage.id, ChatMessage.role, ChatMessage.content, ChatMessage.customer_id]
    column_searchable_list = [ChatMessage.content]
    name = "微信对话流存档"
    name_plural = "大模型语料(勿改)"
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
                ("dify_base_url", "大脑中枢：Dify 核心请求网关 URL (含版本号)")
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

admin_views = [UserAdmin, CustomerAdmin, OrderAdmin, RelationAdmin, ChatAdmin, ProductAdmin, ConfigAdmin]
