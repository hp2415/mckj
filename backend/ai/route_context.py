"""路由上下文：从现有客户/订单/聊天/画像数据提炼结构化信号，供 SceneRouter 决策。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import and_, desc, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

import crud
from ai.chat_log_filter import raw_chat_log_meaningful_clause
from models import RawChatLog, RawCustomer, RawOrder, SalesCustomerProfile


_NEW_FRIEND_DAYS = 7
_DORMANT_CHAT_DAYS = 30

_QUOTE_TOPIC_WORDS = ("报价", "方案", "价格", "多少钱", "预算", "型号", "礼盒", "推品")
_CLOSE_TOPIC_WORDS = ("下单", "链接", "就这款", "确定", "成交", "名额", "积分")
_OBJECTION_TOPIC_WORDS = ("考虑", "再看看", "太贵", "犹豫", "对比")

_NEW_CUSTOMER_TAG_HINTS = ("新客户", "新好友", "新客", "新增好友", "首次")
_OLD_CUSTOMER_TAG_HINTS = ("老客户", "复购", "熟客", "回头客")
_FORBIDDEN_TAG_HINTS = ("禁止打扰", "勿打扰", "不要联系")
_NOT_RESPONSIBLE_TAG_HINTS = ("不负责", "已调离", "非采购")


@dataclass
class RouteContext:
  customer_id: Optional[str] = None
  customer_lifecycle: str = "unknown"
  intent_band: str = "unknown"
  unit_segment: str = "unknown"
  gender: str = "unknown"
  has_order_current_year: bool = False
  days_since_last_chat: Optional[int] = None
  days_since_last_order: Optional[int] = None
  purchase_months: list[str] = field(default_factory=list)
  budget_amount: Optional[float] = None
  purchase_type: str = ""
  profile_tag_ids: list[int] = field(default_factory=list)
  profile_tag_names: list[str] = field(default_factory=list)
  forbidden_outreach: bool = False
  not_responsible: bool = False
  chat_topics: list[str] = field(default_factory=list)
  quote_pending_days: Optional[int] = None

  def to_dict(self) -> dict[str, Any]:
    return {
      "customer_id": self.customer_id,
      "customer_lifecycle": self.customer_lifecycle,
      "intent_band": self.intent_band,
      "unit_segment": self.unit_segment,
      "gender": self.gender,
      "has_order_current_year": self.has_order_current_year,
      "days_since_last_chat": self.days_since_last_chat,
      "days_since_last_order": self.days_since_last_order,
      "purchase_months": list(self.purchase_months),
      "budget_amount": self.budget_amount,
      "purchase_type": self.purchase_type,
      "profile_tag_ids": list(self.profile_tag_ids),
      "profile_tag_names": list(self.profile_tag_names),
      "forbidden_outreach": self.forbidden_outreach,
      "not_responsible": self.not_responsible,
      "chat_topics": list(self.chat_topics),
      "quote_pending_days": self.quote_pending_days,
    }

  def fingerprint(self) -> str:
    payload = {
      "customer_id": self.customer_id or "",
      "customer_lifecycle": self.customer_lifecycle,
      "intent_band": self.intent_band,
      "unit_segment": self.unit_segment,
      "gender": self.gender,
      "has_order_current_year": self.has_order_current_year,
      "days_since_last_chat": self.days_since_last_chat,
      "days_since_last_order": self.days_since_last_order,
      "purchase_months": self.purchase_months,
      "budget_amount": self.budget_amount,
      "purchase_type": self.purchase_type,
      "profile_tag_ids": self.profile_tag_ids,
      "forbidden_outreach": self.forbidden_outreach,
      "not_responsible": self.not_responsible,
      "chat_topics": self.chat_topics,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

  def summary_text(self) -> str:
    lines = [
      f"客户生命周期: {self.customer_lifecycle}",
      f"意向档位: {self.intent_band}",
      f"单位类型: {self.unit_segment}",
      f"性别: {self.gender}",
      f"今年是否下单: {'是' if self.has_order_current_year else '否'}",
    ]
    if self.days_since_last_chat is not None:
      lines.append(f"距上次微信沟通: {self.days_since_last_chat} 天")
    if self.days_since_last_order is not None:
      lines.append(f"距上次订单: {self.days_since_last_order} 天")
    if self.purchase_months:
      lines.append(f"历史采购月份: {', '.join(self.purchase_months)}")
    if self.budget_amount is not None and self.budget_amount > 0:
      lines.append(f"预算金额: {self.budget_amount}")
    if self.purchase_type:
      lines.append(f"采购类型: {self.purchase_type}")
    if self.profile_tag_names:
      lines.append(f"动态标签: {', '.join(self.profile_tag_names)}")
    if self.chat_topics:
      lines.append(f"聊天主题: {', '.join(self.chat_topics)}")
    if self.forbidden_outreach:
      lines.append("触达限制: 禁止打扰")
    if self.not_responsible:
      lines.append("触达限制: 不负责采购/已调离")
    return "\n".join(lines)


class RouteContextBuilder:
  def __init__(self, db: AsyncSession):
    self.db = db

  async def build(
    self,
    *,
    user_id: int,
    customer_phone: Optional[str] = None,
    raw_customer_id: Optional[str] = None,
    resolved_sales_wechat_id: Optional[str] = None,
  ) -> RouteContext:
    customer = await self._load_customer(customer_phone, raw_customer_id)
    if customer is None:
      return RouteContext()

    relation = await self._load_relation(user_id, customer.id, resolved_sales_wechat_id)
    prof_tags: list[dict] = []
    if relation:
      prof_tags = await crud.profile_tags_for_relation(self.db, relation.id)

    has_order_year, last_order_days = await self._order_stats(customer)
    last_chat_days, chat_blob = await self._chat_stats(customer.id, resolved_sales_wechat_id)
    tag_ids, tag_names, forbidden, not_resp = self._tag_signals(prof_tags)
    lifecycle = self._infer_lifecycle(
      customer,
      relation,
      has_order_year,
      last_order_days,
      last_chat_days,
      tag_names=tag_names,
    )
    intent = self._infer_intent_band(
      has_order_year=has_order_year,
      relation=relation,
      chat_blob=chat_blob,
      prof_tags=prof_tags,
    )
    topics = self._chat_topics(chat_blob)

    purchase_months = self._purchase_months(customer, relation)
    budget = None
    if relation and relation.budget_amount is not None:
      try:
        budget = float(relation.budget_amount)
      except (TypeError, ValueError):
        budget = None

    return RouteContext(
      customer_id=str(customer.id),
      customer_lifecycle=lifecycle,
      intent_band=intent,
      unit_segment=self._unit_segment(customer),
      gender=self._normalize_gender(customer.gender),
      has_order_current_year=has_order_year,
      days_since_last_chat=last_chat_days,
      days_since_last_order=last_order_days,
      purchase_months=purchase_months,
      budget_amount=budget,
      purchase_type=(relation.purchase_type or "").strip() if relation else "",
      profile_tag_ids=tag_ids,
      profile_tag_names=tag_names,
      forbidden_outreach=forbidden,
      not_responsible=not_resp,
      chat_topics=topics,
    )

  async def _load_customer(
    self,
    customer_phone: Optional[str],
    raw_customer_id: Optional[str],
  ) -> Optional[RawCustomer]:
    rid = (raw_customer_id or "").strip()
    ph = (customer_phone or "").strip()
    customer = None
    if rid:
      res = await self.db.execute(select(RawCustomer).where(RawCustomer.id == rid))
      customer = res.scalars().first()
    if not customer and ph:
      res = await self.db.execute(
        select(RawCustomer).where(
          or_(RawCustomer.phone == ph, RawCustomer.phone_normalized == ph)
        )
      )
      customer = res.scalars().first()
    return customer

  async def _load_relation(
    self,
    user_id: int,
    raw_customer_id: str,
    resolved_sales_wechat_id: Optional[str],
  ) -> Optional[SalesCustomerProfile]:
    sw_id = (resolved_sales_wechat_id or "").strip()
    if not sw_id:
      sw_id = (await crud.primary_sales_wechat_for_user(self.db, user_id)) or ""
    relation = None
    if sw_id:
      res = await self.db.execute(
        select(SalesCustomerProfile).where(
          SalesCustomerProfile.raw_customer_id == raw_customer_id,
          SalesCustomerProfile.sales_wechat_id == sw_id,
        )
      )
      relation = res.scalars().first()
    if not relation:
      res = await self.db.execute(
        select(SalesCustomerProfile).where(
          SalesCustomerProfile.raw_customer_id == raw_customer_id,
          SalesCustomerProfile.user_id == user_id,
          SalesCustomerProfile.sales_wechat_id.is_(None),
        )
      )
      relation = res.scalars().first()
    return relation

  async def _order_stats(self, customer: RawCustomer) -> tuple[bool, Optional[int]]:
    clean_phone = "".join(filter(str.isdigit, str(customer.phone_normalized or customer.phone or "")))
    if len(clean_phone) < 7:
      return False, None
    res = await self.db.execute(
      select(RawOrder)
      .where(RawOrder.search_phone == clean_phone)
      .order_by(desc(RawOrder.order_time))
      .limit(1)
    )
    latest = res.scalars().first()
    if not latest or not latest.order_time:
      return False, None
    year = datetime.now().year
    has_year = latest.order_time.year == year
    days = (date.today() - latest.order_time.date()).days
    return has_year, max(days, 0)

  async def _chat_stats(
    self,
    raw_customer_id: str,
    sales_wechat_id: Optional[str],
  ) -> tuple[Optional[int], str]:
    sw = (sales_wechat_id or "").strip()
    if not sw:
      return None, ""
    stmt_a = (
      select(RawChatLog)
      .where(
        and_(
          RawChatLog.wechat_id == sw,
          RawChatLog.talker == raw_customer_id,
          raw_chat_log_meaningful_clause(RawChatLog.text),
        )
      )
      .order_by(func.coalesce(RawChatLog.time_ms, RawChatLog.timestamp, 0).desc())
      .limit(20)
    )
    stmt_b = (
      select(RawChatLog)
      .where(
        and_(
          RawChatLog.wechat_id == raw_customer_id,
          RawChatLog.talker == sw,
          raw_chat_log_meaningful_clause(RawChatLog.text),
        )
      )
      .order_by(func.coalesce(RawChatLog.time_ms, RawChatLog.timestamp, 0).desc())
      .limit(20)
    )
    res_a = await self.db.execute(stmt_a)
    res_b = await self.db.execute(stmt_b)
    records = list(res_a.scalars().all()) + list(res_b.scalars().all())
    if not records:
      return None, ""

    def _ts(v) -> int:
      try:
        return int(v or 0)
      except (TypeError, ValueError):
        return 0

    records.sort(key=lambda r: _ts(r.time_ms or r.timestamp), reverse=True)
    latest = records[0]
    ts = _ts(latest.time_ms or latest.timestamp)
    days = None
    if ts > 0:
      dt = datetime.fromtimestamp(ts / 1000 if ts > 10_000_000_000 else ts)
      days = max((date.today() - dt.date()).days, 0)
    blob = " ".join(str(getattr(r, "text", "") or "") for r in records[:20])
    return days, blob

  @staticmethod
  def _purchase_months(customer: RawCustomer, relation: Optional[SalesCustomerProfile]) -> list[str]:
    months: list[str] = []
    raw = customer.purchase_months
    if isinstance(raw, list):
      months.extend(str(x).strip() for x in raw if str(x).strip())
    elif raw:
      months.append(str(raw).strip())
    return months

  @staticmethod
  def _normalize_gender(value: Optional[str]) -> str:
    g = (value or "").strip().lower()
    if g in ("女", "female", "f", "2"):
      return "female"
    if g in ("男", "male", "m", "1"):
      return "male"
    return "unknown"

  @staticmethod
  def _unit_segment(customer: RawCustomer) -> str:
    text = (customer.unit_type or customer.unit_name or "").strip()
    if not text:
      return "unknown"
    mapping = (
      ("school", ("学校", "幼儿园", "小学", "中学", "高校", "职高", "教育")),
      ("fire", ("消防",)),
      ("tax", ("税务",)),
      ("justice", ("公安", "检察", "法院", "监狱", "派出所", "公检法")),
      ("health", ("医院", "卫生", "疾控", "卫健委", "医疗")),
      ("bank", ("银行",)),
      ("government", ("人民政府", "街道办", "政府")),
      ("other", ("气象", "海关", "国企", "军队")),
    )
    for key, needles in mapping:
      if any(n in text for n in needles):
        return key
    return "other"

  @staticmethod
  def _lifecycle_from_tags(tag_names: list[str]) -> Optional[str]:
    joined = " ".join(tag_names)
    if any(h in joined for h in _NEW_CUSTOMER_TAG_HINTS):
      return "new_friend"
    if any(h in joined for h in _OLD_CUSTOMER_TAG_HINTS):
      return "active_old"
    return None

  @staticmethod
  def _infer_lifecycle(
    customer: RawCustomer,
    relation: Optional[SalesCustomerProfile],
    has_order_year: bool,
    last_order_days: Optional[int],
    last_chat_days: Optional[int],
    tag_names: Optional[list[str]] = None,
  ) -> str:
    names = [str(x).strip() for x in (tag_names or []) if str(x).strip()]
    tag_lifecycle = RouteContextBuilder._lifecycle_from_tags(names)
    has_past_order = last_order_days is not None

    # 画像/运营标签优先：无历史订单时，「新客户」类标签不应被陈旧聊天记录误判为沉睡老客。
    if tag_lifecycle == "new_friend" and not has_past_order and not has_order_year:
      return "new_friend"

    if relation and relation.contact_date:
      days_known = (date.today() - relation.contact_date).days
      if days_known <= _NEW_FRIEND_DAYS and not has_order_year and not has_past_order:
        return "new_friend"
    if customer.add_time:
      try:
        add_days = (date.today() - customer.add_time.date()).days
        if add_days <= _NEW_FRIEND_DAYS and not has_order_year and not has_past_order:
          return "new_friend"
      except Exception:
        pass

    if has_past_order or has_order_year:
      if last_chat_days is not None and last_chat_days >= _DORMANT_CHAT_DAYS:
        return "dormant_old"
      return "active_old"

    if tag_lifecycle == "new_friend":
      return "new_friend"
    if last_chat_days is not None and last_chat_days < _DORMANT_CHAT_DAYS:
      return "active_old"
    if last_chat_days is not None:
      return "unknown"
    return "unknown"

  @staticmethod
  def _infer_intent_band(
    *,
    has_order_year: bool,
    relation: Optional[SalesCustomerProfile],
    chat_blob: str,
    prof_tags: list[dict],
  ) -> str:
    blob = chat_blob or ""
    tag_text = " ".join(str(t.get("name") or "") for t in prof_tags)
    has_budget = bool(relation and relation.budget_amount and float(relation.budget_amount) > 0)
    has_purchase_type = bool(relation and (relation.purchase_type or "").strip())
    if any(w in blob for w in _CLOSE_TOPIC_WORDS) or ("高意向" in tag_text):
      return "40"
    if has_budget or has_purchase_type or any(w in blob for w in _QUOTE_TOPIC_WORDS):
      return "30"
    if not has_order_year:
      return "20"
    if any(w in blob for w in _OBJECTION_TOPIC_WORDS):
      return "30"
    return "unknown"

  @staticmethod
  def _tag_signals(prof_tags: list[dict]) -> tuple[list[int], list[str], bool, bool]:
    ids: list[int] = []
    names: list[str] = []
    forbidden = False
    not_resp = False
    for t in prof_tags or []:
      try:
        tid = int(t.get("id"))
      except (TypeError, ValueError):
        tid = None
      name = str(t.get("name") or "").strip()
      if tid is not None:
        ids.append(tid)
      if name:
        names.append(name)
        if any(h in name for h in _FORBIDDEN_TAG_HINTS):
          forbidden = True
        if any(h in name for h in _NOT_RESPONSIBLE_TAG_HINTS):
          not_resp = True
    return ids, names, forbidden, not_resp

  @staticmethod
  def _chat_topics(chat_blob: str) -> list[str]:
    if not chat_blob:
      return []
    topics: list[str] = []
    if any(w in chat_blob for w in _QUOTE_TOPIC_WORDS):
      topics.append("报价")
    if any(w in chat_blob for w in _CLOSE_TOPIC_WORDS):
      topics.append("逼单")
    if any(w in chat_blob for w in _OBJECTION_TOPIC_WORDS):
      topics.append("异议")
    if "问候" in chat_blob or "您好" in chat_blob:
      topics.append("问候")
    return topics


def evaluate_customer_conditions(
  route_context: Optional[RouteContext],
  conditions: Optional[dict],
) -> bool:
  if not conditions or not isinstance(conditions, dict):
    return True
  ctx = route_context.to_dict() if route_context else {}
  all_conds = conditions.get("all") if isinstance(conditions.get("all"), list) else []
  any_conds = conditions.get("any") if isinstance(conditions.get("any"), list) else []
  for cond in all_conds:
    if not _eval_single(ctx, cond):
      return False
  if any_conds:
    return any(_eval_single(ctx, cond) for cond in any_conds)
  return True


def _eval_single(ctx: dict, cond: Any) -> bool:
  if not isinstance(cond, dict):
    return True
  field = str(cond.get("field") or "").strip()
  if not field:
    return True
  op = str(cond.get("op") or "eq").strip().lower()
  expected = cond.get("value")
  actual = ctx.get(field)

  if op == "true":
    return bool(actual)
  if op == "false":
    return not bool(actual)
  if op == "eq":
    return actual == expected
  if op == "ne":
    return actual != expected
  if op == "in":
    if not isinstance(expected, list):
      return actual == expected
    return actual in expected
  if op == "not_in":
    if not isinstance(expected, list):
      return actual != expected
    return actual not in expected
  if op in ("gt", "gte", "lt", "lte"):
    try:
      left = float(actual)  # type: ignore[arg-type]
      right = float(expected)  # type: ignore[arg-type]
    except (TypeError, ValueError):
      return False
    if op == "gt":
      return left > right
    if op == "gte":
      return left >= right
    if op == "lt":
      return left < right
    return left <= right
  if op == "contains":
    if isinstance(actual, list):
      return expected in actual
    return str(expected or "") in str(actual or "")
  return actual == expected


def parse_scenario_hint(hint: Optional[str]) -> tuple[list[str], str]:
  raw = (hint or "").strip()
  if not raw or raw.lower() == "auto":
    return [], ""
  parts = [p.strip() for p in re.split(r"[+,，、|]", raw) if p.strip()]
  if not parts:
    return [], ""
  return parts, parts[0]
