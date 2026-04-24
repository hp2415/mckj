import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update, or_
from models import Customer, User, UserCustomerRelation, ChatMessage, Product, SystemConfig
from schemas import normalize_purchase_months
from .context import ContextAssembler
from .prompt_service import PromptService
from .llm_client import LLMClient
from core.logger import logger
from typing import AsyncIterator


def _is_model_identity_query(q: str) -> bool:
    """
    识别「当前对话用的是哪个模型」类问题：走直连 LLM，不注入销售场景与知识库文档。
    匹配尽量收紧，避免正常业务句误触（如仅含「模型」二字）。
    """
    if not q or not str(q).strip():
        return False
    raw = str(q).strip()
    compact = "".join(raw.split()).replace("？", "?").lower()
    phrases = (
        "你用的什么模型",
        "你用的是什么模型",
        "你用哪个模型",
        "你是什么模型",
        "你现在用的什么模型",
        "当前用的什么模型",
        "现在用的什么模型",
        "用的什么模型",
        "用的哪个模型",
        "用的哪款模型",
        "什么大模型",
        "哪个大模型",
        "你是gpt吗",
        "你是chatgpt吗",
        "底层是什么模型",
        "接的什么模型",
        "调用的是什么模型",
    )
    if any(p in compact for p in phrases):
        return True
    low = raw.lower()
    if "what model" in low and "you" in low:
        return True
    return False


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
        # 提示词解析入口：按 scenario 读取 DB 化的 published 版本并渲染；
        # DB 无版本或开关关闭时自动回退到旧 prompts.py 逻辑。
        self.prompt_service = PromptService(db)

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
            # 1. 客户与落库用户消息（两条分支共用）
            cust_res = await self.db.execute(select(Customer).where(Customer.phone == customer_phone))
            customer = cust_res.scalars().first()
            customer_id = customer.id if customer else None

            if customer_id:
                user_msg = ChatMessage(
                    user_id=user_id,
                    customer_id=customer_id,
                    role="user",
                    content=query,
                    dify_conv_id=conversation_id,
                )
                self.db.add(user_msg)
                await self.db.commit()

            # 2. 模型身份直连：不装配客户上下文、不注入话术文档、不开工具
            if _is_model_identity_query(query):
                logger.info(
                    "AI Gateway: 模型身份直连 query_preview={} model={}",
                    (query[:40] + "…") if len(query) > 40 else query,
                    self.llm.model,
                )
                yield json.dumps(
                    {
                        "event": "meta",
                        "chat_model": self.llm.model,
                        "scenario": "model_identity",
                    },
                    ensure_ascii=False,
                )
                sys_direct = (
                    f"你是技术说明助手。当前请求在兼容 OpenAI 的 Chat Completions 接口里使用的 model 参数为「{self.llm.model}」。\n"
                    "用户正在询问模型身份。请用一到两句中文直接回答：说出上述标识即可；不要销售话术，不要提客户/订单/商品；"
                    "不要编造其它模型名。若用户追问能力，可简短说明你是通过该接口提供回复。"
                )
                messages_direct = [
                    {"role": "system", "content": sys_direct},
                    {"role": "user", "content": query},
                ]
                full_answer = ""
                async for chunk_text in self.llm.stream_chat(messages_direct, tools=None):
                    if chunk_text.startswith("__TOOL_CALL__:"):
                        continue
                    full_answer += chunk_text
                    yield json.dumps({"event": "chunk", "text": chunk_text}, ensure_ascii=False)

                msg_id = None
                if customer_id and full_answer:
                    ai_msg = ChatMessage(
                        user_id=user_id,
                        customer_id=customer_id,
                        role="assistant",
                        content=full_answer,
                        dify_conv_id=conversation_id,
                    )
                    self.db.add(ai_msg)
                    await self.db.commit()
                    await self.db.refresh(ai_msg)
                    msg_id = ai_msg.id
                    logger.info(f"AI Gateway: 模型身份回复已保存 msg_id={msg_id}")
                yield json.dumps({"event": "done", "msg_id": msg_id}, ensure_ascii=False)
                return

            # 3. 常规路径：装配上下文 + 场景话术 + 工具
            ctx = await self.assembler.assemble(user_id, customer_phone)
            logger.info(f"AI Gateway: 上下文装配完成 for {customer_phone}, scenario={scenario}")

            # 通过 PromptService 解析 system prompt（DB 化 + 回滚兜底）
            resolution = await self.prompt_service.resolve(
                scenario_key=scenario,
                ctx=ctx,
                query=query,
                history=ctx.get("ai_history_messages", []),
                customer_id=customer_id,
                user_id=user_id,
            )
            messages = resolution.messages
            logger.info("AI Gateway: prompt resolved meta={}", resolution.meta)

            # 4. 告知客户端实际使用的对话模型（与画像 llm_model 无关）
            yield json.dumps(
                {
                    "event": "meta",
                    "chat_model": self.llm.model,
                    "scenario": scenario,
                    "prompt_version": resolution.meta.get("version"),
                },
                ensure_ascii=False,
            )

            # 5. 调用 LLM 流式 (Phase 1)
            full_answer = ""
            tool_calls = []
            tools = [UPDATE_CUSTOMER_TOOL, SEARCH_PRODUCTS_TOOL] if resolution.tools_enabled else None

            async for chunk_text in self.llm.stream_chat(messages, tools=tools):
                if chunk_text.startswith("__TOOL_CALL__:"):
                    tc_json = chunk_text.split(":", 1)[1]
                    tool_calls.append(json.loads(tc_json))
                else:
                    full_answer += chunk_text
                    yield json.dumps({"event": "chunk", "text": chunk_text}, ensure_ascii=False)

            if not full_answer.strip() and not tool_calls:
                logger.warning(
                    "AI Gateway: 首轮 LLM 无文本且无工具调用（可能被上游吞掉或解析失败），"
                    "query_preview={}",
                    (query[:50] + "…") if len(query) > 50 else query,
                )

            # 4.5 处理 Tool Calls 并发起第二轮请求
            if tool_calls:
                logger.info(f"AI Gateway: 检测到工具调用: {tool_calls}")
                # 构造符合 OpenAI 格式的 tool_calls 用于历史消息
                formatted_tcs = []
                for tc in tool_calls:
                    formatted_tcs.append({
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {"name": tc.get("name", ""), "arguments": tc.get("arguments", "")},
                    })

                messages.append({"role": "assistant", "content": full_answer, "tool_calls": formatted_tcs})

                for tc in tool_calls:
                    if tc.get("name") == "update_customer_info":
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
                    elif tc.get("name") == "search_products":
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

