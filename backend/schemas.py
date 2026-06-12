from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Literal
from datetime import date
from decimal import Decimal
import datetime


def normalize_purchase_months(value: Optional[str]) -> str:
    """
    客户「采购月份」在库中约定为英文逗号分隔，与桌面端 MultiSelect 解析一致。
    将模型常输出的顿号、中文逗号等统一为 ", " 分隔。
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    for sep in "、，；;":
        s = s.replace(sep, ",")
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return ", ".join(parts)


class ProfileTagOut(BaseModel):
    id: int
    name: str
    feature_note: Optional[str] = None
    strategy_note: Optional[str] = None

    class Config:
        from_attributes = True


# 桌面端上报的综合客户信息片段（包含客观与主观信息）
class CustomerSync(BaseModel):
    phone: str = Field(..., description="客户手机号，作为客观实体的唯一标识")
    customer_name: str = Field(..., description="客户真实姓名")
    unit_name: str = Field(..., description="单位名称")
    unit_type: Optional[str] = None
    admin_division: Optional[str] = None
    external_id: Optional[str] = None
    
    # 以下为主观/互动字段 (UserCustomerRelation)
    title: Optional[str] = None
    budget_amount: Optional[Decimal] = Decimal("0.00")
    ai_profile: Optional[str] = None
    
    # 新增客观/业务属性字段
    purchase_months: Optional[str] = None

class CustomerResponse(BaseModel):
    id: str
    phone: Optional[str] = None
    customer_name: str
    unit_name: str
    # 返回的当前员工互动属性
    title: Optional[str] = None
    budget_amount: Decimal = Decimal("0.00")
    # 列表瘦身：/api/customer/my 不再返回画像全文（恒为 None），
    # 桌面端用 has_ai_profile 做分组判定，画像全文走 /id/{rid}/detail 按需拉取
    ai_profile: Optional[str] = None
    has_ai_profile: bool = False
    dify_conversation_id: Optional[str] = None
    contact_date: Optional[date] = None
    suggested_followup_date: Optional[date] = None
    sales_wechat_id: Optional[str] = None
    sales_wechat_label: Optional[str] = None  # sales_wechat_accounts.nickname（侧栏分组展示）

    # 返回的新增客观属性
    unit_type: Optional[str] = None
    admin_division: Optional[str] = None
    purchase_months: Optional[str] = None
    purchase_type: Optional[str] = None
    
    # 动态聚合属性
    historical_amount: Decimal = Decimal("0.00")
    historical_order_count: int = 0
    wechat_remark: Optional[str] = None
    profile_tags: List[ProfileTagOut] = Field(default_factory=list)

    class Config:
        from_attributes = True

class RegisterRequest(BaseModel):
    """桌面端自助注册：至少绑定一个销售微信号（与云客 raw_customers.sales_wechat_id 一致）。"""

    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=6, max_length=128)
    real_name: str = Field(..., min_length=1, max_length=50)
    sales_wechat_ids: list[str] = Field(..., min_length=1, max_length=50)

    @field_validator("sales_wechat_ids")
    @classmethod
    def strip_ids(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            t = (s or "").strip()
            if t and t not in out:
                out.append(t)
        if not out:
            raise ValueError("至少填写一个有效的销售微信号")
        return out


class SalesWechatBindingCreate(BaseModel):
    sales_wechat_id: str = Field(..., min_length=1, max_length=100)
    label: Optional[str] = Field(None, max_length=100)
    is_primary: bool = False


class SalesWechatBindingOut(BaseModel):
    id: int
    sales_wechat_id: str
    alias_name: Optional[str] = None
    label: Optional[str] = None
    is_primary: bool = False

    class Config:
        from_attributes = True


class MibuddyBindingCreate(BaseModel):
    uuid: str = Field(..., min_length=32, max_length=36, description="米城主系统用户 UUID")

    @field_validator("uuid")
    @classmethod
    def strip_uuid(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("UUID 不能为空")
        return s


class MibuddyUserProfileOut(BaseModel):
    uuid: str
    name: str
    account: str
    changhu: list[str] = Field(default_factory=list)


class MibuddyBindingOut(BaseModel):
    uuid: Optional[str] = None
    profile: Optional[MibuddyUserProfileOut] = None


class MibuddyLeadOut(BaseModel):
    id: int
    unit_name: str
    customer_name: str
    phone: str
    region: str = ""
    tags: str = "待设置"
    color: str = "灰色"
    budget: str = "待设置"
    followup_time: str = "待设置"
    wechat_id: str = "待设置"
    purchase_month: str = "待设置"
    purchase_type: str = "待设置"
    allocation_time: str = "-"
    recycle_days: str = "-"
    last_call_time: str = "-"
    is_favorite: bool = False
    favorite_time: str = "-"
    followup_records: list[dict] = Field(default_factory=list)
    remarks: str = ""


class MibuddyLeadsPageOut(BaseModel):
    page: int
    page_size: int
    total: int
    leads: list[MibuddyLeadOut] = Field(default_factory=list, serialization_alias="list", validation_alias="list")

    model_config = {"populate_by_name": True}


class MibuddyLeadFormUpdate(BaseModel):
    tags: Optional[str] = None
    color: Optional[str] = None
    purchase_month: Optional[str] = None
    followup_time: Optional[str] = None
    wechat_id: Optional[str] = None
    budget: Optional[str] = None
    purchase_type: Optional[str] = None
    is_favorite: Optional[bool] = None


class MibuddyLeadUpdateRequest(BaseModel):
    info: MibuddyLeadFormUpdate


class MibuddyLeadRemarkOut(BaseModel):
    id: int
    remark: str = ""
    create_time: str = ""


class MibuddyLeadRemarkCreate(BaseModel):
    remark: str = Field(..., min_length=1, max_length=500)


class MibuddyLeadRemarksPageOut(BaseModel):
    page: int
    page_size: int
    total: int
    remarks: list[MibuddyLeadRemarkOut] = Field(
        default_factory=list, serialization_alias="list", validation_alias="list"
    )

    model_config = {"populate_by_name": True}


class MibuddyCallYunkeRequest(BaseModel):
    tel: Optional[str] = Field(None, max_length=20)
    lead_id: Optional[int] = None
    user_wechat_account: Optional[str] = Field(None, min_length=1, max_length=100)


class MibuddyCallChanghuRequest(BaseModel):
    tel: Optional[str] = Field(None, max_length=20)
    lead_id: Optional[int] = None
    changhu_tel: str = Field(..., min_length=1, max_length=20)
    user_wechat_account: Optional[str] = Field(None, min_length=1, max_length=100)


class MibuddyCallYunkeOut(BaseModel):
    call_id: Optional[str] = None


# 销售人员更新客户动态资料的提交模型
class RelationUpdate(BaseModel):
    title: Optional[str] = None
    budget_amount: Optional[Decimal] = None
    ai_profile: Optional[str] = None
    dify_conversation_id: Optional[str] = None
    wechat_remark: Optional[str] = None

# 面板数据全量更新模型
class CustomerDataUpdate(BaseModel):
    # 客观属性 (Customer)
    customer_name: Optional[str] = None
    phone: Optional[str] = None
    unit_type: Optional[str] = None
    admin_division: Optional[str] = None
    purchase_months: Optional[str] = None
    
    # 主观属性 (UserCustomerRelation)
    contact_date: Optional[date] = None # 支持自定义建联日
    suggested_followup_date: Optional[date] = None # AI建议跟进日期
    purchase_type: Optional[str] = None # 移动到主观交互表
    
    # 动态业务属性 (UserCustomerRelation)
    title: Optional[str] = None
    budget_amount: Optional[Decimal] = None
    ai_profile: Optional[str] = None
    wechat_remark: Optional[str] = None
    dify_conversation_id: Optional[str] = None
    profile_tag_ids: Optional[List[int]] = None
    # 列表行来自某一业务微信；多号绑定时须指定，否则会误写到「主号」画像行
    sales_wechat_id: Optional[str] = None

# 聊天消息存取记录模型
class ChatMessageBase(BaseModel):
    role: str # 'user' 或 'assistant'
    content: str
    dify_conv_id: Optional[str] = None

class ChatMessageCreate(ChatMessageBase):
    is_regenerated: Optional[bool] = False
    sales_wechat_id: Optional[str] = None

class ChatMessageOut(ChatMessageBase):
    id: int
    rating: int
    is_regenerated: bool
    is_copied: bool
    chat_model: Optional[str] = None
    sales_wechat_id: Optional[str] = None
    created_at: datetime.datetime

    class Config:
        from_attributes = True

# 响应包装模型：用于隔离 ORM 对象并防止无限递归
class CustomerListResponse(BaseModel):
    code: int
    message: str
    data: List[CustomerResponse]

class ChatHistoryResponse(BaseModel):
    code: int
    data: List[ChatMessageOut]


class RawWechatChatLogOut(BaseModel):
    """云客同步的微信原始聊天记录（raw_chat_logs）。"""

    id: int
    text: str = ""
    is_send: int = 0
    time_ms: Optional[int] = None
    send_timestamp_ms: Optional[int] = None
    timestamp: Optional[int] = None
    message_type: Optional[int] = None
    name: Optional[str] = None

    @field_validator("text", mode="before")
    @classmethod
    def _coerce_text(cls, v):
        return (v or "").strip() if v is not None else ""

    @field_validator("is_send", mode="before")
    @classmethod
    def _coerce_is_send(cls, v):
        if v is None:
            return 0
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    @field_validator("time_ms", "send_timestamp_ms", "timestamp", "message_type", mode="before")
    @classmethod
    def _coerce_optional_int(cls, v):
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    class Config:
        from_attributes = True


class RawWechatChatLogResponse(BaseModel):
    code: int
    message: str = "ok"
    data: List[RawWechatChatLogOut]
    has_more: bool = False


class WechatOutboundCreate(BaseModel):
    """桌面端发起「发微信」审计记录（发送前调用）。"""

    raw_customer_id: str = Field(..., min_length=1, max_length=100)
    sales_wechat_id: str = Field(..., min_length=1, max_length=100)
    claimed_local_sales_wechat_id: str = Field(..., min_length=1, max_length=100)
    action_type: Literal["send", "edit_send"]
    edited_text: str = Field(..., min_length=1)
    original_text: Optional[str] = None
    source_chat_message_id: Optional[int] = None


class WechatOutboundResultIn(BaseModel):
    """RPA 执行结束后回写。"""

    status: Literal["sent", "failed", "blocked"]
    error: Optional[str] = None
    block_reason: Optional[str] = None
    auto_detected_wxid: Optional[str] = None


class ContactTaskOut(BaseModel):
    id: int
    batch_id: int
    raw_customer_id: str
    sales_wechat_id: str
    period_type: str
    due_date: date
    task_kind: str
    contact_channel: str = "wechat"
    priority_rank: int
    priority_score: Optional[float] = None
    title: Optional[str] = None
    instruction: Optional[str] = None
    status: str
    completed_at: Optional[datetime.datetime] = None
    completion_note: Optional[str] = None
    customer_name: Optional[str] = None
    unit_name: Optional[str] = None
    wechat_remark: Optional[str] = None
    phone: Optional[str] = None
    phone_raw: Optional[str] = None
    phone_normalized: Optional[str] = None
    ai_profile: Optional[str] = None
    suggested_followup_date: Optional[date] = None

    class Config:
        from_attributes = True


class TaskPeriodStatsOut(BaseModel):
    total: int = 0
    done: int = 0
    pending: int = 0
    in_progress: int = 0
    skipped: int = 0
    overdue: int = 0
    completion_rate: float = 0.0


class TaskOverviewOut(BaseModel):
    period_type: str
    period_start: date
    period_end: date
    batch_id: Optional[int] = None
    batch_status: Optional[str] = None
    stats: TaskPeriodStatsOut
    items: List[ContactTaskOut] = Field(default_factory=list)
    page: int = 1
    page_size: int = 0
    total_items: int = 0
    progress: Optional[dict] = None
    view_mode: Optional[str] = None  # month_progress = 月进度汇总（非分配批次）
    snapshot: Optional[dict] = None  # 批次分配快照摘要（渠道上限等）


class TaskAllocationJobOut(BaseModel):
    job_id: int
    batch_id: int
    status: str
    phase: Optional[str] = None
    detail: Optional[str] = None
    pct: float = 0.0
    task_count: Optional[int] = None
    error: Optional[str] = None
    period_type: str
    period_start: date
    period_end: date
    sales_wechat_id: str


class TaskCalendarDayOut(BaseModel):
    date: date
    total: int = 0
    done: int = 0
    pending: int = 0
    overdue: int = 0


class TaskCompleteIn(BaseModel):
    note: Optional[str] = None


class TaskSkipIn(BaseModel):
    note: Optional[str] = None


class TaskAppealIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class TaskAppealReasonStatOut(BaseModel):
    reason: str
    count: int = 0
