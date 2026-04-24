from sqlalchemy import Column, Integer, String, Boolean, DateTime, Date, ForeignKey, Text, Numeric, Index, JSON, UniqueConstraint
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
    active_token_jti = Column(String(50), nullable=True) # 用于单端登录校验的 JWT 唯一标识符

    # 关联对象
    # 关系线：改为 select 延迟加载，防止 DetachedInstanceError 与 N+1 查询风暴
    relations = relationship("UserCustomerRelation", back_populates="user", lazy="select")
    chat_messages = relationship("ChatMessage", back_populates="user", lazy="select")

    def __str__(self):
        return f"{self.real_name} ({self.username})"

# 2. Customer (客户资料主表) - 只保留客观静态事实
class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    phone = Column(String(100), unique=True, index=True, nullable=True)
    unit_name = Column(String(100), nullable=False)
    customer_name = Column(String(50), nullable=False)
    unit_type = Column(String(50), nullable=True)
    admin_division = Column(String(100), nullable=True)
    external_id = Column(String(50), nullable=True)
    purchase_months = Column(String(200), nullable=True) # 以逗号分隔的月份数据字符串
    profile_status = Column(Integer, default=0, server_default="0") # 0:未分析, 1:已分析

    # 关联对象
    # 关系线与对话：改为 select 延迟加载，防止性能隐患
    relations = relationship("UserCustomerRelation", back_populates="customer", lazy="select")
    orders = relationship("Order", back_populates="customer", lazy="select")
    chat_messages = relationship("ChatMessage", back_populates="customer", lazy="select")

    def __str__(self):
        return f"{self.customer_name} ({self.phone})"

# 3. Order (客户订单表)
class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    
    # 核心关联字段：迁移至 ID 绑定，物理手机号保留作为快照
    customer_id = Column(Integer, ForeignKey("customers.id"), index=True, nullable=True)
    consignee_phone = Column(String(100), index=True, nullable=False) 
    
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
    buyer_phone = Column(String(100), nullable=True)
    purchase_type = Column(Integer, default=0)
    user_id = Column(Integer, nullable=True)

# 4. UserCustomerRelation (归属关联表)
class UserCustomerRelation(Base):
    __tablename__ = "user_customer_relations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 标准化物理外键
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=True)
    
    # 核心关联引用：改为 select 延迟加载，并移除冗余物理字段
    user = relationship("User", back_populates="relations", lazy="select")
    customer = relationship("Customer", back_populates="relations", lazy="select")

    # [RECOVERY] 核心业务字段恢复
    relation_type = Column(String(20), default="active", nullable=False)
    title = Column(String(50), nullable=True)
    budget_amount = Column(Numeric(12, 2), default=0.0)
    contact_date = Column(Date, nullable=False, default=datetime.date.today)
    purchase_type = Column(String(100), nullable=True)
    wechat_remark = Column(String(100), nullable=True)
    ai_profile = Column(Text, nullable=True)
    suggested_followup_date = Column(Date, nullable=True)
    dify_conversation_id = Column(String(100), nullable=True)
    assigned_at = Column(DateTime, default=func.now(), nullable=False)

    def __str__(self):
        # 针对 Session 脱离场景进行鲁棒性处理，防止 DetachedInstanceError
        from sqlalchemy import inspect
        ins = inspect(self)
        
        # 检查 user 关联是否已加载
        if "user" in ins.unloaded:
            u_name = f"User(ID:{self.user_id})"
        else:
            u_name = self.user.real_name if self.user else "未知"
            
        # 检查 customer 关联是否已加载
        if "customer" in ins.unloaded:
            c_name = f"Cust(ID:{self.customer_id})"
        else:
            c_name = self.customer.customer_name if self.customer else "未知"
            
        return f"{u_name} -> {c_name}"

# 5. ChatMessage (聊天记录表)
class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    # 核心关联：改为 select 延迟加载，避免在搜索跳转时产生 SQL JOIN 别名冲突
    customer = relationship("Customer", back_populates="chat_messages", lazy="select")
    user = relationship("User", back_populates="chat_messages", lazy="select")
    
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
    category_name_one = Column(String(50), nullable=True)
    category_name_two = Column(String(50), nullable=True)
    category_name_three = Column(String(50), nullable=True)
    origin_province = Column(String(50), nullable=True)
    origin_city = Column(String(50), nullable=True)
    origin_district = Column(String(50), nullable=True)

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

