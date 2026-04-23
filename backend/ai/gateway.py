import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update, or_
from models import Customer, User, UserCustomerRelation, ChatMessage, Product, SystemConfig
from schemas import normalize_purchase_months
from .context import ContextAssembler
from .prompts import get_prompt_for_scenario
from .llm_client import LLMClient
from core.logger import logger
from typing import AsyncIterator

UPDATE_CUSTOMER_TOOL = {
    "type": "function",
    "function": {
        "name": "update_customer_info",
        "description": "修改当前客户的资料信息。当用户在聊天中明确要求修改预算、称呼、单位、采购类型、采购月份或客户画像等信息时调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "budget": {"type": "number", "description": "客户预算金额 (数字，如 5000)"},
                "title": {"type": "string", "description": "客户头衔/称呼 (如: 张总, 李主任)"},
                "unit_name": {"type": "string", "description": "所属单位名称"},
                "purchase_type": {"type": "string", "description": "采购类型"},
                "purchase_months": {"type": "string", "description": "采购月份，多个用英文逗号分隔 (如: 3月,4月)，勿用顿号"},
                "ai_profile": {"type": "string", "description": "对客户的补充说明、标签或客情画像"}
            }
        }
    }
}

SEARCH_PRODUCTS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_products",
        "description": "在商品库中搜索产品。当用户询问有什么商品、需要推荐产品、询问价格、或需要根据预算/品类查找商品时调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词，如: 茶叶, 办公用品"},
                "category": {"type": "string", "description": "商品分类"},
                "max_price": {"type": "number", "description": "价格上限"},
                "min_price": {"type": "number", "description": "价格下限"}
            }
        }
    }
}

