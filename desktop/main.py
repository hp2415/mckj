import os
import sys
import asyncio
from datetime import datetime
import httpx
# 确保在 import qasync 时，环境已经被净化
from qasync import QEventLoop, asyncSlot
from collections import OrderedDict
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QPixmap, QColor, QIcon
from PySide6.QtCore import Qt, QSettings, QTimer

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
from ui.app_fonts import apply_app_typography
from image_manager import ImageManager
from chat_handler import ChatHandler
from phone_script_handler import PhoneScriptHandler
from ui.chat_widgets import format_message_time
from wechat_send_handler import WechatSendHandler
from logger_cfg import logger
from config_loader import cfg
from storage import CUSTOMERS_LIST_CACHE_KEY, TODAY_TASK_KEYS_CACHE_KEY
from utils import resolve_display_phone
from app_identity import DISPLAY_NAME, cleanup_legacy_install_files
from updater import enforce_latest_or_exit
from app_mutex import acquire_app_mutex, activate_existing_instance
import logging
import ctypes

# 强制为 Windows 进程设置 AppUserModelID，否则任务栏无法正确显示自定义图标
try:
    myappid = 'com.mibuddy.assistant.v1' 
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

def _flush_logs_blocking():
    """同步阻塞地把 loguru 的异步队列 (enqueue=True) 全部刷盘。

    若不调用，sys.exit(1) 会在后台线程 flush 之前就杀掉解释器，
    日志文件里看不到任何错误，用户感受就是“无报错闪退”。
    """
    try:
        logger.complete()
    except Exception:
        pass
    try:
        for handler in list(getattr(logger, "_core", None).handlers.values()):  # type: ignore[attr-defined]
            sink = getattr(handler, "_sink", None)
            stream = getattr(sink, "_file", None) or getattr(sink, "_stream", None)
            if stream and hasattr(stream, "flush"):
                stream.flush()
    except Exception:
        pass


