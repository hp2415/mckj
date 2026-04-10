import os
import sys
import asyncio
import httpx
# 确保在 import qasync 时，环境已经被净化
from qasync import QEventLoop, asyncSlot
from collections import OrderedDict
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt

# 强制指定 Qt API，防止 qasync 寻找残留的 PyQt5
os.environ['QT_API'] = 'pyside6'

# 本地模块导入
from api_client import APIClient
from ui.login_dialog import LoginDialog
from ui.main_window import MainWindow
from logger_cfg import logger
import logging

class InterceptHandler(logging.Handler):
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

# 屏蔽第三方库的冗余 DEBUG 噪音，只保留业务关键 INFO 指令
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)

def global_exception_handler(exctype, value, traceback):
    """捕捉并记录所有未捕毁的 GUI 线程异常"""
    logger.opt(exception=(exctype, value, traceback)).error("检测到未处理的全局异常 (GUI Thread)")
    sys.__excepthook__(exctype, value, traceback)

sys.excepthook = global_exception_handler

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
        # L1 内存缓存：url -> QPixmap，采用 OrderedDict 实现 LRU 淘汰
        self._pixmap_cache = OrderedDict()  
        self.MAX_PIXMAP_COUNT = 150  # 内存中最多保留 150 张图片
        self._load_stylesheet()

    def _load_stylesheet(self):
        """记录并加载全局 QSS 样式表"""
        import os
        qss_path = os.path.join(os.path.dirname(__file__), "ui", "style.qss")
        if os.path.exists(qss_path):
            with open(qss_path, "r", encoding="utf-8") as f:
                QApplication.instance().setStyleSheet(f.read())
            logger.info("已成功加载外部 QSS 样式表")
        else:
            logger.warning("未找到外部 QSS 样式表，将使用默认内联样式")

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
            self.main_win.upload_wechat_clicked.connect(self._handle_upload_wechat)
            
            # 5.1 AI 聊天信号连接
            self.main_win.chat_page.send_requested.connect(self._handle_ai_chat_sent)
            self.main_win.chat_page.copy_event_triggered.connect(self._handle_ai_copy)
            self.main_win.chat_page.feedback_requested.connect(self._handle_ai_feedback)
            self.main_win.chat_page.regenerate_requested.connect(self._handle_ai_regenerate)
            
            # 5.2 商品同步信号连接 (NEW) - 使用 lambda 适配纯协程
            self.main_win.sync_triggered.connect(lambda: asyncio.create_task(self._handle_sync_trigger()))
            self.main_win.btn_prod.clicked.connect(lambda: asyncio.create_task(self._refresh_sync_status()))
            
            self.main_win.show()
            
            # 6. 根据角色权限展示同步按钮
            user_role = self.api.user_data.get("role", "staff")
            if user_role == "admin":
                self.main_win.btn_sync_now.show()
            
            # 主窗口显示后，恢复“最后一个窗口关闭即退出”的行为，
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
    async def _handle_upload_wechat(self):
        """打开文件选择器，上传微信记录文件"""
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        import os
        
        filepath, _ = QFileDialog.getOpenFileName(
            self.main_win,
            "选择微信历史流水表格",
            "",
            "表格文件 (*.xlsx *.csv)"
        )
        if not filepath:
            return
            
        self.main_win.btn_import_wechat.setEnabled(False)
        self.main_win.btn_import_wechat.setText("正在上传并深度解析中...")
        
        try:
            resp = await self.api.upload_wechat_history(filepath)
            if resp and resp.get("code") == 200:
                QMessageBox.information(self.main_win, "上传成功", resp.get("message", "解析成功"))
            else:
                msg = resp.get("message") if resp else "网络传输失败"
                if not msg and resp: msg = resp.get("msg", "未知网络错误")
                QMessageBox.warning(self.main_win, "上传受阻", f"处理失败: {msg}")
        except Exception as e:
            QMessageBox.critical(self.main_win, "未期错误", f"发生了未知错误: {str(e)}")
        finally:
            self.main_win.btn_import_wechat.setEnabled(True)
            self.main_win.btn_import_wechat.setText("导入微信聊天记录")

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
        
        # 加载云端历史记录 (NEW)
        history = await self.api.get_chat_history(customer_data.get("phone"))
        if history:
            for msg in history:
                role = msg.get("role")
                content = msg.get("content")
                msg_id = msg.get("id")
                rating = msg.get("rating", 0)
                self.main_win.chat_page.add_message(content, is_user=(role == "user"), msg_id=msg_id, rating=rating)
        
        welcome_msg = f"您好，我是您的 AI 业务助理。当前已锁定客户【{customer_data.get('customer_name')}】，请问关于这位客户有什么可以帮您？"
        if not history:
            self.main_win.chat_page.add_message(welcome_msg, False)

    @asyncSlot()
    async def _handle_ai_copy(self, msg_id):
        """处理来自气泡的复制上报信号 (采纳统计)"""
        logger.info(f"监测到 AI 回复采纳行为 (复制): MsgID={msg_id}")
        await self.api.record_message_copy(msg_id)

    @asyncSlot()
    async def _handle_ai_feedback(self, msg_id, rating):
        """处理来自气泡的评价信号"""
        logger.info(f"提交消息评价: ID={msg_id}, Rating={rating}")
        await self.api.set_message_feedback(msg_id, rating)

    @asyncSlot()
    async def _handle_ai_regenerate(self):
        """处理重新生成请求"""
        if not hasattr(self, "_last_user_query") or not self._last_user_query:
            return
            
        # 1. 界面清理：删除最后一条消息 (通常是 AI 的那条消息)
        chat_layout = self.main_win.chat_page.chat_layout
        if chat_layout.count() > 1:
            item = chat_layout.takeAt(chat_layout.count() - 2)
            if item.widget():
                item.widget().deleteLater()
        
        # 2. 重新触发发送 (带入 is_regenerated 标记提升数据颗粒度)
        logger.info(f"重新生成 AI 回复，原问题: {self._last_user_query}")
        await self._handle_ai_chat_sent(self._last_user_query, is_regen=True)

    @asyncSlot()
    async def _handle_ai_chat_sent(self, text, is_regen=False):
        """处理来自 UI 的 AI 发送请求"""
        self._last_user_query = text # 记录用于重发
        
        if not hasattr(self, "_current_customer") or not self._current_customer:
            QMessageBox.warning(self.main_win, "未选中客户", "请先在左侧选择一个客户再进行对话。")
            return

        # 1. UI 展示用户消息 (重发时不重复展示用户消息)
        if not is_regen:
            self.main_win.chat_page.add_message(text, True)
        
        # 2. 创建一个空的 AI 气泡用于流式接收
        ai_bubble = self.main_win.chat_page.add_message("", False)
        
        # 3. 准备 Dify 参数
        user_id = self.api.username if hasattr(self.api, "username") else "anonymous"
        conv_id = self._current_customer.get("dify_conversation_id")
        phone = self._current_customer.get("phone")
        
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
        
        # 3.2 预落盘：保存用户发送的消息 (如果是重发则标记)
        asyncio.create_task(self.api.save_chat_message(phone, "user", text, conv_id))
        
        # 4. 执行流式迭代
        full_answer = ""
        try:
            async for chunk in self.api.stream_dify_chat(text, user_id, conv_id):
                if chunk.startswith("[CONV_ID:"):
                    new_id = chunk[9:-1]
                    if new_id != conv_id:
                        self._current_customer["dify_conversation_id"] = new_id
                        asyncio.create_task(self.api.update_customer_relation(phone, {"dify_conversation_id": new_id}))
                        conv_id = new_id
                elif chunk.startswith("Error:"):
                    ai_bubble.append_text(f"\n⚠️ {chunk}")
                else:
                    ai_bubble.append_text(chunk)
                    full_answer += chunk
        except Exception as e:
            ai_bubble.append_text(f"\n⚠️ 连接异常: {str(e)}")
        
        # 5. 后落盘：保存 AI 回复的消息 (如果是重发则标记)
        if full_answer:
            save_resp = await self.api.save_chat_message(
                phone, "assistant", full_answer, conv_id, is_regen=is_regen
            )
            # 5.1 将生成的 ID 回填给气泡，供评价使用
            if save_resp and save_resp.get("code") == 200:
                msg_id = save_resp.get("data", {}).get("id")
                if msg_id:
                    ai_bubble.msg_id = msg_id
                    print(f"AI 回复已落盘，ID: {msg_id}")

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
    async def _handle_history_clicked(self, customer_id):
        """弹出历史订单对话框 (基于 ID 穿透)"""
        resp = await self.api.get_customer_orders(customer_id)
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
                # 连接原图复制信号
                card_widget.full_copy_requested.connect(self._handle_full_copy_image)
                # 后台异步并发下载图片
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
        try:
            status_data = await self.api.get_sync_status()
            if not status_data:
                raise Exception("Empty response from server")
            
            status = status_data.get("status", "idle")
            last_success = status_data.get("last_success", "从未同步")
            message = status_data.get("message", "")

            # 1. 颜色与文字判别
            color = "#8c8c8c" # 默认中性灰
            status_text = f"云端货源更新于: {last_success}"
            
            if status == "running":
                color = "#1890ff"
                status_text = "云端货源正在同步中..."
            elif status == "error":
                color = "#ff4d4f"
                status_text = f"同步异常: {message[:15]}..."
            
            # 2. 超时检测：如果超过 24 小时没更新，视为潜在风险
            try:
                from datetime import datetime
                if last_success != "从未同步":
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
        except Exception as e:
            # 处理云端离线状态
            if self.main_win:
                self.main_win.sync_status_lbl.setText("● 云端连接失败 (离线)")
                self.main_win.sync_status_lbl.setStyleSheet("font-size: 11px; color: #ff4d4f; font-weight: bold;")
                self.main_win.btn_sync_now.setEnabled(False)
            logger.error(f"刷新同步状态失败: {str(e)}")

    async def _async_load_image(self, card_widget, relative_url):
        """三级缓存图片加载策略：L1(内存LRU) -> L2(SQLite) -> L3(网络)"""
        if not relative_url:
            return
            
        # 1. 检查 L1 内存缓存 (命中则提升至最新)
        if relative_url in self._pixmap_cache:
            self._pixmap_cache.move_to_end(relative_url)
            card_widget.update_image(self._pixmap_cache[relative_url])
            return

        pixmap = None

        # 2. 检查 L2 磁盘持久化缓存 (SQLite)
        cache_key = self.api._generate_cache_key("img", path=relative_url)
        if self.api.storage:
            cached_blob = self.api.storage.load_data(cache_key)
            if cached_blob:
                pixmap = QPixmap()
                if not pixmap.loadFromData(cached_blob):
                    pixmap = None

        # 3. 发起 L3 网络请求 (复用持久会话)
        if not pixmap and self._http_session:
            full_url = f"{self.api.base_url}{relative_url}"
            try:
                resp = await self._http_session.get(full_url)
                if resp.status_code == 200:
                    if self.api.storage:
                        self.api.storage.save_data(cache_key, resp.content)
                    pixmap = QPixmap()
                    if not pixmap.loadFromData(resp.content):
                        pixmap = None
            except Exception:
                pass

        # 4. 后处理：缩放并压入 L1 缓存
        if pixmap and not pixmap.isNull():
            # 预缩放优化：由于 UI 上显示尺寸为 110x120，我们按 2 倍图存储(220x240)
            # 这样既能保证高分屏清晰度，又能比原始大图节省 80% 以上内存
            scaled_pixmap = pixmap.scaled(
                220, 240, 
                Qt.KeepAspectRatio, 
                Qt.SmoothTransformation
            )
            
            # 维护 LRU 队列上限
            self._pixmap_cache[relative_url] = scaled_pixmap
            self._pixmap_cache.move_to_end(relative_url)
            if len(self._pixmap_cache) > self.MAX_PIXMAP_COUNT:
                self._pixmap_cache.popitem(last=False) # 弹出最旧的
            
            card_widget.update_image(scaled_pixmap)

    def _handle_full_copy_image(self, relative_url):
        """处理高清原图复制请求：从 L2 (SQLite) 提取原始二进制数据"""
        if not relative_url: return
        
        cache_key = self.api._generate_cache_key("img", path=relative_url)
        if self.api.storage:
            # 1. 从 L2 磁盘缓存持久层直接读取原始字节
            raw_blob = self.api.storage.load_data(cache_key)
            if raw_blob:
                pixmap = QPixmap()
                # 2. 转化为 QPixmap 并塞入剪贴板 (此时为高清原图)
                if pixmap.loadFromData(raw_blob):
                    QApplication.clipboard().setPixmap(pixmap)
                    logger.info(f"高清原图已复制至剪贴板: {relative_url}")
                    return
        
        # 兜底方案：如果 L2 还没下载完，尝试从 L1 内存缓存(缩放版)先复制一个
        if relative_url in self._pixmap_cache:
            QApplication.clipboard().setPixmap(self._pixmap_cache[relative_url])

if __name__ == "__main__":
    # 初始化 Qt 程序
    qt_app = QApplication(sys.argv)
    
    qt_app.setQuitOnLastWindowClosed(False)
    
    # 将 asyncio 循环与 Qt 循环融合
    event_loop = QEventLoop(qt_app)
    asyncio.set_event_loop(event_loop)

    def handle_async_exception(loop, context):
        """捕捉并记录所有未捕毁的 Asyncio 异步任务异常"""
        msg = context.get("exception", context["message"])
        logger.error(f"捕捉到未处理的异步任务异常: {msg}")
        if "exception" in context:
            logger.opt(exception=context["exception"]).error("详细堆栈如下:")

    event_loop.set_exception_handler(handle_async_exception)
    
    desktop_app = DesktopApp()
    
    with event_loop:
        # 在入口处主动创建第一个 launch 任务
        # 使用 event_loop 实例直接创建任务，避免 asyncio 的运行时检查报错
        event_loop.create_task(desktop_app.launch())
        event_loop.run_forever()
