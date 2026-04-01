from sqlalchemy import Column, Integer, String, Boolean, DateTime, Date, ForeignKey, Text, Numeric
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import func

class Base(DeclarativeBase):
    pass

# 1. User (工作人员表)
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    real_name = Column(String(50), nullable=False)
    wechat_id = Column(String(100), unique=True, nullable=True)
    role = Column(String(20), default="staff", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

# 2. Customer (客户资料主表) - 只保留客观静态事实
class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    phone = Column(String(20), unique=True, index=True, nullable=False)
    unit_name = Column(String(100), nullable=False)
    customer_name = Column(String(50), nullable=False)
    unit_type = Column(String(50), nullable=True)
    admin_division = Column(String(100), nullable=True)
    external_id = Column(String(50), nullable=True)

# 3. Order (客户订单表)
class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True) # 新增：这笔订单是哪个员工带来的
    order_date = Column(DateTime, nullable=False)
    product_title = Column(String(255), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    category_name = Column(String(50), nullable=True)
    external_order_id = Column(String(50), unique=True, nullable=True)

# 4. UserCustomerRelation (归属关联表) - 加入了主观互动信息
class UserCustomerRelation(Base):
    __tablename__ = "user_customer_relations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    relation_type = Column(String(20), default="active", nullable=False)
    title = Column(String(50), nullable=True)                      # 员工对客户的独有称呼
    budget_amount = Column(Numeric(12, 2), default=0.0)            # 客户向该员工透露的预算
    contact_date = Column(Date, nullable=False, default=func.current_date()) # 该员工与客户的建联时间
    ai_profile = Column(Text, nullable=True)                       # AI 为该员工生成的特定客户画像
    assigned_at = Column(DateTime, default=func.now(), nullable=False)

# 5. ChatMessage (聊天记录表)
class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    dify_conv_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

# 6. Product (商品公用资源表)
class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(50), unique=True, nullable=True)
    product_id = Column(String(50), nullable=False)
    product_name = Column(String(255), nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    cover_img = Column(String(255), nullable=True)
    unit = Column(String(20), nullable=True)
    supplier_name = Column(String(100), nullable=True)

# 7. SystemConfig (系统配置表)
class SystemConfig(Base):
    __tablename__ = "system_configs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    config_key = Column(String(100), unique=True, nullable=False)
    config_value = Column(Text, nullable=False)
    config_group = Column(String(50), default="general", nullable=False)
    description = Column(String(255), nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)