from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Date,
    ForeignKey,
    Text,
    Numeric,
    Index,
    JSON,
    UniqueConstraint,
    Table,
    and_,
)
from sqlalchemy.orm import DeclarativeBase, relationship, foreign
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
    wechat_remark_for_prompt = Column(String(200), nullable=True)
    wechat_id = Column(String(100), unique=True, nullable=True)
    role = Column(String(20), default="staff", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    active_token_jti = Column(String(50), nullable=True) # 用于单端登录校验的 JWT 唯一标识符

    # 关联对象
    # 关系线：改为 select 延迟加载，防止 DetachedInstanceError 与 N+1 查询风暴
    sales_customer_profiles = relationship(
        "SalesCustomerProfile", back_populates="user", lazy="select"
    )
    chat_messages = relationship("ChatMessage", back_populates="user", lazy="select")
    sales_wechat_bindings = relationship(
        "UserSalesWechat",
        back_populates="user",
        lazy="select",
        cascade="all, delete-orphan",
    )

    # 新增：直接关联销售微信主数据（方便管理后台在编辑账号时，能搜索到主数据池里的所有微信进行新增绑定）
    wechat_accounts = relationship(
        "SalesWechatAccount",
        secondary="user_sales_wechats",
        primaryjoin="User.id == UserSalesWechat.user_id",
        secondaryjoin="SalesWechatAccount.sales_wechat_id == UserSalesWechat.sales_wechat_id",
        overlaps="sales_wechat_bindings,user",
        viewonly=False,
        lazy="select",
    )

    @property
    def sales_wechat_bindings_count(self) -> int:
        try:
            return len(self.sales_wechat_bindings or [])
        except Exception:
            return 0

    def __str__(self):
        return f"{self.real_name} ({self.username})"


class UserSalesWechat(Base):
    """登录用户与云客侧销售微信号（业务微信）绑定；全局唯一 sales_wechat_id，交接时改 user_id 即可。"""

    __tablename__ = "user_sales_wechats"
    __table_args__ = (Index("ix_user_sales_wechats_user_id", "user_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    sales_wechat_id = Column(String(100), unique=True, nullable=False)
    label = Column(String(100), nullable=True)
    is_primary = Column(Boolean, default=False, nullable=False, server_default="0")
    created_at = Column(DateTime, default=func.now(), nullable=False)
    verified_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="sales_wechat_bindings")

    def __str__(self) -> str:
        # 用于管理后台展示，避免出现 <models.UserSalesWechat object at ...>
        sw = (self.sales_wechat_id or "").strip()
        label = (self.label or "").strip()
        if label and sw:
            return f"{sw}（{label}）"
        return sw or label or f"Binding#{self.id}"


class SalesWechatAccount(Base):
    """
    销售业务微信主数据，与云客侧 sales_wechat_id（如 wxid_…）对齐。
    前期由 accounts.xlsx 导入；后期可由云客接口同步覆盖/更新。
    昵称、别名用于 LLM 上下文中的「当前业务微信」说明（非 raw_customers 字段）。
    """

    __tablename__ = "sales_wechat_accounts"

    sales_wechat_id = Column(String(100), primary_key=True, nullable=False)
    nickname = Column(String(200), nullable=True)
    alias_name = Column(String(200), nullable=True)
    account_code = Column(String(100), nullable=True)
    phone = Column(String(50), nullable=True)
    source = Column(String(30), default="xlsx", nullable=False, server_default="xlsx")
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    def __str__(self) -> str:
        sw = (self.sales_wechat_id or "").strip()
        nick = (self.nickname or "").strip()
        alias = (self.alias_name or "").strip()
        display = nick or alias or "未命名"
        return f"{sw} ({display})"


class ProfileTagDefinition(Base):
    """管理平台维护的客户画像动态标签：特征与策略说明会注入画像 LLM，匹配结果挂在跟进关系上。"""

    __tablename__ = "profile_tag_definitions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), nullable=False)
    feature_note = Column(Text, nullable=True)
    strategy_note = Column(Text, nullable=True)
    sort_order = Column(Integer, default=0, nullable=False, server_default="0")
    is_active = Column(Boolean, default=True, nullable=False, server_default="1")
    # 须用 Python 侧 default，勿用 func.now()：sqladmin 新建表单的 DateTimeField 会误判 SQL 表达式并报错
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)

    def __str__(self):
        return self.name or f"Tag#{self.id}"


scp_profile_tags = Table(
    "scp_profile_tags",
    Base.metadata,
    Column(
        "sales_customer_profile_id",
        Integer,
        ForeignKey("sales_customer_profiles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "profile_tag_id",
        Integer,
        ForeignKey("profile_tag_definitions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Index("ix_scp_profile_tags_profile_tag_id", "profile_tag_id"),
)


class SalesCustomerProfile(Base):
    """
    私域画像与跟进线：同一客户在不同销售微信号下，允许存在独立的一行画像与标签集合。
    主键用自增，业务唯一约束为 (raw_customer_id, sales_wechat_id)。
    """

    __tablename__ = "sales_customer_profiles"
    __table_args__ = (
        UniqueConstraint(
            "raw_customer_id",
            "sales_wechat_id",
            name="uq_scp_customer_sales_wechat",
        ),
        Index("ix_scp_user_sales", "user_id", "sales_wechat_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_customer_id = Column(
        String(100),
        ForeignKey("raw_customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sales_wechat_id = Column(
        String(100),
        ForeignKey("sales_wechat_accounts.sales_wechat_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    # 画像与私域字段（原 UCR 侧）
    relation_type = Column(String(20), default="active", nullable=False, server_default="active")
    title = Column(String(50), nullable=True)
    budget_amount = Column(Numeric(12, 2), default=0.0, server_default="0")
    contact_date = Column(Date, nullable=True)
    purchase_type = Column(String(100), nullable=True)
    wechat_remark = Column(String(200), nullable=True)
    ai_profile = Column(Text, nullable=True)
    suggested_followup_date = Column(Date, nullable=True)
    dify_conversation_id = Column(String(100), nullable=True)

    # per-sales 画像状态（避免 raw_customers.profile_status 的全局串扰）
    profile_status = Column(Integer, default=0, nullable=False, server_default="0")  # 0未分析,1已分析
    profiled_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="sales_customer_profiles", lazy="select")
    raw_customer = relationship("RawCustomer", back_populates="sales_profiles", lazy="select")
    profile_tags = relationship(
        "ProfileTagDefinition",
        secondary=scp_profile_tags,
        lazy="select",
        order_by=(ProfileTagDefinition.sort_order, ProfileTagDefinition.id),
    )

    def __str__(self) -> str:
        return f"SCP#{self.id}({self.raw_customer_id},{self.sales_wechat_id})"


# 5. ChatMessage (聊天记录表)
class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_customer_id = Column(
        String(100), ForeignKey("raw_customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    # 核心关联：改为 select 延迟加载，避免在搜索跳转时产生 SQL JOIN 别名冲突
    raw_customer = relationship("RawCustomer", back_populates="chat_messages", lazy="select")
    user = relationship("User", back_populates="chat_messages", lazy="select")
    
    @hybrid_property
    def search_index(self):
        """组合搜索标识，格式: username_phone"""
        u = self.user.username if self.user else "System"
        c = self.raw_customer.phone if self.raw_customer else "Unknown"
        return f"{u}_{c}"

    @search_index.expression
    def search_index(cls):
        """数据库侧的复合搜索表达式：利用 SQLAlchemy 的内置逻辑处理 JOIN"""
        return func.concat(User.username, "_", RawCustomer.phone)

    def __repr__(self):
        role_map = {"user": "员", "assistant": "AI"}
        r = role_map.get(self.role, "?")
        return f"[{r}] {self.content[:15]}..."

    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    # 与侧栏「客户×业务微信」行对齐；多号绑定下各号线程的 AI 历史互不串台
    sales_wechat_id = Column(String(100), nullable=True)
    dify_conv_id = Column(String(100), nullable=True)
    # AI 回复所用模型（仅 assistant 角色必填；user 消息可为空）
    chat_model = Column(String(80), nullable=True)
    rating = Column(Integer, default=0, nullable=False)
    is_regenerated = Column(Boolean, default=False, nullable=False)
    is_copied = Column(Boolean, default=False, nullable=False)
    feedback_at = Column(DateTime, nullable=True)
    copied_at = Column(DateTime, nullable=True)
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
    profile_status = Column(Integer, default=0, server_default="0") # 0:未分析, 1:已分析（原始池）
    synced_at = Column(DateTime, default=datetime.datetime.now)

    # 归一化/业务层字段（原 customers 表字段合并至此）
    phone_normalized = Column(String(100), nullable=True, index=True)
    customer_name = Column(String(100), nullable=True)
    unit_name = Column(String(200), nullable=True)
    unit_type = Column(String(50), nullable=True)
    admin_division = Column(String(100), nullable=True)
    purchase_months = Column(JSON, nullable=True)
    profile_updated_at = Column(DateTime, nullable=True)
    entity_created_at = Column(DateTime, default=func.now(), nullable=False)
    entity_updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    sales_profiles = relationship(
        "SalesCustomerProfile",
        back_populates="raw_customer",
        lazy="select",
        cascade="all, delete-orphan",
    )
    chat_messages = relationship(
        "ChatMessage",
        back_populates="raw_customer",
        lazy="select",
        cascade="all, delete-orphan",
    )

    def __str__(self) -> str:
        # 用于管理后台/日志展示，避免出现 <models.RawCustomer object at ...>
        name = (self.customer_name or self.remark or self.name or "").strip()
        phone = (self.phone_normalized or self.phone or "").strip()
        extra = " ".join([x for x in [name, phone] if x]) or "—"
        return f"{self.id} ({extra})"


class RawCustomerSalesWechat(Base):
    """
    云客导出的好友记录中，同一个客户 id 可能被多个销售微信号添加。
    raw_customers 保留「客户实体」(按 id 去重)；本表保留 (客户, sales_wechat_id) 的归属关系，避免覆盖丢失。
    """

    __tablename__ = "raw_customer_sales_wechats"
    __table_args__ = (
        UniqueConstraint("raw_customer_id", "sales_wechat_id", name="uq_rcsw_customer_sales"),
        Index("ix_rcsw_sales_wechat_id", "sales_wechat_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_customer_id = Column(
        String(100),
        ForeignKey("raw_customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sales_wechat_id = Column(String(100), nullable=False)
    # 归属维度下的“好友快照字段”（与云客 wechat_friends 对齐；允许不同销售号下值不同）
    alias = Column(String(100), nullable=True)
    name = Column(String(100), nullable=True)
    remark = Column(String(100), nullable=True)
    phone = Column(String(100), nullable=True)
    label = Column(String(200), nullable=True)
    head_url = Column(String(500), nullable=True)
    description = Column(Text, nullable=True)
    note_des = Column(Text, nullable=True)
    gender = Column(String(10), nullable=True)
    region = Column(String(100), nullable=True)
    type = Column(Integer, nullable=True)
    from_type = Column(String(50), nullable=True)

    create_time = Column(DateTime, nullable=True)
    add_time = Column(DateTime, nullable=True)
    update_time = Column(DateTime, nullable=True)
    last_chat_time = Column(DateTime, nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False, server_default="0")
    synced_at = Column(DateTime, nullable=True)

    raw_customer = relationship("RawCustomer", lazy="select")
    # per-sales 画像（viewonly）：(raw_customer_id, sales_wechat_id) → SalesCustomerProfile
    sales_profile = relationship(
        "SalesCustomerProfile",
        primaryjoin=and_(
            raw_customer_id == foreign(SalesCustomerProfile.raw_customer_id),
            sales_wechat_id == foreign(SalesCustomerProfile.sales_wechat_id),
        ),
        viewonly=True,
        uselist=False,
        lazy="select",
    )

# 11. RawChatLog (业务系统原始聊天记录表)
class RawChatLog(Base):
    __tablename__ = "raw_chat_logs"
    __table_args__ = (
        UniqueConstraint("wechat_id", "talker", "msg_svr_id", name="uq_raw_chat_wechat_talker_msg"),
        Index("ix_raw_chat_time_ms", "time_ms"),
        Index("ix_raw_chat_wechat_time", "wechat_id", "time_ms"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    talker = Column(String(100), index=True)
    wechat_id = Column(String(100), index=True)
    msg_svr_id = Column(String(100), nullable=True)
    roomid = Column(String(100), nullable=True)
    # 业务可读内容（按 type 解释后的文本/标题/文件名/定位地址等）
    text = Column(Text)
    # 保留原始消息 JSON，避免字段丢失（refermsgjson/patMsgs/originalImage 等）
    raw_json = Column(Text, nullable=True)
    # 发送时间 / 保存时间（13位毫秒）
    send_timestamp_ms = Column(Numeric(20, 0), nullable=True)
    time_ms = Column(Numeric(20, 0), nullable=True)
    # 兼容历史字段：timestamp 仍保留（旧数据/代码），新写入优先用 time_ms
    timestamp = Column(Numeric(20, 0)) # 存储毫秒时间戳（历史字段）
    is_send = Column(Integer)
    message_type = Column(Integer)
    # 兼容历史字段：name 过去被滥用为 text 预览；新同步不再写该字段
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
    # 桌面端场景分流：free_chat（无客户）、customer_chat（绑定客户）、backend_only（仅后台任务如画像）
    ui_category = Column(
        String(20),
        nullable=False,
        default="customer_chat",
        server_default="customer_chat",
    )
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