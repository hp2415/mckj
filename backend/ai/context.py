from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, or_, and_
from models import (
    RawCustomer,
    User,
    SalesCustomerProfile,
    SalesWechatAccount,
    RawOrder,
    RawOrderItem,
    ChatMessage,
    Product,
    RawChatLog,
)
from datetime import datetime
from typing import Optional
import crud

class ContextAssembler:
    """从 MySQL 结构化数据中装配 LLM 上下文"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def _profile_tag_catalog_block(self) -> str:
        """供工具与提示词引用：启用中的客户动态标签 id ↔ 名称。"""
        rows = await crud.list_active_profile_tag_options(self.db)
        if not rows:
            return "（当前后台未配置启用的客户动态标签；勿臆造 id。）"
        lines = [f"- id={int(r['id'])} 名称「{str(r.get('name') or '').strip()}」" for r in rows]
        return "\n".join(lines)

    async def assemble_for_staff(self, user_id: int) -> dict:
        """
        无客户上下文：仅销售员身份 + 占位块，供「自由对话 / 内部问答」场景使用。
        """
        staff_identity = ""
        u_res = await self.db.execute(select(User).where(User.id == user_id))
        staff_user = u_res.scalars().first()
        sw_id = (await crud.primary_sales_wechat_for_user(self.db, user_id)) or ""
        sales_acc = None
        if sw_id:
            acc_res = await self.db.execute(
                select(SalesWechatAccount).where(SalesWechatAccount.sales_wechat_id == sw_id)
            )
            sales_acc = acc_res.scalar_one_or_none()
        if staff_user:
            rn = (staff_user.real_name or "").strip()
            parts = []
            if rn:
                parts.append(f"员工姓名：{rn}")
            sw_line = self._format_sales_wechat_line(sales_acc)
            if sw_line:
                parts.append(sw_line)
            staff_identity = "；".join(parts)
        persona = self._compose_sales_wechat_persona_block(sw_id, sales_acc)
        placeholder = "（未选择客户：无客户档案、订单、微信记录与 AI 历史。需要客户数据时请切换到「客户对话」并选定客户。）"
        catalog = await self._profile_tag_catalog_block()
        return {
            "customer_card": placeholder,
            "order_summary": "—",
            "chat_summary": "—",
            "ai_history": "暂无 AI 对话历史。",
            "ai_history_messages": [],
            "ai_profile": "—",
            "budget_amount": "—",
            "purchase_type": "—",
            "staff_identity": staff_identity,
            "sales_wechat_persona": persona,
            "profile_tag_catalog": catalog,
        }

    async def assemble(
        self,
        user_id: int,
        *,
        customer_phone: Optional[str] = None,
        raw_customer_id: Optional[str] = None,
        resolved_sales_wechat_id: Optional[str] = None,
    ) -> dict:
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
        # 1. 查找客户实体：优先 raw_customer_id（可无手机号），否则按手机号
        customer = None
        rid = (raw_customer_id or "").strip()
        ph = (customer_phone or "").strip()
        if rid:
            cust_res = await self.db.execute(select(RawCustomer).where(RawCustomer.id == rid))
            customer = cust_res.scalars().first()
        if not customer and ph:
            cust_res = await self.db.execute(
                select(RawCustomer).where(
                    or_(RawCustomer.phone == ph, RawCustomer.phone_normalized == ph)
                )
            )
            customer = cust_res.scalars().first()
        if not customer:
            catalog = await self._profile_tag_catalog_block()
            return {
                "customer_card": "未找到客户信息",
                "order_summary": "",
                "chat_summary": "",
                "ai_history": "",
                "ai_history_messages": [],
                "ai_profile": "",
                "budget_amount": "未知",
                "purchase_type": "未知",
                "staff_identity": "",
                "sales_wechat_persona": "",
                "profile_tag_catalog": catalog,
            }

        staff_identity = ""
        u_res = await self.db.execute(select(User).where(User.id == user_id))
        staff_user = u_res.scalars().first()
        # 2. per-sales 画像：与当前会话行对齐（网关已解析为主号或侧栏传入的绑定号）
        if resolved_sales_wechat_id is not None:
            sw_id = str(resolved_sales_wechat_id).strip()
        else:
            sw_id = (await crud.primary_sales_wechat_for_user(self.db, user_id)) or ""
        relation = None
        if sw_id:
            rel_res = await self.db.execute(
                select(SalesCustomerProfile).where(
                    SalesCustomerProfile.raw_customer_id == customer.id,
                    SalesCustomerProfile.sales_wechat_id == sw_id,
                )
            )
            relation = rel_res.scalars().first()
        if not relation:
            # 兜底：历史数据（user_id + NULL sales_wechat）
            rel_res = await self.db.execute(
                select(SalesCustomerProfile).where(
                    SalesCustomerProfile.raw_customer_id == customer.id,
                    SalesCustomerProfile.user_id == user_id,
                    SalesCustomerProfile.sales_wechat_id.is_(None),
                )
            )
            relation = rel_res.scalars().first()

        sales_acc = None
        if sw_id:
            acc_res = await self.db.execute(
                select(SalesWechatAccount).where(SalesWechatAccount.sales_wechat_id == sw_id)
            )
            sales_acc = acc_res.scalar_one_or_none()

        if staff_user:
            rn = (staff_user.real_name or "").strip()
            parts = []
            if rn:
                parts.append(f"员工姓名：{rn}")
            sw_line = self._format_sales_wechat_line(sales_acc)
            if sw_line:
                parts.append(sw_line)
            staff_identity = "；".join(parts)

        persona = self._compose_sales_wechat_persona_block(sw_id, sales_acc)

        # 3. 组装客户档案卡片
        customer_card = self._build_customer_card(customer, relation)

        # 4. 查询近期订单 (最近 10 笔，raw_orders)
        order_summary = await self._build_order_summary(customer)

        # 5. 查询微信聊天记录摘要 (最近 20 条)
        chat_summary = await self._build_chat_summary(customer.id)

        # 6. 查询 AI 历史对话 (最近 6 轮 = 12 条)，与当前业务微信线程对齐
        ai_history, ai_history_messages = await self._build_ai_history(
            user_id, customer.id, session_sales_wechat_id=(sw_id or None)
        )

        prof_tags: list[dict] = []
        if relation:
            prof_tags = await crud.profile_tags_for_relation(self.db, relation.id)

        catalog = await self._profile_tag_catalog_block()

        return {
            "customer_card": customer_card,
            "order_summary": order_summary,
            "chat_summary": chat_summary,
            "ai_history": ai_history,
            "ai_history_messages": ai_history_messages,
            "ai_profile": self._compose_ai_profile_block(relation, sales_acc, prof_tags),
            "budget_amount": str(relation.budget_amount) if relation and relation.budget_amount else "未知",
            "purchase_type": (relation.purchase_type or "未知") if relation else "未知",
            "staff_identity": staff_identity,
            "sales_wechat_persona": persona,
            "profile_tag_catalog": catalog,
            "profile_tags_detail": self._compose_profile_tags_detail(prof_tags),
        }

    @staticmethod
    def _format_sales_wechat_line(acc: SalesWechatAccount | None) -> str:
        """销售业务微信主数据（accounts.xlsx / 云客）：昵称、别名。"""
        if not acc:
            return ""
        nick = (acc.nickname or "").strip()
        als = (acc.alias_name or "").strip()
        if nick and als:
            return f"当前业务微信：昵称「{nick}」；别名/备注「{als}」"
        if nick:
            return f"当前业务微信：昵称「{nick}」"
        if als:
            return f"当前业务微信：别名/备注「{als}」"
        return ""

    @staticmethod
    def _compose_sales_wechat_persona_block(
        sw_id: str,
        sales_acc: SalesWechatAccount | None,
    ) -> str:
        """对客户可见的自称约束：显式强调 nickname / 备注，避免与混在一起的聊天摘要或历史串号。"""
        sw = (sw_id or "").strip()
        if not sw:
            return "（当前会话未解析到具体业务微信号；代写客户话术前请与使用者确认自称，勿臆造人名。）"
        nick = (sales_acc.nickname or "").strip() if sales_acc else ""
        als = (sales_acc.alias_name or "").strip() if sales_acc else ""
        parts: list[str] = []
        if nick:
            parts.append(f"对外昵称「{nick}」")
        if als:
            parts.append(f"别名/备注「{als}」")
        head = "；".join(parts) if parts else f"业务微信 id「{sw}」（主数据未维护昵称/别名）"
        return (
            f"{head}\n"
            "撰写开场白、自我介绍或给客户的署名时，**必须**与上述一致；"
            "禁止沿用下方「近期微信沟通记录」「AI 对话历史」中出现的、与上述不一致的销售称呼。"
        )

    @staticmethod
    def _compose_ai_profile_block(
        relation: SalesCustomerProfile | None,
        sales_acc: SalesWechatAccount | None,
        profile_tags: list[dict] | None = None,
    ) -> str:
        """画像区：业务微信主数据 + 客户侧微信备注 + 分析正文。"""
        chunks: list[str] = []
        sw_line = ContextAssembler._format_sales_wechat_line(sales_acc)
        if sw_line:
            chunks.append(sw_line)
        cust_rmk = (relation.wechat_remark or "").strip() if relation else ""
        if cust_rmk:
            chunks.append(f"客户微信备注：{cust_rmk}")
        tags = profile_tags or []
        if tags:
            names = [str(t.get("name") or "").strip() for t in tags if (t.get("name") or "").strip()]
            if names:
                chunks.append("客户动态标签：" + "、".join(names))
        body = (relation.ai_profile or "").strip() if relation else ""
        if body:
            chunks.append(body)
        if not chunks:
            return "暂无画像"
        return "\n".join(chunks)

    @staticmethod
    def _compose_profile_tags_detail(tags: list[dict] | None) -> str:
        """格式化标签及其特征、策略话术，供 LLM 参考。"""
        if not tags:
            return "暂无动态标签"
        lines = []
        for t in tags:
            name = (t.get("name") or "").strip()
            if not name:
                continue
            feat = (t.get("feature_note") or "").strip()
            strat = (t.get("strategy_note") or "").strip()
            
            line = f"- 标签：【{name}】"
            if feat or strat:
                notes = []
                if feat:
                    notes.append(f"特征说明: {feat}")
                if strat:
                    notes.append(f"跟进策略/话术: {strat}")
                line += f"\n  " + "\n  ".join(notes)
            lines.append(line)
        return "\n".join(lines) if lines else "暂无动态标签"

    def _build_customer_card(self, customer: RawCustomer, relation) -> str:
        """拼装客户档案卡片 (纯文本)"""
        lines = []
        lines.append(f"姓名: {customer.customer_name or customer.name or '未知'}")
        lines.append(f"手机号: {customer.phone or '未知'}")
        lines.append(f"单位: {customer.unit_name or '未知'}")
        if customer.unit_type:
            lines.append(f"单位类型: {customer.unit_type}")
        if customer.admin_division:
            lines.append(f"行政区域: {customer.admin_division}")
        if customer.purchase_months:
            if isinstance(customer.purchase_months, list):
                lines.append(f"历史采购月份: {', '.join([str(x) for x in customer.purchase_months])}")
            else:
                lines.append(f"历史采购月份: {customer.purchase_months}")
        if relation:
            if relation.title:
                lines.append(f"称呼/头衔: {relation.title}")
            if relation.purchase_type:
                lines.append(f"采购类型: {relation.purchase_type}")
            if relation.budget_amount and relation.budget_amount > 0:
                lines.append(f"预算金额: ¥{relation.budget_amount}")
            if relation.wechat_remark:
                lines.append(f"客户微信备注: {relation.wechat_remark}")
            if relation.contact_date:
                lines.append(f"建联日期: {relation.contact_date}")
        return "\n".join(lines)

    async def _build_order_summary(self, customer: RawCustomer) -> str:
        """最近 10 笔订单摘要（与业务侧一致：raw_orders + raw_order_items，按手机号 search_phone 关联）"""
        clean_phone = "".join(filter(str.isdigit, str(customer.phone_normalized or customer.phone or "")))
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

    async def _build_chat_summary(self, raw_customer_id: str) -> str:
        """最近 20 条微信聊天记录（raw_chat_logs）"""
        stmt = (
            select(RawChatLog)
            .where(or_(RawChatLog.talker == raw_customer_id, RawChatLog.wechat_id == raw_customer_id))
            .order_by(desc(RawChatLog.timestamp))
            .limit(20)
        )
        res = await self.db.execute(stmt)
        records = res.scalars().all()
        if not records:
            return "暂无微信聊天记录。"

        records = list(reversed(records))
        lines = []
        for r in records:
            # raw_chat_logs.timestamp 为毫秒
            time_str = ""
            try:
                if r.timestamp is not None:
                    ts = int(r.timestamp) / 1000
                    time_str = datetime.fromtimestamp(ts).strftime("%m/%d %H:%M")
            except Exception:
                time_str = ""
            sender = "客户" if int(r.is_send or 0) == 0 else "工作人员"
            content = r.text or ""
            if len(content) > 80:
                content = content[:80] + "..."
            lines.append(f"[{time_str}] {sender}: {content}")
        return "\n".join(lines)

    async def _build_ai_history(
        self,
        user_id: int,
        customer_id: int,
        *,
        session_sales_wechat_id: Optional[str] = None,
    ) -> tuple[str, list]:
        """最近 6 轮 AI 对话 (12 条消息)，按业务微信线程过滤。"""
        primary = await crud.primary_sales_wechat_for_user(self.db, user_id)
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.user_id == user_id)
            .where(ChatMessage.raw_customer_id == customer_id)
            .where(crud.chat_message_thread_clause(session_sales_wechat_id, primary))
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
