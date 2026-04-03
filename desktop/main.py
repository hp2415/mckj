import sys
import asyncio
import httpx
from qasync import QEventLoop, asyncSlot
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QPixmap

# 本地模块导入
from api_client import APIClient
from ui.login_dialog import LoginDialog
from ui.main_window import MainWindow
from logger_cfg import logger

class DesktopApp:
    """
    整合 UI 界面与异步通讯总线。
    负责控制窗口跳转、异步信号处理以及数据同步流。
    """
    def __init__(self):
        # 默认连接本地后端
        self.api = APIClient("http://localhost:8000")
        self.login_dlg = None
        self.main_win = None
        self._http_session = None
        self._pixmap_cache = {}  # L1 内存缓存：url -> QPixmap

    async def launch(self):
        """进入程序生命周期"""
        logger.info("====== 微企 AI 桌面端助理启动 ======")
        # 初始化持续性的 HTTP 会话连接池，消除握手延迟
        if not self._http_session:
            self._http_session = httpx.AsyncClient(timeout=10.0)
            
        self.login_dlg = LoginDialog()
        self.login_dlg.login_requested.connect(self._handle_login)
        self.login_dlg.show()
        
        self._login_future = asyncio.get_event_loop().create_future()
        self.login_dlg.finished.connect(self._on_login_dialog_finished)
        
        result_code = await self._login_future
        
        if result_code == LoginDialog.Accepted:
            # 鉴权通过，构建主看板
            user_name = self.api.user_data.get("real_name", "管理员")
            logger.info(f"登录校验成功，操作人: {user_name} (Role: {self.api.user_data.get('role')})")
            self.main_win = MainWindow(user_name)
            self.main_win.search_requested.connect(self._handle_search)
            self.main_win.customer_selected.connect(self._handle_customer_selected)
            self.main_win.info_page.save_clicked.connect(self._handle_save_customer_relation)
            self.main_win.info_page.history_clicked.connect(self._handle_history_clicked)
            self.main_win.logout_btn.clicked.connect(self._handle_logout)
            
            # 5.1 AI 聊天信号连接
            self.main_win.chat_page.send_requested.connect(self._handle_ai_chat_sent)
            # 5.2 商品同步信号连接 (NEW) - 使用 lambda 适配纯协程
            self.main_win.sync_triggered.connect(lambda: asyncio.create_task(self._handle_sync_trigger()))
            self.main_win.btn_prod.clicked.connect(lambda: asyncio.create_task(self._refresh_sync_status()))
            
            self.main_win.show()
            
            # 6. 根据角色权限展示同步按钮
            user_role = self.api.user_data.get("role", "staff")
            if user_role == "admin":
                self.main_win.btn_sync_now.show()
            
            # 【关键】主窗口显示后，恢复“最后一个窗口关闭即退出”的行为，
            # 这样手动点击 X 时，QApplication 会正常终止
            QApplication.setQuitOnLastWindowClosed(True)
            
            # 并行初始化数据加载
            asyncio.create_task(self._initial_data_fetch())
        else:
            # 登录界面被手动关闭
            if self._http_session:
                asyncio.create_task(self._http_session.aclose())
            QApplication.quit()

    async def _initial_data_fetch(self):
        """首屏数据并行拉取逻辑"""
        # 1. 加载客户列表
        customers_resp = await self.api.get_my_customers()
        if customers_resp and customers_resp.get("code") == 200:
            self.main_win.update_customer_list(customers_resp.get("data", []))

        # 2. 拉取动态 AI 配置与系统字典配置 (NEW)
        await self.api.get_ai_config()
        configs_dict = await self.api.get_configs_dict()
        if configs_dict:
            self.main_win.info_page.populate_combo_boxes(configs_dict)
            
        await self._refresh_sync_status()

        # 3. 默认加载 AI 对话页
        self.main_win.stack.setCurrentIndex(0)
        await self.perform_search("", 0, 20)

    @asyncSlot()
    async def _handle_logout(self):
        """注销重启：临时接管退出信号，防止主窗口关闭导致进程被杀"""
        if self.main_win:
            # 临时关闭自动退出，确保接下来的 close() 不会干掉整个进程
            QApplication.setQuitOnLastWindowClosed(False)
            self.main_win.close()
            self.main_win = None
            
        # 清理内存 L1 缓存与会话
        self._pixmap_cache.clear()
        if self._http_session:
            await self._http_session.aclose()
            self._http_session = None
            
        self.api.logout()
        # 重启登录流程
        self._restart_task = asyncio.create_task(self.launch())

    @asyncSlot()
    async def _handle_customer_selected(self, customer_data):
        """当侧边栏选中某个客户时触发"""
        self._current_customer = customer_data # 锁定当前业务上下文
        print(f"已选中客户: {customer_data.get('customer_name')}, ConvID: {customer_data.get('dify_conversation_id')}")
        
        # 自动切换到资料页并填充表单 (通过新的整合函数触发正确的 UI 状态)
        self.main_win.switch_tab(1)
        self.main_win.info_page.set_customer(customer_data)
        
        # 准备 AI 对话页 (切换客户时清空历史，准备新上下文)
        self.main_win.chat_page.clear()
        welcome_msg = f"您好，我是您的 AI 业务助理。当前已锁定客户【{customer_data.get('customer_name')}】，请问关于这位客户有什么可以帮您？"
        self.main_win.chat_page.add_message(welcome_msg, False)

    @asyncSlot()
    async def _handle_ai_chat_sent(self, text):
        """处理来自 UI 的 AI 发送请求"""
        if not hasattr(self, "_current_customer") or not self._current_customer:
            QMessageBox.warning(self.main_win, "未选中客户", "请先在左侧选择一个客户再进行对话。")
            return

        # 1. UI 展示用户消息
        self.main_win.chat_page.add_message(text, True)
        
        # 2. 创建一个空的 AI 气泡用于流式接收
        ai_bubble = self.main_win.chat_page.add_message("", False)
        
        # 3. 准备 Dify 参数
        user_id = self.api.username if hasattr(self.api, "username") else "anonymous"
        conv_id = self._current_customer.get("dify_conversation_id")
        
        # 4. 执行流式迭代
        async for chunk in self.api.stream_dify_chat(text, user_id, conv_id):
            if chunk.startswith("[CONV_ID:"):
                # 侦测到 Dify 分配的新会话 ID
                new_id = chunk[9:-1]
                if new_id != conv_id:
                    print(f"检测到新会话 ID: {new_id}，正在同步至后端...")
                    self._current_customer["dify_conversation_id"] = new_id
                    # 异步静默回提，不阻塞 UI
                    asyncio.create_task(self.api.update_customer_relation(
                        self._current_customer["phone"], 
                        {"dify_conversation_id": new_id}
                    ))
            elif chunk.startswith("Error:"):
                ai_bubble.append_text(f"\n⚠️ {chunk}")
            else:
                # 核心文本逐字上屏
                ai_bubble.append_text(chunk)

    @asyncSlot()
    async def _handle_save_customer_relation(self, phone, update_data):
        """处理客户动态资料的全量保存提交 (扩充了单位类型等客观字段)"""
        resp = await self.api.update_customer_full_info(phone, update_data)
        if resp and resp.get("code") == 200:
            QMessageBox.information(self.main_win, "同步成功", "客户动态笔记已成功更新至云端。")
            # 重新拉取一次客户列表以刷新本地数据
            customers_resp = await self.api.get_my_customers()
            if customers_resp and customers_resp.get("code") == 200:
                self.main_win.update_customer_list(customers_resp.get("data", []))
        else:
            msg = resp.get("message", "未知错误") if resp else "服务器无响应"
            QMessageBox.warning(self.main_win, "同步失败", f"更新失败: {msg}")

    @asyncSlot()
    async def _handle_history_clicked(self, phone):
        """弹出历史订单对话框"""
        resp = await self.api.get_customer_orders(phone)
        if resp and resp.get("code") == 200:
            orders = resp.get("data", [])
            from PySide6.QtWidgets import QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView
            from PySide6.QtCore import Qt
            
            dlg = QDialog(self.main_win)
            dlg.setWindowTitle("历史关联订单流水")
            dlg.resize(680, 400)
            layout = QVBoxLayout(dlg)
            
            table = QTableWidget(len(orders), 5)
            table.setHorizontalHeaderLabels(["订单日期", "订单编号", "摘要", "实收金额", "状态"])
            
            # 列宽分配
            header = table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
            
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            
            for row, order in enumerate(orders):
                table.setItem(row, 0, QTableWidgetItem(str(order.get("order_time", ""))))
                table.setItem(row, 1, QTableWidgetItem(str(order.get("dddh", ""))))
                table.setItem(row, 2, QTableWidgetItem(str(order.get("product_title", ""))))
                table.setItem(row, 3, QTableWidgetItem(f"¥{order.get('pay_amount', 0)}"))
                table.setItem(row, 4, QTableWidgetItem(str(order.get("status_name", ""))))
            
            layout.addWidget(table)
            dlg.exec()
        else:
            QMessageBox.warning(self.main_win, "提示", "获取历史订单失败或该用户无数据！")

    def _on_login_dialog_finished(self, result_code):
        """当对话框关闭时，通知 launch 协程继续执行"""
        if not self._login_future.done():
            self._login_future.set_result(result_code)

    @asyncSlot()
    async def _handle_login(self, u, p):
        """处理来自 UI 的登录请求信号"""
        # 登录过程中禁用按钮，防止重复提交
        self.login_dlg.login_btn.setEnabled(False)
        self.login_dlg.login_btn.setText("验证中...")
        
        success, msg = await self.api.login(u, p)
        
        if success:
            self.login_dlg.accept() # 这会触发 finished 信号
        else:
            QMessageBox.warning(self.login_dlg, "登录识别失败", msg)
            self.login_dlg.login_btn.setEnabled(True)
            self.login_dlg.login_btn.setText("立即验证并登录")

    async def perform_search(self, keyword, skip, limit):
        """核心业务：执行搜索并驱动 UI 更新"""
        # 0. 伴随搜索动作同步探测一次云端新鲜度，确保 UI 状态的一致性
        asyncio.create_task(self._refresh_sync_status())
        
        response_json = await self.api.search_products(keyword, skip, limit)
        if response_json and response_json.get("code") == 200:
            payload = response_json.get("data", {})
            items = payload.get("items", [])
            
            # 如果是第一页，清空列表防止重复
            if skip == 0:
                self.main_win.product_list.clear()

            for item_data in items:
                card_widget = self.main_win.add_product_card(item_data)
                # 后台异步并发下载图片，不阻塞渲染
                asyncio.create_task(self._async_load_image(card_widget, item_data.get("cover_img")))
            
            # 更新“加载更多”按钮的可见性
            self.main_win.update_has_more(payload.get("has_more", False))

    @asyncSlot()
    async def _handle_search(self, keyword, skip, limit):
        """处理来自 UI 的搜索/翻页信号"""
        await self.perform_search(keyword, skip, limit)

    async def _handle_sync_trigger(self):
        """手动触发后端 832 抓取任务 (仅限 Admin)"""
        self.main_win.btn_sync_now.setEnabled(False)
        self.main_win.sync_status_lbl.setText("正在向云端下达同步指令...")
        
        resp = await self.api.trigger_sync_task()
        if resp and resp.get("code") == 200:
            self.main_win.sync_status_lbl.setText("指令已送达，云端同步队列排队中...")
            # 延时 3 秒后尝试刷新一次状态
            await asyncio.sleep(3)
            await self._refresh_sync_status()
        else:
            msg = resp.get("msg", "无法拉起任务") if resp else "后端无响应"
            QMessageBox.warning(self.main_win, "同步失败", f"无法触发云端同步: {msg}")
            self.main_win.btn_sync_now.setEnabled(True)

    async def _refresh_sync_status(self):
        """拉取后端同步状态并渲染 UI 警告色"""
        status_data = await self.api.get_sync_status()
        if not status_data:
            if self.main_win:
                self.main_win.sync_status_lbl.setText("无法获取同步状态")
                self.main_win.sync_status_lbl.setStyleSheet("font-size: 11px; color: #bfbfbf;")
            return

        status = status_data.get("status", "idle")
        last_success = status_data.get("last_success", "从未同步")
        message = status_data.get("message", "")

        # 1. 颜色与文字判别
        color = "#8c8c8c" # 默认中性灰
        status_text = f"云端货源更新于: {last_success}"
        
        if status == "running":
            color = "#1890ff"
            status_text = "云端货源正在同步中 (10% - 90%)..."
        elif status == "error":
            color = "#ff4d4f"
            status_text = f"同步异常: {message[:15]}..."
        
        # 2. 超时检测：如果超过 24 小时没更新，视为潜在风险
        try:
            from datetime import datetime
            if last_success != "从未同步":
                # 后端返回格式通常为 '2026-04-02 12:00:00'
                last_dt = datetime.strptime(last_success, "%Y-%m-%d %H:%M:%S")
                delta_hours = (datetime.now() - last_dt).total_seconds() / 3600
                if delta_hours > 24:
                    color = "#faad14" # 警告橙
                    status_text += " (同步已逾 24 小时)"
        except: pass

        if self.main_win:
            self.main_win.sync_status_lbl.setText(status_text)
            self.main_win.sync_status_lbl.setStyleSheet(f"font-size: 11px; color: {color};")
            self.main_win.btn_sync_now.setEnabled(status != "running")

    async def _async_load_image(self, card_widget, relative_url):
        """三级缓存图片加载策略：L1(内存) -> L2(SQLite) -> L3(网络)"""
        if not relative_url:
            return
            
        # 1. 检查 L1 内存缓存 (瞬时响应)
        if relative_url in self._pixmap_cache:
            card_widget.update_image(self._pixmap_cache[relative_url])
            return

        # 2. 检查 L2 磁盘持久化缓存 (SQLite)
        cache_key = self.api._generate_cache_key("img", path=relative_url)
        if self.api.storage:
            cached_blob = self.api.storage.load_data(cache_key)
            if cached_blob:
                pixmap = QPixmap()
                if pixmap.loadFromData(cached_blob):
                    # 存入 L1 方便下次使用
                    self._pixmap_cache[relative_url] = pixmap
                    card_widget.update_image(pixmap)
                    return

        # 3. 发起 L3 网络请求 (复用持久会话)
        if not self._http_session:
            return
            
        full_url = f"{self.api.base_url}{relative_url}"
        try:
            resp = await self._http_session.get(full_url)
            if resp.status_code == 200:
                # 写入 L2 磁盘
                if self.api.storage:
                    self.api.storage.save_data(cache_key, resp.content)
                
                pixmap = QPixmap()
                if pixmap.loadFromData(resp.content):
                    # 写入 L1 内存
                    self._pixmap_cache[relative_url] = pixmap
                    card_widget.update_image(pixmap)
        except Exception:
            pass

if __name__ == "__main__":
    # 初始化 Qt 程序
    qt_app = QApplication(sys.argv)
    
    # 【核心修复 2】防止登录窗口关闭导致整个程序被 OS 给掐死
    # 默认情况下，Qt 发现最后一个窗口（登录框）关闭时会直接退出，
    # 导致主窗口还没来得及 show() 程序就没了。
    qt_app.setQuitOnLastWindowClosed(False)
    
    # 将 asyncio 循环与 Qt 循环融合
    event_loop = QEventLoop(qt_app)
    asyncio.set_event_loop(event_loop)
    
    desktop_app = DesktopApp()
    
    with event_loop:
        # 在入口处主动创建第一个 launch 任务
        # 使用 event_loop 实例直接创建任务，避免 asyncio 的运行时检查报错
        event_loop.create_task(desktop_app.launch())
        event_loop.run_forever()