def global_exception_handler(exctype, value, traceback):
    """捕捉并记录所有未捕毁的 GUI 线程异常，并自动退出防止卡死"""
    logger.opt(exception=(exctype, value, traceback)).error("检测到未处理的全局异常 (GUI Thread)")
    _flush_logs_blocking()
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
        self.image_manager = ImageManager(self.api, lite_mode=cfg.lite_mode)
        self.chat_handler = ChatHandler(self, self.api)
        self.phone_script_handler = PhoneScriptHandler(self, self.api)
        self.wechat_send_handler = WechatSendHandler(self, self.api)
        self._is_handling_expiry = False # 标记是否正在处理会话过期，防止重复弹窗
        self._current_customer = None  # 登录后、首次选中客户前，设置页刷新等逻辑会读到
        self._chat_surface_mode = "customer"  # staff=自由对话；与 MainWindow._chat_surface_mode 同步
        self._pending_chat_prompt: str | None = None  # 任务卡片跳转后待发送的提问
        self._from_task_phone_nav = False  # 电话主线任务跳转：跳过订单/历史等重载
        self._task_nav_seq = 0  # 任务卡片跳转序号，用于丢弃过期的并发导航
        self._completing_task_ids: set[int] = set()
        self._ai_scenarios_free: list = []
        self._ai_scenarios_customer: list = []
        self._products_page_loaded = False  # 商品页懒加载：首次进入才拉首屏
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
        if getattr(sys, "frozen", False):
            cleanup_legacy_install_files(os.path.dirname(sys.executable))
        logger.info(f"====== {DISPLAY_NAME} 桌面端启动 ======")
            
        self.login_dlg = LoginDialog()
        self.login_dlg.login_requested.connect(self._handle_login)
        self.login_dlg.open_register_requested.connect(self._open_register_dialog)
        # 万向监听：令牌过期自动重定向
        self.api.unauthorized.connect(self._handle_unauthorized)
        self.login_dlg.show()

        # 每次进入登录界面时检查更新（含注销/会话过期后重登），避免进程常驻时长期不更新
        ok = await enforce_latest_or_exit(parent_widget=self.login_dlg)
        if not ok:
            QApplication.instance().quit()
            return

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
            self.main_win.info_page.wechat_chat_clicked.connect(self._handle_wechat_chat_view)
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
            self.main_win.chat_page.wechat_send_requested.connect(
                lambda mid, tx: asyncio.create_task(self.wechat_send_handler.handle_send(mid, tx))
            )
            self.main_win.chat_page.wechat_edit_send_requested.connect(
                lambda mid, tx: asyncio.create_task(self.wechat_send_handler.handle_edit_send(mid, tx))
            )
            self.main_win.claim_local_wechat_requested.connect(
                self._handle_claim_local_wechat
            )
            self.main_win.chat_page.history_requested.connect(lambda: asyncio.create_task(self._handle_history_requested()))
            self.main_win.chat_page.cleared.connect(self._handle_chat_cleared)
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
            self.main_win.mibuddy_binding_refresh_requested.connect(self._refresh_mibuddy_binding)
            self.main_win.mibuddy_binding_bind_requested.connect(self._bind_mibuddy_uuid)
            self.main_win.mibuddy_binding_unbind_requested.connect(self._unbind_mibuddy_uuid)
            self.main_win.customer_leads_page.claimed_leads_fetch_requested.connect(self._fetch_claimed_leads)
            self.main_win.customer_leads_page.favorite_leads_fetch_requested.connect(self._fetch_favorite_leads)
            self.main_win.customer_leads_page.lead_update_requested.connect(self._update_mibuddy_lead)
            self.main_win.customer_leads_page.lead_remarks_fetch_requested.connect(self._fetch_lead_remarks)
            self.main_win.customer_leads_page.lead_remark_add_requested.connect(self._add_lead_remark)
            self.main_win.customer_leads_page.lead_tel_approve_requested.connect(self._approve_lead_tel)
            self.main_win.customer_leads_page.lead_ignore_requested.connect(self._ignore_mibuddy_lead)
            self.main_win.customer_leads_page.lead_changhu_call_requested.connect(self._call_lead_changhu)
            self.main_win.customer_leads_page.lead_yunke_call_requested.connect(self._call_lead_yunke)
            self.main_win.manual_import_requested.connect(self._handle_manual_import)
            self.main_win.clear_manual_requested.connect(self._handle_clear_manual)
            # 任务分配：拉取总览 + 完成/跳过操作
            self.main_win.task_allocation_request.connect(self._handle_task_allocation_request)
            self.main_win.task_allocation_action.connect(self._handle_task_allocation_action)
            self.main_win.task_open_customer_chat.connect(self._handle_task_open_customer_chat)
            self.main_win.task_open_customer_phone.connect(self._handle_task_open_customer_phone)
            self.main_win.task_wechat_send_requested.connect(self._handle_task_wechat_send)
            self.main_win.phone_workbench.generate_script_requested.connect(
                lambda: asyncio.create_task(self.phone_script_handler.generate())
            )
            self.main_win.phone_workbench.changhu_call_clicked.connect(self._call_phone_changhu)
            self.main_win.phone_workbench.yunke_call_clicked.connect(self._call_phone_yunke)
            self.main_win.phone_workbench.complete_task_clicked.connect(
                self._complete_phone_task_only
            )
            
            # 使用标签切换信号检测进入“商品”页 (Index 2)
            def on_tab_changed(index):
                if index == 2:
                    asyncio.create_task(self._refresh_sync_status())
                    # 商品首屏懒加载：首次进入商品页才拉取，缩短登录后首屏耗时
                    if not self._products_page_loaded:
                        self._products_page_loaded = True
                        asyncio.create_task(self.perform_search("", 0, 20))
            self.main_win.tab_changed.connect(on_tab_changed)
            
            self.main_win.show()
            self.image_manager.bind_product_list(self.main_win.product_list)
            
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

    def _load_customers_from_cache(self) -> list | None:
        storage = getattr(self.api, "storage", None)
        if storage is None:
            return None
        try:
            items = storage.load_json_list(CUSTOMERS_LIST_CACHE_KEY)
            return items if items else None
        except Exception as e:
            logger.warning(f"读取客户列表本地缓存失败: {e}")
            return None

    def _save_customers_to_cache(self, customers: list):
        storage = getattr(self.api, "storage", None)
        if storage is None:
            return
        try:
            storage.save_json_list(CUSTOMERS_LIST_CACHE_KEY, customers)
        except Exception as e:
            logger.warning(f"写入客户列表本地缓存失败: {e}")

    def _apply_customer_list(self, customers: list, *, force_rebuild: bool = False):
        """更新侧栏客户列表并回写本地缓存。"""
        if self.main_win is None:
            return
        self.main_win.update_customer_list(customers, force_rebuild=force_rebuild)
        self._save_customers_to_cache(customers)

    def _load_today_task_order_from_cache(self) -> list | None:
        storage = getattr(self.api, "storage", None)
        if storage is None:
            return None
        try:
            rows = storage.load_json_list(TODAY_TASK_KEYS_CACHE_KEY)
        except Exception as e:
            logger.warning(f"读取今日任务缓存失败: {e}")
            return None
        if not rows:
            return None
        order: list = []
        seen: set = set()
        for r in rows:
            if isinstance(r, (list, tuple)) and len(r) == 2:
                k = (str(r[0] or "").strip(), str(r[1] or "").strip())
                if k[0] and k not in seen:
                    seen.add(k)
                    order.append(k)
        return order or None

    def _save_today_task_order_to_cache(self, order: list):
        storage = getattr(self.api, "storage", None)
        if storage is None:
            return
        try:
            storage.save_json_list(TODAY_TASK_KEYS_CACHE_KEY, [list(k) for k in (order or [])])
        except Exception as e:
            logger.warning(f"写入今日任务缓存失败: {e}")

    @staticmethod
    def _merge_today_task_order(sw_batches: list[list]) -> list:
        """合并各销售号今日任务，按 priority_rank、task_id 排序，去重保留首次出现顺序。"""
        entries: list[tuple] = []
        for batch in sw_batches:
            if isinstance(batch, list):
                entries.extend(batch)
        entries.sort(key=lambda x: (int(x[1]), int(x[2])))
        order: list = []
        seen: set = set()
        for key, _rank, _tid in entries:
            if key not in seen:
                seen.add(key)
                order.append(key)
        return order

    async def _load_customers_first(self):
        """Stale-While-Revalidate：本地缓存先渲染，网络数据回来后无感刷新。"""
        if self.main_win is None:
            return

        cached = self._load_customers_from_cache()
        had_cache = bool(cached)
        # 「今日建议联系」分组与客户列表分开加载：先用缓存的今日任务键即时补出分组
        cached_today_order = self._load_today_task_order_from_cache()
        if cached:
            logger.info(f"客户列表命中本地缓存，共 {len(cached)} 条")
            self.main_win.update_customer_list(cached, today_task_order=cached_today_order)

        customers_resp = await self.api.get_my_customers()
        if self.main_win is None:
            return

        if customers_resp and customers_resp.get("code") == 200:
            customers = customers_resp.get("data", [])
            self._apply_customer_list(customers)
        elif not had_cache:
            self.main_win.set_customer_list_loading(False)

        # 客户列表就绪后，后台异步刷新今日任务分组（不阻塞首屏）
        asyncio.create_task(self._load_today_tasks())

    async def _load_today_tasks(self):
        """异步拉取今日任务，映射为客户键集合后刷新「今日建议联系」分组。

        与客户列表完全解耦：任务接口较慢，这里独立成任务并发拉取各绑定销售号的
        当日任务，仅提取 (raw_customer_id, sales_wechat_id) 用于侧栏分组。
        """
        if self.main_win is None:
            return
        try:
            bindings = await self.api.list_sales_wechats()
        except Exception as e:
            logger.warning(f"今日任务：拉取销售绑定失败: {e}")
            return
        if self.main_win is None:
            return
        sales_ids = [
            str(r.get("sales_wechat_id") or "").strip()
            for r in (bindings or [])
            if str(r.get("sales_wechat_id") or "").strip()
        ]
        if not sales_ids:
            return

        results = await asyncio.gather(
            *(self._fetch_today_tasks_for_sw(sw) for sw in sales_ids),
            return_exceptions=True,
        )
        if self.main_win is None:
            return
        batches = [r for r in results if isinstance(r, list)]
        order = self._merge_today_task_order(batches)
        self._save_today_task_order_to_cache(order)
        self.main_win.set_today_task_order(order)
        logger.info(f"今日建议联系：命中 {len(order)} 位客户")

    async def _fetch_today_tasks_for_sw(self, sales_wechat_id: str) -> list:
        """拉取单个销售微信号的今日任务，返回 [(key, priority_rank, task_id), ...]（API 顺序）。"""
        entries: list = []
        page = 1
        page_size = 200
        actionable = {"pending", "in_progress", "overdue"}
        while True:
            resp = await self.api.get_tasks_overview(
                period="daily",
                sales_wechat_id=sales_wechat_id,
                page=page,
                page_size=page_size,
            )
            if not resp or resp.get("code") != 200:
                break
            data = resp.get("data") or {}
            items = data.get("items") or []
            for it in items:
                if (it.get("status") or "pending").strip() not in actionable:
                    continue
                rid = str(it.get("raw_customer_id") or "").strip()
                if not rid:
                    continue
                sw = str(it.get("sales_wechat_id") or sales_wechat_id or "").strip()
                rank = int(it.get("priority_rank") or 0)
                tid = int(it.get("id") or 0)
                entries.append(((rid, sw), rank, tid))
            total = int(data.get("total_items") or 0)
            if page * page_size >= total or not items:
                break
            page += 1
        return entries

    async def _load_secondary_configs(self):
        """首屏次要配置：与客户列表解耦，后台并行拉取。"""
        def _ok(v):
            return None if isinstance(v, BaseException) else v

        results = await asyncio.gather(
            self.api.get_configs_dict(),
            self.api.get_profile_tag_options(),
            self.api.get_ai_scenarios("free"),
            self.api.get_ai_scenarios("customer"),
            self._refresh_sales_bindings(),
            self._refresh_mibuddy_binding(),
            return_exceptions=True,
        )
        configs_dict, tag_resp, free_resp, cust_resp, _, _ = [_ok(r) for r in results]
        if self.main_win is None:
            return

        if configs_dict:
            self.main_win.info_page.populate_combo_boxes(configs_dict)
            models = configs_dict.get("llm_chat_models")
            if models:
                self.main_win.chat_page.set_chat_model_options(models)
            default_models = configs_dict.get("desktop_default_chat_models")
            if default_models and hasattr(self.main_win.chat_page, "apply_server_default_chat_models"):
                self.main_win.chat_page.apply_server_default_chat_models(default_models)

        if tag_resp and tag_resp.get("code") == 200:
            self.main_win.info_page.set_profile_tag_catalog(tag_resp.get("data") or [])
            if self._current_customer:
                self.main_win.info_page.set_customer(self._current_customer)

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

    async def _initial_data_fetch(self):
        """首屏数据加载：客户列表优先（含本地缓存秒开），其余配置后台并行。"""
        if self.main_win is not None:
            self.main_win.set_customer_list_loading(True)

        await asyncio.gather(
            self._load_customers_first(),
            self._load_secondary_configs(),
            return_exceptions=True,
        )
        if self.main_win is None:
            return

        await self._refresh_sync_status()

        # 主窗口 __init__ 已默认进入客户对话页；此处不再强制 setCurrentIndex(0)，
        # 避免用户登录后切到其他模块时，等客户列表拉取完成又被拽回对话页。

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
        self._history_mode_enabled = not staff
        self.main_win.chat_page.clear()
        if staff:
            self.main_win.apply_staff_chat_header()
            welcome = (
                "您好，这是内部问答模式（未绑定客户）。可询问产品知识、平台规则与话术思路；"
                "需要某位客户的档案、订单或微信摘要时，请点击「客户对话」并在左侧选择客户。"
            )
            self.main_win.chat_page.add_message(welcome, False)
            self.main_win.chat_page.scroll_to_bottom(instant=True)
        else:
            if self._current_customer:
                self.main_win.apply_customer_header(self._current_customer)
                # 切换为客户对话时，自动加载当前选中客户的历史记录，并支持上划加载更多
                await self._load_latest_history_first_page(show_toast=False)
            else:
                self.main_win.apply_customer_header_placeholder()
                welcome = "请在左侧选择一位客户，或点击机器人图标进入「自由对话」进行内部问答。"
                self.main_win.chat_page.add_message(welcome, False)
                self.main_win.chat_page.scroll_to_bottom(instant=True)

    @asyncSlot()
    async def _handle_claim_local_wechat(self):
        await self.wechat_send_handler.open_claim_dialog_manual()

    @staticmethod
    def _customer_keys_match(a: dict | None, b: dict | None) -> bool:
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        return str(a.get("id") or "") == str(b.get("id") or "") and str(
            a.get("sales_wechat_id") or ""
        ) == str(b.get("sales_wechat_id") or "")

    def _refresh_customer_drawer_panels(self, customer: dict) -> None:
        """切换客户时立即刷新抽屉内资料/订单/电话工作台，避免任务快路径残留上一位客户。"""
        if not self.main_win or not isinstance(customer, dict):
            return
        self.main_win.update_order_table([])
        self.main_win.info_page.set_customer(customer)
        self.main_win._sync_phone_workbench(customer)

    async def _fetch_and_show_customer_orders(
        self, customer_id, *, nav_seq: int | None = None
    ) -> None:
        cid = str(customer_id or "").strip()
        if not cid or not self.main_win:
            return
        try:
            resp = await self.api.get_customer_orders(cid)
        except Exception as e:
            logger.warning(f"拉取客户订单异常 customer_id={cid}: {e}")
            return
        if nav_seq is not None and nav_seq != self._task_nav_seq:
            return
        cur = self._current_customer
        if not cur or str(cur.get("id") or "").strip() != cid:
            return
        if resp and resp.get("code") == 200:
            self.main_win.update_order_table(resp.get("data", []))

    def _prime_task_customer_ui(self, customer: dict) -> None:
        """任务卡片跳转：先同步切到对话页并刷新顶栏，避免等网络请求后才改界面。"""
        self._current_customer = customer
        self.main_win._set_chat_surface_mode("customer")
        self.main_win.apply_customer_header(customer, sync_phone=False)
        self._refresh_customer_drawer_panels(customer)

    def _schedule_sidebar_customer_select(
        self,
        customer: dict,
        *,
        defer_ms: int = 200,
        lightweight: bool = False,
        today_group_only: bool = False,
    ) -> None:
        """侧栏树定位延后执行，避免 _render_group_children 阻塞跳转主线。"""
        rid = customer.get("id")
        sw = customer.get("sales_wechat_id")

        def _select():
            if not self.main_win:
                return
            if today_group_only:
                self.main_win.select_customer_by_key(rid, sw, today_group_only=True)
            elif lightweight:
                self.main_win.select_customer_by_key_if_visible(rid, sw)
            else:
                self.main_win.select_customer_by_key(rid, sw)

        QTimer.singleShot(max(0, int(defer_ms)), _select)

    async def _hydrate_customer_profile(self, customer: dict):
        """列表瘦身后画像全文按需拉取：合并进客户 dict 并刷新资料面板。

        原地修改 dict——电话工作台等持同一引用、懒读取 ai_profile 的组件无需额外通知。
        """
        if not isinstance(customer, dict):
            return
        if customer.get("ai_profile") is not None:
            return  # 已加载过（或旧版后端仍全量下发）
        cid = customer.get("id")
        if not cid:
            customer["ai_profile"] = ""
            return
        sw = customer.get("sales_wechat_id")
        sw = (str(sw).strip() or None) if sw is not None else None
        resp = await self.api.get_customer_detail(str(cid), sales_wechat_id=sw)
        if not resp or resp.get("code") != 200:
            return  # 失败不缓存空值，下次选中可重试
        detail = resp.get("data") or {}
        customer["ai_profile"] = detail.get("ai_profile") or ""
        customer["has_ai_profile"] = bool((customer["ai_profile"] or "").strip())
        # 仍是当前客户时刷新资料面板（面板在选中瞬间用瘦身数据渲染过一次）
        if self.main_win and self._customer_keys_match(self._current_customer, customer):
            self.main_win.info_page.set_customer(customer)
            wb = getattr(self.main_win, "phone_workbench", None)
            if wb is not None:
                wb.refresh_profile_section()

    @asyncSlot()
    async def _handle_customer_selected(self, customer_data):
        """当侧边栏选中某个客户时触发"""
        # 1. 如果还在由于上一位客户进行 AI 对话，先行取消，防止由于回包延迟导致的“消息穿越”
        self.chat_handler.cancel_current_task()
        self.phone_script_handler.cancel()
        
        self._current_customer = customer_data # 锁定当前业务上下文
        # 画像全文按需拉取，与订单/历史并行，不阻塞首屏
        asyncio.create_task(self._hydrate_customer_profile(customer_data))
        self._chat_history_skip = 0      # 聊天记录分页偏移量
        self._has_more_history = True    # 是否还有更多历史记录
        self._is_loading_history = False # 是否正在加载历史中
        self._history_mode_enabled = True # 开启历史加载模式，以便支持向上划动加载更多
        
        logger.info(f"已选中客户: {customer_data.get('customer_name')}, ConvID: {customer_data.get('dify_conversation_id')}")

        from_task_chat = bool(getattr(self, "_pending_chat_prompt", None))
        from_task_phone = bool(getattr(self, "_from_task_phone_nav", False))
        fast_nav = from_task_chat or from_task_phone

        if not from_task_phone and self.main_win:
            self.main_win.clear_pending_phone_task()
        if not from_task_chat and self.main_win:
            self.main_win.clear_pending_wechat_task()

        self._refresh_customer_drawer_panels(customer_data)

        if not fast_nav:
            self.main_win.apply_customer_header(customer_data)

        customer_id = customer_data.get("id")
        orders_task = None
        if customer_id is not None:
            if fast_nav:
                asyncio.create_task(
                    self._fetch_and_show_customer_orders(
                        customer_id, nav_seq=self._task_nav_seq
                    )
                )
            else:
                orders_task = self._handle_history_clicked(customer_id)

        if from_task_chat:
            # 任务跳转：留在对话区，不自动展开右侧资料/订单抽屉
            if getattr(self.main_win, "_drawer_open", False):
                self.main_win._toggle_drawer(self.main_win.drawer_stack.currentIndex())
        elif not fast_nav:
            self.main_win.switch_tab(1)
        
        # 准备 AI 对话页 (切换客户时清空历史，准备新上下文)
        if not fast_nav:
            self.main_win.chat_page.clear()
        self._chat_history_skip = 0
        self._has_more_history = True
        self._is_loading_history = False

        if from_task_chat:
            self.main_win.chat_page.clear()
            await asyncio.sleep(0)
            await self._load_latest_history_first_page(show_toast=False, skip_clear=True)
            prompt = getattr(self, "_pending_chat_prompt", None)
            self._pending_chat_prompt = None
            if prompt:
                asyncio.create_task(self.chat_handler.handle_ai_chat_sent(prompt))
            return

        if from_task_phone:
            self._from_task_phone_nav = False
            if self.main_win:
                self.main_win.clear_pending_phone_task()
            self.main_win.chat_page.clear()
            await asyncio.sleep(0)
            await self._load_latest_history_first_page(show_toast=False, skip_clear=True)
            return
        
        # 侧栏点选：自动拉取第一页历史记录并展示，隐藏提示弹窗（订单已并行在拉）
        await self._load_latest_history_first_page(show_toast=False)
        if orders_task is not None:
            await orders_task

    @asyncSlot(dict, bool)
    async def _handle_task_wechat_send(self, task: dict, edit_mode: bool):
        """激活任务卡片：用 instruction 作为内容，复用聊天气泡发微信逻辑。"""
        if not self.main_win:
            return
        text = (task.get("instruction") or "").strip()
        if not text:
            self.main_win.show_info_bar("warning", "内容为空", "该激活任务没有可发送的话术。")
            return
        customer = self.main_win.find_customer_by_task(task)
        if not customer:
            name = (task.get("customer_name") or "").strip() or "该客户"
            self.main_win.show_info_bar(
                "warning",
                "未找到客户",
                f"「{name}」不在当前客户列表中，请先同步客户数据后再试。",
            )
            return
        self.main_win.set_pending_wechat_task(dict(task))
        if edit_mode:
            await self.wechat_send_handler.handle_edit_send(
                None, text, customer=customer, contact_task=task
            )
        else:
            await self.wechat_send_handler.handle_send(
                None, text, customer=customer, contact_task=task
            )

    @asyncSlot(dict)
    async def _handle_task_open_customer_phone(self, task: dict):
        """电话主线任务：定位客户并展开联系电话抽屉（异步执行，避免阻塞后续点击）。"""
        if self.main_win:
            self.main_win.flash_task_nav_ui()
        asyncio.create_task(self._run_task_open_customer_phone(task))

    async def _run_task_open_customer_phone(self, task: dict):
        self._task_nav_seq += 1
        nav_seq = self._task_nav_seq
        if not self.main_win:
            return
        customer = self.main_win.find_customer_by_task(task)
        if not customer:
            name = (task.get("customer_name") or "").strip() or "该客户"
            self.main_win.show_info_bar(
                "warning",
                "未找到客户",
                f"「{name}」不在当前客户列表中，请先同步客户数据后再试。",
            )
            return
        self.main_win.set_pending_phone_task(dict(task))
        self._from_task_phone_nav = True
        self._prime_task_customer_ui(customer)
        drawer = self.main_win
        if not getattr(drawer, "_drawer_open", False) or drawer.drawer_stack.currentIndex() != 1:
            QTimer.singleShot(0, lambda d=drawer: d._toggle_drawer(1))
        self.main_win.ensure_today_task_customer_for_nav(customer)
        await asyncio.sleep(0)
        self._schedule_sidebar_customer_select(customer, today_group_only=True)
        await self._handle_customer_selected(customer)
        if nav_seq != self._task_nav_seq:
            return
        phone = resolve_display_phone(task) or resolve_display_phone(customer)
        if not phone:
            self.main_win.show_info_bar(
                "info",
                "暂无电话",
                "该客户尚未登记手机号，可在右侧资料面板补充。",
            )

    @asyncSlot(dict)
    async def _handle_task_open_customer_chat(self, task: dict):
        """任务卡片点击：进入客户对话并自动提问开场白（异步执行，避免阻塞后续点击）。"""
        if self.main_win:
            self.main_win.flash_task_nav_ui()
        asyncio.create_task(self._run_task_open_customer_chat(task))

    async def _run_task_open_customer_chat(self, task: dict):
        self._task_nav_seq += 1
        nav_seq = self._task_nav_seq
        if not self.main_win:
            return
        customer = self.main_win.find_customer_by_task(task)
        if not customer:
            name = (task.get("customer_name") or "").strip() or "该客户"
            self.main_win.show_info_bar(
                "warning",
                "未找到客户",
                f"「{name}」不在当前客户列表中，请先同步客户数据后再试。",
            )
            return
        self._pending_chat_prompt = "给我一个开场白"
        self.main_win.clear_pending_phone_task()
        self.main_win.set_pending_wechat_task(dict(task))
        self._prime_task_customer_ui(customer)
        self.main_win.ensure_today_task_customer_for_nav(customer)
        await asyncio.sleep(0)
        self._schedule_sidebar_customer_select(customer, today_group_only=True)
        await self._handle_customer_selected(customer)
        if nav_seq != self._task_nav_seq:
            return

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
                self._apply_customer_list(data_list)
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
                    # 列表瘦身后 refreshed 不含画像全文，按需补齐并刷新面板
                    asyncio.create_task(self._hydrate_customer_profile(refreshed))
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
        cur = self._current_customer
        if not cur or str(cur.get("id") or "").strip() != cid:
            return
        if resp and resp.get("code") == 200:
            orders = resp.get("data", [])
            self.main_win.update_order_table(orders)
        else:
            self.main_win.show_info_bar("warning", "查询失败", "未能获取到该客户的历史订单数据。")

    @asyncSlot()
    async def _handle_wechat_chat_view(self, customer_id, sales_wechat_id):
        """打开对话框展示云客同步的微信原始聊天记录。"""
        from ui.wechat_chat_history_dialog import WechatChatHistoryDialog
        from wechat_send_handler import _exec_dialog_async

        cid = str(customer_id or "").strip()
        sw = (str(sales_wechat_id).strip() if sales_wechat_id else "") or ""
        if not cid:
            return
        if not sw:
            self.main_win.show_info_bar(
                "warning", "无法查看", "当前客户行缺少业务微信号，无法定位微信会话。"
            )
            return

        cust = getattr(self, "_current_customer", None) or {}
        label_parts = [
            str(cust.get("wechat_remark") or cust.get("customer_name") or cid),
            str(cust.get("phone") or ""),
        ]
        customer_label = " · ".join(p for p in label_parts if p)

        limit = 50
        skip = 0
        dlg = WechatChatHistoryDialog(
            self.main_win,
            customer_label=customer_label,
            loading=True,
        )

        async def _load_more():
            nonlocal skip
            more_resp = await self.api.get_wechat_chat_logs_by_id(
                cid, limit=limit, skip=skip, sales_wechat_id=sw
            )
            if not more_resp or more_resp.get("code") != 200:
                self.main_win.show_info_bar(
                    "warning",
                    "加载失败",
                    (more_resp or {}).get("message") or "请稍后重试",
                )
                return
            more_rows = more_resp.get("data") or []
            skip += len(more_rows)
            dlg.append_rows(more_rows, has_more=bool(more_resp.get("has_more")))

        async def _fetch_initial():
            nonlocal skip
            resp = await self.api.get_wechat_chat_logs_by_id(
                cid, limit=limit, skip=0, sales_wechat_id=sw
            )
            if not resp or resp.get("code") != 200:
                msg = (resp or {}).get("message") or "未能获取微信聊天记录。"
                dlg.show_error(msg)
                return
            rows = resp.get("data") or []
            skip = len(rows)
            dlg.set_initial_data(rows, has_more=bool(resp.get("has_more")))

        dlg.load_more_requested.connect(lambda: asyncio.create_task(_load_more()))
        asyncio.create_task(_fetch_initial())
        try:
            await _exec_dialog_async(dlg)
        finally:
            dlg.deleteLater()

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
        self._apply_customer_list(customers)
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
            # 列表瘦身后 updated 不含画像全文，按需补齐并刷新面板
            asyncio.create_task(self._hydrate_customer_profile(updated))
        elif self._current_customer:
            # 绑定变更后原客户可能已不在可见列表中
            self._current_customer = None
            self.main_win.lbl_header_unit.setText("")
            self.main_win.lbl_header_info.setText("")
            if hasattr(self.main_win, "phone_workbench"):
                self.main_win.phone_workbench.clear()

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

    @asyncSlot()
    async def _refresh_mibuddy_binding(self):
        main_win = self.main_win
        if not main_win:
            return
        data = await self.api.get_mibuddy_binding()
        if self.main_win is None or main_win is not self.main_win:
            return
        main_win.update_mibuddy_binding_ui(data)
        page = getattr(main_win, "customer_leads_page", None)
        if page is not None:
            page.apply_mibuddy_binding_state(data)

    async def _refresh_mibuddy_leads(self, leads_page, *, force: bool = False):
        leads_page.ensure_current_tab_loaded(force=force)

    @asyncSlot(int, int, bool, bool, int)
    async def _fetch_claimed_leads(
        self, page: int, page_size: int, append: bool, silent: bool = False, seq: int = 0
    ):
        main_win = self.main_win
        if not main_win:
            return
        leads_page = getattr(main_win, "customer_leads_page", None)
        if leads_page is None:
            return
        if seq and seq != leads_page._claimed_fetch_seq:
            return
        if not silent:
            leads_page.set_claimed_leads_loading(True)

        fetch_page = max(1, int(page or 1))
        fetch_size = max(1, int(page_size or leads_page.CLAIMED_FETCH_BATCH_SIZE))
        if append:
            if seq and seq != leads_page._claimed_fetch_seq:
                return
            if leads_page._claimed_pending_display_advance:
                leads_page.set_claimed_page_loading(True)
            resp = await self.api.get_mibuddy_claimed_leads(
                fetch_page,
                fetch_size,
                sort=leads_page.claimed_sort,
                order=leads_page.claimed_order,
            )
            if self.main_win is None or main_win is not self.main_win:
                return
            if seq and seq != leads_page._claimed_fetch_seq:
                leads_page.set_claimed_page_loading(False)
                return
            if not (resp and resp.get("code") == 200):
                leads_page.set_claimed_page_loading(False)
                if silent and getattr(leads_page, "_claimed_prefetching", False):
                    leads_page._on_claimed_prefetch_failed()
                    return
                if not silent:
                    r = resp or {}
                    msg = r.get("message") or r.get("detail") or "加载认领客资失败"
                    if isinstance(msg, list):
                        msg = "; ".join(str(x) for x in msg)
                    leads_page.show_claimed_leads_error(str(msg))
                    InfoBar.warning(
                        title="加载失败",
                        content=str(msg),
                        duration=3500,
                        position=InfoBarPosition.TOP,
                        parent=main_win,
                    )
                return
            data = resp.get("data") if isinstance(resp, dict) else None
            leads_page.set_claimed_leads_page(
                data or {},
                append=True,
                preserve_scroll=True,
                seq=seq,
                silent=silent,
            )
            return

        if seq and seq != leads_page._claimed_fetch_seq:
            return
        resp = await self.api.get_mibuddy_claimed_leads(
            fetch_page,
            fetch_size,
            sort=leads_page.claimed_sort,
            order=leads_page.claimed_order,
        )
        if self.main_win is None or main_win is not self.main_win:
            return
        if seq and seq != leads_page._claimed_fetch_seq:
            return
        if not (resp and resp.get("code") == 200):
            if silent:
                return
            r = resp or {}
            msg = r.get("message") or r.get("detail") or "加载认领客资失败"
            if isinstance(msg, list):
                msg = "; ".join(str(x) for x in msg)
            leads_page.show_claimed_leads_error(str(msg))
            InfoBar.warning(
                title="加载失败",
                content=str(msg),
                duration=3500,
                position=InfoBarPosition.TOP,
                parent=main_win,
            )
            return
        data = resp.get("data") if isinstance(resp, dict) else None
        leads_page.set_claimed_leads_page(
            data or {},
            preserve_scroll=silent,
            seq=seq,
            silent=silent,
        )

    @asyncSlot(int, int, bool, bool, str, int)
    async def _fetch_favorite_leads(
        self,
        page: int,
        page_size: int,
        append: bool,
        silent: bool = False,
        client_name: str = "",
        seq: int = 0,
    ):
        main_win = self.main_win
        if not main_win:
            return
        leads_page = getattr(main_win, "customer_leads_page", None)
        if leads_page is None:
            return
        keyword = (client_name or "").strip()
        if seq and seq != leads_page._favorite_fetch_seq:
            return
        if keyword != leads_page._favorite_client_name:
            return
        if not silent:
            leads_page.set_favorite_leads_loading(True)
        elif leads_page._favorite_page_loading:
            pass

        fetch_page = max(1, int(page or 1))
        fetch_size = max(1, int(page_size or leads_page.FAVORITE_PAGE_SIZE))
        resp = await self.api.get_mibuddy_favorite_leads(
            fetch_page,
            fetch_size,
            client_name=keyword or None,
            sort=leads_page.favorite_sort,
            order=leads_page.favorite_order,
        )
        if self.main_win is None or main_win is not self.main_win:
            return
        if seq and seq != leads_page._favorite_fetch_seq:
            return
        if resp and resp.get("code") == 200:
            data = resp.get("data") if isinstance(resp, dict) else None
            leads_page.set_favorite_leads_page(
                data,
                client_name=keyword,
                preserve_scroll=silent,
                seq=seq,
                silent=silent,
            )
            return
        leads_page.set_favorite_page_loading(False)
        if silent:
            return
        r = resp or {}
        msg = r.get("message") or r.get("detail") or "加载收藏客资失败"
        if isinstance(msg, list):
            msg = "; ".join(str(x) for x in msg)
        leads_page.show_favorite_leads_error(str(msg))
        InfoBar.warning(
            title="加载失败",
            content=str(msg),
            duration=3500,
            position=InfoBarPosition.TOP,
            parent=main_win,
        )

    @asyncSlot(int, int, int)
    async def _fetch_lead_remarks(self, lead_id: int, page: int, page_size: int):
        main_win = self.main_win
        if not main_win:
            return
        leads_page = getattr(main_win, "customer_leads_page", None)
        if leads_page is None:
            return
        dialog = leads_page._active_detail_dialog
        if dialog is None:
            return
        resp = await self.api.get_mibuddy_lead_remarks(lead_id, page, page_size)
        if leads_page._active_detail_dialog is not dialog:
            return
        try:
            if resp and resp.get("code") == 200:
                data = resp.get("data") if isinstance(resp, dict) else None
                dialog.set_remarks_page(data if isinstance(data, dict) else None)
                return
            r = resp or {}
            msg = r.get("message") or r.get("detail") or "加载跟进记录失败"
            if isinstance(msg, list):
                msg = "; ".join(str(x) for x in msg)
            dialog.show_remarks_error(str(msg))
        except RuntimeError:
            return

    @staticmethod
    def _primary_sales_wechat_id(main_win) -> str:
        rows = getattr(main_win, "_cached_sales_bindings", []) or []
        for row in rows:
            if row.get("is_primary"):
                return str(row.get("sales_wechat_id") or "").strip()
        if rows:
            return str(rows[0].get("sales_wechat_id") or "").strip()
        return ""

    @staticmethod
    def _api_error_message(resp: dict | None, default: str) -> str:
        r = resp or {}
        msg = r.get("message") or r.get("detail") or default
        if isinstance(msg, list):
            msg = "; ".join(str(x) for x in msg)
        msg = str(msg)
        code = r.get("code")
        if code is not None and code != 200:
            return f"[{code}] {msg}"
        return msg

    @staticmethod
    def _extract_call_id(resp: dict | None) -> str:
        data = (resp or {}).get("data") if isinstance(resp, dict) else None
        if isinstance(data, dict):
            return str(data.get("call_id") or "").strip()
        return ""

    @asyncSlot(int, str)
    async def _call_lead_changhu(self, lead_id: int, changhu_tel: str):
        main_win = self.main_win
        if not main_win:
            return
        leads_page = getattr(main_win, "customer_leads_page", None)
        if leads_page is None:
            return
        caller = (changhu_tel or "").strip()
        if not caller:
            leads_page.handle_changhu_call_result(int(lead_id), False, "请选择畅呼主叫号码")
            return
        sales_wechat_id = self._primary_sales_wechat_id(main_win)
        resp = await self.api.call_mibuddy_changhu(
            changhu_tel=caller,
            lead_id=int(lead_id),
            user_wechat_account=sales_wechat_id or None,
        )
        call_id = self._extract_call_id(resp)
        if resp and resp.get("code") == 200:
            msg = "外呼已发起"
            if call_id:
                msg = f"外呼已发起（{call_id}）"
            leads_page.handle_changhu_call_result(int(lead_id), True, msg)
            return
        leads_page.handle_changhu_call_result(
            int(lead_id),
            False,
            self._api_error_message(resp, "畅呼外呼失败"),
        )

    @asyncSlot(int)
    async def _call_lead_yunke(self, lead_id: int):
        main_win = self.main_win
        if not main_win:
            return
        leads_page = getattr(main_win, "customer_leads_page", None)
        if leads_page is None:
            return
        sales_wechat_id = self._primary_sales_wechat_id(main_win)
        resp = await self.api.call_mibuddy_yunke(
            lead_id=int(lead_id),
            user_wechat_account=sales_wechat_id or None,
        )
        call_id = self._extract_call_id(resp)
        if resp and resp.get("code") == 200:
            msg = "外呼已发起"
            if call_id:
                msg = f"外呼已发起（{call_id}）"
            leads_page.handle_yunke_call_result(int(lead_id), True, msg)
            return
        leads_page.handle_yunke_call_result(
            int(lead_id),
            False,
            self._api_error_message(resp, "云客外呼失败"),
        )

    @asyncSlot(str)
    async def _call_phone_changhu(self, changhu_tel: str):
        main_win = self.main_win
        if not main_win:
            return
        wb = getattr(main_win, "phone_workbench", None)
        if wb is None:
            return
        tel = wb.dial_phone()
        caller = (changhu_tel or "").strip()
        if not tel or not caller:
            wb.set_changhu_call_busy(False)
            return
        sales_wechat_id = wb.customer_sales_wechat_id()
        wb.set_changhu_call_busy(True)
        try:
            resp = await self.api.call_mibuddy_changhu(
                changhu_tel=caller,
                tel=tel,
                user_wechat_account=sales_wechat_id or None,
            )
        finally:
            wb.set_changhu_call_busy(False)
        call_id = self._extract_call_id(resp)
        if resp and resp.get("code") == 200:
            await self._complete_phone_task_if_applicable()
            from utils import mask_phone

            content = f"已发起畅呼外呼，拨打 {mask_phone(tel)}"
            if call_id:
                content = f"{content}（{call_id}）"
            main_win.show_info_bar("success", "畅呼外呼", content, duration=3500)
            return
        main_win.show_info_bar(
            "warning",
            "畅呼外呼失败",
            self._api_error_message(resp, "请稍后重试"),
            duration=4000,
        )

    @asyncSlot()
    async def _call_phone_yunke(self):
        main_win = self.main_win
        if not main_win:
            return
        wb = getattr(main_win, "phone_workbench", None)
        if wb is None:
            return
        tel = wb.dial_phone()
        if not tel:
            wb.set_yunke_call_busy(False)
            return
        sales_wechat_id = wb.customer_sales_wechat_id()
        wb.set_yunke_call_busy(True)
        try:
            from ui.phone_workbench import PHONE_YUNKE_OUTBOUND_DISABLED

            if PHONE_YUNKE_OUTBOUND_DISABLED:
                await self._complete_phone_task_if_applicable()
                from utils import mask_phone

                main_win.show_info_bar(
                    "success",
                    "云客外呼",
                    f"（测试）已跳过外呼 API，拨打 {mask_phone(tel)}",
                    duration=3500,
                )
                return
            resp = await self.api.call_mibuddy_yunke(
                tel=tel,
                user_wechat_account=sales_wechat_id or None,
            )
        finally:
            wb.set_yunke_call_busy(False)
        call_id = self._extract_call_id(resp)
        if resp and resp.get("code") == 200:
            await self._complete_phone_task_if_applicable()
            from utils import mask_phone

            content = f"已发起云客外呼，拨打 {mask_phone(tel)}"
            if call_id:
                content = f"{content}（{call_id}）"
            main_win.show_info_bar("success", "云客外呼", content, duration=3500)
            return
        main_win.show_info_bar(
            "warning",
            "云客外呼失败",
            self._api_error_message(resp, "请稍后重试"),
            duration=4000,
        )

    @asyncSlot(int)
    async def _approve_lead_tel(self, lead_id: int):
        main_win = self.main_win
        if not main_win:
            return
        leads_page = getattr(main_win, "customer_leads_page", None)
        if leads_page is None:
            return
        dialog = leads_page._active_detail_dialog
        if dialog is None:
            return
        resp = await self.api.approve_mibuddy_lead_tel(int(lead_id))
        if leads_page._active_detail_dialog is not dialog:
            return
        try:
            if resp and resp.get("code") == 200:
                dialog.handle_tel_approve_result(True)
                return
            r = resp or {}
            msg = r.get("message") or r.get("detail") or "提交申请失败"
            if isinstance(msg, list):
                msg = "; ".join(str(x) for x in msg)
            dialog.handle_tel_approve_result(False, str(msg))
        except RuntimeError:
            return

    @asyncSlot(dict)
    async def _add_lead_remark(self, payload: dict):
        main_win = self.main_win
        if not main_win:
            return
        leads_page = getattr(main_win, "customer_leads_page", None)
        if leads_page is None:
            return
        dialog = leads_page._active_detail_dialog
        if dialog is None:
            return
        lead_id = payload.get("lead_id")
        remark = str(payload.get("remark") or "").strip()
        if lead_id is None or not remark:
            dialog.handle_remark_add_result(False, "缺少客资 ID 或备注内容")
            return
        resp = await self.api.add_mibuddy_lead_remark(int(lead_id), remark)
        if leads_page._active_detail_dialog is not dialog:
            return
        try:
            if resp and resp.get("code") == 200:
                data = resp.get("data") if isinstance(resp, dict) else None
                dialog.handle_remark_add_result(
                    True, "", data if isinstance(data, dict) else None
                )
                return
            r = resp or {}
            msg = r.get("message") or r.get("detail") or "提交跟进记录失败"
            if isinstance(msg, list):
                msg = "; ".join(str(x) for x in msg)
            dialog.handle_remark_add_result(False, str(msg))
        except RuntimeError:
            return

    @asyncSlot(int)
    async def _ignore_mibuddy_lead(self, lead_id: int):
        main_win = self.main_win
        if not main_win:
            return
        leads_page = getattr(main_win, "customer_leads_page", None)
        if leads_page is None:
            return
        resp = await self.api.ignore_mibuddy_lead(int(lead_id))
        if self.main_win is None or main_win is not self.main_win:
            return
        if resp and resp.get("code") == 200:
            leads_page.handle_lead_ignore_result(int(lead_id), True)
            return
        r = resp or {}
        msg = r.get("message") or r.get("detail") or "移除客资失败"
        if isinstance(msg, list):
            msg = "; ".join(str(x) for x in msg)
        leads_page.handle_lead_ignore_result(int(lead_id), False, str(msg))

    @asyncSlot(dict)
    async def _update_mibuddy_lead(self, payload: dict):
        main_win = self.main_win
        if not main_win:
            return
        leads_page = getattr(main_win, "customer_leads_page", None)
        if leads_page is None:
            return
        lead_id = payload.get("lead_id")
        info = payload.get("info") or {}
        if lead_id is None:
            leads_page.handle_lead_update_result(False, "缺少客资 ID", payload)
            return
        resp = await self.api.update_mibuddy_lead(int(lead_id), info)
        if self.main_win is None or main_win is not self.main_win:
            return
        if resp and resp.get("code") == 200:
            leads_page.handle_lead_update_result(True, "", payload)
            return
        r = resp or {}
        msg = r.get("message") or r.get("detail") or "更新客资失败"
        if isinstance(msg, list):
            msg = "; ".join(str(x) for x in msg)
        leads_page.handle_lead_update_result(False, str(msg), payload)

    @asyncSlot(str)
    async def _bind_mibuddy_uuid(self, uuid: str):
        if not self.main_win:
            return
        resp = await self.api.bind_mibuddy_uuid(uuid.strip())
        if resp and resp.get("code") == 200:
            InfoBar.success(
                title="已绑定",
                content="米城 UUID 绑定成功",
                duration=2500,
                position=InfoBarPosition.TOP,
                parent=self.main_win,
            )
            data = resp.get("data") if isinstance(resp, dict) else None
            self.main_win.update_mibuddy_binding_ui(data)
            page = getattr(self.main_win, "customer_leads_page", None)
            if page is not None:
                page.apply_mibuddy_binding_state(data)
                page.invalidate_leads_cache()
                await self._refresh_mibuddy_leads(page, force=True)
        else:
            r = resp or {}
            msg = r.get("message") or r.get("detail", "绑定失败")
            if isinstance(msg, list):
                msg = "; ".join(str(x) for x in msg)
            InfoBar.warning(
                title="绑定失败",
                content=str(msg),
                duration=4000,
                position=InfoBarPosition.TOP,
                parent=self.main_win,
            )

    @asyncSlot()
    async def _unbind_mibuddy_uuid(self):
        if not self.main_win:
            return
        ok = await self.api.unbind_mibuddy_uuid()
        if ok:
            InfoBar.success(
                title="已解绑",
                content="米城 UUID 已移除",
                duration=2000,
                position=InfoBarPosition.TOP,
                parent=self.main_win,
            )
            self.main_win.update_mibuddy_binding_ui(None)
            page = getattr(self.main_win, "customer_leads_page", None)
            if page is not None:
                page.apply_mibuddy_binding_state(None)
        else:
            InfoBar.warning(
                title="解绑失败",
                content="请稍后重试",
                duration=3000,
                position=InfoBarPosition.TOP,
                parent=self.main_win,
            )

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

    @asyncSlot(str, str, int, int, object)
    async def _handle_task_allocation_request(
        self,
        sales_wechat_id: str,
        period: str,
        page: int,
        page_size: int,
        status: object,
    ):
        """拉取任务分配总览并刷新到桌面页面。"""
        if not self.main_win:
            return
        sw = (sales_wechat_id or "").strip()
        p = (period or "daily").strip() or "daily"
        if not sw:
            self.main_win.show_task_allocation_error("请选择销售微信号")
            return
        try:
            st = str(status).strip() if isinstance(status, str) and str(status).strip() else None
            resp = await self.api.get_tasks_overview(
                period=p,
                sales_wechat_id=sw,
                status=st,
                page=int(page or 1),
                page_size=int(page_size or 0),
            )
        except Exception as e:
            logger.exception(f"拉取任务分配总览失败 sw={sw} period={p}: {e}")
            if self.main_win:
                self.main_win.show_task_allocation_error(f"请求异常: {e}")
            return
        if self.main_win is None:
            return
        if not resp:
            self.main_win.show_task_allocation_error("服务器无响应")
            return
        if resp.get("code") != 200:
            msg = resp.get("message") or resp.get("detail") or f"HTTP {resp.get('code')}"
            self.main_win.show_task_allocation_error(str(msg))
            return
        alloc_page = getattr(self.main_win, "task_allocation_page", None)
        fetch_key = (sw, p, int(page or 1), int(page_size or 0))
        if alloc_page is not None and alloc_page._inflight_fetch_key != fetch_key:
            return
        # 让出事件循环，避免大批量卡片同步构建阻塞 Tab 切换
        await asyncio.sleep(0)
        if self.main_win is None:
            return
        if alloc_page is not None and alloc_page._inflight_fetch_key != fetch_key:
            return
        self.main_win.update_task_allocation_overview(resp.get("data") or {})

    @staticmethod
    def _task_actionable_status(task: dict | None) -> bool:
        if not isinstance(task, dict):
            return False
        status = (task.get("status") or "pending").strip()
        return status in ("pending", "in_progress", "overdue")

    @staticmethod
    def _task_is_wechat_completable(task: dict | None) -> bool:
        if not DesktopApp._task_actionable_status(task):
            return False
        kind = (task.get("task_kind") or "contact").strip()
        channel = (task.get("contact_channel") or "wechat").strip()
        return kind == "icebreaker" or channel != "phone"

    @staticmethod
    def _task_is_phone_completable(task: dict | None) -> bool:
        if not DesktopApp._task_actionable_status(task):
            return False
        kind = (task.get("task_kind") or "contact").strip()
        channel = (task.get("contact_channel") or "").strip()
        return kind != "icebreaker" and channel == "phone"

    async def _try_complete_contact_task(
        self,
        task: dict | None,
        *,
        note: str | None = None,
        success_title: str = "任务已完成",
    ) -> bool:
        """将待办联系任务标记为已完成，并局部刷新任务分配列表。"""
        if not self.main_win or not isinstance(task, dict):
            return False
        try:
            task_id = int(task.get("id"))
        except (TypeError, ValueError):
            return False
        if task_id in self._completing_task_ids:
            return False
        self._completing_task_ids.add(task_id)
        try:
            resp = await self.api.complete_task(task_id, note=note)
        except Exception as e:
            logger.exception(f"完成任务异常 task_id={task_id}: {e}")
            self.main_win.show_info_bar("warning", "操作失败", f"任务 #{task_id} 完成异常")
            return False
        finally:
            self._completing_task_ids.discard(task_id)

        if not resp or resp.get("code") != 200:
            msg = (resp or {}).get("message") or "操作失败，请稍后重试"
            self.main_win.show_info_bar("warning", "操作失败", str(msg))
            return False

        title = (task.get("title") or "").strip()
        tip = f"「{title}」已标记完成" if title else f"任务 #{task_id} 已标记完成"
        self.main_win.show_info_bar("success", success_title, tip)

        page = getattr(self.main_win, "task_allocation_page", None)
        if page is not None and page.patch_task_status(task_id, "done"):
            wb = getattr(self.main_win, "phone_workbench", None)
            if wb is not None and isinstance(getattr(wb, "current_task", None), dict):
                if int(wb.current_task.get("id") or 0) == task_id:
                    wb.patch_task_status("done")
            pending = self.main_win.pending_wechat_task()
            if isinstance(pending, dict) and int(pending.get("id") or 0) == task_id:
                self.main_win.clear_pending_wechat_task()
            return True

        sw = page.current_sales_wechat_id() if page else ""
        period = page.current_period() if page else "daily"
        if sw:
            await self._handle_task_allocation_request(sw, period)
        return True

    @asyncSlot()
    async def _complete_phone_task_only(self):
        """电话工作台外呼临时关闭时，仅标记当前电话主线任务为已完成。"""
        main_win = self.main_win
        if not main_win:
            return
        wb = getattr(main_win, "phone_workbench", None)
        task = wb.current_task if wb is not None else None
        if not self._task_is_phone_completable(task):
            if wb is not None:
                wb.set_complete_task_busy(False)
            main_win.show_info_bar(
                "warning",
                "无法完成任务",
                "当前没有待完成的电话主线任务。",
            )
            return
        try:
            await self._try_complete_contact_task(
                task,
                note="电话工作台已标记完成（外呼临时关闭）",
                success_title="电话任务已完成",
            )
        finally:
            if wb is not None:
                wb.set_complete_task_busy(False)

    async def _complete_phone_task_if_applicable(self):
        """电话工作台点击畅呼/云客外呼后，完成当前电话主线任务。"""
        if not self.main_win:
            return
        wb = getattr(self.main_win, "phone_workbench", None)
        task = wb.current_task if wb is not None else None
        if not self._task_is_phone_completable(task):
            return
        await self._try_complete_contact_task(
            task,
            note="电话工作台已点击拨打",
            success_title="电话任务已完成",
        )

    async def _complete_wechat_task_after_send(self, contact_task: dict | None = None):
        """微信外发确认送达后，完成关联的微信/激活任务。"""
        if not self.main_win:
            return
        task = contact_task if isinstance(contact_task, dict) else self.main_win.pending_wechat_task()
        if not self._task_is_wechat_completable(task):
            return
        await self._try_complete_contact_task(
            task,
            note="微信外发已确认送达",
            success_title="微信任务已完成",
        )

    @asyncSlot(int, str, object)
    async def _handle_task_allocation_action(self, task_id: int, op: str, payload: object):
        """处理任务卡片的「申诉 / 改待办」操作。"""
        if not self.main_win:
            return
        op = (op or "").strip().lower()
        if op not in ("appeal", "restore"):
            return
        try:
            if op == "appeal":
                reason = str(payload or "").strip()
                resp = await self.api.appeal_task(int(task_id), reason=reason)
            else:
                resp = await self.api.restore_task(int(task_id))
        except Exception as e:
            logger.exception(f"任务操作失败 task_id={task_id} op={op}: {e}")
            if self.main_win:
                self.main_win.show_info_bar("warning", "操作失败", f"任务 #{task_id} 操作异常")
            return
        if self.main_win is None:
            return
        if not resp or resp.get("code") != 200:
            msg = (resp or {}).get("message") or "操作失败，请稍后重试"
            self.main_win.show_info_bar("warning", "操作失败", str(msg))
            return
        tip_map = {"appeal": "已申诉", "restore": "已恢复待办"}
        self.main_win.show_info_bar("success", "操作完成", f"任务 #{task_id} {tip_map.get(op, op)}")
        # 本地更新单卡与统计，避免整表重拉导致卡顿
        status_map = {"appeal": "skipped", "restore": "pending"}
        new_status = status_map.get(op)
        page = getattr(self.main_win, "task_allocation_page", None)
        if page is not None and new_status and page.patch_task_status(task_id, new_status):
            return
        sw = page.current_sales_wechat_id() if page else ""
        period = page.current_period() if page else "daily"
        if sw:
            await self._handle_task_allocation_request(sw, period)

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
        
        def _setup_product_card(card, p):
            card.full_copy_requested.connect(self.image_manager.handle_full_copy_image)
            card.copy_finished.connect(
                lambda msg: self.main_win.show_info_bar("success", "复制成功", msg, duration=1500)
            )

        cards = self.main_win.render_product_search_page(
            items,
            clear=(skip == 0),
            has_more=has_more,
            setup_card=_setup_product_card,
        )
        self.image_manager.schedule_product_list_images_deferred(self.main_win.product_list)

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
            
            def _setup_product_card(card_widget, item_data):
                card_widget.full_copy_requested.connect(self.image_manager.handle_full_copy_image)
                card_widget.copy_finished.connect(
                    lambda msg: self.main_win.show_info_bar("success", "复制成功", msg, duration=1500)
                )

            cards = self.main_win.render_product_search_page(
                items,
                clear=(skip == 0),
                has_more=payload.get("has_more", False),
                setup_card=_setup_product_card,
            )
            self.image_manager.schedule_product_list_images_deferred(self.main_win.product_list)

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

    def _handle_chat_cleared(self):
        """当前对话窗口被手动清空时，重置该客户的历史记录加载状态，允许重新拉取"""
        logger.info("对话窗口已清空，重置历史记录加载状态。")
        self._chat_history_skip = 0
        self._has_more_history = True
        self._is_loading_history = False
        self._history_mode_enabled = False

    def _on_chat_scroll_changed(self, value):
        """处理聊天区域滚动条变化，实现上划自动加载更多"""
        if value == 0 and self._has_more_history and not self._is_loading_history and getattr(self, "_history_mode_enabled", False):
            if self._current_customer:
                asyncio.create_task(self._load_more_history())

    async def _handle_history_requested(self):
        """手动点击历史记录按钮

        关键修复：整段包 try/except，避免任何异常逃逸到全局
        handle_async_exception 触发 sys.exit(1)，造成“无报错闪退”体验。
        """
        try:
            if not self._current_customer:
                logger.info("点击历史聊天按钮：当前未选择客户，忽略。")
                return

            logger.info(
                f"开始加载历史聊天 customer_id={self._current_customer.get('id')} "
                f"skip={self._chat_history_skip}"
            )

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
        except Exception as e:
            logger.exception(f"历史聊天加载失败（外层）：{e}")
            try:
                self.main_win.show_info_bar(
                    "warning", "加载失败", "历史聊天记录加载异常，请稍后重试或反馈日志。"
                )
            except Exception:
                pass
            # 解锁状态，避免下次点击被卡住
            self._is_loading_history = False

    async def _load_latest_history_first_page(self, show_toast: bool = True, *, skip_clear: bool = False):
        """首次进入历史模式：拉取最新 20 条并直接展示在对话区。

        关键修复：
          1) 整段 try/except + 详细日志，避免 Qt 渲染异常被全局 handler 杀进程；
          2) 每渲染若干气泡 await 一次 sleep(0)，把控制权交还 Qt 事件循环，
             让 deleteLater()/QTimer.singleShot(0) 等队列分散执行，
             规避一次性插入 20 个带阴影/透明效果的气泡可能触发的 PySide6 段错误。
        """
        if not self._current_customer:
            return
        if self._is_loading_history:
            return

        self._history_mode_enabled = True
        self._is_loading_history = True
        try:
            cid = self._current_customer.get("id")
            phone = self._current_customer.get("phone")
            session_sw = self._current_customer.get("sales_wechat_id")
            if session_sw is not None:
                session_sw = str(session_sw).strip() or None
            limit = 20
            try:
                if cid:
                    history = await self.api.get_chat_history_by_id(
                        cid, limit=limit, skip=0, sales_wechat_id=session_sw
                    )
                else:
                    history = await self.api.get_chat_history(
                        phone, limit=limit, skip=0, sales_wechat_id=session_sw
                    )
            except Exception as e:
                logger.exception(f"拉取历史聊天接口异常：{e}")
                self.main_win.show_info_bar("warning", "网络异常", "拉取历史聊天记录失败，请稍后重试。")
                return

            if not history:
                self._has_more_history = False
                # 如果没有聊天记录，显示欢迎语
                welcome_msg = f"您好，我是您的 AI 业务助理。当前已锁定客户【{self._current_customer.get('customer_name')}】，请问关于这位客户有什么可以帮您？"
                self.main_win.chat_page.add_message(welcome_msg, False)
                self.main_win.chat_page.scroll_to_bottom(instant=True)
                if show_toast:
                    self.main_win.show_info_bar("info", "提示", "暂无历史聊天记录。")
                return

            logger.info(f"历史聊天接口返回 {len(history)} 条记录，开始渲染。")

            # 进入“历史显示模式”：清空当前显示，保证“最新 20 条”可见
            if not skip_clear:
                try:
                    self.main_win.chat_page.clear()
                except Exception as e:
                    logger.exception(f"清空对话区失败：{e}")
                # 让 clear() 内部 deleteLater() 先排空一轮，再开始新建气泡，
                # 避免“正在删除的旧气泡”与“新建中的气泡”同时持有 GraphicsEffect。
                await asyncio.sleep(0)

            rendered = 0
            load_now = datetime.now()
            chat_container = self.main_win.chat_page.chat_container
            chat_container.setUpdatesEnabled(False)
            try:
                for idx, msg in enumerate(history):
                    try:
                        role = msg.get("role")
                        content = msg.get("content")
                        msg_id = msg.get("id")
                        rating = msg.get("rating", 0)
                        chat_model = (msg.get("chat_model") or "").strip()
                        is_user = (role == "user")
                        time_text = format_message_time(msg.get("created_at"), now=load_now)
                        self.main_win.chat_page.add_message(
                            content,
                            is_user=is_user,
                            msg_id=msg_id,
                            rating=rating,
                            user_query="",
                            model_tag=chat_model if not is_user else "",
                            message_time_text=time_text,
                        )
                        rendered += 1
                        # 每 5 条让出一次事件循环，分摊 layout / effect 的渲染压力
                        if (idx + 1) % 5 == 0:
                            await asyncio.sleep(0)
                    except Exception as e:
                        logger.exception(f"渲染第 {idx} 条历史消息失败 (msg_id={msg.get('id')})：{e}")
                        continue
            finally:
                chat_container.setUpdatesEnabled(True)
            await asyncio.sleep(0)

            self._chat_history_skip = rendered
            self._has_more_history = rendered >= limit
            self._history_mode_enabled = True

            # 展示最新一页后吸底，用户一眼看到“最近对话”
            try:
                self.main_win.chat_page.scroll_to_bottom(instant=True)
            except Exception as e:
                logger.exception(f"滚动到底部失败：{e}")

            if show_toast:
                self.main_win.show_info_bar(
                    "success",
                    "已显示历史记录",
                    f"已加载最新 {rendered} 条，上划可加载更早记录。",
                )
        except Exception as e:
            logger.exception(f"_load_latest_history_first_page 总体失败：{e}")
            try:
                self.main_win.show_info_bar(
                    "warning", "加载失败", "历史聊天记录渲染异常，请反馈日志。"
                )
            except Exception:
                pass
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
            load_now = datetime.now()
            chat_container = self.main_win.chat_page.chat_container
            chat_container.setUpdatesEnabled(False)
            try:
                for msg in reversed(history):
                    role = msg.get("role")
                    content = msg.get("content")
                    msg_id = msg.get("id")
                    rating = msg.get("rating", 0)
                    chat_model = (msg.get("chat_model") or "").strip()
                    is_user = (role == "user")
                    time_text = format_message_time(msg.get("created_at"), now=load_now)

                    self.main_win.chat_page.prepend_message(
                        content,
                        is_user=is_user,
                        msg_id=msg_id,
                        rating=rating,
                        user_query="",
                        model_tag=chat_model if not is_user else "",
                        message_time_text=time_text,
                    )
            finally:
                chat_container.setUpdatesEnabled(True)

            # 让出事件循环完成布局，再计算滚动条偏移（避免 processEvents 重入）
            await asyncio.sleep(0)

            # 默认保持视窗位置（适合“上拉自动加载更多”）；手动点击则切到顶部让用户立刻看见变化
            if keep_viewport_position:
                def restore_scroll():
                    new_max = bar.maximum()
                    target_val = old_val + (new_max - old_max)
                    bar.setValue(target_val)
                    if hasattr(self.main_win.chat_page.scroll_area, "delegate"):
                        self.main_win.chat_page.scroll_area.delegate.vScrollBar.scrollTo(target_val, useAni=False)

                restore_scroll()
                # 异步于当次事件循环尾部再进行一次精准定位，防范子部件大小改变引发的位置偏移
                QTimer.singleShot(0, restore_scroll)
            else:
                bar.setValue(0)
                if hasattr(self.main_win.chat_page.scroll_area, "delegate"):
                    self.main_win.chat_page.scroll_area.delegate.vScrollBar.scrollTo(0, useAni=False)
                if show_loaded_hint:
                    self.main_win.show_info_bar("success", "已显示历史记录", "已加载历史聊天记录（可继续向上滑动加载更多）。")

        except Exception as e:
            logger.error(f"加载更多历史记录失败: {e}")
        finally:
            self.main_win.chat_page._is_batch_loading = False
            self.main_win.chat_page._is_prepending = False
            self._is_loading_history = False


