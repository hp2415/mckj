import os
import sys
import asyncio
import httpx
# 确保在 import qasync 时，环境已经被净化
from qasync import QEventLoop, asyncSlot
from collections import OrderedDict
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QPixmap, QColor, QIcon
from PySide6.QtCore import Qt, QSettings

# QFluentWidgets 主题
from qfluentwidgets import (
    setTheme, setThemeColor, Theme, 
    InfoBar, InfoBarPosition
)

# 强制指定 Qt API，防止 qasync 寻找残留的 PyQt5
os.environ['QT_API'] = 'pyside6'

# 本地模块导入
from api_client import APIClient
from ui.login_dialog import LoginDialog
from ui.register_dialog import RegisterDialog
from ui.main_window import MainWindow
from image_manager import ImageManager
from chat_handler import ChatHandler
from logger_cfg import logger
from config_loader import cfg
from updater import enforce_latest_or_exit
import logging
import ctypes

# 强制为 Windows 进程设置 AppUserModelID，否则任务栏无法正确显示自定义图标
try:
    myappid = 'com.wechataiai.assistant.v1' 
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
except Exception:
    pass

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
    """捕捉并记录所有未捕毁的 GUI 线程异常，并自动退出防止卡死"""
    logger.opt(exception=(exctype, value, traceback)).error("检测到未处理的全局异常 (GUI Thread)")
    if QApplication.instance():
        QApplication.instance().exit(1)
    sys.exit(1)

sys.excepthook = global_exception_handler

