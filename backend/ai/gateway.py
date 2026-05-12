import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update, or_
from models import RawCustomer, User, SalesCustomerProfile, ChatMessage, Product, SystemConfig, PromptAuditLog
import crud
from schemas import normalize_purchase_months
from .context import ContextAssembler
from .prompt_service import PromptService
from .llm_client import LLMClient
from .scene_router import SceneRouter, RouteDecision
from core.logger import logger
from typing import AsyncIterator, Optional
from datetime import date


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
        "description": (
            "修改当前客户资料。用户要求改预算、称呼、单位、采购类型/月份、私域画像文本或「客户动态标签」时调用。"
            "动态标签必须用 profile_tag_ids（系统标签 id），禁止把标签内容写进 ai_profile。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "budget": {"type": "number", "description": "客户预算金额 (数字，如 5000)"},
                "title": {"type": "string", "description": "客户头衔/称呼 (如: 张总, 李主任)"},
                "unit_name": {"type": "string", "description": "所属单位名称"},
                "purchase_type": {"type": "string", "description": "采购类型"},
                "purchase_months": {"type": "string", "description": "采购月份，多个用英文逗号分隔 (如: 3月,4月)，勿用顿号"},
                "ai_profile": {
                    "type": "string",
                    "description": "仅自由文本客情（性格、偏好、跟进要点）。禁止写入动态标签名、禁止写「客户动态标签：」前缀；改标签请用 profile_tag_ids。",
                },
                "profile_tag_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "客户动态标签 id 列表（仅使用系统提示中「客户动态标签」段落列出的 id）。清空全部标签传 []。",
                },
            },
        },
    },
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

COUNT_PRODUCTS_TOOL = {
    "type": "function",
    "function": {
        "name": "count_products",
        "description": "统计当前在售/可用商品数量（按系统配置的可用供应商范围）。当用户询问在售商品总数、上架数量、商品有多少等统计口径时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "可选：关键词（商品名/供应商名模糊匹配）"},
                "category": {"type": "string", "description": "可选：分类关键词（匹配一级/二级/三级分类名）"},
                "max_price": {"type": "number", "description": "可选：价格上限"},
                "min_price": {"type": "number", "description": "可选：价格下限"}
            }
        }
    }
}