if __name__ == "__main__":
    if getattr(sys, "frozen", False) and sys.platform.startswith("win") and not acquire_app_mutex():
        # 安装/更新进行中用户常会再次双击图标；与 updater 的锁文件形成双保险
        if activate_existing_instance():
            sys.exit(0)
        try:
            qt_early = QApplication(sys.argv)
            QMessageBox.warning(
                None,
                "客户端已在运行",
                "检测到本程序已在运行，或正在安装更新。\n\n"
                "请勿重复打开。若正在安装，请等待更新完成。",
            )
        except Exception:
            pass
        sys.exit(0)

    # 初始化 Qt 程序
    qt_app = QApplication(sys.argv)
    apply_app_typography(qt_app)

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

        # 关键：loguru 默认 enqueue=True，必须先把后台队列刷盘，
        # 否则 sys.exit(1) 会让用户看到“无报错闪退”。
        _flush_logs_blocking()

        if QApplication.instance():
            QApplication.instance().exit(1)
        sys.exit(1)

    event_loop.set_exception_handler(handle_async_exception)
    _desktop_app_holder = {"app": None}

    def _shutdown_ui_background_workers() -> None:
        app = _desktop_app_holder.get("app")
        main_win = getattr(app, "main_win", None) if app else None
        if main_win is not None and hasattr(main_win, "shutdown_background_workers"):
            try:
                main_win.shutdown_background_workers()
            except Exception:
                pass

    def _request_app_shutdown() -> None:
        _shutdown_ui_background_workers()
        try:
            event_loop.stop()
        except Exception:
            pass

    qt_app.aboutToQuit.connect(_request_app_shutdown)

    with event_loop:
        async def bootstrap():
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
                        _shutdown_ui_background_workers()
                        try:
                            await app.image_manager.close()
                        except Exception:
                            pass
                        try:
                            await app.api.aclose()
                        except Exception:
                            pass
                finally:
                    _shutdown_ui_background_workers()
                    # 2) 取消仍在跑的 asyncio 任务，避免退出时悬挂
                    try:
                        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
                        for t in pending:
                            t.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                    except Exception:
                        pass

                    # 3) 默认 ThreadPoolExecutor 的工作线程是“非 daemon”的，
                    #    若微信 RPA UIA 调用还卡在 COM 里，atexit 会无限期 join，
                    #    用户感觉就是“关掉桌面端进程仍然在”。这里主动放弃等待。
                    try:
                        loop = asyncio.get_event_loop()
                        executor = getattr(loop, "_default_executor", None)
                        if executor is not None:
                            try:
                                executor.shutdown(wait=False, cancel_futures=True)
                            except TypeError:
                                # Python < 3.9 没有 cancel_futures 参数
                                executor.shutdown(wait=False)
                    except Exception:
                        pass
                    # concurrent.futures.thread._python_exit 会遍历该 dict
                    # join 所有未结束线程；清空它即可让进程立即退出。
                    try:
                        import concurrent.futures.thread as _cf_thread
                        _cf_thread._threads_queues.clear()
                    except Exception:
                        pass
                    try:
                        _flush_logs_blocking()
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
            try:
                _flush_logs_blocking()
            except Exception:
                pass
