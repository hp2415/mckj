from sqlalchemy import Column, Integer, String, Boolean, DateTime, Date, ForeignKey, Text, Numeric
from sqlalchemy.orm import DeclarativeBase
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
    
    # 核心关联字段：使用手机号进行“逻辑关联”，增加级联更新约束
    consignee_phone = Column(String(20), ForeignKey("customers.phone", onupdate="CASCADE"), index=True, nullable=False) 
    
    # 订单索引与归属
    store = Column(String(50), nullable=True)          # 店铺编码
    order_id = Column(String(100), unique=True, index=True) # 内部业务 ID (通常来自 API)
    dddh = Column(String(100), nullable=True)          # 订单号 (展示名)
    
    # 财务详情
    pay_type_name = Column(String(50), nullable=True)  # 支付方式
    pay_amount = Column(Numeric(12, 2), default=0.0)   # 支付金额
    freight = Column(Numeric(10, 2), default=0.0)      # 运费
    
    # 状态与生命周期
    status_name = Column(String(50), nullable=True)    # 订单状态 (如：已支付、待发货)
    order_time = Column(DateTime, nullable=True)       # 订单创建时间
    update_time = Column(DateTime, nullable=True)      # 业务更新时间
    
    # 备注与备注
    remark = Column(Text, nullable=True)               # 备注信息
    product_title = Column(String(255), nullable=True) # 商品摘要
    
    # 收货详情 (用于侧栏地址匹配)
    consignee = Column(String(50), nullable=True)      # 收货人姓名
    consignee_address = Column(Text, nullable=True)     # 详细地址
    province_code = Column(String(20), nullable=True)
    city_code = Column(String(20), nullable=True)
    district_code = Column(String(20), nullable=True)
    
    # 采购单位 (B端客户深度画像)
    buyer_id = Column(String(100), nullable=True)      # 采购单位 ID
    buyer_name = Column(String(200), nullable=True)    # 采购单位名称
    buyer_phone = Column(String(20), nullable=True)    # 采购单位联系电话
    
    # 业务属性
    purchase_type = Column(Integer, default=0)         # 采购类型 (整数标识)
    user_id = Column(Integer, nullable=True)           # 选填：负责该订单的跟进员工 ID

# 4. UserCustomerRelation (归属关联表) - 加入了主观互动信息
class UserCustomerRelation(Base):
    __tablename__ = "user_customer_relations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 核心自然键关联：支持外部大批量导入，增加级联更新约束
    username = Column(String(50), ForeignKey("users.username", onupdate="CASCADE"), index=True, nullable=False)        # 对应员工工号 (zh_liang)
    customer_phone = Column(String(20), ForeignKey("customers.phone", onupdate="CASCADE"), index=True, nullable=False)   # 对应客户手机号 (138xxx)
    
    # 业务属性
    relation_type = Column(String(20), default="active", nullable=False)
    title = Column(String(50), nullable=True)                      # 员工对客户的独有称呼
    budget_amount = Column(Numeric(12, 2), default=0.0)            # 客户向该员工透露的预算
    contact_date = Column(Date, nullable=False, default=datetime.date.today) # 该员工与客户的建联时间
    ai_profile = Column(Text, nullable=True)                       # AI 为该员工生成的特定客户画像
    dify_conversation_id = Column(String(100), nullable=True)      # Dify AI 持久化对话 ID (支持业务移交)
    assigned_at = Column(DateTime, default=datetime.datetime.now, nullable=False)

    # 兼容性字段：保留 ID 引用但设为可选
    user_id = Column(Integer, nullable=True)
    customer_id = Column(Integer, nullable=True)

# 5. ChatMessage (聊天记录表)
class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    dify_conv_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)

# 6. Product (商品公用资源表)
class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(50), unique=True, nullable=True)
    product_id = Column(String(50), nullable=False)
    product_name = Column(String(255), nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    cover_img = Column(String(255), nullable=True)
    product_url = Column(String(500), nullable=True) # 新增：商品外部跳转页面
    unit = Column(String(20), nullable=True)
    supplier_name = Column(String(100), nullable=True)
    supplier_id = Column(String(50), nullable=True) # 供应商数字 ID

# 7. SystemConfig (系统配置表)
class SystemConfig(Base):
    __tablename__ = "system_configs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    config_key = Column(String(100), unique=True, nullable=False)
    config_value = Column(Text, nullable=False)
    config_group = Column(String(50), default="general", nullable=False)
    description = Column(String(255), nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now, nullable=False)