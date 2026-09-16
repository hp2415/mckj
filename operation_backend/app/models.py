"""运营后台用到的表映射（独立于 backend 包，字段与迁移对齐）。"""
from __future__ import annotations

import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    real_name = Column(String(50), nullable=False)
    role = Column(String(20), default="staff", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    active_token_jti = Column(String(50), nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    mibuddy_uuid = Column(String(36), unique=True, nullable=True)


class OpDepartment(Base):
    __tablename__ = "op_departments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    parent_id = Column(Integer, ForeignKey("op_departments.id", ondelete="RESTRICT"), nullable=True)
    name = Column(String(100), nullable=False)
    kind = Column(String(20), nullable=True)
    leader_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    sort_order = Column(Integer, nullable=False, server_default="0")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now, nullable=False
    )


class OpDepartmentClosure(Base):
    __tablename__ = "op_department_closure"

    ancestor_id = Column(
        Integer, ForeignKey("op_departments.id", ondelete="CASCADE"), primary_key=True
    )
    descendant_id = Column(
        Integer, ForeignKey("op_departments.id", ondelete="CASCADE"), primary_key=True
    )
    depth = Column(Integer, nullable=False)


class OpDepartmentMember(Base):
    __tablename__ = "op_department_members"
    __table_args__ = (UniqueConstraint("user_id", name="uq_op_department_members_user_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    department_id = Column(
        Integer, ForeignKey("op_departments.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    joined_at = Column(DateTime, default=datetime.datetime.now, nullable=False)


class OpDeptRolePerm(Base):
    """部门 × 角色档 → 菜单权限码。含哨兵行 __configured__ 表示该档已手工配置（可为空集）。"""

    __tablename__ = "op_dept_role_perms"
    __table_args__ = (
        UniqueConstraint(
            "department_id",
            "op_role",
            "perm_code",
            name="uq_op_dept_role_perms_dept_role_code",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    department_id = Column(
        Integer, ForeignKey("op_departments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    op_role = Column(String(20), nullable=False)  # manager / staff
    perm_code = Column(String(64), nullable=False)
    updated_at = Column(
        DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now, nullable=False
    )


class OpUserProfile(Base):
    __tablename__ = "op_user_profiles"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    op_role = Column(String(20), nullable=False, server_default="none")
    status = Column(String(20), nullable=False, server_default="pending")
    created_via = Column(String(30), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now, nullable=False
    )


class OpInviteCode(Base):
    __tablename__ = "op_invite_codes"
    __table_args__ = (UniqueConstraint("code", name="uq_op_invite_codes_code"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    department_id = Column(
        Integer, ForeignKey("op_departments.id", ondelete="RESTRICT"), nullable=False
    )
    grant_op_role = Column(String(20), nullable=False, server_default="none")
    max_uses = Column(Integer, nullable=False, server_default="1")
    used_count = Column(Integer, nullable=False, server_default="0")
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    note = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)


class OpInviteRedemption(Base):
    __tablename__ = "op_invite_redemptions"
    __table_args__ = (UniqueConstraint("user_id", name="uq_op_invite_redemptions_user_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    invite_id = Column(
        Integer, ForeignKey("op_invite_codes.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    redeemed_at = Column(DateTime, default=datetime.datetime.now, nullable=False)


class OpAuditLog(Base):
    __tablename__ = "op_audit_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at = Column(DateTime, default=datetime.datetime.now, nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(60), nullable=False)
    target_type = Column(String(40), nullable=True)
    target_id = Column(String(64), nullable=True)
    detail_json = Column(JSON, nullable=True)
    ip = Column(String(64), nullable=True)


class UserActivityEvent(Base):
    __tablename__ = "user_activity_events"
    __table_args__ = (
        Index("ix_user_activity_events_user_occurred", "user_id", "occurred_at"),
        Index("ix_user_activity_events_type_occurred", "event_type", "occurred_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at = Column(DateTime, default=datetime.datetime.now, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event_type = Column(String(40), nullable=False)
    source = Column(String(20), nullable=False)
    sales_wechat_id = Column(String(100), nullable=True)
    raw_customer_id = Column(String(100), nullable=True)
    object_type = Column(String(40), nullable=True)
    object_id = Column(String(100), nullable=True)
    extra_json = Column(JSON, nullable=True)
    client_session_id = Column(String(64), nullable=True)


# ---- 业务只读表（使用率聚合） ----


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True)
    role = Column(String(20), nullable=False)
    is_copied = Column(Boolean, default=False, nullable=False)
    rating = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, nullable=False)


class WechatOutboundAction(Base):
    __tablename__ = "wechat_outbound_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_user_id = Column(Integer, nullable=True)
    action_type = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False)
    created_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class ContactTask(Base):
    __tablename__ = "contact_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    status = Column(String(20), nullable=False)
    completed_at = Column(DateTime, nullable=True)
    completed_by_user_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False)


class SystemConfig(Base):
    __tablename__ = "system_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_key = Column(String(100), unique=True, nullable=False)
    config_value = Column(Text, nullable=False)
    config_group = Column(String(50), default="general", nullable=False)
    description = Column(String(255), nullable=True)
    updated_at = Column(
        DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now, nullable=False
    )


class Campaign(Base):
    """营销活动（与桌面 backend 共用 campaigns 表）。"""

    __tablename__ = "campaigns"
    __table_args__ = (Index("ix_campaigns_status_window", "status", "start_at", "end_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    start_at = Column(DateTime, nullable=False)
    end_at = Column(DateTime, nullable=False)
    audience_unit_types = Column(JSON, nullable=False)
    rules = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="enabled", server_default="enabled")
    priority = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now, nullable=False
    )


class CampaignPoster(Base):
    __tablename__ = "campaign_posters"
    __table_args__ = (Index("ix_campaign_posters_campaign", "campaign_id", "is_active"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(
        Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    image_path = Column(String(500), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    is_active = Column(Boolean, nullable=False, default=True, server_default="1")
    send_count = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)


class CampaignBlastJob(Base):
    __tablename__ = "campaign_blast_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False)
    created_at = Column(DateTime, nullable=False)


class CampaignBlastRecipient(Base):
    __tablename__ = "campaign_blast_recipients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False)
    sent_at = Column(DateTime, nullable=True)


class PhoneCallRecord(Base):
    __tablename__ = "phone_call_records"

    call_id = Column(String(64), primary_key=True)
    create_time = Column(DateTime, nullable=False)
    staff_uuid = Column(String(64), nullable=True)
    call_seconds = Column(Integer, nullable=True)
    status_text = Column(String(16), nullable=True)