class AIGateway:
    """AI 网关: 上下文装配 → Prompt 选择 → LLM 调用 → 流式返回 → 自动存盘"""

    def __init__(self, db: AsyncSession, llm: LLMClient):
        self.db = db
        self.llm = llm
        self.assembler = ContextAssembler(db)

    async def stream_chat(
        self,
        user_id: int,
        customer_phone: str,
        query: str,
        scenario: str = "general_chat",
        conversation_id: str = None,
    ) -> AsyncIterator[str]:
        """
        主入口: 流式 AI 对话。
        """
        try:
            # 1. 装配上下文
            ctx = await self.assembler.assemble(user_id, customer_phone)
            logger.info(f"AI Gateway: 上下文装配完成 for {customer_phone}, scenario={scenario}")

            # 2. 构建 messages[]
            system_prompt = get_prompt_for_scenario(scenario, ctx)
            messages = [{"role": "system", "content": system_prompt}]

            # 2.1 注入历史对话
            ai_history_messages = ctx.get("ai_history_messages", [])
            if ai_history_messages:
                messages.extend(ai_history_messages)

            # 2.2 注入当前用户提问
            messages.append({"role": "user", "content": query})

            # 3. 查找 customer_id
            cust_res = await self.db.execute(select(Customer).where(Customer.phone == customer_phone))
            customer = cust_res.scalars().first()
            customer_id = customer.id if customer else None

            # 3.1 先保存用户消息
            if customer_id:
                user_msg = ChatMessage(
                    user_id=user_id,
                    customer_id=customer_id,
                    role="user",
                    content=query,
                    dify_conv_id=conversation_id
                )
                self.db.add(user_msg)
                await self.db.commit()

            # 4. 调用 LLM 流式 (Phase 1)
            full_answer = ""
            tool_calls = []
            tools = [UPDATE_CUSTOMER_TOOL, SEARCH_PRODUCTS_TOOL]
            
            async for chunk_text in self.llm.stream_chat(messages, tools=tools):
                if chunk_text.startswith("__TOOL_CALL__:"):
                    tc_json = chunk_text.split(":", 1)[1]
                    tool_calls.append(json.loads(tc_json))
                else:
                    full_answer += chunk_text
                    yield json.dumps({"event": "chunk", "text": chunk_text}, ensure_ascii=False)

            # 4.5 处理 Tool Calls 并发起第二轮请求
            if tool_calls:
                logger.info(f"AI Gateway: 检测到工具调用: {tool_calls}")
                # 构造符合 OpenAI 格式的 tool_calls 用于历史消息
                formatted_tcs = []
                for tc in tool_calls:
                    formatted_tcs.append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]}
                    })
                
                messages.append({"role": "assistant", "content": full_answer, "tool_calls": formatted_tcs})
                
                for tc in tool_calls:
                    if tc["name"] == "update_customer_info":
                        try:
                            args = json.loads(tc["arguments"])
                            await self._execute_update_customer_tool(customer_id, user_id, args)
                            # 告诉前端弹窗
                            yield json.dumps({"event": "system_action", "action": "update_customer", "changes": args}, ensure_ascii=False)
                            
                            # 强化二阶段推销指令
                            success_msg = {
                                "status": "success", 
                                "message": "数据库已更新。系统提示：作为一个销售，请现在立刻利用刚刚更新的这些客户线索（如新的预算、采购时间等），自然地向客户推荐合适的产品或推进下一步约访，不要仅仅回复确认修改！"
                            }
                            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(success_msg, ensure_ascii=False)})
                        except Exception as e:
                            logger.error(f"Tool execution failed: {e}")
                            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": f'{{"status": "error", "message": "{str(e)}"}}'})
                    elif tc["name"] == "search_products":
                        try:
                            args = json.loads(tc["arguments"])
                            search_res = await self._execute_search_products_tool(args)
                            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": search_res})
                        except Exception as e:
                            logger.error(f"Search products tool failed: {e}")
                            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": f"查询出错: {str(e)}"})
                                
                # 第二轮流式请求
                async for chunk_text in self.llm.stream_chat(messages, tools=tools):
                    if not chunk_text.startswith("__TOOL_CALL__:"):
                        full_answer += chunk_text
                        yield json.dumps({"event": "chunk", "text": chunk_text}, ensure_ascii=False)

            # 5. 保存 AI 回复
            msg_id = None
            if customer_id and full_answer:
                ai_msg = ChatMessage(
                    user_id=user_id,
                    customer_id=customer_id,
                    role="assistant",
                    content=full_answer,
                    dify_conv_id=conversation_id
                )
                self.db.add(ai_msg)
                await self.db.commit()
                await self.db.refresh(ai_msg)
                msg_id = ai_msg.id
                logger.info(f"AI Gateway: 回复已保存 msg_id={msg_id}")

            # 6. 发送完成事件
            yield json.dumps({"event": "done", "msg_id": msg_id}, ensure_ascii=False)

        except Exception as e:
            logger.error(f"AI Gateway Error: {str(e)}")
            yield json.dumps({"event": "error", "text": str(e)}, ensure_ascii=False)

    async def _execute_update_customer_tool(self, customer_id: int, user_id: int, args: dict):
        """执行数据库更新操作"""
        if not args: return
        
        # 1. 更新 Customer 表
        cust_updates = {}
        if "unit_name" in args: cust_updates["unit_name"] = args["unit_name"]
        if "purchase_months" in args:
            cust_updates["purchase_months"] = normalize_purchase_months(args["purchase_months"])
        
        if cust_updates:
            await self.db.execute(update(Customer).where(Customer.id == customer_id).values(**cust_updates))
            
        # 2. 更新 UserCustomerRelation 表
        rel_updates = {}
        if "budget" in args: 
            try: rel_updates["budget_amount"] = float(args["budget"])
            except: pass
        if "title" in args: rel_updates["title"] = args["title"]
        if "purchase_type" in args: rel_updates["purchase_type"] = args["purchase_type"]
        if "ai_profile" in args: rel_updates["ai_profile"] = args["ai_profile"]
        
        if rel_updates:
            await self.db.execute(update(UserCustomerRelation).where(
                UserCustomerRelation.customer_id == customer_id,
                UserCustomerRelation.user_id == user_id
            ).values(**rel_updates))
            
        await self.db.commit()

    async def _execute_search_products_tool(self, args: dict) -> str:
        """执行商品搜索并返回格式化结果给大模型"""
        keyword = args.get("keyword", "")
        category = args.get("category", "")
        max_price = args.get("max_price")
        min_price = args.get("min_price")
        
        query = select(Product)
        
        # 过滤 active suppliers
        config_res = await self.db.execute(select(SystemConfig).where(SystemConfig.config_key == "supplier_ids"))
        config_obj = config_res.scalars().first()
        active_ids = []
        if config_obj and config_obj.config_value and config_obj.config_value.strip():
            active_ids = [s.strip() for s in config_obj.config_value.split(",") if s.strip()]
            
        if active_ids:
            query = query.where(Product.supplier_id.in_(active_ids))
        else:
            return "当前系统没有配置可用的供应商，无法查询到商品。"

        if keyword:
            query = query.where(or_(
                Product.product_name.ilike(f"%{keyword}%"),
                Product.supplier_name.ilike(f"%{keyword}%")
            ))
        if category:
            query = query.where(or_(
                Product.category_name_one.ilike(f"%{category}%"),
                Product.category_name_two.ilike(f"%{category}%")
            ))
        if min_price is not None:
            query = query.where(Product.price >= min_price)
        if max_price is not None:
            query = query.where(Product.price <= max_price)
            
        query = query.order_by(Product.id.desc()).limit(10) # 限制10条，避免大模型上下文爆仓
        result = await self.db.execute(query)
        products = result.scalars().all()
        
        if not products:
            return f"未能找到符合条件的商品 (关键词: {keyword}, 分类: {category}, 价格区间: {min_price}-{max_price})"
            
        res_text = "找到以下商品：\n"
        for p in products:
            res_text += f"- 【{p.product_name}】 价格: ￥{p.price}/{p.unit}，分类: {p.category_name_one}，供应商: {p.supplier_name}\n"
        return res_text

