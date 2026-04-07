from pydantic import BaseModel, Field
from typing import Optional
from datetime import date
from decimal import Decimal

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
    id: int
    phone: str
    customer_name: str
    unit_name: str
    # 返回的当前员工互动属性
    title: Optional[str] = None
    budget_amount: Decimal = Decimal("0.00")
    ai_profile: Optional[str] = None
    dify_conversation_id: Optional[str] = None
    contact_date: Optional[date] = None
    
    # 返回的新增客观属性
    unit_type: Optional[str] = None
    admin_division: Optional[str] = None
    purchase_months: Optional[str] = None
    purchase_type: Optional[str] = None
    
    # 动态聚合属性
    historical_amount: Decimal = Decimal("0.00")
    historical_order_count: int = 0

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
    unit_type: Optional[str] = None
    admin_division: Optional[str] = None
    purchase_months: Optional[str] = None
    
    # 主观属性 (UserCustomerRelation)
    contact_date: Optional[date] = None # 支持自定义建联日
    purchase_type: Optional[str] = None # 移动到主观交互表
    
    # 动态业务属性 (UserCustomerRelation)
    title: Optional[str] = None
    budget_amount: Optional[Decimal] = None
    ai_profile: Optional[str] = None
    wechat_remark: Optional[str] = None
    dify_conversation_id: Optional[str] = None

# 聊天消息存取记录模型
class ChatMessageBase(BaseModel):
    role: str # 'user' 或 'assistant'
    content: str
    dify_conv_id: Optional[str] = None

class ChatMessageCreate(ChatMessageBase):
    pass

class ChatMessageOut(ChatMessageBase):
    id: int
    created_at: date # 或 datetime

    class Config:
        from_attributes = True
