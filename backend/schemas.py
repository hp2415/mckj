from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
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
    ai_profile: Optional[str] = None
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
    label: Optional[str] = None
    is_primary: bool = False

    class Config:
        from_attributes = True


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

class ChatMessageOut(ChatMessageBase):
    id: int
    rating: int
    is_regenerated: bool
    is_copied: bool
    chat_model: Optional[str] = None
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