# 9. SyncFailure (同步失败供货商记录)
class SyncFailure(Base):
    __tablename__ = "sync_failures"
    id = Column(Integer, primary_key=True, autoincrement=True)
    supplier_id = Column(String(50), unique=True, nullable=False)
    last_error = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)

# 10. RawCustomer (业务系统原始客户表)
class RawCustomer(Base):
    __tablename__ = "raw_customers"
    id = Column(String(100), primary_key=True)
    type = Column(Integer, nullable=True)
    from_type = Column(String(50), nullable=True)
    head_url = Column(String(500), nullable=True)
    create_time = Column(DateTime, nullable=True)
    add_time = Column(DateTime, nullable=True)
    sales_wechat_id = Column(String(100), nullable=True)
    is_deleted = Column(Boolean, default=False)
    update_time = Column(DateTime, nullable=True)
    alias = Column(String(100), nullable=True)
    name = Column(String(100), nullable=True)
    remark = Column(String(100), nullable=True)
    phone = Column(String(100), nullable=True)
    description = Column(Text, nullable=True)
    note_des = Column(Text, nullable=True)
    gender = Column(String(10), nullable=True)
    region = Column(String(100), nullable=True)
    label = Column(String(200), nullable=True)
    profile_status = Column(Integer, default=0, server_default="0") # 0:未分析, 1:已分析
    synced_at = Column(DateTime, default=datetime.datetime.now)

# 11. RawChatLog (业务系统原始聊天记录表)
class RawChatLog(Base):
    __tablename__ = "raw_chat_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    talker = Column(String(100), index=True)
    wechat_id = Column(String(100), index=True)
    text = Column(Text)
    timestamp = Column(Numeric(20, 0)) # 存储毫秒时间戳
    is_send = Column(Integer)
    message_type = Column(Integer)
    name = Column(String(100))
    file_source = Column(String(100))
    imported_at = Column(DateTime, default=datetime.datetime.now)

# 12. RawOrder (API 原始订单表)
class RawOrder(Base):
    __tablename__ = "raw_orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(100), unique=True, index=True)
    dddh = Column(String(100), index=True)
    store = Column(String(50))
    pay_type_name = Column(String(50))
    pay_amount = Column(Numeric(12, 2))
    freight = Column(Numeric(10, 2))
    status_name = Column(String(50))
    order_time = Column(DateTime)
    update_time = Column(DateTime)
    remark = Column(Text)
    consignee = Column(String(100))
    consignee_phone = Column(String(100), index=True)
    consignee_address = Column(Text)
    buyer_id = Column(String(100))
    buyer_name = Column(String(200))
    buyer_phone = Column(String(100), index=True)
    purchase_type = Column(Integer)
    search_phone = Column(String(100), index=True)
    raw_json = Column(Text)
    imported_at = Column(DateTime, default=datetime.datetime.now)

# 13. RawOrderItem (API 原始订单商品详情)
class RawOrderItem(Base):
    __tablename__ = "raw_order_items"
    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_order_id = Column(Integer, ForeignKey("raw_orders.id", ondelete="CASCADE"))
    uuid = Column(String(100))
    product_name = Column(String(255))
    number = Column(Integer)
    pay_price = Column(Numeric(12, 2))
    pay_money = Column(Numeric(12, 2))
    sku_id = Column(String(100))


# ============ 提示词场景化与管理平台 ============

