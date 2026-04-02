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

    class Config:
        from_attributes = True

# 销售人员更新客户动态资料的提交模型
class RelationUpdate(BaseModel):
    title: Optional[str] = None
    budget_amount: Optional[Decimal] = None
    ai_profile: Optional[str] = None
    dify_conversation_id: Optional[str] = None