class AIGateway:
    """AI 网关: 上下文装配 → Prompt 选择 → LLM 调用 → 流式返回 → 自动存盘"""

    def __init__(
        self,
        db: AsyncSession,
        llm: LLMClient,
        router_llm: Optional[LLMClient] = None,
        router_enabled: bool = True,
    ):
        self.db = db
        self.llm = llm
        self.assembler = ContextAssembler(db)
        # 提示词解析入口：按 scenario 读取 DB 化的 published 版本并渲染；
        # DB 无版本或开关关闭时自动回退到旧 prompts.py 逻辑。
        self.prompt_service = PromptService(db)
        # 场景路由器：在 PromptService 之前决定 scenario_key。
        # router_llm 为 None 时只跑规则+兜底（仍可工作）。
        self.scene_router = SceneRouter(
            llm_router=router_llm,
            enabled=bool(router_enabled and router_llm is not None),
        )

    async def stream_chat(
        self,
        user_id: int,
        customer_phone: Optional[str] = None,
        query: str = "",
        scenario: str = "general_chat",
        conversation_id: str = None,
        sales_wechat_id: Optional[str] = None,
        raw_customer_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """
        主入口: 流式 AI 对话。
        """
        try:
            # 1. 客户与落库用户消息（支持仅有 raw_customer_id、无手机号的客户）
            phone = (customer_phone or "").strip()
            rid = (raw_customer_id or "").strip()
            customer = None
            customer_id = None
            if rid:
                cust_res = await self.db.execute(select(RawCustomer).where(RawCustomer.id == rid))
                customer = cust_res.scalars().first()
            if not customer and phone:
                cust_res = await self.db.execute(
                    select(RawCustomer).where(or_(RawCustomer.phone == phone, RawCustomer.phone_normalized == phone))
                )
                customer = cust_res.scalars().first()
            if customer:
                customer_id = customer.id
            elif not phone and not rid:
                customer_id = "INTERNAL_QA"
                from sqlalchemy import text
                await self.db.execute(text(
                    "INSERT INTO raw_customers (id, name, remark, is_deleted) "
                    "VALUES (:id, :name, :remark, :is_deleted) "
                    "ON DUPLICATE KEY UPDATE id=id"
                ), {"id": customer_id, "name": "内部问答", "remark": "无具体客户关联的内部问答日志", "is_deleted": True})
                await self.db.commit()

            is_real_customer = bool(customer_id and str(customer_id) != "INTERNAL_QA")

            # 与侧栏「客户×业务微信」行对齐，便于落库与历史检索按线程隔离
            resolved_session_sw: Optional[str] = None
            if is_real_customer:
                resolved_session_sw = await crud.effective_sales_wechat_for_customer_session(
                    self.db, user_id, sales_wechat_id
                )

            if customer_id:
                user_msg = ChatMessage(
                    user_id=user_id,
                    raw_customer_id=customer_id,
                    role="user",
                    content=query,
                    dify_conv_id=conversation_id,
                    sales_wechat_id=(resolved_session_sw if is_real_customer else None),
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
                        raw_customer_id=customer_id,
                        role="assistant",
                        content=full_answer,
                        dify_conv_id=conversation_id,
                        chat_model=self.llm.model,
                        sales_wechat_id=(resolved_session_sw if is_real_customer else None),
                    )
                    self.db.add(ai_msg)
                    await self.db.commit()
                    await self.db.refresh(ai_msg)
                    msg_id = ai_msg.id
                    logger.info(f"AI Gateway: 模型身份回复已保存 msg_id={msg_id}")
                yield json.dumps({"event": "done", "msg_id": msg_id}, ensure_ascii=False)
                return

            # 3. 场景路由：在重 IO 之前决定 scenario_key
            # ui_category 由"是否绑定客户"反推（与桌面端 chat_context 对齐）。
            scenario_hint = (scenario or "").strip()
            inferred_ui_category = "customer_chat" if (is_real_customer or bool(phone)) else "free_chat"
            decision: RouteDecision = await self.scene_router.classify(
                query=query,
                ui_category=inferred_ui_category,
                has_customer=is_real_customer or bool(phone),
                hint=scenario_hint,
                user_id=user_id,
            )
            resolved_scenario = decision.scenario_key
            logger.info(
                "AI Gateway: scene routed hint={} → scenario={} source={} score={:.2f} cached={} reason={}",
                scenario_hint or "(none)",
                resolved_scenario,
                decision.source,
                decision.score,
                decision.cached,
                decision.reason,
            )

            # 审计：把决策结果落 prompt_audit_log，便于后续复盘准确率
            try:
                self.db.add(PromptAuditLog(
                    actor_id=user_id,
                    action="router.decide",
                    target_type="scenario",
                    target_id=None,
                    payload_json={
                        "query_preview": (query or "")[:80],
                        "hint": scenario_hint,
                        "ui_category": inferred_ui_category,
                        "decision": decision.to_meta_dict(),
                    },
                ))
                await self.db.commit()
            except Exception as e:
                logger.warning("AI Gateway: 审计路由决策失败（忽略）: {}", e)

            # 4. 常规路径：装配上下文 + 场景话术 + 工具
            if is_real_customer:
                ctx = await self.assembler.assemble(
                    user_id,
                    customer_phone=phone or None,
                    raw_customer_id=str(customer_id),
                    resolved_sales_wechat_id=resolved_session_sw,
                )
                logger.info(
                    "AI Gateway: 上下文装配完成 raw_customer_id={} phone={} scenario={}",
                    customer_id,
                    phone or "",
                    resolved_scenario,
                )
            elif phone:
                ctx = await self.assembler.assemble(
                    user_id,
                    customer_phone=phone,
                    raw_customer_id=None,
                    resolved_sales_wechat_id=None,
                )
                logger.info(
                    "AI Gateway: 上下文装配完成 phone={} scenario={}（未命中客户实体）",
                    phone,
                    resolved_scenario,
                )
            else:
                ctx = await self.assembler.assemble_for_staff(user_id)
                logger.info(
                    "AI Gateway: 无客户上下文(内部问答) user_id={} scenario={}",
                    user_id,
                    resolved_scenario,
                )

            # 通过 PromptService 解析 system prompt（DB 化 + 回滚兜底）
            resolution = await self.prompt_service.resolve(
                scenario_key=resolved_scenario,
                ctx=ctx,
                query=query,
                history=ctx.get("ai_history_messages", []),
                customer_id=customer_id,
                user_id=user_id,
                tags=decision.to_tags(),
            )
            messages = resolution.messages
            logger.info("AI Gateway: prompt resolved meta={}", resolution.meta)

            # 5. 告知客户端实际使用的对话模型 + 路由结果（meta 中新增 scenario_hint / route）
            yield json.dumps(
                {
                    "event": "meta",
                    "chat_model": self.llm.model,
                    "scenario": resolved_scenario,
                    "scenario_hint": scenario_hint or None,
                    "route": decision.to_meta_dict(),
                    "prompt_version": resolution.meta.get("version"),
                },
                ensure_ascii=False,
            )

            # 5. 多轮 tool calling 循环
            # 模型可能分步使用工具：典型链路 = update_customer_info → search_products(关键词A)
            # → search_products(关键词B) → 总结文本。每轮内部由模型自主决定，跑满 MAX_TOOL_ITERATIONS
            # 仍未给出最终文本就中止并走兜底，避免死循环。
            full_answer = ""
            tools = None
            if resolution.tools_enabled:
                if is_real_customer:
                    tools = [UPDATE_CUSTOMER_TOOL, SEARCH_PRODUCTS_TOOL, COUNT_PRODUCTS_TOOL]
                else:
                    tools = [SEARCH_PRODUCTS_TOOL, COUNT_PRODUCTS_TOOL]

            MAX_TOOL_ITERATIONS = 4
            last_reasoning_preview: Optional[str] = None
            for iteration in range(MAX_TOOL_ITERATIONS):
                iter_text = ""
                iter_tool_calls: list[dict] = []
                iter_reasoning: Optional[str] = None

                async for chunk_text in self.llm.stream_chat(messages, tools=tools):
                    if chunk_text.startswith("__REASONING_CONTENT__:"):
                        iter_reasoning = chunk_text.split(":", 1)[1]
                        continue
                    if chunk_text.startswith("__TOOL_CALL__:"):
                        tc_json = chunk_text.split(":", 1)[1]
                        try:
                            iter_tool_calls.append(json.loads(tc_json))
                        except json.JSONDecodeError as e:
                            logger.warning(
                                "AI Gateway: tool_call JSON 解析失败 iter={} err={} payload={}",
                                iteration, e, tc_json[:200],
                            )
                        continue
                    iter_text += chunk_text
                    full_answer += chunk_text
                    yield json.dumps({"event": "chunk", "text": chunk_text}, ensure_ascii=False)

                if iter_reasoning:
                    last_reasoning_preview = iter_reasoning[:120]

                # 首轮就什么都没产生：上游可能吞包或解析失败，记一条 warn 便于排障
                if iteration == 0 and not iter_text.strip() and not iter_tool_calls:
                    logger.warning(
                        "AI Gateway: 首轮 LLM 无文本且无工具调用（可能被上游吞掉或解析失败），"
                        "query_preview={}",
                        (query[:50] + "…") if len(query) > 50 else query,
                    )

                # 本轮没有 tool_calls → 模型已经给出最终文本，结束循环
                if not iter_tool_calls:
                    break

                logger.info(
                    "AI Gateway: iter={} 检测到工具调用: {}",
                    iteration, iter_tool_calls,
                )

                # 把本轮 assistant 消息（含 tool_calls / 可选 reasoning_content）追加进 history
                formatted_tcs = [
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": tc.get("arguments", ""),
                        },
                    }
                    for tc in iter_tool_calls
                ]
                assistant_msg = {
                    "role": "assistant",
                    "content": iter_text,
                    "tool_calls": formatted_tcs,
                }
                if iter_reasoning:
                    # DeepSeek thinking 模式要求把 reasoning_content 原样回传到下一轮
                    assistant_msg["reasoning_content"] = iter_reasoning
                messages.append(assistant_msg)

                # 执行 tool_calls，把工具结果 append 到 messages，并把需要透传到前端的事件 yield 出去
                async for action_event in self._execute_tool_calls_and_collect(
                    tool_calls=iter_tool_calls,
                    messages=messages,
                    customer_id=customer_id,
                    user_id=user_id,
                    sales_wechat_id=resolved_session_sw,
                ):
                    yield action_event
            else:
                # for...else：循环跑满 MAX_TOOL_ITERATIONS 才进这里（被 break 跳出时不会）
                logger.warning(
                    "AI Gateway: tool call 迭代达上限 {} 仍未得到最终回复 model={}",
                    MAX_TOOL_ITERATIONS,
                    self.llm.model,
                )

            # 兜底：如果整条会话流程结束仍没产生任何正文，给一段可读文本，避免空回复
            if not full_answer.strip():
                fallback_text = (
                    "抱歉，按当前条件没有合适的商品可推荐，"
                    "可以补充一下品类偏好或放宽预算，我再帮你筛选。"
                )
                logger.warning(
                    "AI Gateway: 多轮 tool calling 结束后仍无正文，使用兜底文案 "
                    "model={} reasoning_preview={}",
                    self.llm.model,
                    last_reasoning_preview or "",
                )
                full_answer = fallback_text
                yield json.dumps({"event": "chunk", "text": fallback_text}, ensure_ascii=False)

            # 5. 保存 AI 回复
            msg_id = None
            if customer_id and full_answer:
                ai_msg = ChatMessage(
                    user_id=user_id,
                    raw_customer_id=customer_id,
                    role="assistant",
                    content=full_answer,
                    dify_conv_id=conversation_id,
                    chat_model=self.llm.model,
                    sales_wechat_id=(resolved_session_sw if is_real_customer else None),
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

    async def _execute_tool_calls_and_collect(
        self,
        *,
        tool_calls: list[dict],
        messages: list[dict],
        customer_id: Optional[str],
        user_id: int,
        sales_wechat_id: Optional[str] = None,
    ):
        """依次执行本轮 tool_calls；把每个工具结果以 OpenAI tool message 形式 append 到 messages，
        并把需要透传给前端的事件（如 update_customer 弹窗）yield 出去。

        无论工具成功失败都会产出一条 tool message，保证 messages 中的 tool_call_id ↔ tool
        响应一一对应，否则下一轮 LLM 会以 400 拒绝整段历史。
        """
        for tc in tool_calls:
            name = tc.get("name") or ""
            tcid = tc.get("id", "")
            raw_args = tc.get("arguments") or "{}"

            if name == "update_customer_info":
                try:
                    args = json.loads(raw_args)
                    await self._execute_update_customer_tool(
                        customer_id, user_id, args, sales_wechat_id=sales_wechat_id,
                    )
                    yield json.dumps(
                        {"event": "system_action", "action": "update_customer", "changes": args},
                        ensure_ascii=False,
                    )
                    success_msg = {
                        "status": "success",
                        "message": (
                            "数据库已更新。系统提示：作为一个销售，请现在立刻利用刚刚更新的这些客户线索"
                            "（如新的预算、采购时间等），自然地向客户推荐合适的产品或推进下一步约访，"
                            "不要仅仅回复确认修改！"
                        ),
                    }
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tcid,
                        "content": json.dumps(success_msg, ensure_ascii=False),
                    })
                except Exception as e:
                    logger.error(f"Tool execution failed (update_customer_info): {e}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tcid,
                        "content": json.dumps(
                            {"status": "error", "message": str(e)}, ensure_ascii=False
                        ),
                    })
            elif name == "search_products":
                try:
                    args = json.loads(raw_args)
                    search_res = await self._execute_search_products_tool(args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tcid,
                        "content": search_res,
                    })
                except Exception as e:
                    logger.error(f"Search products tool failed: {e}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tcid,
                        "content": f"查询出错: {str(e)}",
                    })
            elif name == "count_products":
                try:
                    args = json.loads(raw_args)
                    res_json = await self._execute_count_products_tool(args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tcid,
                        "content": res_json,
                    })
                except Exception as e:
                    logger.error(f"Count products tool failed: {e}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tcid,
                        "content": f"统计出错: {str(e)}",
                    })
            else:
                logger.warning(
                    "AI Gateway: 未知 tool_call name={} 已忽略 args_preview={}",
                    name, raw_args[:120],
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tcid,
                    "content": json.dumps(
                        {"status": "error", "message": f"unknown tool: {name}"},
                        ensure_ascii=False,
                    ),
                })

    async def _execute_update_customer_tool(
        self,
        customer_id: str,
        user_id: int,
        args: dict,
        *,
        sales_wechat_id: Optional[str] = None,
    ):
        """执行数据库更新操作"""
        if not customer_id or not args:
            return

        tag_ids = crud.parse_profile_tag_ids(args["profile_tag_ids"]) if "profile_tag_ids" in args else None

        cust_updates = {}
        if "unit_name" in args and args["unit_name"] is not None:
            cust_updates["unit_name"] = args["unit_name"]
        if "purchase_months" in args:
            norm = normalize_purchase_months(args["purchase_months"])
            cust_updates["purchase_months"] = (
                [p.strip() for p in norm.split(",") if p.strip()] if norm else []
            )

        rel_updates = {}
        if "budget" in args:
            try:
                rel_updates["budget_amount"] = float(args["budget"])
            except (TypeError, ValueError):
                pass
        if "title" in args and args["title"] is not None:
            rel_updates["title"] = args["title"]
        if "purchase_type" in args and args["purchase_type"] is not None:
            rel_updates["purchase_type"] = args["purchase_type"]
        if "ai_profile" in args and args["ai_profile"] is not None:
            rel_updates["ai_profile"] = args["ai_profile"]

        touch_profile = bool(rel_updates) or tag_ids is not None

        sw = (str(sales_wechat_id).strip() if sales_wechat_id else "") or None
        if not sw:
            sw = await crud.primary_sales_wechat_for_user(self.db, user_id)

        relation = None
        if sw:
            r = await self.db.execute(
                select(SalesCustomerProfile).where(
                    SalesCustomerProfile.raw_customer_id == customer_id,
                    SalesCustomerProfile.sales_wechat_id == sw,
                )
            )
            relation = r.scalars().first()
        if relation is None:
            r = await self.db.execute(
                select(SalesCustomerProfile).where(
                    SalesCustomerProfile.raw_customer_id == customer_id,
                    SalesCustomerProfile.user_id == user_id,
                    SalesCustomerProfile.sales_wechat_id.is_(None),
                )
            )
            relation = r.scalars().first()

        if touch_profile and relation is None:
            relation = SalesCustomerProfile(
                raw_customer_id=customer_id,
                sales_wechat_id=sw,
                user_id=user_id,
                relation_type="active",
                contact_date=date.today(),
            )
            self.db.add(relation)
            await self.db.flush()

        if cust_updates:
            await self.db.execute(
                update(RawCustomer).where(RawCustomer.id == customer_id).values(**cust_updates)
            )

        if relation is not None and rel_updates:
            for k, v in rel_updates.items():
                setattr(relation, k, v)

        if relation is not None and tag_ids is not None:
            await crud.replace_ucr_profile_tags(
                self.db, relation, tag_ids, require_active=False
            )

        if cust_updates or touch_profile:
            await self.db.execute(
                update(RawCustomer)
                .where(RawCustomer.id == customer_id)
                .values(profile_status=1)
            )

        await self.db.commit()

    async def _execute_search_products_tool(self, args: dict) -> str:
        """执行商品搜索并返回格式化结果给大模型"""
        keyword = args.get("keyword", "")
        category = args.get("category", "")
        max_price = args.get("max_price")
        min_price = args.get("min_price")
        
        query = select(Product)
        
        # 可选：过滤 active suppliers（配置缺失时不要阻断查询）
        config_res = await self.db.execute(select(SystemConfig).where(SystemConfig.config_key == "supplier_ids"))
        config_obj = config_res.scalars().first()
        active_ids = []
        if config_obj and config_obj.config_value and config_obj.config_value.strip():
            active_ids = [s.strip() for s in config_obj.config_value.split(",") if s.strip()]
            
        if active_ids:
            query = query.where(Product.supplier_id.in_(active_ids))
        # 若 supplier_ids 未配置：默认查询全库（仍然 limit，避免上下文爆仓）

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
            if not active_ids:
                return f"商品库暂无可用数据，或未命中搜索条件 (关键词: {keyword}, 分类: {category}, 价格区间: {min_price}-{max_price})"
            return f"未能找到符合条件的商品 (关键词: {keyword}, 分类: {category}, 价格区间: {min_price}-{max_price})"
            
        res_text = "找到以下商品：\n"
        if not active_ids:
            res_text += "（提示：当前未配置 supplier_ids，结果为全库检索）\n"
        for p in products:
            res_text += f"- 【{p.product_name}】 价格: ￥{p.price}/{p.unit}，分类: {p.category_name_one}，供应商: {p.supplier_name}\n"
        return res_text

    async def _execute_count_products_tool(self, args: dict) -> str:
        """统计商品数量并返回 JSON 字符串给大模型（避免模型误读口径）。"""
        from sqlalchemy import func

        keyword = (args.get("keyword") or "").strip()
        category = (args.get("category") or "").strip()
        max_price = args.get("max_price")
        min_price = args.get("min_price")

        stmt = select(func.count(Product.id))

        # 过滤 active suppliers（与 search_products 口径一致）
        config_res = await self.db.execute(select(SystemConfig).where(SystemConfig.config_key == "supplier_ids"))
        config_obj = config_res.scalars().first()
        active_ids = []
        if config_obj and config_obj.config_value and config_obj.config_value.strip():
            active_ids = [s.strip() for s in config_obj.config_value.split(",") if s.strip()]

        if active_ids:
            stmt = stmt.where(Product.supplier_id.in_(active_ids))
        else:
            return json.dumps(
                {"status": "error", "message": "当前系统没有配置可用的供应商，无法统计商品数量。"},
                ensure_ascii=False,
            )

        if keyword:
            stmt = stmt.where(or_(
                Product.product_name.ilike(f"%{keyword}%"),
                Product.supplier_name.ilike(f"%{keyword}%")
            ))
        if category:
            stmt = stmt.where(or_(
                Product.category_name_one.ilike(f"%{category}%"),
                Product.category_name_two.ilike(f"%{category}%"),
                Product.category_name_three.ilike(f"%{category}%"),
            ))
        if min_price is not None:
            stmt = stmt.where(Product.price >= min_price)
        if max_price is not None:
            stmt = stmt.where(Product.price <= max_price)

        cnt = (await self.db.execute(stmt)).scalar_one()
        payload = {
            "status": "success",
            "count": int(cnt or 0),
            "scope": "products",
            "filters": {
                "keyword": keyword or None,
                "category": category or None,
                "min_price": min_price,
                "max_price": max_price,
                "supplier_ids_configured": len(active_ids),
            },
        }
        return json.dumps(payload, ensure_ascii=False)