# 14. PromptScenario - 提示词场景主表
class PromptScenario(Base):
    __tablename__ = "prompt_scenarios"
    id = Column(Integer, primary_key=True, autoincrement=True)
    scenario_key = Column(String(80), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    tools_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.now, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, default=datetime.datetime.now, onupdate=datetime.datetime.now, server_default=func.now())

    versions = relationship(
        "PromptVersion",
        back_populates="scenario",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __str__(self):
        return f"{self.name}({self.scenario_key})"


# 15. PromptVersion - 提示词版本（含 template/doc_refs/params）
class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    scenario_id = Column(Integer, ForeignKey("prompt_scenarios.id", ondelete="CASCADE"), index=True, nullable=False)
    version = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="draft", server_default="draft")
    template_json = Column(JSON, nullable=False)
    doc_refs_json = Column(JSON, nullable=True)
    params_json = Column(JSON, nullable=True)
    rollout_json = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.now, server_default=func.now())
    published_at = Column(DateTime, nullable=True)

    scenario = relationship("PromptScenario", back_populates="versions", lazy="select")

    __table_args__ = (
        UniqueConstraint("scenario_id", "version", name="uq_prompt_version_sv"),
        Index("ix_prompt_versions_scenario_status", "scenario_id", "status"),
    )

    def __str__(self):
        return f"V{self.version}({self.status})"

    # -------- 管理后台友好视图（把 JSON 字段拆成纯文本/列表，供表单展示） --------
    @property
    def template_system(self) -> str:
        """从 template_json 中取出纯文本/markdown 的 system 正文。"""
        tj = self.template_json
        if isinstance(tj, dict):
            return str(tj.get("system") or "")
        if isinstance(tj, str):
            return tj
        return ""

    @template_system.setter
    def template_system(self, value: str) -> None:
        base = dict(self.template_json) if isinstance(self.template_json, dict) else {}
        base["system"] = value or ""
        self.template_json = base

    @property
    def template_user(self) -> str:
        """可选 user 消息模板（如客户画像场景）；占位符与 system 相同，使用 {{var}}。"""
        tj = self.template_json
        if isinstance(tj, dict):
            return str(tj.get("user") or "")
        return ""

    @template_user.setter
    def template_user(self, value: str) -> None:
        base = dict(self.template_json) if isinstance(self.template_json, dict) else {}
        if value and str(value).strip():
            base["user"] = str(value).strip()
        else:
            base.pop("user", None)
        if "system" not in base:
            base["system"] = ""
        self.template_json = base

    @property
    def template_notes(self) -> str:
        tj = self.template_json
        if isinstance(tj, dict):
            return str(tj.get("notes") or "")
        return ""

    @template_notes.setter
    def template_notes(self, value: str) -> None:
        base = dict(self.template_json) if isinstance(self.template_json, dict) else {}
        if value:
            base["notes"] = value
        else:
            base.pop("notes", None)
        # 不能完全置空，至少保留 system 键的形态
        if "system" not in base:
            base["system"] = ""
        self.template_json = base

    @property
    def doc_refs_keys(self) -> list[str]:
        """从 doc_refs_json 数组取出所有 doc_key，供多选框回显。"""
        refs = self.doc_refs_json or []
        if not isinstance(refs, list):
            return []
        out: list[str] = []
        for r in refs:
            if isinstance(r, dict):
                k = r.get("doc_key")
                if k:
                    out.append(str(k))
        return out


# 16. PromptDoc - 参考话术文档主表
class PromptDoc(Base):
    __tablename__ = "prompt_docs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    doc_key = Column(String(80), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.now, server_default=func.now())

    versions = relationship(
        "PromptDocVersion",
        back_populates="doc",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __str__(self):
        return f"{self.name}({self.doc_key})"


# 17. PromptDocVersion - 参考话术文档版本内容
class PromptDocVersion(Base):
    __tablename__ = "prompt_doc_versions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    doc_id = Column(Integer, ForeignKey("prompt_docs.id", ondelete="CASCADE"), index=True, nullable=False)
    version = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="draft", server_default="draft")
    content = Column(Text(length=16_000_000), nullable=False)
    source_filename = Column(String(255), nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.now, server_default=func.now())
    published_at = Column(DateTime, nullable=True)

    doc = relationship("PromptDoc", back_populates="versions", lazy="select")

    __table_args__ = (
        UniqueConstraint("doc_id", "version", name="uq_prompt_doc_version_dv"),
        Index("ix_prompt_doc_versions_doc_status", "doc_id", "status"),
    )

    def __str__(self):
        return f"DocV{self.version}({self.status})"


# 18. PromptRule - Phase3 动态标签决策规则（预留，MVP 不查询）
class PromptRule(Base):
    __tablename__ = "prompt_rules"
    id = Column(Integer, primary_key=True, autoincrement=True)
    scenario_id = Column(Integer, ForeignKey("prompt_scenarios.id", ondelete="CASCADE"), index=True, nullable=False)
    priority = Column(Integer, nullable=False, default=0, server_default="0")
    condition_json = Column(JSON, nullable=True)
    action_json = Column(JSON, nullable=True)
    status = Column(String(20), nullable=False, default="disabled", server_default="disabled")
    description = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.now, server_default=func.now())


# 19. PromptAuditLog - 管理操作审计
class PromptAuditLog(Base):
    __tablename__ = "prompt_audit_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_id = Column(Integer, nullable=True)
    action = Column(String(50), nullable=False)
    target_type = Column(String(50), nullable=False)
    target_id = Column(Integer, nullable=True)
    payload_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.now, server_default=func.now())

    __table_args__ = (
        Index("ix_prompt_audit_log_target", "target_type", "target_id"),
    )