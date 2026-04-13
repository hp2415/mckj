import asyncio
import httpx
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
        self._last_user_query = ""

    async def handle_ai_copy(self, msg_id):
        """处理来自气泡的复制上报信号 (采纳统计)"""
        logger.info(f"监测到 AI 回复采纳行为 (复制): MsgID={msg_id}")
        await self.api.record_message_copy(msg_id)

    async def handle_ai_feedback(self, msg_id, rating):
        """处理来自气泡的评价信号"""
        logger.info(f"提交消息评价: ID={msg_id}, Rating={rating}")
        await self.api.set_message_feedback(msg_id, rating)

    async def handle_ai_regenerate(self):
        """处理重新生成请求"""
        if not self._last_user_query:
            return
            
        # 1. 界面清理：删除最后一条消息 (通常是 AI 的气泡)
        chat_layout = self.app.main_win.chat_page.chat_layout
        if chat_layout.count() > 1:
            item = chat_layout.takeAt(chat_layout.count() - 2)
            if item.widget():
                item.widget().deleteLater()
        
        # 2. 重新触发发送 (带入 is_regenerated 标记提升数据颗粒度)
        logger.info(f"重新生成 AI 回复，原问题: {self._last_user_query}")
        await self.handle_ai_chat_sent(self._last_user_query, is_regen=True)

    async def handle_ai_chat_sent(self, text, is_regen=False):
        """处理来自 UI 的 AI 发送请求与流式对话拼接"""
        self._last_user_query = text # 记录用于重发
        
        current_customer = getattr(self.app, "_current_customer", None)
        if not current_customer:
            QMessageBox.warning(self.app.main_win, "未选中客户", "请先在左侧选择一个客户再进行对话。")
            return

        # 1. UI 展示用户消息 (重发时不重复展示用户消息)
        if not is_regen:
            self.app.main_win.chat_page.add_message(text, True)
        
        # 2. 创建一个空的 AI 气泡用于流式接收
        ai_bubble = self.app.main_win.chat_page.add_message("", False)
        
        # 3. 准备 Dify 调度参数
        user_id = getattr(self.api, "username", "anonymous")
        conv_id = current_customer.get("dify_conversation_id")
        phone = current_customer.get("phone")
        
        # 3.1 后端在线探测
        try:
            async with httpx.AsyncClient(timeout=3.0) as probe:
                probe_resp = await probe.get(
                    f"{self.api.base_url}/api/system/sync/status",
                    headers={"Authorization": f"Bearer {self.api.token}"}
                )
                if probe_resp.status_code not in (200, 403):
                    raise httpx.RequestError("Backend returned unexpected status")
        except Exception:
            ai_bubble.append_text("⚠️ 云端连接失败：服务器已离线，请检查后端是否正常运行。")
            return
        
        # 3.2 预落盘流水：保存用户发送的消息 (此方法由主循环接管)
        asyncio.create_task(self.api.save_chat_message(phone, "user", text, conv_id))
        
        # 4. 执行 Dify 长链接流式迭代
        full_answer = ""
        try:
            async for chunk in self.api.stream_dify_chat(text, user_id, conv_id):
                if chunk.startswith("[CONV_ID:"):
                    new_id = chunk[9:-1]
                    if new_id != conv_id:
                        current_customer["dify_conversation_id"] = new_id
                        asyncio.create_task(self.api.update_customer_relation(phone, {"dify_conversation_id": new_id}))
                        conv_id = new_id
                elif chunk.startswith("Error:"):
                    ai_bubble.append_text(f"\n⚠️ {chunk}")
                else:
                    ai_bubble.append_text(chunk)
                    full_answer += chunk
        except Exception as e:
            ai_bubble.append_text(f"\n⚠️ 连接异常: {str(e)}")
        
        # 5. 后落盘流水：保存 AI 回复的消息 (更新流记录)
        if full_answer:
            save_resp = await self.api.save_chat_message(
                phone, "assistant", full_answer, conv_id, is_regen=is_regen
            )
            # 5.1 回填业务审计标识 UUID 用于反馈追溯
            if save_resp and save_resp.get("code") == 200:
                msg_id = save_resp.get("data", {}).get("id")
                if msg_id:
                    ai_bubble.msg_id = msg_id
                    logger.info(f"AI 回复已落盘成功，返回标识: {msg_id}")
