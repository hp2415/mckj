from sqladmin import ModelView
from models import User, Customer, Order, UserCustomerRelation, ChatMessage, Product, SystemConfig

class UserAdmin(ModelView, model=User):
    column_list = [User.id, User.username, User.real_name, User.role, User.is_active]
    column_searchable_list = [User.username, User.real_name]
    name = "员工管理"
    name_plural = "员工管理"

class CustomerAdmin(ModelView, model=Customer):
    column_list = [Customer.id, Customer.customer_name, Customer.phone, Customer.unit_name, Customer.unit_type]
    column_searchable_list = [Customer.phone, Customer.customer_name, Customer.unit_name]
    name = "客户库"
    name_plural = "客户库"

class OrderAdmin(ModelView, model=Order):
    column_list = [Order.id, Order.product_title, Order.amount, Order.order_date, Order.customer_id, Order.user_id]
    name = "订单记录"
    name_plural = "订单记录"

class RelationAdmin(ModelView, model=UserCustomerRelation):
    column_list = [UserCustomerRelation.id, UserCustomerRelation.user_id, UserCustomerRelation.customer_id, UserCustomerRelation.relation_type, UserCustomerRelation.budget_amount]
    name = "员工与线索"
    name_plural = "员工与线索"

class ChatAdmin(ModelView, model=ChatMessage):
    column_list = [ChatMessage.id, ChatMessage.role, ChatMessage.content, ChatMessage.customer_id]
    column_searchable_list = [ChatMessage.content]
    name = "聊天对话"
    name_plural = "聊天对话"

class ProductAdmin(ModelView, model=Product):
    column_list = [Product.id, Product.product_name, Product.product_id, Product.price, Product.supplier_name]
    column_searchable_list = [Product.product_name, Product.product_id]
    name = "通用商品库"
    name_plural = "通用商品库"

class ConfigAdmin(ModelView, model=SystemConfig):
    column_list = [SystemConfig.id, SystemConfig.config_key, SystemConfig.config_group, SystemConfig.updated_at]
    name = "系统核心参数"
    name_plural = "系统核心参数"

admin_views = [UserAdmin, CustomerAdmin, OrderAdmin, RelationAdmin, ChatAdmin, ProductAdmin, ConfigAdmin]
