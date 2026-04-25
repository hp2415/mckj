import asyncio
import httpx
import json
from PySide6.QtWidgets import QMessageBox
from logger_cfg import logger

class ChatHandler:
    """
    独立接管 AI 对话的交互信号调度、对话历史网络请求及数据库落盘同步
    """
    def __init__(self, app_controller, api_client):
        """
        :param app_controller: 传入主控制器提取 current_customer 上下文状态
        :param api_client: API 请求层
        """
        self.app = app_controller
        self.api = api_client
        self._current_tasks: list[asyncio.Task] = []  # 用于管理正在运行的 AI 生成任务（多模型并发）

    async def handle_ai_copy(self, msg_id):
        """处理来自气泡的复制上报信号 (采纳统计)"""
        logger.info(f"监测到 AI 回复采纳行为 (复制): MsgID={msg_id}")
        await self.api.record_message_copy(msg_id)

    async def handle_ai_feedback(self, msg_id, rating):
        """处理来自气泡的评价信号"""
        logger.info(f"提交消息评价: ID={msg_id}, Rating={rating}")
        await self.api.set_message_feedback(msg_id, rating)

    async def handle_ai_regenerate(self, query: str):
        """处理针对特定问题的重新生成请求"""
        if not query:
            logger.warning("尝试重新生成，但未找到原始提问文本")
            return
            
        # 1. 界面清理：删除所有关联该提问的 AI 气泡（多模型并发会产生多条）
        chat_layout = self.app.main_win.chat_page.chat_layout
        # 倒序扫描，删除所有 user_query 匹配且为 AI 的气泡
        for i in reversed(range(chat_layout.count())):
            item = chat_layout.itemAt(i)
            w = item.widget() if item else None
            if not w:
                continue
            if getattr(w, "is_user", False):
                continue
            if getattr(w, "user_query", "") == query:
                w.deleteLater()
        
        # 2. 重新触发发送
        logger.info(f"重新生成 AI 回复，原问题: {query}")
        await self.handle_ai_chat_sent(query, is_regen=True)

    def cancel_current_task(self):
        """取消当前正在进行的 AI 对话任务"""
        alive = [t for t in (self._current_tasks or []) if t and not t.done()]
        for t in alive:
            t.cancel()
        if alive:
            logger.info(f"已手动取消当前 AI 对话任务 ({len(alive)} 个并发流)")
        self._current_tasks = []

    async def handle_ai_chat_sent(self, text, is_regen=False):
        """处理来自 UI 的 AI 发送请求与流式对话拼接"""
        # 0. 先取消可能存在的旧任务
        self.cancel_current_task()
        
        # 1. 获取当前场景 (如果有 UI 元素支持)
        scenario = "general_chat"
        if hasattr(self.app.main_win.chat_page, "get_selected_scenario_key"):
            scenario = self.app.main_win.chat_page.get_selected_scenario_key() or "general_chat"

        # 2. 启动新任务（按模型并发）
        root = asyncio.create_task(self._do_ai_chat_multi(text, is_regen, scenario))
        self._current_tasks = [root]

    async def _do_ai_chat_multi(self, text, is_regen=False, scenario="general_chat"):
        """真正的 AI 对话执行逻辑（可被取消）"""
        staff_mode = getattr(self.app, "_chat_surface_mode", "customer") == "staff"
        current_customer = getattr(self.app, "_current_customer", None)
        if not staff_mode and not current_customer:
            self.app.main_win.show_info_bar("warning", "未选中客户", "请先在左侧选择一个客户再进行对话。")
            return

        # 1. UI 展示用户消息 (重发时不重复展示用户消息)
        if not is_regen:
            self.app.main_win.chat_page.add_message(text, True)

        phone = None if staff_mode else (current_customer or {}).get("phone")
        conv_id = None if staff_mode else (current_customer or {}).get("dify_conversation_id")
        
        # 3. 后端在线探测
        try:
            async with httpx.AsyncClient(timeout=3.0) as probe:
                probe_resp = await probe.get(
                    f"{self.api.base_url}/api/system/sync/status",
                    headers={"Authorization": f"Bearer {self.api.token}"}
                )
                if probe_resp.status_code not in (200, 403):
                    raise httpx.RequestError("Backend returned unexpected status")
        except Exception:
            # 多模型模式下此处还未创建气泡：仅提示，不要引用未定义变量
            self.app.main_win.show_info_bar("error", "云端连接失败", "服务器可能已离线。")
            return
        
        # 4. 执行后端 AI 网关流式迭代（按模型并发）
        chat_page = self.app.main_win.chat_page
        if hasattr(chat_page, "get_chat_models"):
            models = chat_page.get_chat_models() or []
        else:
            models = [chat_page.get_chat_model()] if hasattr(chat_page, "get_chat_model") else []
        models = [m for m in models if (m or "").strip()]
        if not models:
            models = [None]

        async def run_one(model_id: str | None):
            mtag = ""
            if model_id and hasattr(chat_page, "get_chat_model_label"):
                mtag = chat_page.get_chat_model_label(model_id)
            elif model_id:
                mtag = model_id
            # 每个模型各自一个气泡
            ai_bubble = chat_page.add_message("", False, user_query=text, model_tag=mtag)
            full_answer = ""
            agen = None
            try:
                agen = self.api.stream_ai_chat(
                    query=text,
                    customer_phone=phone,
                    scenario=scenario,
                    conversation_id=conv_id,
                    chat_model=model_id,
                )
                async for chunk in agen:
                    if chunk.startswith("[META_MODEL:"):
                        try:
                            raw = chunk[12:-1]
                            payload = json.loads(raw)
                            mid = payload.get("chat_model") or ""
                            scen = payload.get("scenario") or ""
                            if hasattr(chat_page, "apply_server_chat_meta"):
                                chat_page.apply_server_chat_meta(mid, scen)
                            # 回写气泡的模型标签（以服务端实际模型为准）
                            if mid and hasattr(chat_page, "get_chat_model_label"):
                                ai_bubble.set_model_tag(chat_page.get_chat_model_label(mid))
                            elif mid:
                                ai_bubble.set_model_tag(mid)
                        except Exception as e:
                            logger.warning(f"解析对话 meta 失败: {e}")
                    elif chunk.startswith("[MSG_ID:"):
                        msg_id_str = chunk[8:-1]
                        try:
                            ai_bubble.msg_id = int(msg_id_str)
                            logger.info(f"AI 回复已落盘成功，返回标识: {msg_id_str}")
                        except ValueError:
                            pass
                    elif chunk.startswith("[SYSTEM_ACTION:"):
                        try:
                            changes_str = chunk[15:-1]
                            changes = json.loads(changes_str)

                            # 翻译字段名为中文
                            field_map = {
                                "budget": "预算",
                                "title": "称呼",
                                "unit_name": "单位",
                                "purchase_type": "采购类型",
                                "purchase_months": "采购月份",
                                "ai_profile": "客户画像",
                            }
                            modified_fields = [field_map.get(k, k) for k in changes.keys()]
                            fields_str = "、".join(modified_fields)

                            self.app.main_win.show_info_bar(
                                "success", "资料已自动更新", f"AI已帮您修改了以下资料: {fields_str}"
                            )
                            # 通知侧边栏和详情页刷新本地数据
                            self.app.main_win.ui_data_refresh_requested.emit()
                        except Exception as e:
                            logger.error(f"Failed to parse system action: {e}")
                    elif chunk.startswith("Error:"):
                        ai_bubble.show_error(chunk[6:].strip())
                        return
                    else:
                        ai_bubble.append_text(chunk)
                        full_answer += chunk
            except (asyncio.CancelledError, RuntimeError) as e:
                if isinstance(e, RuntimeError) and "cancel scope" not in str(e):
                    ai_bubble.show_error(f"系统错误: {str(e)}")
                else:
                    logger.info("AI 任务已正常中断")
                return
            except Exception as e:
                ai_bubble.show_error(f"连接异常: {str(e)}")
                return
            finally:
                # 显式关闭 async generator，避免 httpcore/anyio 在取消时输出
                # "async generator ignored GeneratorExit" / "exit cancel scope in a different task"
                try:
                    if agen is not None:
                        await agen.aclose()
                except Exception:
                    pass
            if not full_answer:
                ai_bubble.show_error("AI 未返回任何内容，请重试。")

        # 为每个模型启动并发任务（用于取消）
        tasks = [asyncio.create_task(run_one(m)) for m in models]
        self._current_tasks = tasks
        await asyncio.gather(*tasks, return_exceptions=True)