class DesktopApp:
    """
    整合 UI 界面与异步通讯总线。
    负责控制窗口跳转、异步信号处理以及数据同步流。
    """
    def __init__(self):
        # ── Fluent 主题初始化 ──
        theme = Theme.DARK if cfg.theme_mode == "dark" else Theme.LIGHT
        setTheme(theme)
        
        setThemeColor(QColor("#07c160"))          # 微信绿作为全局主题色

        # 配置加载与配套图标设置
        self._is_logging_in = False
        self.api = APIClient(cfg.api_url)
        
        # 使用全局 QApplication 设置图标，确保所有窗口共享
        icon_path = os.path.join(os.path.dirname(__file__), "assets", "mibuddy.png")
        if os.path.exists(icon_path):
            QApplication.setWindowIcon(QIcon(icon_path))

        self.login_dlg = None
        self.main_win = None
        self.image_manager = ImageManager(self.api)
        self.chat_handler = ChatHandler(self, self.api)
        self._is_handling_expiry = False # 标记是否正在处理会话过期，防止重复弹窗
        self._current_customer = None  # 登录后、首次选中客户前，设置页刷新等逻辑会读到
        self._chat_surface_mode = "customer"  # staff=自由对话；与 MainWindow._chat_surface_mode 同步
        self._ai_scenarios_free: list = []
        self._ai_scenarios_customer: list = []
        # self._load_legacy_qss() # Phase 6: 彻底脱离 QSS 硬编码

    # def _load_legacy_qss(self):
    #     """兼容性加载：迁移期间保留 QSS 作为补丁，迁移完成后删除。"""
    #     qss_path = os.path.join(os.path.dirname(__file__), "ui", "style.qss")
    #     if os.path.exists(qss_path):
    #         with open(qss_path, "r", encoding="utf-8") as f:
    #             QApplication.instance().setStyleSheet(f.read())
    #         logger.info("已加载兼容 QSS（迁移期过渡）")
    #     else:
    #         logger.info("QSS 文件不存在，完全由 Fluent 主题接管")

    async def launch(self):
        """进入程序生命周期"""
        logger.info("====== 微企 AI 桌面端助理启动 ======")
            
        self.login_dlg = LoginDialog()
        self.login_dlg.login_requested.connect(self._handle_login)
        self.login_dlg.open_register_requested.connect(self._open_register_dialog)
        # 万向监听：令牌过期自动重定向
        self.api.unauthorized.connect(self._handle_unauthorized)
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
            self.main_win.order_history_requested.connect(self._handle_history_clicked)
            self.main_win.logout_btn.clicked.connect(self._handle_logout)
            self.main_win.filter_requested.connect(self._handle_filter_search)
            self.main_win.shop_metadata_refresh_requested.connect(self._on_shop_metadata_refresh)
            
            # 5.1 AI 聊天信号路由 -> ChatHandler (利用 asyncSlot 或 lambda 自动桥接异步协程)
            def route_send(text): asyncio.create_task(self.chat_handler.handle_ai_chat_sent(text))
            def route_copy(msg_id): asyncio.create_task(self.chat_handler.handle_ai_copy(msg_id))
            def route_feedback(msg_id, rt): asyncio.create_task(self.chat_handler.handle_ai_feedback(msg_id, rt))
            def route_regen(query): asyncio.create_task(self.chat_handler.handle_ai_regenerate(query))
            
            self.main_win.chat_page.send_requested.connect(route_send)
            self.main_win.chat_page.copy_event_triggered.connect(route_copy)
            self.main_win.chat_page.feedback_requested.connect(route_feedback)
            self.main_win.chat_page.regenerate_requested.connect(route_regen)
            self.main_win.chat_page.history_requested.connect(lambda: asyncio.create_task(self._handle_history_requested()))
            self.main_win.chat_page.scroll_area.verticalScrollBar().valueChanged.connect(self._on_chat_scroll_changed)
            # 与 sales_binding_* 相同：@asyncSlot 由 qasync 调度，勿再 create_task
            self.main_win.chat_surface_mode_changed.connect(self._on_chat_surface_mode_changed)
            
            # 5.2 商品同步与本地数据刷新 (NEW)
            self.main_win.sync_triggered.connect(lambda: asyncio.create_task(self._handle_sync_trigger()))
            self.main_win.ui_data_refresh_requested.connect(lambda: asyncio.create_task(self._handle_ui_data_refresh()))
            # asyncSlot 包装后由 qasync 调度协程，不可再包一层 create_task（会传入 Task 导致 TypeError）
            self.main_win.sales_bindings_refresh_requested.connect(self._refresh_sales_bindings)
            self.main_win.sales_binding_add_requested.connect(self._add_sales_binding)
            self.main_win.sales_binding_delete_requested.connect(self._delete_sales_binding)
            self.main_win.sales_binding_primary_requested.connect(self._primary_sales_binding)
            self.main_win.manual_import_requested.connect(self._handle_manual_import)
            self.main_win.clear_manual_requested.connect(self._handle_clear_manual)
            
            # 使用标签切换信号检测进入“商品”页 (Index 2)
            def on_tab_changed(index):
                if index == 2:
                    asyncio.create_task(self._refresh_sync_status())
            self.main_win.tab_changed.connect(on_tab_changed)
            
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
            asyncio.create_task(self._fetch_product_metadata())
        else:
            # 登录界面被手动关闭
            asyncio.create_task(self.image_manager.close())
            QApplication.quit()

    async def _initial_data_fetch(self):
        """首屏数据并行拉取逻辑"""
        # 1. 加载客户列表
        customers_resp = await self.api.get_my_customers()
        if customers_resp and customers_resp.get("code") == 200:
            self.main_win.update_customer_list(customers_resp.get("data", []))

        # 2. 拉取系统字典配置（模型列表/字典下拉项等）
        configs_dict = await self.api.get_configs_dict()
        if configs_dict:
            self.main_win.info_page.populate_combo_boxes(configs_dict)
            models = configs_dict.get("llm_chat_models")
            if models:
                self.main_win.chat_page.set_chat_model_options(models)
            # 后端下发：桌面端默认选中模型（仅在本机未固定偏好时生效）
            default_models = configs_dict.get("desktop_default_chat_models")
            if default_models and hasattr(self.main_win.chat_page, "apply_server_default_chat_models"):
                self.main_win.chat_page.apply_server_default_chat_models(default_models)

        tag_resp = await self.api.get_profile_tag_options()
        if tag_resp and tag_resp.get("code") == 200:
            self.main_win.info_page.set_profile_tag_catalog(tag_resp.get("data") or [])
            if self._current_customer:
                self.main_win.info_page.set_customer(self._current_customer)

        # 2.1 拉取可选场景列表（按界面分类：自由对话 / 客户对话；画像等为 backend_only 不在此返回）
        free_resp = await self.api.get_ai_scenarios("free")
        cust_resp = await self.api.get_ai_scenarios("customer")
        self._ai_scenarios_free = (free_resp or {}).get("data") or []
        self._ai_scenarios_customer = (cust_resp or {}).get("data") or []
        if not self._ai_scenarios_free:
            self._ai_scenarios_free = [
                {
                    "scenario_key": "staff_assistant",
                    "name": "内部问答",
                    "tools_enabled": True,
                    "ui_category": "free_chat",
                }
            ]
        if not self._ai_scenarios_customer:
            self._ai_scenarios_customer = [
                {
                    "scenario_key": "general_chat",
                    "name": "客户沟通",
                    "tools_enabled": True,
                    "ui_category": "customer_chat",
                },
                {
                    "scenario_key": "product_recommend",
                    "name": "推品报价",
                    "tools_enabled": True,
                    "ui_category": "customer_chat",
                },
            ]
        if self._chat_surface_mode == "staff":
            self.main_win.chat_page.set_scenario_options(self._ai_scenarios_free)
            self.main_win.chat_page.set_history_button_visible(False)
        else:
            self.main_win.chat_page.set_scenario_options(self._ai_scenarios_customer)
            self.main_win.chat_page.set_history_button_visible(True)

        await self._refresh_sync_status()

        # 3. 默认加载 AI 对话页
        self.main_win.stack.setCurrentIndex(0)
        await self.perform_search("", 0, 20)

    @asyncSlot()
    async def _handle_unauthorized(self):
        """处理令牌过期的全局槽函数"""
        if self._is_handling_expiry:
            return
        self._is_handling_expiry = True
        
        logger.warning("正在处理全局会话过期跳转...")
        
        if self.main_win:
            # 在主窗口显示显眼的警告
            self.main_win.show_info_bar(
                "warning", 
                "登录状态已过期", 
                "您的登录信息已过期，系统将在 2 秒后带您回到登录界面...",
                duration=4000
            )
        
        # 留出 2 秒让用户看清提示
        await asyncio.sleep(2.0)
        
        # 执行注销与重启流程
        await self._handle_logout()
        self._is_handling_expiry = False

    @asyncSlot()
    async def _handle_logout(self):
        """注销重启：临时接管退出信号，防止主窗口关闭导致进程被杀"""
        if self.main_win:
            # 临时关闭自动退出，确保接下来的 close() 不会干掉整个进程
            QApplication.setQuitOnLastWindowClosed(False)
            self.main_win.close()
            self.main_win = None
        self._current_customer = None

        # 清理资源统领器
        await self.image_manager.close()

        self.api.logout()
        # 重启登录流程
        self._restart_task = asyncio.create_task(self.launch())


    @asyncSlot()
    async def _on_chat_surface_mode_changed(self, mode: str):
        """全局导航：自由对话 ↔ 客户对话；切换场景列表与欢迎语。"""
        self.chat_handler.cancel_current_task()
        self._chat_surface_mode = mode
        staff = mode == "staff"
        self.main_win.chat_page.set_scenario_options(
            self._ai_scenarios_free if staff else self._ai_scenarios_customer
        )
        self.main_win.chat_page.set_history_button_visible(not staff)
        self._chat_history_skip = 0
        self._has_more_history = True
        self._is_loading_history = False
        self._history_mode_enabled = False
        self.main_win.chat_page.clear()
        if staff:
            self.main_win.apply_staff_chat_header()
            welcome = (
                "您好，这是内部问答模式（未绑定客户）。可询问产品知识、平台规则与话术思路；"
                "需要某位客户的档案、订单或微信摘要时，请点击「客户对话」并在左侧选择客户。"
            )
        else:
            if self._current_customer:
                self.main_win.apply_customer_header(self._current_customer)
                name = self._current_customer.get("customer_name")
                welcome = (
                    f"您好，我是您的 AI 业务助理。当前已锁定客户【{name}】，"
                    "请问关于这位客户有什么可以帮您？"
                )
            else:
                self.main_win.apply_customer_header_placeholder()
                welcome = "请在左侧选择一位客户，或点击机器人图标进入「自由对话」进行内部问答。"
        self.main_win.chat_page.add_message(welcome, False)
        self.main_win.chat_page.scroll_to_bottom(instant=True)

    @asyncSlot()
    async def _handle_customer_selected(self, customer_data):
        """当侧边栏选中某个客户时触发"""
        # 1. 如果还在由于上一位客户进行 AI 对话，先行取消，防止由于回包延迟导致的“消息穿越”
        self.chat_handler.cancel_current_task()
        
        self._current_customer = customer_data # 锁定当前业务上下文
        self._chat_history_skip = 0      # 聊天记录分页偏移量
        self._has_more_history = True    # 是否还有更多历史记录
        self._is_loading_history = False # 是否正在加载历史中
        self._history_mode_enabled = False # 当前客户是否已开启历史记录模式
        
        logger.info(f"已选中客户: {customer_data.get('customer_name')}, ConvID: {customer_data.get('dify_conversation_id')}")
        
        # 自动切换到资料页并填充表单 (通过新的整合函数触发正确的 UI 状态)
        self.main_win.switch_tab(1)
        self.main_win.info_page.set_customer(customer_data)
        
        # 准备 AI 对话页 (切换客户时清空历史，准备新上下文)
        self.main_win.chat_page.clear()
        self._chat_history_skip = 0
        self._has_more_history = True
        self._is_loading_history = False
        
        # [MODIFIED] 不再自动加载历史记录，等待用户点击“历史”按钮
        # 仅清除显示，显示初始化状态
        welcome_msg = f"您好，我是您的 AI 业务助理。当前已锁定客户【{customer_data.get('customer_name')}】，请问关于这位客户有什么可以帮您？"
        self.main_win.chat_page.add_message(welcome_msg, False)
        
        # 瞬间触底
        self.main_win.chat_page.scroll_to_bottom(instant=True)

    @asyncSlot()
    async def _handle_save_customer_relation(self, customer_id, lookup_phone, update_data):
        """处理客户动态资料的全量保存提交 (含姓名、手机号等客观字段)"""
        resp = await self.api.update_customer_full_info(
            customer_id, lookup_phone or None, update_data
        )
        if resp and resp.get("code") == 200:
            self.main_win.show_info_bar("success", "同步成功", "客户资料已成功更新至云端。")
            customers_resp = await self.api.get_my_customers()
            if customers_resp and customers_resp.get("code") == 200:
                data_list = customers_resp.get("data", [])
                self.main_win.update_customer_list(data_list)
                # 关键：同一客户可能被多个销售微信绑定，列表里会出现多条记录。
                # 仅按 id 回填可能跳到“另一条跟进线路”，因此优先用 (id, sales_wechat_id) 精准定位。
                target_id = customer_id
                target_sw = None
                try:
                    target_sw = (update_data or {}).get("sales_wechat_id")
                except Exception:
                    target_sw = None
                if target_sw is None and self._current_customer:
                    target_sw = self._current_customer.get("sales_wechat_id")

                refreshed = None
                if target_sw is not None:
                    refreshed = next(
                        (
                            c
                            for c in data_list
                            if str(c.get("id") or "") == str(target_id or "")
                            and str(c.get("sales_wechat_id") or "") == str(target_sw or "")
                        ),
                        None,
                    )
                if refreshed is None:
                    refreshed = next(
                        (c for c in data_list if str(c.get("id") or "") == str(target_id or "")),
                        None,
                    )
                if refreshed:
                    self._current_customer = refreshed
                    self.main_win.info_page.set_customer(refreshed)
                    self.main_win.apply_customer_header(refreshed)
        else:
            msg = resp.get("message", "未知错误") if resp else "服务器无响应"
            self.main_win.show_info_bar("warning", "同步失败", f"更新失败: {msg}")

    @asyncSlot()
    async def _handle_history_clicked(self, customer_id):
        """将历史订单流水渲染到侧边抽屉面板 (不再弹出对话框)"""
        cid = str(customer_id) if customer_id is not None else ""
        resp = await self.api.get_customer_orders(cid)
        try:
            code = resp.get("code") if isinstance(resp, dict) else None
            n = len(resp.get("data") or []) if isinstance(resp, dict) else None
            logger.info(f"订单明细响应: customer_id={cid} code={code} rows={n}")
        except Exception:
            pass
        if resp and resp.get("code") == 200:
            orders = resp.get("data", [])
            # 通知主窗口刷新表格
            self.main_win.update_order_table(orders)
        else:
            self.main_win.show_info_bar("warning", "查询失败", "未能获取到该客户的历史订单数据。")

    def _on_login_dialog_finished(self, result_code):
        """当对话框关闭时，通知 launch 协程继续执行"""
        if not self._login_future.done():
            self._login_future.set_result(result_code)

    @asyncSlot()
    async def _open_register_dialog(self):
        dlg = RegisterDialog(self.login_dlg)

        async def do_reg(u, p, rn, blob):
            lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
            ok, msg = await self.api.register_account(u, p, rn, lines)
            if ok:
                InfoBar.success(
                    title="注册成功",
                    content="请使用新账号登录",
                    duration=3500,
                    position=InfoBarPosition.TOP,
                    parent=dlg,
                )
                dlg.mark_success()
            else:
                InfoBar.warning(
                    title="注册失败",
                    content=str(msg),
                    duration=4500,
                    position=InfoBarPosition.TOP,
                    parent=dlg,
                )

        dlg.register_submitted.connect(
            lambda u, p, rn, b: asyncio.create_task(do_reg(u, p, rn, b))
        )
        dlg.show()

    async def _sync_customer_list_with_details(self):
        """重新拉取客户列表，并同步当前选中客户的详情/顶栏（销售号绑定变更后可见范围会变）。"""
        main_win = self.main_win
        if not main_win:
            return
        customers_resp = await self.api.get_my_customers()
        # 退出/切换账号期间 main_win 可能已被销毁
        if self.main_win is None or main_win is not self.main_win:
            return
        if not (customers_resp and customers_resp.get("code") == 200):
            return
        customers = customers_resp.get("data", [])
        if not self.main_win:
            return
        self.main_win.update_customer_list(customers)
        if not self._current_customer:
            return
        cid = self._current_customer.get("id")
        csw = self._current_customer.get("sales_wechat_id")
        phone = self._current_customer.get("phone")
        updated = None
        # 同一客户可能存在多条“销售微信绑定线路”，优先用 (id, sales_wechat_id) 精准回填
        if cid is not None and csw is not None:
            updated = next(
                (
                    c
                    for c in customers
                    if str(c.get("id") or "") == str(cid or "")
                    and str(c.get("sales_wechat_id") or "") == str(csw or "")
                ),
                None,
            )
        if updated is None and cid is not None:
            updated = next((c for c in customers if str(c.get("id") or "") == str(cid or "")), None)
        if updated is None and phone:
            updated = next((c for c in customers if c.get("phone") == phone), None)
        if updated:
            self._current_customer = updated
            self.main_win.info_page.set_customer(updated)
            self.main_win.apply_customer_header(updated)
        elif self._current_customer:
            # 绑定变更后原客户可能已不在可见列表中
            self._current_customer = None
            self.main_win.lbl_header_unit.setText("")
            self.main_win.lbl_header_info.setText("")
            self.main_win.phone_label.setText("请先选择左侧客户")

    @asyncSlot()
    async def _refresh_sales_bindings(self):
        main_win = self.main_win
        if not main_win:
            return
        rows = await self.api.list_sales_wechats()
        if self.main_win is None or main_win is not self.main_win:
            return
        self.main_win.update_sales_bindings_list(rows or [])
        await self._sync_customer_list_with_details()

    @asyncSlot(str)
    async def _add_sales_binding(self, sales_id: str):
        if not self.main_win:
            return
        resp = await self.api.add_sales_wechat_bind(sales_id.strip(), is_primary=False)
        if resp and resp.get("code") == 200:
            InfoBar.success(
                title="已添加",
                content="销售微信号已绑定",
                duration=2500,
                position=InfoBarPosition.TOP,
                parent=self.main_win,
            )
            await self._refresh_sales_bindings()
        else:
            r = resp or {}
            msg = r.get("message") or r.get("detail", "添加失败")
            if isinstance(msg, list):
                msg = "; ".join(str(x) for x in msg)
            InfoBar.warning(
                title="添加失败",
                content=str(msg),
                duration=4000,
                position=InfoBarPosition.TOP,
                parent=self.main_win,
            )

    @asyncSlot(int)
    async def _delete_sales_binding(self, binding_id: int):
        if not self.main_win:
            return
        ok = await self.api.delete_sales_wechat_bind(binding_id)
        if ok:
            InfoBar.success(
                title="已删除",
                content="绑定已移除",
                duration=2000,
                position=InfoBarPosition.TOP,
                parent=self.main_win,
            )
            await self._refresh_sales_bindings()
        else:
            InfoBar.warning(
                title="删除失败",
                content="请稍后重试",
                duration=3000,
                position=InfoBarPosition.TOP,
                parent=self.main_win,
            )

    @asyncSlot(int)
    async def _primary_sales_binding(self, binding_id: int):
        if not self.main_win:
            return
        resp = await self.api.set_primary_sales_wechat_bind(binding_id)
        if resp and resp.get("code") == 200:
            InfoBar.success(
                title="已更新",
                content="主号已切换",
                duration=2000,
                position=InfoBarPosition.TOP,
                parent=self.main_win,
            )
            await self._refresh_sales_bindings()
        else:
            InfoBar.warning(
                title="操作失败",
                content=(resp or {}).get("message", "请重试"),
                duration=3000,
                position=InfoBarPosition.TOP,
                parent=self.main_win,
            )

    @asyncSlot(str)
    async def _handle_manual_import(self, file_path: str):
        if not self.main_win:
            return
            
        self.main_win.show_info_bar("info", "处理中", "正在上传并解析名单，请稍候...", duration=2000)
        
        resp = await self.api.import_manual_followup(file_path)
        if resp and resp.get("code") == 200:
            msg = resp.get("message", "导入成功")
            InfoBar.success(
                title="导入完成",
                content=msg,
                duration=5000,
                position=InfoBarPosition.TOP,
                parent=self.main_win
            )
            # 刷新本地列表
            await self._sync_customer_list_with_details()
        else:
            msg = resp.get("message", "导入失败") if resp else "网络请求失败"
            InfoBar.error(
                title="导入失败",
                content=msg,
                duration=5000,
                position=InfoBarPosition.TOP,
                parent=self.main_win
            )

    @asyncSlot()
    async def _handle_clear_manual(self):
        """一键清空手动导入标签"""
        if not self.main_win:
            return
            
        resp = await self.api.clear_manual_followup()
        if resp and resp.get("code") == 200:
            InfoBar.success(
                title="清空成功",
                content=resp.get("message", "导入名单已移除"),
                duration=3000,
                position=InfoBarPosition.TOP,
                parent=self.main_win
            )
            # 刷新列表
            await self._sync_customer_list_with_details()
        else:
            msg = resp.get("message", "清空失败") if resp else "网络请求失败"
            InfoBar.error(
                title="清空失败",
                content=msg,
                duration=5000,
                position=InfoBarPosition.TOP,
                parent=self.main_win
            )

    @asyncSlot()
    async def _handle_login(self, u, p):
        """处理来自 UI 的登录请求信号"""
        if self._is_logging_in:
            return
            
        self._is_logging_in = True
        # 登录过程中禁用按钮，防止重复提交
        self.login_dlg.login_btn.setEnabled(False)
        self.login_dlg.login_btn.setText("验证中...")
        
        try:
            success, msg = await self.api.login(u, p)
            if success:
                self.login_dlg.accept() # 这会触发 finished 信号
            else:
                InfoBar.warning(
                    title="登录识别失败",
                    content=str(msg),
                    duration=3000,
                    position=InfoBarPosition.TOP,
                    parent=self.login_dlg
                )
                self.login_dlg.login_btn.setEnabled(True)
                self.login_dlg.login_btn.setText("立即验证并登录")
        finally:
            self._is_logging_in = False

    @asyncSlot(str)
    async def _on_shop_metadata_refresh(self, shop_name):
        """处理店铺切换导致的元数据联动刷新"""
        metadata = await self.api.get_product_metadata(shop_name)
        if metadata:
            self.main_win.filter_bar.set_metadata(
                suppliers=[], # 不更新店铺列表，防止死循环
                categories=metadata.get("categories", []),
                origins=metadata.get("origins", []),
                update_shop=False
            )

    @asyncSlot(dict, int, int)
    async def _handle_filter_search(self, filters, skip=0, limit=20):
        """统一的高阶过滤搜索处理器"""
        data = await self.api.search_products(
            keyword=filters.get("keyword", ""),
            supplier_name=filters.get("supplier_name", ""),
            cat1=filters.get("cat1", ""),
            cat2=filters.get("cat2", ""),
            cat3=filters.get("cat3", ""),
            province=filters.get("province", ""),
            city=filters.get("city", ""),
            district=filters.get("district", ""),
            min_price=filters.get("min_price"),
            max_price=filters.get("max_price"),
            skip=skip,
            limit=limit
        )
        if not data: return
        
        items = data.get("data", {}).get("items", [])
        total = data.get("data", {}).get("total", 0)
        has_more = data.get("data", {}).get("has_more", False)
        
        if skip == 0:
            self.main_win.product_list.clear()
            self.main_win._load_more_item = None

        for p in items:
            card = self.main_win.add_product_card(p)
            # 5.4 重构修复：恢复卡片内部交互信号连接
            card.full_copy_requested.connect(self.image_manager.handle_full_copy_image)
            # 5.4 修复方法名调用错误：由不存在的 load_product_image 修正为 async_load_image
            asyncio.create_task(self.image_manager.async_load_image(card, p.get("cover_img")))
        
        self.main_win.update_has_more(has_more)

    async def perform_search(self, keyword, skip, limit):
        """核心业务：执行搜索并驱动 UI 更新"""
        # 0. 伴随搜索动作同步探测一次云端新鲜度，确保 UI 状态的一致性
        asyncio.create_task(self._refresh_sync_status())
        
        # 融合当前 FilterBar 的状态进行过滤搜索
        filters = self.main_win._current_filters if hasattr(self.main_win, "_current_filters") else {}
        
        response_json = await self.api.search_products(
            keyword=keyword, 
            supplier_name=filters.get("supplier_name", ""),
            cat1=filters.get("cat1", ""),
            cat2=filters.get("cat2", ""),
            cat3=filters.get("cat3", ""),
            province=filters.get("province", ""),
            city=filters.get("city", ""),
            district=filters.get("district", ""),
            min_price=filters.get("min_price"),
            max_price=filters.get("max_price"),
            skip=skip, 
            limit=limit
        )
        if response_json and response_json.get("code") == 200:
            payload = response_json.get("data", {})
            items = payload.get("items", [])
            
            # 如果是第一页，清空列表防止重复
            if skip == 0:
                self.main_win.product_list.clear()

            for item_data in items:
                card_widget = self.main_win.add_product_card(item_data)
                # 连接原图复制信号 -> ImageManager
                card_widget.full_copy_requested.connect(self.image_manager.handle_full_copy_image)
                # 后台异步并发下载图片 -> ImageManager
                asyncio.create_task(self.image_manager.async_load_image(card_widget, item_data.get("cover_img")))
            
            # 更新“加载更多”按钮的可见性
            self.main_win.update_has_more(payload.get("has_more", False))

    async def _fetch_product_metadata(self):
        """拉取商品库的分类、厂家以及产地元数据"""
        meta = await self.api.get_product_metadata()
        if meta and self.main_win:
            self.main_win.filter_bar.set_metadata(
                suppliers=meta.get("suppliers", []),
                categories=meta.get("categories", []),
                origins=meta.get("origins", []),
                update_shop=True
            )

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
            # 3. 如果在商品页，也要重刷状态
            if self.main_win.center_stack.currentIndex() == 2:
                await self._refresh_sync_status()
                await self._fetch_product_metadata()
        else:
            msg = resp.get("msg", "无法拉起任务") if resp else "后端无响应"
            self.main_win.show_info_bar("error", "同步失败", f"无法触发云端同步: {msg}")
            self.main_win.btn_sync_now.setEnabled(True)

    async def _handle_ui_data_refresh(self):
        """轻量级刷新：仅重新拉取客户列表并更新当前详情页"""
        logger.info("正在执行本地数据刷新...")
        await self._sync_customer_list_with_details()

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
            except Exception as e:
                logger.warning(f"解析同步时间出错: {e}")

            if self.main_win:
                self.main_win.sync_status_lbl.setText(status_text)
                self.main_win.sync_status_lbl.setStyleSheet(f"color: {color};")
                self.main_win.btn_sync_now.setEnabled(status != "running")
        except Exception as e:
            # 处理云端离线状态
            if self.main_win:
                self.main_win.sync_status_lbl.setText("● 云端连接失败 (离线)")
                self.main_win.sync_status_lbl.setStyleSheet("color: #ff4d4f; font-weight: bold;")
                self.main_win.btn_sync_now.setEnabled(False)
            logger.error(f"刷新同步状态失败: {str(e)}")

    def _on_chat_scroll_changed(self, value):
        """处理聊天区域滚动条变化，实现上划自动加载更多"""
        if value == 0 and self._has_more_history and not self._is_loading_history and getattr(self, "_history_mode_enabled", False):
            if self._current_customer:
                asyncio.create_task(self._load_more_history())

    async def _handle_history_requested(self):
        """手动点击历史记录按钮"""
        if not self._current_customer:
            return
        
        # 标记当前客户已开启历史加载模式
        self._history_mode_enabled = True
        
        # 第一次点击：加载“最新 20 条”并展示；后续点击可按需扩展（目前不重复拉取）
        if self._chat_history_skip == 0:
            await self._load_latest_history_first_page()
            return

        if not self._has_more_history:
            self.main_win.show_info_bar("info", "提示", "已加载全部历史记录。")
            return
            
        # 已经进入历史模式时，再点一次仅提示（避免重复插入造成混乱）
        self.main_win.show_info_bar("info", "提示", "历史记录已显示，可继续上划加载更早记录。")

    async def _load_latest_history_first_page(self):
        """首次进入历史模式：拉取最新 20 条并直接展示在对话区。"""
        if not self._current_customer:
            return
        if self._is_loading_history:
            return

        self._is_loading_history = True
        try:
            cid = self._current_customer.get("id")
            phone = self._current_customer.get("phone")
            session_sw = self._current_customer.get("sales_wechat_id")
            if session_sw is not None:
                session_sw = str(session_sw).strip() or None
            limit = 20
            if cid:
                history = await self.api.get_chat_history_by_id(
                    cid, limit=limit, skip=0, sales_wechat_id=session_sw
                )
            else:
                history = await self.api.get_chat_history(
                    phone, limit=limit, skip=0, sales_wechat_id=session_sw
                )
            if not history:
                self._has_more_history = False
                self.main_win.show_info_bar("info", "提示", "暂无历史聊天记录。")
                return

            # 进入“历史显示模式”：清空当前显示，保证“最新 20 条”可见
            self.main_win.chat_page.clear()
            for msg in history:
                role = msg.get("role")
                content = msg.get("content")
                msg_id = msg.get("id")
                rating = msg.get("rating", 0)
                chat_model = (msg.get("chat_model") or "").strip()
                is_user = (role == "user")
                self.main_win.chat_page.add_message(
                    content,
                    is_user=is_user,
                    msg_id=msg_id,
                    rating=rating,
                    user_query="",
                    model_tag=chat_model if not is_user else "",
                )

            self._chat_history_skip = len(history)
            self._has_more_history = len(history) >= limit

            # 展示最新一页后吸底，用户一眼看到“最近对话”
            self.main_win.chat_page.scroll_to_bottom(instant=True)
            self.main_win.show_info_bar("success", "已显示历史记录", "已加载最新 20 条，上划可加载更早记录。")
        finally:
            self._is_loading_history = False

    async def _load_more_history(self, *, keep_viewport_position: bool = True, show_loaded_hint: bool = False):
        """拉取更多历史聊天记录 (分页)"""
        if self._is_loading_history or not self._has_more_history:
            return
            
        self._is_loading_history = True
        cid = self._current_customer.get("id")
        phone = self._current_customer.get("phone")
        session_sw = self._current_customer.get("sales_wechat_id")
        if session_sw is not None:
            session_sw = str(session_sw).strip() or None
        limit = 20
        
        # 标记进入批量加载状态 (防止自动触底)
        self.main_win.chat_page._is_batch_loading = True
        self.main_win.chat_page._is_prepending = True # 防止触发底层范围变化时的滚回底部逻辑
        
        try:
            # 记录当前滚动条位置和最大值，以便在插入后保持视觉平衡
            bar = self.main_win.chat_page.scroll_area.verticalScrollBar()
            old_max = bar.maximum()
            old_val = bar.value()

            if cid:
                history = await self.api.get_chat_history_by_id(
                    cid, limit=limit, skip=self._chat_history_skip, sales_wechat_id=session_sw
                )
            else:
                history = await self.api.get_chat_history(
                    phone, limit=limit, skip=self._chat_history_skip, sales_wechat_id=session_sw
                )
            
            if not history:
                self._has_more_history = False
                if self._chat_history_skip > 0:
                    self.main_win.show_info_bar("info", "提示", "已加载全部历史记录。")
                return

            # 更新偏移量
            self._chat_history_skip += len(history)
            if len(history) < limit:
                self._has_more_history = False

            # 倒序遍历插入到顶部 (因为 get_chat_history 返回的是时间正序，最新的在最后)
            for msg in reversed(history):
                role = msg.get("role")
                content = msg.get("content")
                msg_id = msg.get("id")
                rating = msg.get("rating", 0)
                chat_model = (msg.get("chat_model") or "").strip()
                is_user = (role == "user")
                
                self.main_win.chat_page.prepend_message(
                    content,
                    is_user=is_user,
                    msg_id=msg_id,
                    rating=rating,
                    user_query="",
                    model_tag=chat_model if not is_user else "",
                )

            # 强制立即刷新界面的布局和几何计算
            from PySide6.QtWidgets import QApplication
            QApplication.processEvents()

            # 默认保持视窗位置（适合“上拉自动加载更多”）；手动点击则切到顶部让用户立刻看见变化
            if keep_viewport_position:
                new_max = bar.maximum()
                bar.setValue(old_val + (new_max - old_max))
            else:
                bar.setValue(0)
                if show_loaded_hint:
                    self.main_win.show_info_bar("success", "已显示历史记录", "已加载历史聊天记录（可继续向上滑动加载更多）。")

        except Exception as e:
            logger.error(f"加载更多历史记录失败: {e}")
        finally:
            self.main_win.chat_page._is_batch_loading = False
            self.main_win.chat_page._is_prepending = False
            self._is_loading_history = False


