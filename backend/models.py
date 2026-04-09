from sqlalchemy import Column, Integer, String, Boolean, DateTime, Date, ForeignKey, Text, Numeric, Index
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.sql import func
import datetime

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

    # 关联对象
    # 关系线：改为 selectin 预加载，彻底解决 DetachedInstanceError
    relations = relationship("UserCustomerRelation", back_populates="user", lazy="selectin")
    chat_messages = relationship("ChatMessage", back_populates="user", lazy="selectin")

    def __str__(self):
        return f"{self.real_name} ({self.username})"

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
    purchase_months = Column(String(200), nullable=True) # 以逗号分隔的月份数据字符串

    # 关联对象
    # 关系线与对话：改为 selectin 预加载，提升列表页稳定性
    relations = relationship("UserCustomerRelation", back_populates="customer", lazy="selectin")
    orders = relationship("Order", back_populates="customer", lazy="selectin")
    chat_messages = relationship("ChatMessage", back_populates="customer", lazy="selectin")

    def __str__(self):
        return f"{self.customer_name} ({self.phone})"

# 3. Order (客户订单表)
class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    
    # 核心关联字段
    consignee_phone = Column(String(20), ForeignKey("customers.phone", onupdate="CASCADE"), index=True, nullable=False) 
    
    # 审计关联
    customer = relationship("Customer", back_populates="orders")

    # 订单详情
    store = Column(String(50), nullable=True)
    order_id = Column(String(100), unique=True, index=True)
    dddh = Column(String(100), nullable=True)
    pay_type_name = Column(String(50), nullable=True)
    pay_amount = Column(Numeric(12, 2), default=0.0)
    freight = Column(Numeric(10, 2), default=0.0)
    status_name = Column(String(50), nullable=True)
    order_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)
    remark = Column(Text, nullable=True)
    product_title = Column(String(255), nullable=True)
    consignee = Column(String(50), nullable=True)
    consignee_address = Column(Text, nullable=True)
    province_code = Column(String(20), nullable=True)
    city_code = Column(String(20), nullable=True)
    district_code = Column(String(20), nullable=True)
    buyer_id = Column(String(100), nullable=True)
    buyer_name = Column(String(200), nullable=True)
    buyer_phone = Column(String(20), nullable=True)
    purchase_type = Column(Integer, default=0)
    user_id = Column(Integer, nullable=True)

# 4. UserCustomerRelation (归属关联表)
class UserCustomerRelation(Base):
    __tablename__ = "user_customer_relations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 标准化物理外键
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=True)
    
    # 核心关联引用：改为 selectin 预加载，并移除冗余物理字段
    user = relationship("User", back_populates="relations", lazy="selectin")
    customer = relationship("Customer", back_populates="relations", lazy="selectin")

    # [RECOVERY] 核心业务字段恢复
    relation_type = Column(String(20), default="active", nullable=False)
    title = Column(String(50), nullable=True)
    budget_amount = Column(Numeric(12, 2), default=0.0)
    contact_date = Column(Date, nullable=False, default=datetime.date.today)
    purchase_type = Column(String(100), nullable=True)
    wechat_remark = Column(String(100), nullable=True)
    ai_profile = Column(Text, nullable=True)
    dify_conversation_id = Column(String(100), nullable=True)
    assigned_at = Column(DateTime, default=func.now(), nullable=False)

    def __str__(self):
        # 完全依赖逻辑关联渲染
        u_name = "未知"
        c_name = "未知"
        try:
            if self.user: u_name = self.user.real_name
            if self.customer: c_name = self.customer.customer_name
        except Exception:
            pass
        return f"{u_name} -> {c_name}"

# 5. ChatMessage (聊天记录表)
class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    # 核心关联：改为 selectin 预加载，避免在搜索跳转时产生 SQL JOIN 别名冲突
    customer = relationship("Customer", back_populates="chat_messages", lazy="selectin")
    user = relationship("User", back_populates="chat_messages", lazy="selectin")
    
    @hybrid_property
    def search_index(self):
        """组合搜索标识，格式: username_phone"""
        u = self.user.username if self.user else "System"
        c = self.customer.phone if self.customer else "Unknown"
        return f"{u}_{c}"

    @search_index.expression
    def search_index(cls):
        """数据库侧的复合搜索表达式：利用 SQLAlchemy 的内置逻辑处理 JOIN"""
        return func.concat(User.username, "_", Customer.phone)

    def __repr__(self):
        role_map = {"user": "员", "assistant": "AI"}
        r = role_map.get(self.role, "?")
        return f"[{r}] {self.content[:15]}..."

    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    dify_conv_id = Column(String(100), nullable=True)
    rating = Column(Integer, default=0, nullable=False)
    is_regenerated = Column(Boolean, default=False, nullable=False)
    is_copied = Column(Boolean, default=False, nullable=False)
    feedback_at = Column(DateTime, nullable=True)
    copied_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)

# 5.5. WechatHistory (原生微信流水长表)
class WechatHistory(Base):
    __tablename__ = "wechat_histories"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", onupdate="CASCADE"), index=True, nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id", onupdate="CASCADE"), index=True, nullable=False)
    sender_name = Column(String(100), nullable=True)
    chat_time = Column(DateTime, nullable=False)
    content = Column(Text, nullable=False)
    imported_at = Column(DateTime, default=datetime.datetime.now, nullable=False)

# 6. Product (商品公用资源表)
class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(50), unique=True, nullable=True)
    product_id = Column(String(50), nullable=False)
    product_name = Column(String(255), nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    cover_img = Column(String(255), nullable=True)
    product_url = Column(String(500), nullable=True)
    unit = Column(String(20), nullable=True)
    supplier_name = Column(String(100), nullable=True)
    supplier_id = Column(String(50), nullable=True)

# 7. SystemConfig (系统配置表)
class SystemConfig(Base):
    __tablename__ = "system_configs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    config_key = Column(String(100), unique=True, nullable=False)
    config_value = Column(Text, nullable=False)
    config_group = Column(String(50), default="general", nullable=False)
    description = Column(String(255), nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now, nullable=False)

# 8. BusinessTransfer (业务移交记录表)
from sqlalchemy.orm import relationship

class BusinessTransfer(Base):
    __tablename__ = "business_transfers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    from_user_id = Column(Integer, ForeignKey("users.id", onupdate="CASCADE"), nullable=False)
    to_user_id = Column(Integer, ForeignKey("users.id", onupdate="CASCADE"), nullable=False)
    transferred_count = Column(Integer, default=0)
    transfer_time = Column(DateTime, default=datetime.datetime.now)
    operator = Column(String(50), nullable=True)

    # 关联对象
    from_user = relationship("User", foreign_keys=[from_user_id])
    to_user = relationship("User", foreign_keys=[to_user_id])