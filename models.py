from sqlalchemy import Column, Integer, String, Float, DateTime, Date, ForeignKey, Text, Numeric
from sqlalchemy.orm import relationship, DeclarativeBase
from sqlalchemy.sql import func
from sqlalchemy.ext.declarative import declarative_base

class Base(DeclarativeBase):
    pass
# 1. 用户表
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    real_name = Column(String(50))
    wechat_nickname = Column(String(100))

# 2. 客户表
class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String(20), unique=True, index=True, nullable=False)
    unit_name = Column(String(100), nullable=False)
    customer_name = Column(String(50), nullable=False)
    title = Column(String(50))
    unit_type = Column(String(50))
    # 修正：使用 Numeric(12, 2)
    budget_amount = Column(Numeric(12, 2), default=0.0)
    admin_division = Column(String(100))
    contact_date = Column(Date, default=func.current_date())
    external_id = Column(String(50))

# 3. 订单表
class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    order_date = Column(DateTime, nullable=False)
    product_title = Column(String(255), nullable=False)
    # 修正：使用 Numeric(12, 2)
    amount = Column(Numeric(12, 2), nullable=False)
    category_name = Column(String(50))
    external_order_id = Column(String(50), unique=True)

# 4. 员工-客户关联表
class UserCustomerRelation(Base):
    __tablename__ = "user_customer_relations"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    customer_id = Column(Integer, ForeignKey("customers.id"))
    status = Column(String(20), default="active")

# 5. 聊天记录表
class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    role = Column(String(20))  # user, assistant
    content = Column(Text, nullable=False)
    dify_conv_id = Column(String(100))
    created_at = Column(DateTime, server_default=func.now())

# 6. 商品精选表
class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    uuid = Column(String(50), unique=True)
    product_id = Column(String(50), nullable=False)
    product_name = Column(String(255), nullable=False)
    # 修正：使用 Numeric(10, 2)
    price = Column(Numeric(10, 2))
    cover_img = Column(String(255))
    unit = Column(String(20))
    supplier_name = Column(String(100))
    link_url = Column(String(255))

# 7. 系统配置表
class SystemConfig(Base):
    __tablename__ = "system_configs"
    id = Column(Integer, primary_key=True)
    config_key = Column(String(100), unique=True)
    config_value = Column(Text, nullable=False)
    config_group = Column(String(50), default="gen")