if __name__ == "__main__":
    # 初始化 Qt 程序
    qt_app = QApplication(sys.argv)
    
    qt_app.setQuitOnLastWindowClosed(False)
    
    # 将 asyncio 循环与 Qt 循环融合
    event_loop = QEventLoop(qt_app)
    asyncio.set_event_loop(event_loop)

    def handle_async_exception(loop, context):
        """捕捉并记录所有未捕毁的 Asyncio 异步任务异常，并自动退出防止卡死"""
        msg = context.get("exception", context["message"])
        logger.error(f"捕捉到未处理的异步任务异常: {msg}")
        if "exception" in context:
            logger.opt(exception=context["exception"]).error("详细堆栈如下:")
        
        if QApplication.instance():
            QApplication.instance().exit(1)
        sys.exit(1)

    event_loop.set_exception_handler(handle_async_exception)
    
    with event_loop:
        _desktop_app_holder = {"app": None}

        async def bootstrap():
            ok = await enforce_latest_or_exit(parent_widget=None)
            if not ok:
                qt_app.quit()
                return
            desktop_app = DesktopApp()
            _desktop_app_holder["app"] = desktop_app
            await desktop_app.launch()

        # 在入口处主动创建第一个 bootstrap 任务
        event_loop.create_task(bootstrap())
        try:
            event_loop.run_forever()
        except KeyboardInterrupt:
            # Ctrl+C：正常退出路径，避免残留任务/连接导致的噪音报错
            logger.info("收到 Ctrl+C，正在安全退出...")
        finally:
            async def _graceful_shutdown():
                # 1) 先释放应用内资源（httpx client / image session）
                app = _desktop_app_holder.get("app")
                try:
                    if app:
                        try:
                            await app.image_manager.close()
                        except Exception:
                            pass
                        try:
                            await app.api.aclose()
                        except Exception:
                            pass
                finally:
                    # 2) 取消仍在跑的 asyncio 任务，避免退出时悬挂
                    try:
                        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
                        for t in pending:
                            t.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                    except Exception:
                        pass

            try:
                # run_forever 返回/被中断后，此处 loop 仍可用于做收尾 await
                event_loop.run_until_complete(_graceful_shutdown())
            except Exception:
                pass
            try:
                qt_app.quit()
            except Exception:
                pass
