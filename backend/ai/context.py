from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc
from models import Customer, UserCustomerRelation, RawOrder, RawOrderItem, ChatMessage, WechatHistory, Product
from datetime import datetime

class ContextAssembler:
    """从 MySQL 结构化数据中装配 LLM 上下文"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def assemble(self, user_id: int, customer_phone: str) -> dict:
        """
        返回格式:
        {
            "customer_card": str,       # 客户档案卡片
            "order_summary": str,       # 近期订单摘要
            "chat_summary": str,        # 微信聊天摘要
            "ai_history": str,          # AI 历史对话
            "ai_history_messages": list, # AI 历史对话 (messages 格式)
            "ai_profile": str,          # AI 画像
            "budget_amount": str,       # 预算金额
            "purchase_type": str,       # 采购类型
        }
        """
        # 1. 查找客户实体
        cust_res = await self.db.execute(select(Customer).where(Customer.phone == customer_phone))
        customer = cust_res.scalars().first()
        if not customer:
            return {"customer_card": "未找到客户信息", "order_summary": "", "chat_summary": "", "ai_history": "", "ai_history_messages": [], "ai_profile": "", "budget_amount": "未知", "purchase_type": "未知"}

        # 2. 查找当前员工与该客户的关系记录
        rel_res = await self.db.execute(
            select(UserCustomerRelation)
            .where(UserCustomerRelation.user_id == user_id)
            .where(UserCustomerRelation.customer_id == customer.id)
        )
        relation = rel_res.scalars().first()

        # 3. 组装客户档案卡片
        customer_card = self._build_customer_card(customer, relation)

        # 4. 查询近期订单 (最近 10 笔，raw_orders)
        order_summary = await self._build_order_summary(customer)

        # 5. 查询微信聊天记录摘要 (最近 20 条)
        chat_summary = await self._build_chat_summary(user_id, customer.id)

        # 6. 查询 AI 历史对话 (最近 6 轮 = 12 条)
        ai_history, ai_history_messages = await self._build_ai_history(user_id, customer.id)

        return {
            "customer_card": customer_card,
            "order_summary": order_summary,
            "chat_summary": chat_summary,
            "ai_history": ai_history,
            "ai_history_messages": ai_history_messages,
            "ai_profile": (relation.ai_profile or "暂无画像") if relation else "暂无画像",
            "budget_amount": str(relation.budget_amount) if relation and relation.budget_amount else "未知",
            "purchase_type": (relation.purchase_type or "未知") if relation else "未知",
        }

    def _build_customer_card(self, customer: Customer, relation) -> str:
        """拼装客户档案卡片 (纯文本)"""
        lines = []
        lines.append(f"姓名: {customer.customer_name}")
        lines.append(f"手机号: {customer.phone}")
        lines.append(f"单位: {customer.unit_name}")
        if customer.unit_type:
            lines.append(f"单位类型: {customer.unit_type}")
        if customer.admin_division:
            lines.append(f"行政区域: {customer.admin_division}")
        if customer.purchase_months:
            lines.append(f"历史采购月份: {customer.purchase_months}")
        if relation:
            if relation.title:
                lines.append(f"称呼/头衔: {relation.title}")
            if relation.purchase_type:
                lines.append(f"采购类型: {relation.purchase_type}")
            if relation.budget_amount and relation.budget_amount > 0:
                lines.append(f"预算金额: ¥{relation.budget_amount}")
            if relation.wechat_remark:
                lines.append(f"微信备注名: {relation.wechat_remark}")
            if relation.contact_date:
                lines.append(f"建联日期: {relation.contact_date}")
        return "\n".join(lines)

    async def _build_order_summary(self, customer: Customer) -> str:
        """最近 10 笔订单摘要（与业务侧一致：raw_orders + raw_order_items，按手机号 search_phone 关联）"""
        clean_phone = "".join(filter(str.isdigit, str(customer.phone or "")))
        if len(clean_phone) < 7:
            return "该客户暂无历史订单记录。"

        stmt = (
            select(RawOrder)
            .where(RawOrder.search_phone == clean_phone)
            .order_by(desc(RawOrder.order_time))
            .limit(10)
        )
        res = await self.db.execute(stmt)
        orders = res.scalars().all()
        if not orders:
            return "该客户暂无历史订单记录。"

        order_ids = [o.id for o in orders]
        stmt_items = select(RawOrderItem).where(RawOrderItem.raw_order_id.in_(order_ids))
        res_items = await self.db.execute(stmt_items)
        items = res_items.scalars().all()
        by_order: dict[int, list[str]] = defaultdict(list)
        for it in items:
            if it.product_name:
                by_order[it.raw_order_id].append(it.product_name)

        lines = []
        for o in orders:
            date_str = o.order_time.strftime("%Y-%m-%d") if o.order_time else "未知"
            amount = float(o.pay_amount) if o.pay_amount else 0
            status = o.status_name or "未知"
            names = by_order.get(o.id, [])
            product = " | ".join(names) if names else "未指定商品"
            if len(product) > 60:
                product = product[:60] + "..."
            lines.append(f"- {date_str}: {product} ¥{amount:.0f} ({status})")
        return "\n".join(lines)

    async def _build_chat_summary(self, user_id: int, customer_id: int) -> str:
        """最近 20 条微信聊天记录"""
        stmt = (
            select(WechatHistory)
            .where(WechatHistory.user_id == user_id)
            .where(WechatHistory.customer_id == customer_id)
            .order_by(desc(WechatHistory.chat_time))
            .limit(20)
        )
        res = await self.db.execute(stmt)
        records = res.scalars().all()
        if not records:
            return "暂无微信聊天记录。"

        records = list(reversed(records))
        lines = []
        for r in records:
            time_str = r.chat_time.strftime("%m/%d %H:%M") if r.chat_time else ""
            sender = r.sender_name or "未知"
            content = r.content or ""
            if len(content) > 80:
                content = content[:80] + "..."
            lines.append(f"[{time_str}] {sender}: {content}")
        return "\n".join(lines)

    async def _build_ai_history(self, user_id: int, customer_id: int) -> tuple[str, list]:
        """最近 6 轮 AI 对话 (12 条消息)"""
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.user_id == user_id)
            .where(ChatMessage.customer_id == customer_id)
            .order_by(desc(ChatMessage.created_at))
            .limit(12)
        )
        res = await self.db.execute(stmt)
        messages = res.scalars().all()

        if not messages:
            return "暂无 AI 对话历史。", []

        messages = list(reversed(messages))
        lines = []
        msg_list = []
        for m in messages:
            role_label = "员工" if m.role == "user" else "AI"
            content = m.content or ""
            if content.startswith("⚠️"):
                continue
            if len(content) > 200:
                content_display = content[:200] + "..."
            else:
                content_display = content
            lines.append(f"{role_label}: {content_display}")
            msg_list.append({"role": m.role, "content": m.content})

        return "\n".join(lines), msg_list
