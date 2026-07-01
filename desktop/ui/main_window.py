"""
主窗口：MainWindow
所有可复用子组件已拆分至各自模块：
  - ui/chat_widgets.py      → QuickTextEdit / ChatActionToolbar / ChatBubble / AIChatWidget
  - ui/customer_info.py     → CustomerInfoWidget
  - ui/phone_workbench.py   → PhoneWorkbenchWidget（右侧电话工作台）
  - ui/widgets/search.py    → SearchTag / TagLineEdit / TagSearchWidget
  - ui/widgets/product_card.py → ProductItemWidget
  - ui/widgets/form_controls.py → MultiSelectComboBox / NoScrollComboBox / CalendarPopup / DatePickerBtn
  - ui/widgets/cascader.py  → CascaderPopup / RegionCascader
"""
import ctypes
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidgetItem, QLabel, QFrame,
    QStackedWidget, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QListView, QAbstractItemView,
    QTreeWidgetItem, QMenu,
    QSplitter, QSizePolicy, QPushButton,
)
from PySide6.QtCore import (
    Qt, Signal, QSize, QTimer, QSettings, QUrl, QEvent,
    QPropertyAnimation, QEasingCurve, QRect, QParallelAnimationGroup,
)
from PySide6.QtGui import QColor, QGuiApplication, QFontMetrics, QAction, QActionGroup, QCloseEvent
from logger_cfg import logger
from config_loader import cfg
from app_identity import DISPLAY_NAME

from qfluentwidgets import (
    ListWidget,
    BodyLabel, CaptionLabel, SubtitleLabel, StrongBodyLabel,
    TransparentPushButton, TransparentToolButton, ToolButton,
    PrimaryPushButton, PushButton, LineEdit,
    FluentIcon, isDarkTheme, SearchLineEdit,
    setTheme, Theme, InfoBar, InfoBarPosition,
    ToolTipFilter, ToolTipPosition, IndeterminateProgressRing,
)
from ui.app_fonts import label_qss, style_label

from ui.chat_widgets import AIChatWidget
from ui.app_icons import AppIcon
from ui.customer_tree_widget import CompactCustomerTreeWidget
from ui.customer_info import CustomerInfoWidget
from ui.phone_workbench import PhoneWorkbenchWidget
from ui.widgets.product_card import ProductItemWidget, ProductLoadMoreButton, ProductLoadMoreRow
from ui.widgets.search import TagSearchWidget
from ui.widgets.filter_bar import ProductFilterBar
from ui.widgets import safe_card_width
from ui.widgets.order_card import OrderCardWidget
from ui.widgets.task_allocation_page import TaskAllocationWidget
from ui.widgets.customer_leads_page import CustomerLeadsWidget
from ui.customer_list_grouping import CUSTOMER_SIDEBAR_GROUP_BUILDER, customer_task_key
from ui.widgets.skeleton import ListSkeletonPanel
from utils import mask_phone


CUSTOMER_GROUP_PAGE_SIZE = 20
# 侧栏定位客户时，超过该条数的增量改为分帧追加，避免一次性创建大量 widget 卡死 UI
CUSTOMER_SELECT_SYNC_EXPAND_MAX = 60
# 超过该条数时分组计算放到后台线程，主线程只负责建树首屏
CUSTOMER_TREE_BG_GROUP_THRESHOLD = 150
_CUSTOMER_FINGERPRINT_KEYS = (
    "id",
    "sales_wechat_id",
    "customer_name",
    "unit_name",
    "phone",
    "wechat_remark",
    "sales_wechat_label",
    "suggested_followup_date",
    "has_ai_profile",
    "profile_tags",
)
CUSTOMER_GROUP_STATE_ROLE = Qt.UserRole + 1
CUSTOMER_ROW_KIND_ROLE = Qt.UserRole + 2
CUSTOMER_ROW_KIND_LOAD_MORE = "load_more"
# update_customer_list 的 today_task_order 默认哨兵：区分“未传入(沿用现有)”与“显式设置(含 None/空列表)”
_TODAY_ORDER_UNSET = object()


def _customers_list_fingerprint(customers: list) -> str:
    """侧栏列表内容指纹：数据未变时跳过重绘。"""
    h = hashlib.md5()
    for c in sorted(
        customers or [],
        key=lambda x: (str(x.get("id") or ""), str(x.get("sales_wechat_id") or "")),
    ):
        blob = {k: c.get(k) for k in _CUSTOMER_FINGERPRINT_KEYS}
        h.update(
            json.dumps(blob, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        )
    return h.hexdigest()


def _group_header_depth(parent_item: QTreeWidgetItem | None) -> int:
    """树节点深度 → 分组标题视觉层级（不依赖 QTreeWidget 缩进）。"""
    if parent_item is None:
        return 0
    depth = 0
    node = parent_item
    while node is not None:
        depth += 1
        node = node.parent()
    return depth


class CustomerGroupHeaderWidget(QWidget):
    """分组标题：按可用宽度做 elide 省略 + 悬停走马灯（窄侧栏自适应）。"""

    _LBL_RIGHT_MARGIN = 2
    # 文本两侧再预留一点 padding，避免被裁切贴边
    _TEXT_SIDE_PADDING = 8

    def __init__(self, parent=None, depth: int = 0):
        super().__init__(parent)
        self._depth = max(0, int(depth or 0))
        self._lbl_left_margin = 2 + min(self._depth, 2) * 4
        layout = QHBoxLayout(self)
        # 客户侧栏分组标题：不展示图标；用控件内边距表达层级，而非树缩进
        layout.setContentsMargins(self._lbl_left_margin, 4, self._LBL_RIGHT_MARGIN, 2)
        layout.setSpacing(0)
        self._lbl = CaptionLabel("")
        layout.addWidget(self._lbl, 1)
        self._full = ""
        self._display = ""
        self._available_width: int | None = None
        self._marquee_win = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_marquee)
        self._offset = 0
        self._apply_theme_style()

    def set_heading(self, text: str):
        self._timer.stop()
        self._full = text or ""
        self._refresh_display()

    def heading_text(self) -> str:
        return self._full

    def set_available_width(self, w: int):
        """根据可用宽度做省略显示（与客户项保持一致的窄侧栏体验）。"""
        self._available_width = max(40, int(w or 0))
        self._refresh_display()

    def _text_width(self) -> int:
        if self._available_width is None:
            return 0
        return max(
            20,
            int(self._available_width)
            - self._lbl_left_margin
            - self._LBL_RIGHT_MARGIN
            - self._TEXT_SIDE_PADDING,
        )

    def _refresh_display(self):
        text_w = self._text_width()
        fm = QFontMetrics(self._lbl.font())
        if text_w <= 0:
            # 宽度还没就绪：先全量显示，等 _sync 后再 elide
            self._display = self._full
            self._marquee_win = len(self._full)
        else:
            self._display = fm.elidedText(self._full, Qt.ElideRight, text_w)
            avg = max(1, fm.averageCharWidth())
            self._marquee_win = max(4, min(40, int(text_w / avg)))
        self._lbl.setText(self._display)

    def enterEvent(self, event):
        # 仅当确实被省略（与全量不同）时才滚动
        if self._full and self._display != self._full:
            self._offset = 0
            self._timer.start(220)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._timer.stop()
        self._lbl.setText(self._display)
        super().leaveEvent(event)

    def _tick_marquee(self):
        if not self._full:
            return
        self._offset += 1
        win = max(4, int(self._marquee_win or len(self._full)))
        text = self._full + "   "
        idx = self._offset % len(text)
        self._lbl.setText((text + text)[idx : idx + win])

    def _apply_theme_style(self):
        role = "sidebar_group" if self._depth <= 0 else "sidebar_group_sub"
        style_label(self._lbl, role)
        self.setStyleSheet("background-color: transparent;")


class FloatingGroupHeader(QPushButton):
    """悬浮在客户列表最上方的组标题，点击可直接收起对应分组。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        # 布局：靠左对齐文本，靠右显示折叠图标
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        
        self.title_label = QLabel()
        style_label(self.title_label, "caption_emphasis")

        self.icon_label = QLabel("▲")
        style_label(self.icon_label, "micro")
        
        layout.addWidget(self.title_label, 1, Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.icon_label, 0, Qt.AlignRight | Qt.AlignVCenter)
        
        self.hide() # 默认隐藏
        self._apply_theme_style()

    def set_text(self, text: str):
        self.title_label.setText(text)

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg_color = "rgba(32, 37, 43, 0.95)" if is_dark else "rgba(245, 245, 245, 0.95)"
        border_color = "#f6f7f9" if is_dark else "#20252b"
        hover_bg = "#2b323a" if is_dark else "#eceef2"

        style_label(self.title_label, "caption_emphasis")
        style_label(self.icon_label, "micro")
        
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg_color};
                border: none;
                border-bottom: 1px solid {border_color};
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: {hover_bg};
            }}
        """)


class CustomerItemWidget(QWidget):
    """自定义客户列表项：双行展示 (单位 + 姓名/电话)"""
    def __init__(self, customer_data, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        # 减少右边距 (从 22 减少到 10) 以缩小与分割线的间距，同时保留足够滚动条空间
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(2)
        
        # 第一行：单位名称
        unit_name = customer_data.get("unit_name") or customer_data.get("unit_type") or "未知单位"
        self.unit_lbl = BodyLabel(unit_name)
        
        # 第二行：姓名 + 脱敏电话
        name = customer_data.get("customer_name") or "未知"
        phone = str(customer_data.get("phone") or "")
        masked_phone = mask_phone(phone)
            
        self.info_lbl = CaptionLabel(f"{name} | {masked_phone}")
        
        self.full_unit = unit_name
        # 悬浮也不展示明文电话
        self.full_info = f"{name} | {masked_phone}"
        self.display_unit = unit_name
        self._base_info = f"{name} | {masked_phone}"
        self.display_info = self._base_info
        
        self.unit_lbl.setText(self.display_unit)
        self.info_lbl.setText(self.display_info)
        
        self._available_width = None
        self._marquee_timer = QTimer(self)
        self._marquee_timer.timeout.connect(self._tick_marquee)
        self._marquee_offset = 0
        self._unit_win = 0
        self._info_win = 0
        self._unit_marquee_on = False
        self._info_marquee_on = False
        
        # 存储搜索文本 (单位 + 姓名 + 原始电话)
        self.search_text = f"{unit_name} {name} {phone}".lower()
        
        layout.addWidget(self.unit_lbl)
        layout.addWidget(self.info_lbl)
        
        # 应用初始样式
        self._apply_theme_style()

    def _apply_theme_style(self):
        style_label(self.unit_lbl, "sidebar_primary")
        style_label(self.info_lbl, "sidebar_secondary")
        self.setStyleSheet("background-color: transparent;")

    def set_available_width(self, w: int):
        """根据可用宽度做省略显示（比按字数截断更贴合窄侧栏）。"""
        self._available_width = max(50, int(w or 0))
        # 预留少量 padding (10+10 边距 + 少量容错)
        text_w = max(20, self._available_width - 16)
        fm1 = QFontMetrics(self.unit_lbl.font())
        fm2 = QFontMetrics(self.info_lbl.font())
        self.display_unit = fm1.elidedText(self.full_unit, Qt.ElideRight, text_w)
        self.display_info = fm2.elidedText(self._base_info, Qt.ElideRight, text_w)
        self.unit_lbl.setText(self.display_unit)
        self.info_lbl.setText(self.display_info)

        # 估算滚动窗口字符数（用于 hover 走马灯）
        avg1 = max(1, fm1.averageCharWidth())
        avg2 = max(1, fm2.averageCharWidth())
        self._unit_win = max(6, min(24, int(text_w / avg1)))
        self._info_win = max(8, min(30, int(text_w / avg2)))

    def enterEvent(self, event):
        """鼠标进入：文字过长则走马灯滚动，保持窄侧栏可读。"""
        self._marquee_offset = 0
        self._unit_marquee_on = bool(self._unit_win and len(self.full_unit) > self._unit_win)
        self._info_marquee_on = bool(self._info_win and len(self.full_info) > self._info_win)

        if self._unit_marquee_on or self._info_marquee_on:
            # 先立即刷新一次，避免等待首个 tick
            self._tick_marquee()
            self._marquee_timer.start(220)
        else:
            # 不滚动时，至少在 hover 显示全量
            self.unit_lbl.setText(self.full_unit)
            self.info_lbl.setText(self.full_info)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标移开：停止走马灯，恢复省略显示。"""
        self._marquee_timer.stop()
        self._unit_marquee_on = False
        self._info_marquee_on = False
        self.unit_lbl.setText(self.display_unit)
        self.info_lbl.setText(self.display_info)
        super().leaveEvent(event)

    def _tick_marquee(self):
        self._marquee_offset += 1

        if self._unit_marquee_on:
            text = (self.full_unit or "") + "   "
            if text:
                idx = self._marquee_offset % len(text)
                show = (text + text)[idx : idx + self._unit_win]
                self.unit_lbl.setText(show)

        if self._info_marquee_on:
            text = (self.full_info or "") + "   "
            if text:
                idx = self._marquee_offset % len(text)
                show = (text + text)[idx : idx + self._info_win]
                self.info_lbl.setText(show)

class MainWindow(QMainWindow):
    """
    主窗口：极致窄屏适配 (430x720)。
    导航：极窄全局左侧导航栏 + 中央内容区 + 右侧可展开抽屉。
    """
    search_requested = Signal(str, int, int)
    customer_selected = Signal(dict)
    sync_triggered = Signal()          # 手动触发同步信号
    tab_changed = Signal(int)          # 标签切换信号
    # raw_customer_id 可能为字符串（如 wxid_... / openim / 数字字符串），用 object 避免强转成 0
    order_history_requested = Signal(object)  # 请求加载订单流水（传入 raw_customer_id）
    filter_requested = Signal(dict, int, int) # [filters, skip, limit]
    shop_metadata_refresh_requested = Signal(str) # 联动信号：传递店铺名
    ui_data_refresh_requested = Signal() # [NEW] 请求刷新本地客户数据（非全量云同步）
    sales_bindings_refresh_requested = Signal()
    sales_binding_add_requested = Signal(str)
    sales_binding_delete_requested = Signal(int)
    sales_binding_primary_requested = Signal(int)
    claim_local_wechat_requested = Signal()
    mibuddy_binding_refresh_requested = Signal()
    mibuddy_binding_bind_requested = Signal(str)
    mibuddy_binding_unbind_requested = Signal()
    manual_import_requested = Signal(str) # [NEW] 请求导入手动跟进名单 (文件路径)
    clear_manual_requested = Signal()    # [NEW] 一键清空手动导入名单
    # "staff" = 自由对话（隐藏客户列表）； "customer" = 客户对话
    chat_surface_mode_changed = Signal(str)
    # 任务分配：拉取/操作
    task_allocation_request = Signal(str, str, int, int, object)  # (sales_wechat_id, period, page, page_size, status)
    task_allocation_action = Signal(int, str, object)   # (task_id, op, payload)
    task_open_customer_chat = Signal(dict)      # 任务卡片 → 客户对话
    task_open_customer_phone = Signal(dict)     # 电话主线 → 联系电话面板
    task_wechat_send_requested = Signal(dict, bool)  # 激活卡片 → 发微信

    def __init__(self, username: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{DISPLAY_NAME} - {username}")
        self.setMinimumSize(430, 600)
        self.resize(430, 720)
        self.setObjectName("MainWindow")

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.root_h_layout = QHBoxLayout(central_widget)
        self.root_h_layout.setContentsMargins(0, 0, 0, 0)
        self.root_h_layout.setSpacing(0)

        # ── 1. 极窄全局左侧导航 (Global Nav) ──
        self.global_nav = QWidget()
        self.global_nav.setObjectName("GlobalNav")
        self.global_nav.setFixedWidth(48)
        self._apply_global_nav_style()
        nav_v_layout = QVBoxLayout(self.global_nav)
        nav_v_layout.setContentsMargins(0, 20, 0, 20)
        nav_v_layout.setSpacing(15)

        # 封装工具按钮，强制白色图标
        def create_nav_btn(icon, tooltip):
            colored_icon = icon.icon(color=Qt.white)
            btn = TransparentToolButton(colored_icon)
            btn.setToolTip(tooltip)
            btn.installEventFilter(ToolTipFilter(btn, showDelay=300, position=ToolTipPosition.RIGHT))
            btn.setFixedSize(48, 48)
            btn.setIconSize(QSize(20, 20))
            return btn

        _staff_icon = FluentIcon.QUESTION if hasattr(FluentIcon, "Question") else FluentIcon.QUESTION
        self.btn_nav_leads = create_nav_btn(FluentIcon.PHONE, "客资列表")
        self.btn_nav_task = create_nav_btn(AppIcon.TASK_LIST, "任务分配")
        self.btn_nav_staff = create_nav_btn(_staff_icon, "自由对话（不选客户）")
        self.btn_nav_chat = create_nav_btn(FluentIcon.CHAT, "客户对话")
        self.btn_nav_shop = create_nav_btn(FluentIcon.SHOPPING_CART, "商品货源")
        self.btn_nav_settings = create_nav_btn(FluentIcon.SETTING, "销售微信号")


        self.btn_snap_wechat = create_nav_btn(FluentIcon.PIN, "窗口收纳吸附")
        self.btn_snap_wechat.setContextMenuPolicy(Qt.CustomContextMenu)
        self.btn_snap_wechat.customContextMenuRequested.connect(self._start_calibration)
        self.btn_snap_wechat.clicked.connect(self._toggle_snap)

        self.btn_theme_toggle = create_nav_btn(FluentIcon.CONSTRACT, "切换主题模式")
        self.btn_theme_toggle.clicked.connect(self._toggle_theme)

        self.logout_btn = create_nav_btn(FluentIcon.POWER_BUTTON, "安全退出")

        # 桌面端左侧导航栏按钮
        nav_v_layout.addWidget(self.btn_nav_leads)
        nav_v_layout.addWidget(self.btn_nav_task)
        nav_v_layout.addWidget(self.btn_nav_staff)
        nav_v_layout.addWidget(self.btn_nav_chat)
        nav_v_layout.addWidget(self.btn_nav_shop)
        nav_v_layout.addWidget(self.btn_nav_settings)
        nav_v_layout.addStretch()
        nav_v_layout.addWidget(self.btn_snap_wechat)
        nav_v_layout.addWidget(self.btn_theme_toggle) # 主题切换
        nav_v_layout.addWidget(self.logout_btn)

        self.root_h_layout.addWidget(self.global_nav)

        # ── 2. 主功能区 (Center Panel) ──
        self.center_panel = QWidget()
        self.center_panel.setObjectName("CenterPanel")
        center_layout = QVBoxLayout(self.center_panel)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)

        self.center_stack = QStackedWidget()

        # --- 2.1 对话模块 (Chat Module) ---
        self.chat_module = QWidget()
        self.chat_module.setObjectName("ChatModule")
        chat_module_layout = QHBoxLayout(self.chat_module)
        chat_module_layout.setContentsMargins(0, 0, 0, 0)
        chat_module_layout.setSpacing(0)

        # 左栏：侧边栏 (客户列表)
        # 使用 QSplitter 支持拖拽改宽：sidebar 仅设置最小/最大宽度，
        # 由用户拖拽 splitter 分隔条来调节实际宽度，并持久化到 config.ini
        self.sidebar = QWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setMinimumWidth(90)
        self.sidebar.setMaximumWidth(420)
        self.sidebar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        # 读取持久化的侧栏宽度（首次或异常时回退到 110）
        try:
            saved_w = int(cfg.config.get("Runtime", "sidebar_width", fallback="110") or 110)
        except Exception:
            saved_w = 110
        self._sidebar_pref_width = max(90, min(420, saved_w))
        self._apply_sidebar_style()
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(0, 8, 0, 8)
        sidebar_layout.setSpacing(6)

        # 客户搜索框
        self.customer_search = SearchLineEdit()
        self.customer_search.setObjectName("CustomerSearch")
        self.customer_search.setPlaceholderText("搜索...")
        # 让搜索框随侧栏宽度自适应，避免窄屏下出现左右溢出/对不齐
        self.customer_search.setMinimumWidth(0)
        # 防抖：停止输入 300ms 后才执行过滤，避免每个按键都触发整组列表项重建
        self._customer_search_debounce = QTimer(self)
        self._customer_search_debounce.setSingleShot(True)
        self._customer_search_debounce.setInterval(300)
        self._customer_search_debounce.timeout.connect(
            lambda: self._filter_customers(self.customer_search.text())
        )
        self.customer_search.textChanged.connect(
            lambda _t: self._customer_search_debounce.start()
        )

        # 原始客户池：条件筛选（放到搜索旁边）
        self._customer_pool_filter_mode = "all"
        # 用 ToolButton 承载 menu（避免部分版本 TransparentToolButton 缺少 popupMode）
        self.btn_customer_filter = ToolButton(FluentIcon.FILTER)
        self.btn_customer_filter.setObjectName("CustomerFilterBtn")
        self.btn_customer_filter.setToolTip("客户筛选")
        self.btn_customer_filter.installEventFilter(
            ToolTipFilter(self.btn_customer_filter, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.btn_customer_filter.setFixedSize(30, 30)
        self.btn_customer_filter.setIconSize(QSize(16, 16))
        self._init_customer_pool_filter_menu()
        
        self.btn_import_manual = ToolButton(FluentIcon.DOCUMENT)
        self.btn_import_manual.setObjectName("ImportManualBtn")
        self.btn_import_manual.setToolTip("导入本周跟进(Excel)")
        self.btn_import_manual.installEventFilter(
            ToolTipFilter(self.btn_import_manual, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.btn_import_manual.setFixedSize(30, 30)
        self.btn_import_manual.setIconSize(QSize(16, 16))
        self.btn_import_manual.clicked.connect(self._on_import_manual_clicked)
        # 需求：隐藏“手动导入本周跟进客户”入口（搜索栏下方）
        self.btn_import_manual.hide()

        self.btn_clear_manual = ToolButton(FluentIcon.BROOM) if hasattr(FluentIcon, "BROOM") else ToolButton(FluentIcon.DELETE)
        self.btn_clear_manual.setObjectName("ClearManualBtn")
        self.btn_clear_manual.setToolTip("清空本周导入名单")
        self.btn_clear_manual.installEventFilter(
            ToolTipFilter(self.btn_clear_manual, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.btn_clear_manual.setFixedSize(30, 30)
        self.btn_clear_manual.setIconSize(QSize(16, 16))
        self.btn_clear_manual.clicked.connect(self.clear_manual_requested.emit)
        # 与导入按钮配套隐藏，避免残留“清空导入名单”孤立入口
        self.btn_clear_manual.hide()

        # 第一排：搜索框
        search_row1 = QWidget()
        search_row1.setObjectName("SidebarSearchRow1")
        search_row1_l = QHBoxLayout(search_row1)
        search_row1_l.setContentsMargins(6, 0, 6, 0)
        search_row1_l.setSpacing(0)
        search_row1_l.addWidget(self.customer_search)
        search_row1_l.addWidget(self.btn_customer_filter)
        sidebar_layout.addWidget(search_row1)

        # 第二排：操作按钮
        search_row2 = QWidget()
        search_row2.setObjectName("SidebarSearchRow2")
        search_row2_l = QHBoxLayout(search_row2)
        search_row2_l.setContentsMargins(6, 0, 6, 0)
        search_row2_l.setSpacing(4)
        # 手动导入入口已隐藏：不再加入布局，避免占位
        # search_row2_l.addWidget(self.btn_import_manual)
        # search_row2_l.addWidget(self.btn_clear_manual)

        
        # search_row2_l.addWidget(self.btn_customer_filter)
        search_row2_l.addStretch()
        sidebar_layout.addWidget(search_row2)


        self.customer_list = CompactCustomerTreeWidget()
        if cfg.lite_mode:
            self.customer_list.setAnimated(False)
        self.customer_list.itemClicked.connect(self._on_customer_tree_item_clicked)

        self._customer_list_stack = QStackedWidget()
        self._customer_list_stack.setObjectName("CustomerListStack")
        self.customer_list_loading = QWidget()
        list_loading_layout = QVBoxLayout(self.customer_list_loading)
        list_loading_layout.setContentsMargins(0, 0, 0, 0)
        list_loading_layout.setSpacing(0)
        self._customer_list_skeleton = ListSkeletonPanel(
            row_count=8,
            row_height=44,
            row_spacing=6,
            margins=(6, 16, 6, 8),
            compact=True,
            parent=self.customer_list_loading,
        )
        list_loading_layout.addWidget(self._customer_list_skeleton, 1)
        self._customer_list_stack.addWidget(self.customer_list_loading)
        self._customer_list_stack.addWidget(self.customer_list)
        self._customer_list_stack.setCurrentIndex(0)
        self._customer_list_skeleton.start()
        sidebar_layout.addWidget(self._customer_list_stack)
        # 移除 sidebar_layout.addStretch() 以允许 ListWidget 铺满垂直空间

        # 悬浮在客户列表最上方的分组标题（用于直接收起）
        self._floating_target_item = None
        self.floating_group_header = FloatingGroupHeader(self.customer_list)
        self.floating_group_header.clicked.connect(self._on_floating_header_clicked)
        self.customer_list.verticalScrollBar().valueChanged.connect(self._update_floating_group_header)
        self.customer_list.itemExpanded.connect(lambda: QTimer.singleShot(0, self._update_floating_group_header))
        self.customer_list.itemCollapsed.connect(lambda: QTimer.singleShot(0, self._update_floating_group_header))

        # 右栏：对话区
        self.chat_area = QWidget()
        self.chat_area.setObjectName("ChatArea")
        self.chat_area.setMinimumWidth(240)
        chat_area_layout = QVBoxLayout(self.chat_area)
        chat_area_layout.setContentsMargins(0, 0, 0, 0)
        chat_area_layout.setSpacing(0)

        # 顶栏：Action Bar (右上角功能图标)
        self.action_bar = QWidget()
        self.action_bar.setObjectName("ChatActionBar")
        self.action_bar.setFixedHeight(45)
        action_layout = QHBoxLayout(self.action_bar)
        action_layout.setContentsMargins(15, 2, 10, 2)
        action_layout.setSpacing(10)

        # 对话头实时信息 (双行)
        self.header_info_container = QWidget()
        hi_layout = QVBoxLayout(self.header_info_container)
        hi_layout.setContentsMargins(0, 4, 0, 4)
        hi_layout.setSpacing(0)
        
        self.lbl_header_unit = StrongBodyLabel("")
        style_label(self.lbl_header_unit, "body_emphasis")
        self.lbl_header_unit.setFixedWidth(200) # 限制宽度防止抖动

        self.lbl_header_info = CaptionLabel("")
        style_label(self.lbl_header_info, "caption")
        
        hi_layout.addWidget(self.lbl_header_unit)
        hi_layout.addWidget(self.lbl_header_info)
        action_layout.addWidget(self.header_info_container)

        action_layout.addStretch()

        self.btn_action_phone = TransparentToolButton(FluentIcon.PHONE)
        self.btn_action_phone.setToolTip("电话工作台")
        self.btn_action_phone.installEventFilter(ToolTipFilter(self.btn_action_phone, 300, ToolTipPosition.BOTTOM))

        self.btn_action_order = TransparentToolButton(FluentIcon.SHOPPING_CART)
        self.btn_action_order.setToolTip("订单信息")
        self.btn_action_order.installEventFilter(ToolTipFilter(self.btn_action_order, 300, ToolTipPosition.BOTTOM))

        self.btn_action_info = TransparentToolButton(AppIcon.PROFILE)
        self.btn_action_info.setToolTip("客户详细资料")
        self.btn_action_info.installEventFilter(ToolTipFilter(self.btn_action_info, 300, ToolTipPosition.BOTTOM))

        action_layout.addWidget(self.btn_action_phone)
        action_layout.addWidget(self.btn_action_order)
        action_layout.addWidget(self.btn_action_info)

        chat_area_layout.addWidget(self.action_bar)

        self.chat_page = AIChatWidget()
        chat_area_layout.addWidget(self.chat_page)

        # 使用 QSplitter 实现侧栏可拖拽改宽：
        # - 仅 sidebar 一侧可被拖动；chat_area 自动随窗口宽度伸缩
        # - 持久化最近一次拖动的侧栏宽度到 config.ini，下次启动复原
        self.chat_splitter = QSplitter(Qt.Horizontal, self.chat_module)
        self.chat_splitter.setObjectName("ChatSplitter")
        self.chat_splitter.setHandleWidth(2)
        self.chat_splitter.setChildrenCollapsible(False)
        self.chat_splitter.addWidget(self.sidebar)
        self.chat_splitter.addWidget(self.chat_area)
        self.chat_splitter.setStretchFactor(0, 0)
        self.chat_splitter.setStretchFactor(1, 1)
        self.chat_splitter.setCollapsible(0, False)
        self.chat_splitter.setCollapsible(1, False)
        self.chat_splitter.splitterMoved.connect(self._on_sidebar_splitter_moved)
        self._apply_chat_splitter_style()
        # 初始尺寸：等到布局完成后再 setSizes 以避免 0 宽度生效
        QTimer.singleShot(0, self._apply_sidebar_pref_width)

        # 侧栏拖拽"溢出即扩窗"：当窗口窄到分隔条无法继续右移时，
        # 直接把整个主窗口向右扩宽，让用户可以继续把客户列表拉宽
        self._sidebar_drag_active = False
        self._sidebar_drag_start_global_x = 0
        self._sidebar_drag_start_w = 0
        try:
            _handle = self.chat_splitter.handle(1)
            if _handle is not None:
                _handle.installEventFilter(self)
        except Exception:
            pass

        chat_module_layout.addWidget(self.chat_splitter)
        chat_module_layout.setStretch(0, 1)
        self.center_stack.addWidget(self.chat_module)

        # --- 2.2 商品模块 (Product Module) ---
        self.product_page = QWidget()
        self.product_page.setObjectName("ProductPage")
        prod_layout = QVBoxLayout(self.product_page)
        prod_layout.setContentsMargins(0, 0, 0, 0)
        prod_layout.setSpacing(0)

        header_container = QWidget()
        header_layout = QVBoxLayout(header_container)
        header_layout.setContentsMargins(15, 8, 15, 0) # 底部边距设为 0，防止与列表产生间隙
        header_layout.setSpacing(0) # 内部间距由 addSpacing 精准控制

        self.search_input = TagSearchWidget()
        self.search_input.search_triggered.connect(self._on_search_clicked)
        self.search_input.filter_clicked.connect(self._toggle_filter_bar)
        header_layout.addWidget(self.search_input)
        header_layout.addSpacing(5) # 仅在搜索框和下方内容间留出固定间距

        self.filter_bar = ProductFilterBar()
        self.filter_bar.filter_changed.connect(self._on_filter_changed)
        self.filter_bar.metadata_refresh_requested.connect(self.shop_metadata_refresh_requested.emit)
        self.filter_bar.setVisible(False)  # 默认折叠隐藏
        header_layout.addWidget(self.filter_bar)
        self._current_filters = {}

        sync_panel = QHBoxLayout()
        sync_panel.setContentsMargins(5, 0, 5, 0)
        self.sync_status_lbl = CaptionLabel("云端货源状态加载中...")
        sync_panel.addWidget(self.sync_status_lbl)
        sync_panel.addStretch()

        self.btn_sync_now = ToolButton(FluentIcon.SYNC)
        self.btn_sync_now.setFixedSize(28, 28)
        self.btn_sync_now.hide()
        self.btn_sync_now.clicked.connect(self.sync_triggered.emit)
        sync_panel.addWidget(self.btn_sync_now)
        header_layout.addLayout(sync_panel)
        prod_layout.addWidget(header_container)

        self.product_list = ListWidget()
        self.product_list.setObjectName("ProductList")
        self.product_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.product_list.setResizeMode(QListView.Adjust)
        self.product_list.setSpacing(0)
        self.product_list.setContentsMargins(0, 0, 0, 0)
        self.product_list.viewport().setContentsMargins(0, 0, 0, 0)
        # 1. 将默认的“按卡片跳跃”改为“按像素平滑滑动”
        self.product_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.product_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 2. 将每次滚轮触发的像素步长调低（数值越小越慢，推荐 15~20 之间，你可以根据手感微调）
        self.product_list.verticalScrollBar().setSingleStep(18)
        prod_layout.addWidget(self.product_list, 1)

        self.load_more_btn = ProductLoadMoreButton()
        self.load_more_btn.hide()
        self.load_more_btn.clicked.connect(self._on_load_more_clicked)
        self._load_more_item = None

        self.center_stack.addWidget(self.product_page)

        # --- 2.3 销售微信号设置 ---
        self.settings_page = QWidget()
        self.settings_page.setObjectName("SettingsPage")
        sp_l = QVBoxLayout(self.settings_page)
        sp_l.setContentsMargins(12, 12, 12, 12)
        sp_l.setSpacing(10)
        sp_l.addWidget(SubtitleLabel("销售微信号绑定"))
        hint = CaptionLabel("可输入微信别名(alias)或微信ID(wxid_)；列表优先显示别名，括号内为微信ID。主号用于默认跟进与画像归属。")
        hint.setWordWrap(True)
        sp_l.addWidget(hint)
        self.sales_bindings_list = ListWidget()
        self.sales_bindings_list.setMinimumHeight(240)
        sp_l.addWidget(self.sales_bindings_list)
        add_row = QHBoxLayout()
        self.new_sales_id_input = LineEdit()
        self.new_sales_id_input.setPlaceholderText("输入别名(alias)或微信ID(wxid_)后点击添加")
        add_row.addWidget(self.new_sales_id_input, 1)
        self.btn_add_sales_bind = PrimaryPushButton("添加")
        add_row.addWidget(self.btn_add_sales_bind)
        sp_l.addLayout(add_row)
        act_row = QHBoxLayout()
        self.btn_sales_set_primary = PushButton("设为主号")
        self.btn_sales_delete = PushButton("删除选中")
        self.btn_claim_local_wechat = PushButton("声明本机微信")
        self.btn_claim_local_wechat.setToolTip("选择当前电脑微信已登录的销售微信号（发微信前须与本客户线程一致）")
        self.btn_sales_refresh = PushButton("刷新")
        act_row.addWidget(self.btn_sales_set_primary)
        act_row.addWidget(self.btn_sales_delete)
        act_row.addWidget(self.btn_claim_local_wechat)
        act_row.addStretch()
        act_row.addWidget(self.btn_sales_refresh)
        sp_l.addLayout(act_row)

        sp_l.addWidget(SubtitleLabel("米城 UUID 绑定"))
        mibuddy_hint = CaptionLabel(
            "绑定米城账号 UUID 后，可在「客资列表」查看认领与收藏客资。UUID位于米城主系统个人中心 -> 账户编码"
        )
        mibuddy_hint.setWordWrap(True)
        sp_l.addWidget(mibuddy_hint)
        mibuddy_row = QHBoxLayout()
        self.mibuddy_uuid_input = LineEdit()
        self.mibuddy_uuid_input.setPlaceholderText("输入米城 UUID")
        mibuddy_row.addWidget(self.mibuddy_uuid_input, 1)
        self.btn_mibuddy_bind = PrimaryPushButton("绑定")
        self.btn_mibuddy_unbind = PushButton("解绑")
        self.btn_mibuddy_refresh = PushButton("刷新")
        mibuddy_row.addWidget(self.btn_mibuddy_bind)
        mibuddy_row.addWidget(self.btn_mibuddy_unbind)
        mibuddy_row.addWidget(self.btn_mibuddy_refresh)
        sp_l.addLayout(mibuddy_row)
        self.mibuddy_status_label = CaptionLabel("未绑定米城账号")
        self.mibuddy_status_label.setWordWrap(True)
        sp_l.addWidget(self.mibuddy_status_label)
        sp_l.addStretch()
        self.center_stack.addWidget(self.settings_page)

        self.btn_add_sales_bind.clicked.connect(self._on_add_sales_bind_clicked)
        self.btn_sales_refresh.clicked.connect(self.sales_bindings_refresh_requested.emit)
        self.btn_claim_local_wechat.clicked.connect(self.claim_local_wechat_requested.emit)
        self.btn_sales_set_primary.clicked.connect(self._on_sales_set_primary_clicked)
        self.btn_sales_delete.clicked.connect(self._on_sales_delete_clicked)
        self.btn_mibuddy_bind.clicked.connect(self._on_mibuddy_bind_clicked)
        self.btn_mibuddy_unbind.clicked.connect(self.mibuddy_binding_unbind_requested.emit)
        self.btn_mibuddy_refresh.clicked.connect(self.mibuddy_binding_refresh_requested.emit)

        # --- 2.4 任务分配模块 ---
        self.task_allocation_page = TaskAllocationWidget()
        self.task_allocation_page.request_overview.connect(self.task_allocation_request.emit)
        self.task_allocation_page.task_action_requested.connect(self.task_allocation_action.emit)
        self.task_allocation_page.task_open_customer_chat.connect(self.task_open_customer_chat.emit)
        self.task_allocation_page.task_open_customer_phone.connect(self.task_open_customer_phone.emit)
        self.task_allocation_page.task_wechat_send_requested.connect(self.task_wechat_send_requested.emit)
        self.center_stack.addWidget(self.task_allocation_page)

        # --- 2.5 客资列表模块 ---
        self.customer_leads_page = CustomerLeadsWidget(self)
        self.center_stack.addWidget(self.customer_leads_page)

        center_layout.addWidget(self.center_stack)
        self.root_h_layout.addWidget(self.center_panel)

        # 兼容旧版 main.py 的外部调用
        self.stack = self.center_stack

        # ── 3. 右侧延展详情面板 (Right Drawer) ──
        self.drawer_widget = QWidget()
        self.drawer_widget.setMaximumWidth(0)  # 初始折叠
        drawer_layout = QVBoxLayout(self.drawer_widget)
        drawer_layout.setContentsMargins(0, 0, 0, 0)
        drawer_layout.setSpacing(0)

        self.drawer_bg = QFrame()
        self.drawer_bg.setObjectName("DrawerBg")
        self._apply_drawer_style()
        drawer_bg_layout = QVBoxLayout(self.drawer_bg)
        drawer_bg_layout.setContentsMargins(0, 0, 0, 0)
        drawer_bg_layout.setSpacing(0)

        # ── 3.1 抽屉页眉 (Close Button) ──
        self.drawer_header = QWidget()
        self.drawer_header.setFixedHeight(40)
        drawer_header_layout = QHBoxLayout(self.drawer_header)
        drawer_header_layout.setContentsMargins(10, 0, 10, 0)

        self.drawer_title = StrongBodyLabel("详细信息")
        drawer_header_layout.addWidget(self.drawer_title)
        drawer_header_layout.addStretch()

        self.btn_close_drawer = TransparentToolButton(FluentIcon.CLOSE)
        self.btn_close_drawer.setFixedSize(32, 32)
        self.btn_close_drawer.clicked.connect(
            lambda: self._toggle_drawer(self.drawer_stack.currentIndex())
        )
        drawer_header_layout.addWidget(self.btn_close_drawer)

        drawer_bg_layout.addWidget(self.drawer_header)

        self.drawer_stack = QStackedWidget()
        self.info_page = CustomerInfoWidget()

        self.phone_workbench = PhoneWorkbenchWidget()
        self._pending_phone_task: dict | None = None
        self._pending_wechat_task: dict | None = None

        self.order_page = QWidget()
        o_l = QVBoxLayout(self.order_page)
        o_l.setContentsMargins(10, 10, 10, 10)

        self.order_list = ListWidget()
        self.order_list.setObjectName("OrderHistoryList")
        self.order_list.setFocusPolicy(Qt.NoFocus)
        self.order_list.setSpacing(8)
        self.order_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.order_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.order_list.verticalScrollBar().setSingleStep(20)

        o_l.addWidget(self.order_list)

        self.drawer_stack.addWidget(self.info_page)
        self.drawer_stack.addWidget(self.phone_workbench)
        self.drawer_stack.addWidget(self.order_page)

        drawer_bg_layout.addWidget(self.drawer_stack)
        drawer_layout.addWidget(self.drawer_bg)
        self.root_h_layout.addWidget(self.drawer_widget)

        # ── 左侧导航栏按钮信号连接 ──
        self.btn_nav_leads.clicked.connect(lambda: self._on_tab_changed(5))
        self.btn_nav_task.clicked.connect(lambda: self._on_tab_changed(4))
        self.btn_nav_staff.clicked.connect(self._on_staff_chat_nav_clicked)
        self.btn_nav_chat.clicked.connect(self._on_customer_chat_nav_clicked)
        self.btn_nav_shop.clicked.connect(lambda: self._on_tab_changed(2))
        self.btn_nav_settings.clicked.connect(lambda: self._on_tab_changed(3))

        self.btn_action_info.clicked.connect(lambda: self._toggle_drawer(0))
        self.btn_action_phone.clicked.connect(lambda: self._toggle_drawer(1))
        self.btn_action_order.clicked.connect(lambda: self._toggle_drawer(2))

        # 点击历史总额 → 只跳转抽屉到订单页（实际数据由 customer_selected 驱动）
        self.info_page.history_clicked.connect(self._on_history_clicked)

        # ── 状态恢复：从 config.ini 读取并应用 ──
        self.custom_snap_class = cfg.snap_class
        self.custom_snap_title = cfg.snap_title
        
        # 初始默认不吸附 (设置默认为 false)，采集信息后由用户手动开启，之后持久化状态
        self.is_snapping = cfg.snap_enabled
        self.snap_timer = QTimer(self)
        self.snap_timer.timeout.connect(self._on_snap_timeout)
        
        if self.is_snapping:
            self.snap_timer.start(50)
            
        self._restore_snap_btn_ui()

        # ── 动画逻辑 ──
        self._drawer_open = False
        self.drawer_anim = None
        # 抽屉收起时窗口的“自然宽度”：动态跟随用户的拖拽/最大化等手动尺寸变化；
        # 抽屉展开/收起的动画基于该宽度推导窗口目标尺寸，避免硬编码 430 导致
        # “展开收回后无法继续加宽”的卡死 bug。
        self._min_window_width = 430
        self._natural_width = max(self._min_window_width, self.width())
        self._drawer_animating = False
        
        # 最后统一应用样式，确保所有子控件已创建
        self._apply_content_style()
        self._chat_surface_mode = "customer"
        self._on_tab_changed(0)

    def _on_staff_chat_nav_clicked(self):
        self._set_chat_surface_mode("staff")

    def _on_customer_chat_nav_clicked(self):
        self._set_chat_surface_mode("customer")
        
    def _on_import_manual_clicked(self):
        from PySide6.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择本周待跟进客户名单",
            "",
            "Excel或CSV文件 (*.xlsx *.xls *.csv)"
        )
        if file_path:
            self.manual_import_requested.emit(file_path)

    def _set_chat_surface_mode(self, mode: str):
        """切换对话界面：自由对话时隐藏客户侧栏以放大聊天区。"""
        if mode not in ("staff", "customer"):
            return
        if getattr(self, "_chat_surface_mode", None) == mode:
            self._on_tab_changed(0)
            return
        self._chat_surface_mode = mode
        staff = (mode == "staff")
        
        # 自由对话模式下隐藏客户相关的操作图标（电话、订单、资料）
        self.btn_action_phone.setVisible(not staff)
        self.btn_action_order.setVisible(not staff)
        self.btn_action_info.setVisible(not staff)

        if staff:
            self.sidebar.setVisible(False)
            self.apply_staff_chat_header()
            # 自动收起已展开的客户资料或订单页面，为自由对话腾出更多横向空间
            if getattr(self, "_drawer_open", False):
                self._toggle_drawer(self.drawer_stack.currentIndex())
        else:
            self.sidebar.setVisible(True)
            # 通过 splitter 恢复用户偏好的宽度；避免 setFixedWidth 把分隔条钉死
            self._apply_sidebar_pref_width()
            QTimer.singleShot(0, self._refresh_customer_sidebar_layout)
        self.chat_surface_mode_changed.emit(mode)
        self._on_tab_changed(0)

    def apply_staff_chat_header(self):
        self.lbl_header_unit.setText("自由对话")
        self.lbl_header_info.setText("内部问答 · 未绑定客户")

    def apply_customer_header_placeholder(self):
        self.lbl_header_unit.setText("客户对话")
        self.lbl_header_info.setText("请从左侧选择客户")
        if hasattr(self, "phone_workbench"):
            self.phone_workbench.clear()

    def set_pending_phone_task(self, task: dict | None):
        """任务分配电话主线跳转时携带的任务上下文（侧栏换客户前勿清空）。"""
        self._pending_phone_task = dict(task) if isinstance(task, dict) else None

    def clear_pending_phone_task(self):
        self._pending_phone_task = None

    def set_pending_wechat_task(self, task: dict | None):
        """任务分配微信主线 / 激活跳转时携带的任务上下文。"""
        self._pending_wechat_task = dict(task) if isinstance(task, dict) else None

    def clear_pending_wechat_task(self):
        self._pending_wechat_task = None

    def pending_wechat_task(self) -> dict | None:
        return self._pending_wechat_task

    def _sync_phone_workbench(self, customer_data: dict | None):
        if not hasattr(self, "phone_workbench"):
            return
        task = getattr(self, "_pending_phone_task", None)
        if customer_data:
            self.phone_workbench.set_context(customer_data, task)
        else:
            self.phone_workbench.clear()

    def show_info_bar(self, type_str, title, content, duration=2000):
        """
        弹出非阻塞提示条 (Fluent InfoBar)
        :param type_str: 'success', 'warning', 'error', 'info'
        :param title: 标题
        :param content: 内容
        :param duration: 持续时间 (ms)
        """
        position = InfoBarPosition.TOP_RIGHT
        if type_str == 'success':
            InfoBar.success(title, content, duration=duration, position=position, parent=self)
        elif type_str == 'warning':
            InfoBar.warning(title, content, duration=duration, position=position, parent=self)
        elif type_str == 'error':
            InfoBar.error(title, content, duration=duration, position=position, parent=self)
        else:
            InfoBar.info(title, content, duration=duration, position=position, parent=self)

    # ── 抽屉动画 ───────────────────────────────────────────────────────────────

    def _toggle_drawer(self, index):
        if self.drawer_anim and self.drawer_anim.state() == QPropertyAnimation.Running:
            return

        # 抽屉标题映射
        _titles = {0: "客户详细资料", 1: "电话工作台", 2: "历史订单流水"}

        # 如果已展开：点击同一个图标 → 收起；不同图标 → 切换内容
        if self._drawer_open:
            if self.drawer_stack.currentIndex() == index:
                # 收起：保留之前记录的 _natural_width 作为回归宽度
                self._drawer_open = False
            else:
                self.drawer_stack.setCurrentIndex(index)
                self.drawer_title.setText(_titles.get(index, "详细信息"))
                QTimer.singleShot(50, self._force_refresh_all_layouts)
                return
        else:
            # 展开前先冻结一次“当前窗口宽度”作为下一次收起的回归目标，
            # 这样用户即便在窗口被拖宽后再展开抽屉，收起后仍能回到他自己设置的宽度。
            self._natural_width = max(self._min_window_width, self.width())
            self.drawer_stack.setCurrentIndex(index)
            self.drawer_title.setText(_titles.get(index, "详细信息"))
            self._drawer_open = True

        drawer_target = 350 if self._drawer_open else 0
        window_target = self._natural_width + drawer_target

        # 关键修复：动画期间放开 max 限制并设置一个 sane 的 min，
        # 但 *不* 在动画结束时把 setMaximumWidth 钉死成 430，
        # 否则用户后续无法再拖宽窗口（也就无法拉宽侧栏）。
        self.setMinimumWidth(self._min_window_width)
        self.setMaximumWidth(16777215)
        self.drawer_widget.setMinimumWidth(0)
        self.drawer_widget.setMaximumWidth(350)
        self._drawer_animating = True

        self.drawer_anim = QPropertyAnimation(self, b"geometry")
        self.drawer_anim.setDuration(300)
        self.drawer_anim.setEasingCurve(QEasingCurve.OutCubic)
        
        start_rect = self.geometry()
        end_rect = QRect(start_rect.x(), start_rect.y(), window_target, start_rect.height())
        
        self.drawer_anim.setStartValue(start_rect)
        self.drawer_anim.setEndValue(end_rect)

        drawer_max_anim = QPropertyAnimation(self.drawer_widget, b"maximumWidth")
        drawer_max_anim.setDuration(320)
        drawer_max_anim.setEasingCurve(QEasingCurve.OutCubic)
        current_max = self.drawer_widget.maximumWidth()
        drawer_max_anim.setStartValue(current_max)
        drawer_max_anim.setEndValue(drawer_target)

        self.anim_group = QParallelAnimationGroup(self)
        self.anim_group.addAnimation(self.drawer_anim)
        self.anim_group.addAnimation(drawer_max_anim)

        def on_finished():
            self._drawer_animating = False
            if not self._drawer_open:
                self.drawer_widget.setMaximumWidth(0)
                # 注意：此处 **不再** 调用 setMaximumWidth(430)；
                # 保持窗口自由可拉伸，确保侧栏拖宽体验不受抽屉历史状态污染。
            else:
                self.drawer_widget.setMaximumWidth(350)
            # 布局补丁：在动画结束后，强制触发一次全局布局刷新，确保订单卡片宽度锚定在 350px 状态
            self._force_refresh_all_layouts()

        self.anim_group.finished.connect(on_finished)
        self.anim_group.start()

    def _on_history_clicked(self, customer_id):
        """点击详情页的历史金额 → 跳转到订单流水页（数据已通过 customer_selected 预加载）"""
        if not self._drawer_open:
            self._toggle_drawer(2)
        else:
            self.drawer_stack.setCurrentIndex(2)
            self.drawer_title.setText("历史订单流水")
            QTimer.singleShot(50, self._force_refresh_all_layouts)

    # ── 数据填充 ───────────────────────────────────────────────────────────────

    def update_order_table(self, orders):
        """填充订单流水数据（已进化为卡片流）"""
        self.order_list.clear()
        
        # 优化可用宽度探测：优先使用当前可视区域
        viewport_w = self.order_list.viewport().width()
        
        # 极致防丢：抽屉未开或正在动画时视口宽度不可信，回退到安全预案宽度；
        # 视口可信时统一走 safe_card_width（自动扣除列表 spacing 与悬浮滚动条）。
        if (self._drawer_open and viewport_w < 200) or (not self._drawer_open):
            target_width = 320 # 预案宽度（标准 350 宽度下的安全内容区）
            card_width = target_width - 24
        else:
            target_width = viewport_w
            card_width = safe_card_width(self.order_list)
            
        if not orders:
            # 当数据为空时展示占位提示
            item = QListWidgetItem(self.order_list)
            placeholder = QLabel("暂无订单记录")
            is_dark = isDarkTheme()
            placeholder.setStyleSheet(label_qss("empty", extra="margin-top: 50px;"))
            placeholder.setAlignment(Qt.AlignCenter)
            item.setSizeHint(QSize(target_width, 150))
            self.order_list.addItem(item)
            self.order_list.setItemWidget(item, placeholder)
            return

        for order in orders:
            item = QListWidgetItem(self.order_list)
            widget = OrderCardWidget(order)
            
            # 锁定宽度适配容器，留出足够的余位防止横向溢出
            widget.setFixedWidth(card_width)
            widget.adjustSize()
            
            # 同步尺寸提示
            size = widget.sizeHint()
            item.setSizeHint(size)
            
            self.order_list.addItem(item)
            self.order_list.setItemWidget(item, widget)
            
        # 4.6 补丁：强行刷新界面，防止“拉长”残留
        QTimer.singleShot(100, self._force_refresh_all_layouts)

    def _force_refresh_all_layouts(self):
        """延迟刷新全局布局，确保在初始加载或复杂状态切换后位置对其"""
        if hasattr(self, "product_list"):
            self.product_list.doItemsLayout()
            self.product_list.viewport().update()
        if hasattr(self, "order_list"):
            self.order_list.doItemsLayout()
            self.order_list.viewport().update()
        if hasattr(self, "product_page"):
            self.product_page.update()
        if hasattr(self, "phone_workbench"):
            self.phone_workbench.refresh_layout()
        if hasattr(self, "customer_leads_page"):
            for lw in self.customer_leads_page.iter_leads_list_widgets():
                lw.doItemsLayout()
                lw.viewport().update()
            self.customer_leads_page.resizeEvent(None)
        self.resizeEvent(None)

    def _on_tab_changed(self, index):
        """切换全局导航模块。

        index 含义（与导航按钮一一对应）：
          0 = 对话 (chat_module, center_stack[0])
          2 = 商品 (product_page, center_stack[1])
          3 = 销售微信号设置 (settings_page, center_stack[2])
          4 = 任务分配 (task_allocation_page, center_stack[3])
        """
        if index != 5 and hasattr(self, "customer_leads_page"):
            self.customer_leads_page.stop_auto_refresh()
        if index == 0:
            self.center_stack.setCurrentIndex(0)
            QTimer.singleShot(0, self._refresh_customer_sidebar_layout)
        elif index == 2:  # 商品
            self.center_stack.setCurrentIndex(1)
            # 切换到商品时，自动合上右侧详情面板
            if self._drawer_open:
                self._toggle_drawer(self.drawer_stack.currentIndex())
            # 延迟触发界面的全面重绘，解决初始进入时宽度为0导致的产品名不换行问题
            QTimer.singleShot(100, self._force_refresh_all_layouts)
        elif index == 3:
            self.center_stack.setCurrentIndex(2)
            self.sales_bindings_refresh_requested.emit()
            self.mibuddy_binding_refresh_requested.emit()
        elif index == 4:
            self.center_stack.setCurrentIndex(3)
            # 进入任务分配页时，先合上右侧详情抽屉，确保横向空间充裕
            if self._drawer_open:
                self._toggle_drawer(self.drawer_stack.currentIndex())
            # 已有绑定缓存时只灌下拉；有任务数据时不再重复请求
            cached_bindings = getattr(self, "_cached_sales_bindings", None)
            if cached_bindings and hasattr(self, "task_allocation_page"):
                self.task_allocation_page.set_sales_options(cached_bindings)
            else:
                self.sales_bindings_refresh_requested.emit()
            if hasattr(self, "task_allocation_page"):
                self.task_allocation_page.on_page_activated()
        elif index == 5:
            self.center_stack.setCurrentIndex(4)
            # 进入客资列表页时，合上右侧详情抽屉
            if self._drawer_open:
                self._toggle_drawer(self.drawer_stack.currentIndex())
            self.mibuddy_binding_refresh_requested.emit()
            self.sales_bindings_refresh_requested.emit()
            self.customer_leads_page.on_page_activated()
            QTimer.singleShot(100, self._force_refresh_all_layouts)

        self.tab_changed.emit(index)

    def _on_add_sales_bind_clicked(self):
        t = self.new_sales_id_input.text().strip()
        if t:
            self.sales_binding_add_requested.emit(t)
            self.new_sales_id_input.clear()

    def _on_sales_set_primary_clicked(self):
        bid = self._selected_sales_binding_id()
        if bid is not None:
            self.sales_binding_primary_requested.emit(bid)

    def _on_sales_delete_clicked(self):
        bid = self._selected_sales_binding_id()
        if bid is not None:
            self.sales_binding_delete_requested.emit(bid)

    def _selected_sales_binding_id(self):
        it = self.sales_bindings_list.currentItem()
        if not it:
            return None
        return it.data(Qt.UserRole)

    def update_sales_bindings_list(self, rows: list):
        self._cached_sales_bindings = list(rows or [])
        self.sales_bindings_list.clear()
        for r in rows or []:
            item = QListWidgetItem()
            sw = str(r.get("sales_wechat_id") or "").strip()
            als = str(r.get("alias_name") or "").strip()
            label = (r.get("label") or "").strip()
            prim = r.get("is_primary")
            extra = f"  ({label})" if label else ""
            star = " ★主号" if prim else ""
            shown = als if als else sw
            tail = f" ({sw})" if als and sw and als != sw else ""
            item.setText(f"{shown}{tail}{extra}{star}")
            item.setData(Qt.UserRole, r.get("id"))
            self.sales_bindings_list.addItem(item)
        # 任务分配页面同样以销售微信号为维度，把绑定列表同步进下拉框
        if hasattr(self, "task_allocation_page") and self.task_allocation_page is not None:
            self.task_allocation_page.set_sales_options(rows or [])

    def update_task_allocation_overview(self, data: dict | None):
        """渲染任务分配总览（由 DesktopApp 调 API 后回调）。"""
        if not hasattr(self, "task_allocation_page") or self.task_allocation_page is None:
            return
        if data is None:
            self.task_allocation_page.show_error("无响应数据")
            return
        self.task_allocation_page.set_overview_data(data)

    def show_task_allocation_error(self, message: str):
        if not hasattr(self, "task_allocation_page") or self.task_allocation_page is None:
            return
        self.task_allocation_page.show_error(message or "未知错误")

    def append_wechat_send_log(self, text: str):
        """兼容 RPA 发送日志回调（原设置页记录区已移除）。"""
        _ = text

    def _on_mibuddy_bind_clicked(self):
        uuid = (self.mibuddy_uuid_input.text() or "").strip()
        if uuid:
            self.mibuddy_binding_bind_requested.emit(uuid)

    def update_mibuddy_binding_ui(self, data: dict | None):
        data = data or {}
        uuid = str(data.get("uuid") or "").strip()
        profile = data.get("profile") or {}
        changhu_raw = profile.get("changhu") or []
        if not isinstance(changhu_raw, list):
            changhu_raw = []
        self._mibuddy_changhu_phones = [
            str(p).strip() for p in changhu_raw if str(p).strip()
        ]
        if uuid:
            self.mibuddy_uuid_input.setText(uuid)
            name = str(profile.get("name") or "").strip()
            account = str(profile.get("account") or "").strip()
            changhu = profile.get("changhu") or []
            phones = "、".join(str(p) for p in changhu if str(p).strip())
            parts = [f"已绑定 {name or uuid}"]
            if account:
                parts.append(f"账号 {account}")
            if phones:
                parts.append(f"畅呼 {phones}")
            self.mibuddy_status_label.setText(" · ".join(parts))
            self.btn_mibuddy_bind.setEnabled(False)
            self.mibuddy_uuid_input.setReadOnly(True)
        else:
            self._mibuddy_changhu_phones = []
            self.mibuddy_status_label.setText("未绑定米城账号")
            self.btn_mibuddy_bind.setEnabled(True)
            self.mibuddy_uuid_input.setReadOnly(False)

    def switch_tab(self, index):
        self._on_tab_changed(index)

    def flash_task_nav_ui(self) -> None:
        """任务卡片点击瞬间切到对话区，避免等异步链路跑完才换页。"""
        if self.center_stack.currentIndex() != 0:
            self.center_stack.setCurrentIndex(0)

    def apply_customer_header(self, customer_data, *, sync_phone: bool = True):
        """同步侧栏顶栏、电话工作台（保存后刷新或点击列表时共用）。"""
        if not customer_data:
            self.apply_customer_header_placeholder()
            return
        unit = customer_data.get("unit_name") or customer_data.get("unit_type") or "未知单位"
        name = customer_data.get("customer_name") or "未知"
        phone = str(customer_data.get("phone") or "")
        masked = mask_phone(phone)
        display_unit = unit[:15] + "..." if len(unit) > 15 else unit
        self.lbl_header_unit.setText(display_unit)
        self.lbl_header_info.setText(f"{name} | {masked}")
        if sync_phone:
            self._sync_phone_workbench(customer_data)

    def find_customer_by_task(self, task: dict) -> dict | None:
        """按任务中的 raw_customer_id + sales_wechat_id 在本地客户快照中查找。"""
        if not isinstance(task, dict):
            return None
        rid = str(task.get("raw_customer_id") or "").strip()
        sw = str(task.get("sales_wechat_id") or "").strip()
        if not rid:
            return None
        for c in getattr(self, "_last_customers_snapshot", []) or []:
            if str(c.get("id") or "").strip() != rid:
                continue
            if sw and str(c.get("sales_wechat_id") or "").strip() != sw:
                continue
            return c
        return None

    def select_customer_by_key_if_visible(self, customer_id, sales_wechat_id=None) -> bool:
        """仅在侧栏已渲染行中定位客户；不展开「加载更多」，供任务跳转等快路径使用。"""
        self._bump_customer_select_progress_token()
        rid = str(customer_id or "").strip()
        sw = str(sales_wechat_id or "").strip()
        if not rid:
            return False
        for leaf in self._iter_customer_tree_leaves():
            data = leaf.data(0, Qt.UserRole)
            if not isinstance(data, dict):
                continue
            if str(data.get("id") or "").strip() != rid:
                continue
            if sw and str(data.get("sales_wechat_id") or "").strip() != sw:
                continue
            self._finalize_customer_tree_selection(leaf)
            return True
        return False

    def select_customer_by_key(
        self, customer_id, sales_wechat_id=None, *, today_group_only: bool = False
    ) -> bool:
        """在侧栏客户树中定位并选中客户（必要时扩展分组「加载更多」范围）。

        today_group_only=True 时仅在「今日建议联系」分组内定位，避免任务跳转扫全树卡顿。
        """
        rid = str(customer_id or "").strip()
        sw = str(sales_wechat_id or "").strip()
        if not rid:
            return False
        key = (rid, sw)

        if self.select_customer_by_key_if_visible(rid, sw):
            return True

        if today_group_only:
            today_group = self._find_group_node_by_id("today")
            if today_group is None:
                return False
            return self._select_customer_in_group(today_group, key)

        for group in self._iter_group_nodes():
            if self._select_customer_in_group(group, key):
                return True
        return False

    def _find_group_node_by_id(self, group_id: str) -> QTreeWidgetItem | None:
        gid = (group_id or "").strip()
        if not gid:
            return None
        for node in self._iter_group_nodes():
            state = node.data(0, CUSTOMER_GROUP_STATE_ROLE)
            if isinstance(state, dict) and state.get("group_id") == gid:
                return node
        return None

    def _select_customer_in_group(self, group: QTreeWidgetItem, key: tuple) -> bool:
        rid, sw = key
        state = group.data(0, CUSTOMER_GROUP_STATE_ROLE)
        if not isinstance(state, dict):
            return False
        active = self._active_customers_for_group_state(state)
        hit_idx = -1
        for idx, c in enumerate(active):
            if str(c.get("id") or "").strip() != rid:
                continue
            if sw and str(c.get("sales_wechat_id") or "").strip() != sw:
                continue
            hit_idx = idx
            break
        if hit_idx < 0:
            return False
        need = hit_idx + 1
        cur_disp = int(state.get("displayed") or 0)
        expand_by = max(0, need - cur_disp)
        if expand_by > CUSTOMER_SELECT_SYNC_EXPAND_MAX:
            self._start_progressive_customer_select(group, key, hit_idx)
            return True
        if need > cur_disp:
            state = {**state, "displayed": need}
            group.setData(0, CUSTOMER_GROUP_STATE_ROLE, state)
        self.customer_list.setUpdatesEnabled(False)
        try:
            hit = self._render_group_children(group, key)
        finally:
            self.customer_list.setUpdatesEnabled(True)
        if hit is not None:
            self._finalize_customer_tree_selection(hit)
            return True
        return False

    def _finalize_customer_tree_selection(self, leaf: QTreeWidgetItem) -> None:
        self.customer_list.setCurrentItem(leaf)
        node = leaf.parent()
        while node is not None:
            self.customer_list.expandItem(node)
            node = node.parent()
        self.customer_list.scrollToItem(leaf)
        self._sync_customer_tree_item_widths()

    def _customer_select_progress_token(self) -> int:
        return int(getattr(self, "_customer_select_progress_seq", 0) or 0)

    def _bump_customer_select_progress_token(self) -> int:
        n = self._customer_select_progress_token() + 1
        self._customer_select_progress_seq = n
        return n

    def _progressive_select_chunk_size(self, remaining: int) -> int:
        if remaining <= CUSTOMER_GROUP_PAGE_SIZE * 3:
            return CUSTOMER_GROUP_PAGE_SIZE
        return min(remaining, CUSTOMER_GROUP_PAGE_SIZE * 5)

    def _strip_group_load_more_row(self, group_parent: QTreeWidgetItem) -> None:
        for j in range(group_parent.childCount() - 1, -1, -1):
            ch = group_parent.child(j)
            if ch.data(0, CUSTOMER_ROW_KIND_ROLE) == CUSTOMER_ROW_KIND_LOAD_MORE:
                group_parent.takeChild(j)

    def _customer_tree_content_width(self) -> int:
        w = self.customer_list.viewport().width()
        return max(50, w - 6) if w >= 50 else 50

    def _append_customers_to_group(
        self,
        group_parent: QTreeWidgetItem,
        customers: list,
        select_customer_key=None,
    ) -> QTreeWidgetItem | None:
        tree = self.customer_list
        content_w = self._customer_tree_content_width()
        target_leaf = None
        for c in customers:
            child = QTreeWidgetItem(group_parent)
            child.setData(0, Qt.UserRole, c)
            child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            widget = CustomerItemWidget(c)
            widget.setFixedWidth(content_w)
            widget.set_available_width(content_w)
            child.setSizeHint(0, widget.sizeHint())
            tree.setItemWidget(child, 0, widget)
            if select_customer_key is not None:
                cid, csw = select_customer_key
                if (
                    str(c.get("id") or "") == str(cid or "")
                    and str(c.get("sales_wechat_id") or "") == str(csw or "")
                ):
                    target_leaf = child
        return target_leaf

    def _add_group_load_more_row(self, group_parent: QTreeWidgetItem, rest: int) -> None:
        if rest <= 0:
            return
        tree = self.customer_list
        load_item = QTreeWidgetItem(group_parent)
        load_item.setData(0, Qt.UserRole, None)
        load_item.setData(0, CUSTOMER_ROW_KIND_ROLE, CUSTOMER_ROW_KIND_LOAD_MORE)
        load_item.setFlags(Qt.ItemIsEnabled)
        btn = TransparentPushButton(f"加载更多 ({rest})")
        btn.setFixedHeight(28)
        btn_font = btn.font()
        btn_font.setPixelSize(11)
        btn.setFont(btn_font)
        btn.clicked.connect(lambda *, gp=group_parent: self._on_customer_group_load_more(gp))
        wrap = QWidget()
        wrap.setStyleSheet("background-color: transparent;")
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(4, 2, 8, 2)
        lay.addWidget(btn)
        load_item.setSizeHint(0, QSize(0, 34))
        tree.setItemWidget(load_item, 0, wrap)

    def _find_customer_leaf_in_group(
        self,
        group_parent: QTreeWidgetItem,
        select_customer_key,
    ) -> QTreeWidgetItem | None:
        cid, csw = select_customer_key
        for j in range(group_parent.childCount()):
            ch = group_parent.child(j)
            if ch.data(0, CUSTOMER_ROW_KIND_ROLE) == CUSTOMER_ROW_KIND_LOAD_MORE:
                continue
            data = ch.data(0, Qt.UserRole)
            if not isinstance(data, dict):
                continue
            if str(data.get("id") or "") != str(cid or ""):
                continue
            if str(data.get("sales_wechat_id") or "") != str(csw or ""):
                continue
            return ch
        return None

    def _start_progressive_customer_select(
        self,
        group_parent: QTreeWidgetItem,
        select_customer_key,
        target_idx: int,
    ) -> None:
        """分帧向分组追加客户行直至目标可见，避免任务跳转时一次性渲染整组客户。"""
        token = self._bump_customer_select_progress_token()
        target_displayed = target_idx + 1
        node = group_parent
        while node is not None:
            self.customer_list.expandItem(node)
            node = node.parent()

        def _step() -> None:
            if token != self._customer_select_progress_token():
                return
            state = group_parent.data(0, CUSTOMER_GROUP_STATE_ROLE)
            if not isinstance(state, dict):
                return
            active = self._active_customers_for_group_state(state)
            cur_disp = int(state.get("displayed") or 0)
            if cur_disp >= target_displayed:
                leaf = self._find_customer_leaf_in_group(group_parent, select_customer_key)
                if leaf is not None:
                    self._finalize_customer_tree_selection(leaf)
                return

            remaining = target_displayed - cur_disp
            chunk = self._progressive_select_chunk_size(remaining)
            next_disp = min(cur_disp + chunk, target_displayed, len(active))
            state = {**state, "displayed": next_disp}
            group_parent.setData(0, CUSTOMER_GROUP_STATE_ROLE, state)

            title_name = state.get("title_name") or ""
            hw = self.customer_list.itemWidget(group_parent, 0)
            if isinstance(hw, CustomerGroupHeaderWidget):
                hw.set_heading(f"{title_name} ({len(active)})")

            self.customer_list.setUpdatesEnabled(False)
            try:
                self._strip_group_load_more_row(group_parent)
                leaf = self._append_customers_to_group(
                    group_parent,
                    active[cur_disp:next_disp],
                    select_customer_key,
                )
                if next_disp < len(active):
                    self._add_group_load_more_row(group_parent, len(active) - next_disp)
            finally:
                self.customer_list.setUpdatesEnabled(True)

            if leaf is not None and next_disp >= target_displayed:
                self._finalize_customer_tree_selection(leaf)
                return
            QTimer.singleShot(0, _step)

        QTimer.singleShot(0, _step)

    def _iter_customer_tree_leaves(self):
        tree = self.customer_list
        def walk(node: QTreeWidgetItem):
            for j in range(node.childCount()):
                ch = node.child(j)
                if ch.data(0, CUSTOMER_ROW_KIND_ROLE) == CUSTOMER_ROW_KIND_LOAD_MORE:
                    continue
                if isinstance(ch.data(0, Qt.UserRole), dict):
                    yield ch
                if ch.childCount() > 0:
                    yield from walk(ch)

        for i in range(tree.topLevelItemCount()):
            yield from walk(tree.topLevelItem(i))

    def _iter_group_nodes(self):
        """遍历所有存有 CUSTOMER_GROUP_STATE_ROLE 的分组节点（支持两层/多层）。"""
        tree = self.customer_list

        def walk(node: QTreeWidgetItem):
            st = node.data(0, CUSTOMER_GROUP_STATE_ROLE)
            if isinstance(st, dict):
                yield node
            for j in range(node.childCount()):
                ch = node.child(j)
                if ch.childCount() > 0:
                    yield from walk(ch)

        for i in range(tree.topLevelItemCount()):
            yield from walk(tree.topLevelItem(i))

    def _customer_matches_search_kw(self, c: dict, kw: str) -> bool:
        if not kw:
            return True
        unit_name = c.get("unit_name") or c.get("unit_type") or "未知单位"
        name = c.get("customer_name") or "未知"
        phone = str(c.get("phone") or "")
        cid = str(c.get("id") or "")
        wechat_remark = str(c.get("wechat_remark") or "")
        sales_label = str(c.get("sales_wechat_label") or "")
        haystack = f"{unit_name} {name} {phone} {cid} {wechat_remark} {sales_label}".lower()
        return kw in haystack

    def _active_customers_for_group_state(self, state: dict) -> list:
        kw = self.customer_search.text().strip().lower()
        src = state.get("source") or []
        # 1) 先做“原始客户池条件筛选”，再做搜索关键词过滤
        filtered = [c for c in src if self._customer_passes_pool_filter(c)]
        if not kw:
            return list(filtered)
        return [c for c in filtered if self._customer_matches_search_kw(c, kw)]

    def _customer_passes_pool_filter(self, c: dict) -> bool:
        """原始客户池条件筛选（客户端侧，本地过滤）。"""
        mode = getattr(self, "_customer_pool_filter_mode", "all") or "all"
        if mode == "all":
            return True
        phone = str(c.get("phone") or "").strip()
        unit = str(c.get("unit_name") or "").strip()
        wechat_remark = str(c.get("wechat_remark") or "").strip()
        orders = int(c.get("historical_order_count") or 0)
        if mode == "no_phone":
            return not phone
        if mode == "no_unit":
            return not unit
        if mode == "has_wechat_remark":
            return bool(wechat_remark)
        if mode == "has_orders":
            return orders > 0
        return True

    def _render_group_children(self, group_parent: QTreeWidgetItem, select_customer_key=None):
        state = group_parent.data(0, CUSTOMER_GROUP_STATE_ROLE)
        if not isinstance(state, dict):
            return None

        active = self._active_customers_for_group_state(state)
        raw_disp = int(state.get("displayed") or CUSTOMER_GROUP_PAGE_SIZE)
        shown = min(raw_disp, len(active))
        state = {**state, "displayed": shown}
        group_parent.setData(0, CUSTOMER_GROUP_STATE_ROLE, state)

        title_name = state.get("title_name") or ""
        hw = self.customer_list.itemWidget(group_parent, 0)
        if isinstance(hw, CustomerGroupHeaderWidget):
            hw.set_heading(f"{title_name} ({len(active)})")

        while group_parent.childCount():
            group_parent.takeChild(0)

        target_leaf = self._append_customers_to_group(
            group_parent, active[:shown], select_customer_key
        )
        if shown < len(active):
            self._add_group_load_more_row(group_parent, len(active) - shown)
        return target_leaf

    def _on_customer_group_load_more(self, group_parent: QTreeWidgetItem):
        state = group_parent.data(0, CUSTOMER_GROUP_STATE_ROLE)
        if not isinstance(state, dict):
            return

        # 记录当前滚动条位置，防止加载更多后滚动条跳到最上方
        vbar = self.customer_list.verticalScrollBar()
        scroll_pos = vbar.value()

        active = self._active_customers_for_group_state(state)
        cur = int(state.get("displayed") or 0)
        next_disp = min(cur + CUSTOMER_GROUP_PAGE_SIZE, len(active))
        state = {**state, "displayed": next_disp}
        group_parent.setData(0, CUSTOMER_GROUP_STATE_ROLE, state)
        self.customer_list.setUpdatesEnabled(False)
        try:
            self._strip_group_load_more_row(group_parent)
            self._append_customers_to_group(group_parent, active[cur:next_disp])
            if next_disp < len(active):
                self._add_group_load_more_row(group_parent, len(active) - next_disp)
            self._sync_customer_tree_item_widths()
        finally:
            self.customer_list.setUpdatesEnabled(True)

        # 强制更新几何尺寸并恢复滚动条位置
        self.customer_list.updateGeometries()
        vbar.setValue(scroll_pos)
        # 用 QTimer 异步兜底，确保在所有子 widget 布局完成后再次校准，防止闪烁
        QTimer.singleShot(0, lambda: vbar.setValue(scroll_pos))

    def _apply_sidebar_pref_width(self):
        """根据用户偏好设置 splitter 内部尺寸（首次启动/切回客户对话时调用）。"""
        if not hasattr(self, "chat_splitter") or self.chat_splitter is None:
            return
        pref = max(90, min(420, int(getattr(self, "_sidebar_pref_width", 110) or 110)))
        total = max(self.chat_splitter.width(), self.chat_area.minimumWidth() + pref + 8)
        chat_w = max(self.chat_area.minimumWidth(), total - pref)
        self.chat_splitter.setSizes([pref, chat_w])

    def _on_sidebar_splitter_moved(self, pos: int, index: int):
        """用户拖拽 splitter 分隔条：持久化新的侧栏宽度并刷新列表项宽度。"""
        if not hasattr(self, "chat_splitter") or self.chat_splitter is None:
            return
        sizes = self.chat_splitter.sizes()
        if not sizes or sizes[0] <= 0:
            return
        new_w = max(90, min(420, int(sizes[0])))
        if new_w == getattr(self, "_sidebar_pref_width", None):
            self._sync_customer_tree_item_widths()
            return
        self._sidebar_pref_width = new_w
        try:
            cfg.set_runtime("sidebar_width", str(new_w))
        except Exception:
            pass
        self._sync_customer_tree_item_widths()

    def eventFilter(self, obj, event):
        """拦截 chat_splitter 分隔条的鼠标事件：当窗口窄到无法继续右移分隔条时，
        自动加宽主窗口，让侧栏（客户列表）能继续被拖宽。"""
        try:
            if (
                hasattr(self, "chat_splitter")
                and self.chat_splitter is not None
                and obj is self.chat_splitter.handle(1)
            ):
                etype = event.type()
                if etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                    self._sidebar_drag_active = True
                    self._sidebar_drag_start_global_x = int(event.globalPosition().x())
                    sizes = self.chat_splitter.sizes()
                    self._sidebar_drag_start_w = int(sizes[0]) if sizes else 0
                elif etype == QEvent.MouseMove and self._sidebar_drag_active:
                    self._maybe_grow_window_for_sidebar_drag(int(event.globalPosition().x()))
                elif etype in (QEvent.MouseButtonRelease, QEvent.Leave):
                    self._sidebar_drag_active = False
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def _maybe_grow_window_for_sidebar_drag(self, current_global_x: int):
        """侧栏拖拽时：若用户向右拉、且当前窗口已无空间让 chat_area 缩到最小以下，
        则把主窗口整体加宽（受屏幕可用宽度约束），并把多出来的宽度都分配给侧栏。"""
        if self.isMaximized() or self.isFullScreen():
            return
        delta = current_global_x - self._sidebar_drag_start_global_x
        if delta <= 0:
            return
        sidebar_min = int(self.sidebar.minimumWidth() or 90)
        sidebar_max = int(self.sidebar.maximumWidth() or 420)
        desired = max(sidebar_min, min(sidebar_max, self._sidebar_drag_start_w + delta))

        splitter_w = int(self.chat_splitter.width())
        chat_min = int(self.chat_area.minimumWidth() or 0)
        handle_w = int(self.chat_splitter.handleWidth() or 0)
        max_in_current = splitter_w - chat_min - handle_w
        if desired <= max_in_current:
            return  # 当前窗口足以容纳，让 QSplitter 自己处理

        # 计算需要给主窗口增加多少宽度
        grow = desired - max_in_current
        new_window_w = int(self.width()) + grow

        # 受屏幕可用宽度约束（保留少量边距，避免顶到屏幕边缘）
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = int(screen.availableGeometry().width())
            new_window_w = min(new_window_w, max(int(self.width()), avail - 8))

        if new_window_w <= int(self.width()):
            return

        # 加宽窗口；layout 立即响应后，把多余空间分给侧栏
        self.resize(new_window_w, int(self.height()))
        # 重新读取 splitter 现宽，按目标 desired 重新分配
        new_splitter_w = int(self.chat_splitter.width())
        chat_w = max(chat_min, new_splitter_w - desired - handle_w)
        sized_sidebar = max(sidebar_min, min(sidebar_max, new_splitter_w - chat_w - handle_w))
        self.chat_splitter.setSizes([sized_sidebar, chat_w])

    def _apply_chat_splitter_style(self):
        """客户列表右侧分隔条：深色主题用暗色线，避免浅色竖线。"""
        if not hasattr(self, "chat_splitter") or self.chat_splitter is None:
            return
        is_dark = isDarkTheme()
        handle = "rgba(0,0,0,0.45)" if is_dark else "rgba(0,0,0,0.08)"
        hover = "rgba(7,193,96,0.45)"
        self.chat_splitter.setStyleSheet(f"""
            QSplitter#ChatSplitter::handle {{
                background-color: {handle};
            }}
            QSplitter#ChatSplitter::handle:hover {{
                background-color: {hover};
            }}
        """)

    def _customer_sidebar_sync_ready(self) -> bool:
        """仅在对话页且侧栏可见时同步客户树宽度，避免隐藏态 viewport 宽度为 0 导致项被压窄。"""
        if not hasattr(self, "center_stack") or self.center_stack.currentIndex() != 0:
            return False
        if not hasattr(self, "sidebar") or not self.sidebar.isVisible():
            return False
        return True

    def _refresh_customer_sidebar_layout(self):
        """回到客户对话或侧栏重新显示后，恢复 splitter 与客户列表项宽度。"""
        if not self._customer_sidebar_sync_ready():
            return
        self._apply_sidebar_pref_width()
        self.customer_list.updateGeometries()
        self.customer_list.doItemsLayout()
        self._sync_customer_tree_item_widths()
        self._update_floating_group_header()

    def _sync_customer_tree_item_widths(self):
        if not self._customer_sidebar_sync_ready():
            return
        tree = self.customer_list
        w = tree.viewport().width()
        if w < 50:
            return
        content_w = max(50, w - 6)
        def sync_node(node: QTreeWidgetItem):
            wg = tree.itemWidget(node, 0)
            if wg:
                wg.setFixedWidth(content_w)
                if isinstance(wg, CustomerItemWidget):
                    wg.set_available_width(content_w)
                elif isinstance(wg, CustomerGroupHeaderWidget):
                    wg.set_available_width(content_w)
                wg.adjustSize()
                node.setSizeHint(0, wg.sizeHint())
            for j in range(node.childCount()):
                sync_node(node.child(j))

        for i in range(tree.topLevelItemCount()):
            sync_node(tree.topLevelItem(i))

    def _on_customer_tree_item_clicked(self, item, column=0):
        # 分组标题行：点击展开/收起（即使不显示三角，也可操作）
        if item.childCount() > 0 and item.data(0, CUSTOMER_ROW_KIND_ROLE) != CUSTOMER_ROW_KIND_LOAD_MORE:
            item.setExpanded(not item.isExpanded())
            self._sync_customer_tree_item_widths()
            return
        if item.data(0, CUSTOMER_ROW_KIND_ROLE) == CUSTOMER_ROW_KIND_LOAD_MORE:
            return
        customer_data = item.data(0, Qt.UserRole)
        if not customer_data:
            return
        self.apply_customer_header(customer_data)
        self.customer_selected.emit(customer_data)
        # 详细资料/订单由 DesktopApp._handle_customer_selected 统一加载

    def _on_floating_header_clicked(self):
        """点击顶部悬浮标题直接折叠当前悬浮的目标分组"""
        if hasattr(self, "_floating_target_item") and self._floating_target_item is not None:
            self._floating_target_item.setExpanded(False)
            self._sync_customer_tree_item_widths()
            self._update_floating_group_header()

    def _update_floating_group_header(self):
        """核心浮动分组标题逻辑：当顶部 header 划出边界时将其置顶"""
        tree = self.customer_list
        if not hasattr(self, "floating_group_header"):
            return
            
        bar = tree.verticalScrollBar()
        top_item = tree.itemAt(10, 10)
        
        if top_item is None or bar.maximum() <= 0:
            self.floating_group_header.hide()
            return
            
        target_group = None
        node = top_item
        while node is not None:
            if node.childCount() > 0:
                rect = tree.visualItemRect(node)
                if rect.top() < 0:
                    target_group = node
                    break
            node = node.parent()
            
        if target_group is None:
            self.floating_group_header.hide()
            return
            
        self._floating_target_item = target_group
        
        # 提取标题
        title = ""
        hw = tree.itemWidget(target_group, 0)
        if isinstance(hw, CustomerGroupHeaderWidget):
            title = hw.heading_text()
            
        # 若有父级分组，支持二级导航路径（如 "销售微信号 A > 未分析"）
        parent_group = target_group.parent()
        if parent_group is not None:
            parent_hw = tree.itemWidget(parent_group, 0)
            if isinstance(parent_hw, CustomerGroupHeaderWidget):
                parent_title = parent_hw.heading_text()
                title = f"{parent_title} > {title}"
                
        self.floating_group_header.set_text(title)
        
        # 精准定位在 viewport 内
        vp_rect = tree.viewport().geometry()
        self.floating_group_header.setGeometry(
            vp_rect.x(),
            vp_rect.y(),
            vp_rect.width(),
            28
        )
        self.floating_group_header.show()
        self.floating_group_header.raise_()

    def set_customer_list_loading(self, loading: bool):
        """首屏或刷新客户列表时展示侧栏骨架屏加载态。"""
        stack = getattr(self, "_customer_list_stack", None)
        skeleton = getattr(self, "_customer_list_skeleton", None)
        if stack is None:
            return
        if loading:
            stack.setCurrentIndex(0)
            if skeleton is not None:
                skeleton.start()
        else:
            stack.setCurrentIndex(1)
            if skeleton is not None:
                skeleton.stop()

    def ensure_today_task_customer_for_nav(self, customer: dict) -> None:
        """任务卡片跳转前：确保客户在「今日建议联系」分组内（任务数据未到时先单条占位）。"""
        if not isinstance(customer, dict):
            return
        key = customer_task_key(customer)
        order = list(getattr(self, "_today_task_order", None) or [])
        if key in order:
            return
        order = [key] + [k for k in order if k != key]
        snapshot = getattr(self, "_last_customers_snapshot", None)
        if not snapshot:
            self._today_task_order = order
            return
        self.update_customer_list(snapshot, force_rebuild=True, today_task_order=order)

    def set_today_task_order(self, order: list | None):
        """异步拉取的「今日任务」有序客户键就绪后调用：刷新「今日建议联系」分组。

        order 为 [(raw_customer_id, sales_wechat_id), ...]，顺序与任务列表 priority_rank 一致。
        """
        normalized = None
        if order:
            normalized = []
            seen: set = set()
            for item in order:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                k = (str(item[0] or "").strip(), str(item[1] or "").strip())
                if k[0] and k not in seen:
                    seen.add(k)
                    normalized.append(k)
        if normalized == getattr(self, "_today_task_order", None):
            return
        self._today_task_order = normalized
        snapshot = getattr(self, "_last_customers_snapshot", None)
        if snapshot:
            self.update_customer_list(snapshot, force_rebuild=True, today_task_order=normalized)

    def update_customer_list(
        self, customers, *, force_rebuild: bool = False, today_task_order=_TODAY_ORDER_UNSET
    ):
        # 记录“全量客户源数据”，供搜索框清空时直接重建树，避免分组/隐藏状态残留
        customers = list(customers or [])
        self._last_customers_snapshot = customers
        if today_task_order is not _TODAY_ORDER_UNSET:
            self._today_task_order = list(today_task_order) if today_task_order else None
        active_order = getattr(self, "_today_task_order", None)
        fp = _customers_list_fingerprint(customers)
        if (
            not force_rebuild
            and fp == getattr(self, "_customers_list_fingerprint", None)
            and self.customer_list.topLevelItemCount() > 0
        ):
            self.set_customer_list_loading(False)
            return
        self._customers_list_fingerprint = fp
        self.set_customer_list_loading(False)

        self._customer_tree_rebuild_seq = int(getattr(self, "_customer_tree_rebuild_seq", 0) or 0) + 1
        seq = self._customer_tree_rebuild_seq

        if len(customers) < CUSTOMER_TREE_BG_GROUP_THRESHOLD:
            self.customer_list.setUpdatesEnabled(False)
            try:
                self._rebuild_customer_tree(customers, today_task_order=active_order)
            finally:
                self.customer_list.setUpdatesEnabled(True)
            return

        executor = getattr(self, "_customer_group_executor", None)
        if executor is None:
            self._customer_group_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="cust_grp"
            )
            executor = self._customer_group_executor

        future = executor.submit(
            CUSTOMER_SIDEBAR_GROUP_BUILDER, customers, today_task_order=active_order
        )

        def _apply_groups() -> None:
            if seq != self._customer_tree_rebuild_seq:
                return
            try:
                groups = future.result()
            except Exception as e:
                logger.warning(f"客户侧栏分组计算失败: {e}")
                return
            self.customer_list.setUpdatesEnabled(False)
            try:
                self._rebuild_customer_tree(customers, groups=groups, today_task_order=active_order)
            finally:
                self.customer_list.setUpdatesEnabled(True)

        def _poll_future() -> None:
            if seq != self._customer_tree_rebuild_seq:
                return
            if future.done():
                _apply_groups()
            else:
                QTimer.singleShot(16, _poll_future)

        QTimer.singleShot(0, _poll_future)

    def _rebuild_customer_tree(self, customers, groups=None, today_task_order=None):
        if hasattr(self, "floating_group_header"):
            self.floating_group_header.hide()
        current_key = None
        sel_item = self.customer_list.currentItem()
        if sel_item:
            cur = sel_item.data(0, Qt.UserRole)
            if isinstance(cur, dict):
                current_key = (cur.get("id"), cur.get("sales_wechat_id"))

        self.customer_list.clear()

        target_item = None
        def add_group_node(
            parent_item: QTreeWidgetItem | None,
            title_name: str,
            source: list,
            default_expanded: bool,
            *,
            heading_count: int | None = None,
            group_id: str = "",
        ):
            node = QTreeWidgetItem(parent_item) if parent_item is not None else QTreeWidgetItem()
            node.setText(0, "")
            node.setData(0, Qt.UserRole, None)
            node.setFlags(Qt.ItemIsEnabled)
            if parent_item is None:
                self.customer_list.addTopLevelItem(node)

            header = CustomerGroupHeaderWidget(
                self.customer_list,
                depth=_group_header_depth(parent_item),
            )
            self.customer_list.setItemWidget(node, 0, header)
            # 顶层/容器组也要立刻显示标题（不依赖 _render_group_children）
            cnt = len(source or []) if heading_count is None else int(heading_count)
            header.set_heading(f"{(title_name or '').strip()} ({cnt})")

            n_src = len(source or [])
            state = {
                "title_name": title_name,
                "source": list(source or []),
                "displayed": min(CUSTOMER_GROUP_PAGE_SIZE, n_src),
                "group_id": (group_id or "").strip(),
            }
            node.setData(0, CUSTOMER_GROUP_STATE_ROLE, state)
            node.setExpanded(bool(default_expanded))
            return node

        if groups is None:
            groups = CUSTOMER_SIDEBAR_GROUP_BUILDER(customers, today_task_order=today_task_order)
        for spec in groups:
            # 顶层分组（如今日建议联系、某销售微信号）
            if spec.children:
                total = sum(len(ch.customers or []) for ch in (spec.children or []))
                top = add_group_node(
                    None, spec.title_name, [], spec.default_expanded,
                    heading_count=total, group_id=spec.id,
                )
            else:
                top = add_group_node(
                    None, spec.title_name, list(spec.customers), spec.default_expanded,
                    group_id=spec.id,
                )

            if spec.children:
                # 销售号分组：二级分组（已分析/未分析）作为子节点，每个子节点再渲染客户列表
                top.setData(0, CUSTOMER_GROUP_STATE_ROLE, {
                    "title_name": spec.title_name,
                    "source": [],
                    "displayed": 0,
                    "group_id": spec.id,
                })
                while top.childCount():
                    top.takeChild(0)

                for child_spec in spec.children:
                    # 需求：默认展开一级时，下一级不要展开
                    sub = add_group_node(
                        top, child_spec.title_name, list(child_spec.customers), False,
                        group_id=child_spec.id,
                    )
                    hit = self._render_group_children(sub, current_key)
                    if hit is not None:
                        target_item = hit
            else:
                hit = self._render_group_children(top, current_key)
                if hit is not None:
                    target_item = hit

        if target_item:
            self.customer_list.setCurrentItem(target_item)
            parent = target_item.parent()
            if parent:
                self.customer_list.expandItem(parent)
        else:
            self.customer_list.clearSelection()

        self._sync_customer_tree_item_widths()
        self._update_floating_group_header()

    # ── 商品列表管理 ───────────────────────────────────────────────────────────

    def _remove_load_more_row(self):
        if not self._load_more_item:
            return
        row = self.product_list.row(self._load_more_item)
        if row >= 0:
            self.load_more_btn.setParent(None)
            self.product_list.takeItem(row)
        self._load_more_item = None

    def _on_search_clicked(self, keyword=""):
        self._remove_load_more_row()
        self.product_list.clear()

        final_kw = keyword if keyword is not None else self.search_input.text()
        
        # 将关键词合并到当前过滤字典中
        filters = self._current_filters.copy()
        filters["keyword"] = final_kw
        
        self.filter_requested.emit(filters, 0, 20)

    def _toggle_filter_bar(self):
        """切换筛选面板的显示/隐藏"""
        is_visible = self.filter_bar.isVisible()
        self.filter_bar.setVisible(not is_visible)

    def _on_filter_changed(self, filters):
        """当筛选栏条件变化时触发"""
        self._current_filters = filters
        
        # 应用筛选后自动收起面板
        self.filter_bar.setVisible(False)
        
        # 更新搜索框图标的“已筛选”状态
        is_active = any([
            filters.get("supplier_name"),
            filters.get("cat1"),
            filters.get("province"),
            filters.get("min_price"),
            filters.get("max_price")
        ])
        self.search_input.set_filter_active(is_active)
        
        self._on_search_clicked(None)

    def _on_load_more_clicked(self):
        actual_count = self.product_list.count()
        if self._load_more_item:
            actual_count -= 1

        filters = self._current_filters.copy()
        filters["keyword"] = self.search_input.text()
        
        self.filter_requested.emit(filters, actual_count, 20)

    def render_product_search_page(
        self,
        items_data: list,
        *,
        clear: bool = False,
        has_more: bool = False,
        setup_card=None,
    ) -> list:
        """批量渲染商品搜索结果页（清空 + 插卡 + 加载更多按钮），期间暂停列表重绘。"""
        items_data = list(items_data or [])
        self.product_list.setUpdatesEnabled(False)
        cards = []
        try:
            if clear:
                self._remove_load_more_row()
                self.product_list.clear()
            for product_data in items_data:
                card = self.add_product_card(product_data)
                if setup_card is not None:
                    setup_card(card, product_data)
                cards.append(card)
            self.update_has_more(has_more)
        finally:
            self.product_list.setUpdatesEnabled(True)
        return cards

    def add_product_card(self, product_data):
        row = self.product_list.count()
        if self._load_more_item:
            row -= 1

        widget = ProductItemWidget(product_data)
        item = QListWidgetItem()
        self.product_list.insertItem(row, item)

        target_width = self.product_list.viewport().width()
        if target_width < 100:
            target_width = 430

        widget.setFixedWidth(target_width)
        widget.adjustSize()
        h = widget.sizeHint().height()
        item.setSizeHint(QSize(0, h))
        widget.setMinimumWidth(0)
        widget.setMaximumWidth(16777215)

        self.product_list.setItemWidget(item, widget)
        return widget

    def update_has_more(self, has_more):
        """在列表内容末尾追加「展开更多」行；无更多数据时不显示。"""
        self._remove_load_more_row()
        if not has_more:
            return

        wrapper = ProductLoadMoreRow(self.load_more_btn)
        self._load_more_item = QListWidgetItem(self.product_list)
        self._load_more_item.setFlags(Qt.ItemIsEnabled)
        self._load_more_item.setSizeHint(QSize(0, wrapper.sizeHint().height()))
        self.product_list.setItemWidget(self._load_more_item, wrapper)
        self.load_more_btn.show()

    # ── 窗口吸附（微信贴靠）──────────────────────────────────────────────────

    def _start_calibration(self, pos=None):
        """开启自定义窗口捕获"""
        reply = QMessageBox.information(
            self,
            "吸附校准",
            "点击「确定」后倒计时 3 秒。\n请在 3 秒内，点击并激活您想吸附的软件窗口（例如微信）。",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        if reply == QMessageBox.Ok:
            self.btn_snap_wechat.setProperty("capturing", True)
            self.btn_snap_wechat.setToolTip("正在捕获目标窗口...")
            self.btn_snap_wechat.setIcon(
                (FluentIcon.TARGET if hasattr(FluentIcon, "TARGET") else FluentIcon.PIN).icon(color=Qt.white)
            )
            self.btn_snap_wechat.style().unpolish(self.btn_snap_wechat)
            self.btn_snap_wechat.style().polish(self.btn_snap_wechat)
            QTimer.singleShot(3000, self._finish_calibration)

    def _finish_calibration(self):
        """延迟捕捉前台窗口并落盘"""
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()

        if hwnd == int(self.winId()):
            QMessageBox.warning(self, "无效目标", "不能吸附在自己身上！请选择微信、企业微信或其他外部软件窗口进行校准。")
            self._restore_snap_btn_ui()
            return

        if hwnd:
            t = ctypes.create_unicode_buffer(255)
            c = ctypes.create_unicode_buffer(255)
            user32.GetWindowTextW(hwnd, t, 255)
            user32.GetClassNameW(hwnd, c, 255)

            title_val = t.value
            class_val = c.value

            cfg.set_runtime("snap_title", title_val)
            cfg.set_runtime("snap_class", class_val)
            self.custom_snap_title = title_val
            self.custom_snap_class = class_val

            QMessageBox.information(self, "捕获成功", f"已成功校准吸附目标！\n\n类名: {class_val}\n标题: {title_val}")
        else:
            QMessageBox.warning(self, "捕获失败", "未能捕获到前台窗口，校准失败。")

        self._restore_snap_btn_ui()

    def _restore_snap_btn_ui(self):
        """恢复吸附按钮图标和提示"""
        self.btn_snap_wechat.setProperty("capturing", False)
        if self.is_snapping:
            self.btn_snap_wechat.setProperty("active", True)
            self.btn_snap_wechat.setToolTip("取消吸附微信")
            self.btn_snap_wechat.setIcon(FluentIcon.UNPIN.icon(color=Qt.white))
        else:
            self.btn_snap_wechat.setProperty("active", False)
            self.btn_snap_wechat.setToolTip("左键点击开关吸附；右键点击重新校准窗口")
            self.btn_snap_wechat.setIcon(FluentIcon.PIN.icon(color=Qt.white))
        self.btn_snap_wechat.style().unpolish(self.btn_snap_wechat)
        self.btn_snap_wechat.style().polish(self.btn_snap_wechat)

    def _toggle_snap(self):
        """开启或关闭吸附微信功能 (状态持久化)"""
        self.is_snapping = not self.is_snapping
        if self.is_snapping:
            self.snap_timer.start(50)
        else:
            self.snap_timer.stop()
            
        # 记录状态到设置
        cfg.set_runtime("snap_enabled", "true" if self.is_snapping else "false")
        self._restore_snap_btn_ui()

    def _on_snap_timeout(self):
        """利用 Windows 系统 API 与微信主窗口坐标保持一致"""
        user32 = ctypes.windll.user32
        hwnd = 0

        if hasattr(self, "custom_snap_class") and self.custom_snap_class:
            title_to_search = self.custom_snap_title if self.custom_snap_title else None
            hwnd = user32.FindWindowW(self.custom_snap_class, title_to_search)

        if not hwnd:
            hwnd = user32.FindWindowW("WeChatMainWndForPC", None)
            if not hwnd:
                hwnd = user32.FindWindowW("Qt51514QWindowIcon", "微信")
            if not hwnd:
                hwnd = user32.FindWindowW("Chrome_WidgetWin_0", "微信")
            if not hwnd:
                hwnd = user32.FindWindowW("WeWorkWindow", None)

        if hwnd and user32.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))

            target_x = rect.right - 8
            target_y = rect.top

            if self.x() != target_x or self.y() != target_y:
                self.move(target_x, target_y)

    # ── 液体布局 ─────────────────────────────────────────────

    def resizeEvent(self, event):
        """核心 Liquid Layout：当窗口拉伸时，强制刷新列表卡片的高度"""
        if event:
            super().resizeEvent(event)

        # 同步“自然宽度”：用户手动拖拽窗口边缘/最大化时，
        # 记录此宽度供下一次抽屉收起/展开使用，避免再次出现“收回后回不去”的卡死。
        if not getattr(self, "_drawer_animating", False):
            w = self.width()
            if self._drawer_open:
                self._natural_width = max(self._min_window_width, w - 350)
            else:
                self._natural_width = max(self._min_window_width, w)

        # 1. 商品列表自适应
        p_width = self.product_list.viewport().width()
        if p_width > 50:
            for i in range(self.product_list.count()):
                item = self.product_list.item(i)
                w = self.product_list.itemWidget(item)
                if w and isinstance(w, ProductItemWidget):
                    w.setFixedWidth(p_width)
                    w.adjustSize()
                    item.setSizeHint(QSize(0, w.sizeHint().height()))

        # 2. 订单流水列表自适应 (解决“拉长”问题)
        o_width = self.order_list.viewport().width()
        if o_width > 50:
            for i in range(self.order_list.count()):
                item = self.order_list.item(i)
                w = self.order_list.itemWidget(item)
                if w and isinstance(w, OrderCardWidget):
                    # 动态适配：统一走 safe_card_width（扣除列表 spacing + 悬浮滚动条 + 容错）
                    widget_target_w = safe_card_width(self.order_list)
                    if widget_target_w > 50:
                        w.setFixedWidth(widget_target_w) 
                    w.adjustSize()
                    item.setSizeHint(w.sizeHint())

        self._sync_customer_tree_item_widths()
        self._update_floating_group_header()

    def _apply_global_nav_style(self):
        """侧边导航栏样式"""
        # GlobalNav：无论浅色/深色主题都保持“深底白图标”的可读性
        #（用户反馈：浅色主题下如果变浅，会导致按钮看不清）
        is_dark = isDarkTheme()
        bg = "#20252b"
        border = "rgba(0,0,0,0.45)" if is_dark else "rgba(0,0,0,0.18)"
        self.global_nav.setStyleSheet(f"""
            QWidget#GlobalNav {{
                background-color: {bg};
                border-right: 1px solid {border};
            }}
            QToolButton {{
                border-radius: 8px;
                border: none;
            }}
            QToolButton:hover {{
                background-color: rgba(255, 255, 255, 0.1);
            }}
        """)

    def _apply_sidebar_style(self):
        """左侧客户列表背景样式"""
        bg, _, text, sub_text, hover_bg = self._ui_left_palette()
        # 侧栏竖线由 chat_splitter 分隔条承担，此处不再重复 border-right

        # 统一左侧两块区域（GlobalNav + Sidebar）的底色与分割线风格
        # 并约束控件内边距/圆角，防止窄屏出现“贴边/溢出”的观感
        self.sidebar.setStyleSheet(f"""
            QWidget#Sidebar {{
                background-color: {bg};
            }}

            QLineEdit#CustomerSearch {{
                background-color: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 8px;
                padding: 6px 10px;
                color: {text};
            }}
            QLineEdit#CustomerSearch:focus {{
                border: 1px solid rgba(0, 120, 212, 0.65);
                background-color: {hover_bg};
            }}

            QToolButton#CustomerFilterBtn {{
                border-radius: 8px;
                border: 1px solid rgba(255, 255, 255, 0.10);
                background-color: rgba(255, 255, 255, 0.06);
            }}
            QToolButton#CustomerFilterBtn:hover {{
                background-color: rgba(255, 255, 255, 0.10);
            }}
            QToolButton#CustomerFilterBtn[property-active="true"] {{
                border: 1px solid rgba(7, 193, 96, 150);
                background-color: rgba(7, 193, 96, 55);
            }}

            QScrollBar:vertical {{
                background: transparent;
                width: 6px;
                margin: 2px 2px 2px 0px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(255, 255, 255, 0.25);
                border-radius: 3px;
                min-height: 28px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba(255, 255, 255, 0.35);
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
        """)
        if hasattr(self, "customer_list") and hasattr(self.customer_list, "apply_sidebar_theme"):
            self.customer_list.apply_sidebar_theme(bg, text)

    def _init_customer_pool_filter_menu(self):
        menu = QMenu(self)
        menu.setObjectName("CustomerPoolFilterMenu")
        group = QActionGroup(menu)
        group.setExclusive(True)

        def add_mode(text: str, mode: str):
            act = QAction(text, menu)
            act.setCheckable(True)
            act.setData(mode)
            group.addAction(act)
            menu.addAction(act)
            return act

        act_all = add_mode("全部客户", "all")
        menu.addSeparator()
        add_mode("无手机号", "no_phone")
        add_mode("无单位信息", "no_unit")
        add_mode("有微信备注", "has_wechat_remark")
        add_mode("有历史订单", "has_orders")

        act_all.setChecked(True)
        group.triggered.connect(self._on_customer_pool_filter_changed)
        self.btn_customer_filter.setMenu(menu)
        self.btn_customer_filter.setPopupMode(ToolButton.InstantPopup)
        self._refresh_customer_filter_btn_ui()

    def _on_customer_pool_filter_changed(self, action: QAction):
        mode = str(action.data() or "all")
        self._customer_pool_filter_mode = mode
        # 触发一次“重新渲染”（保留现有搜索关键词）
        try:
            self.update_customer_list(
                getattr(self, "_last_customers_snapshot", []) or [],
                force_rebuild=True,
            )
        except Exception:
            pass
        self._refresh_customer_filter_btn_ui()

    def _refresh_customer_filter_btn_ui(self):
        active = (getattr(self, "_customer_pool_filter_mode", "all") or "all") != "all"
        self.btn_customer_filter.setProperty("active", active)
        tip = "原始客户池筛选" if not active else "已启用原始客户池筛选"
        self.btn_customer_filter.setToolTip(tip)
        self.btn_customer_filter.style().unpolish(self.btn_customer_filter)
        self.btn_customer_filter.style().polish(self.btn_customer_filter)

    def _ui_left_palette(self) -> tuple[str, str, str, str, str]:
        """左侧区域（GlobalNav/Sidebar）统一调色板。"""
        is_dark = isDarkTheme()
        if is_dark:
            # 深色主题：保持左侧功能栏为深底，保证白色图标清晰可见
            bg = "#20252b"
            border = "rgba(255,255,255,0.10)"
            text = "#eeeeee"
            sub_text = "#aaaaaa"
            hover_bg = "rgba(255,255,255,0.08)"
        else:
            bg = "#f6f7f9"
            border = "rgba(0,0,0,0.08)"
            text = "#222222"
            sub_text = "#666666"
            hover_bg = "rgba(0,0,0,0.03)"
        return bg, border, text, sub_text, hover_bg

    def _apply_drawer_style(self):
        """右侧详情抽屉背景样式"""
        is_dark = isDarkTheme()
        bg = "#272727" if is_dark else "#ffffff"
        border = "#3a3a3a" if is_dark else "rgba(0, 0, 0, 0.1)"
        self.drawer_bg.setStyleSheet(f"QFrame#DrawerBg {{ background-color: {bg}; border-left: 1px solid {border}; }}")

    def _apply_content_style(self):
        """同步聊天区域与商品列表区域的背景"""
        is_dark = isDarkTheme()
        bg = "#272727" if is_dark else "#ffffff"
        
        # 页头文字颜色适配 (增加属性检查，防止初始化顺序导致的崩溃)
        if hasattr(self, "lbl_header_unit") and hasattr(self, "lbl_header_info"):
            style_label(self.lbl_header_unit, "body_emphasis")
            style_label(self.lbl_header_info, "caption")
        
        # 应用于聊天容器和商品主页
        style = f"background-color: {bg}; border: none;"
        if hasattr(self, "chat_area"):
            self.chat_area.setStyleSheet(f"QWidget#ChatArea {{ {style} }}")
        if hasattr(self, "product_page"):
            self.product_page.setStyleSheet(f"QWidget#ProductPage {{ {style} }}")
        if hasattr(self, "settings_page"):
            self.settings_page.setStyleSheet(f"QWidget#SettingsPage {{ {style} }}")
        
        # 针对列表组件的彻底透明化与对其优化：移除所有内建的 item 选中与悬浮样式
        list_style = """
            QListWidget { 
                background-color: transparent; 
                border: none; 
                outline: none; 
            }
            QListWidget::item { 
                border: none; 
                padding: 0px; 
                margin: 0px; 
                background-color: transparent;
            }
            QListWidget::item:selected, QListWidget::item:hover, QListWidget::item:active {
                border: none;
                background-color: transparent;
                outline: none;
            }
        """
        if hasattr(self, "product_list"): 
            self.product_list.setStyleSheet(list_style)
            self.product_list.viewport().setContentsMargins(0, 0, 0, 0)
        if hasattr(self, "customer_list") and hasattr(self.customer_list, "apply_sidebar_theme"):
            sidebar_bg, _, sidebar_text, _, _ = self._ui_left_palette()
            self.customer_list.apply_sidebar_theme(sidebar_bg, sidebar_text)
        if hasattr(self, "order_list"): 
            self.order_list.setStyleSheet(list_style)
            self.order_list.viewport().setContentsMargins(0, 0, 0, 0)
        if hasattr(self, "sales_bindings_list"):
            self.sales_bindings_list.setStyleSheet(list_style)
            self.sales_bindings_list.viewport().setContentsMargins(0, 0, 0, 0)
        if hasattr(self, "customer_leads_page"):
            for lw in self.customer_leads_page.iter_leads_list_widgets():
                lw.setStyleSheet(list_style)
                lw.viewport().setContentsMargins(0, 0, 0, 0)

    def _toggle_theme(self):
        """切换深浅主题模式"""
        is_dark = not isDarkTheme()
        theme = Theme.DARK if is_dark else Theme.LIGHT
        setTheme(theme)

        # 持久化主题设置
        cfg.set_runtime("theme_mode", "dark" if is_dark else "light")
        
        # 重新应用所有局部样式方法
        self._apply_global_nav_style()
        self._apply_sidebar_style()
        self._apply_content_style()
        self._apply_drawer_style()
        self._apply_chat_splitter_style()
        
        # 刷新详情页与容器样式
        self.info_page._apply_theme_style()
        self.chat_page._apply_theme_style()
        self.search_input._apply_theme_style()
        self.filter_bar._apply_theme_style()
        if hasattr(self, "floating_group_header"):
            self.floating_group_header._apply_theme_style()
        if hasattr(self, "task_allocation_page"):
            self.task_allocation_page._apply_theme_style()
        if hasattr(self, "phone_workbench"):
            self.phone_workbench._apply_theme_style()
        if hasattr(self, "customer_leads_page"):
            self.customer_leads_page._apply_theme_style()
            self.customer_leads_page._rendered_fingerprints.pop("claimed", None)
            self.customer_leads_page._rendered_fingerprints.pop("favorite", None)
            self.customer_leads_page._refresh_tab_list("claimed")
            self.customer_leads_page._refresh_tab_list("favorite")
        if hasattr(self, "load_more_btn"):
            self.load_more_btn._apply_theme_style()
        
        # --- 增量刷新：遍历所有动态列表项并热刷新其内部样式 ---
        for ti in range(self.customer_list.topLevelItemCount()):
            p = self.customer_list.topLevelItem(ti)
            ph = self.customer_list.itemWidget(p, 0)
            if ph and hasattr(ph, "_apply_theme_style"):
                ph._apply_theme_style()
        for item in self._iter_customer_tree_leaves():
            widget = self.customer_list.itemWidget(item, 0)
            if widget and hasattr(widget, "_apply_theme_style"):
                widget._apply_theme_style()
        for list_widget in (self.product_list, self.order_list):
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                widget = list_widget.itemWidget(item)
                if widget and hasattr(widget, "_apply_theme_style"):
                    widget._apply_theme_style()
                elif widget and hasattr(widget, "_apply_theme_styles"):
                    widget._apply_theme_styles()
        
        
        # 强制在主题切换后进行一次全局对齐，防止绘制残影或布局错位
        QTimer.singleShot(50, self._force_refresh_all_layouts)
        
        logger.info(f"Theme switched to: {'DARK' if is_dark else 'LIGHT'} (Settings Saved)")

    def _filter_customers(self, text):
        """根据搜索框文字过滤客户列表 (支持单位、姓名、电话、客户ID、微信备注、销售号昵称)；与分组分页联动，匹配项重新从首屏条数起展示。"""
        kw = text.strip().lower()
        # 清空搜索：直接按全量源数据重建，避免“容器分组/子分组”隐藏状态残留导致分组消失
        if not kw:
            try:
                self.update_customer_list(
                    getattr(self, "_last_customers_snapshot", []) or [],
                    force_rebuild=True,
                )
            except Exception:
                # 若还没拿到过列表数据，则走下面的增量过滤逻辑兜底
                pass
            else:
                return
        tree = self.customer_list
        # 批量重建期间暂停重绘，避免逐组刷新造成的闪烁与掉帧
        tree.setUpdatesEnabled(False)
        try:
            # 先过滤“有数据源”的分组，再处理“容器节点”（source 为空的顶层销售号组等）
            container_nodes: list[QTreeWidgetItem] = []
            for node in self._iter_group_nodes():
                state = node.data(0, CUSTOMER_GROUP_STATE_ROLE)
                if not isinstance(state, dict):
                    continue
                src = state.get("source") or []
                # “容器节点”（例如销售号顶层）source 为空时不参与过滤与隐藏
                if not src:
                    container_nodes.append(node)
                    continue

                active = self._active_customers_for_group_state(state)
                new_state = {**state, "displayed": min(CUSTOMER_GROUP_PAGE_SIZE, len(active))}
                node.setData(0, CUSTOMER_GROUP_STATE_ROLE, new_state)
                self._render_group_children(node)
                node.setHidden(len(active) == 0)
                if active and kw:
                    tree.expandItem(node)

            # 容器节点：若所有子分组都被隐藏，则隐藏容器；否则展示并在搜索时自动展开
            for node in container_nodes:
                has_visible_child = False
                for j in range(node.childCount()):
                    ch = node.child(j)
                    if not ch.isHidden():
                        has_visible_child = True
                        break
                node.setHidden(not has_visible_child)
                if has_visible_child and kw:
                    tree.expandItem(node)
        finally:
            tree.setUpdatesEnabled(True)
        self._sync_customer_tree_item_widths()

    def shutdown_background_workers(self) -> None:
        """关闭后台线程/定时器，避免非 daemon 线程拖住进程退出。"""
        self._customer_tree_rebuild_seq = int(
            getattr(self, "_customer_tree_rebuild_seq", 0) or 0
        ) + 1
        self._bump_customer_select_progress_token()
        leads = getattr(self, "customer_leads_page", None)
        if leads is not None and hasattr(leads, "stop_auto_refresh"):
            try:
                leads.stop_auto_refresh()
            except Exception:
                pass
        executor = getattr(self, "_customer_group_executor", None)
        if executor is not None:
            try:
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    executor.shutdown(wait=False)
            except Exception:
                pass
            self._customer_group_executor = None

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown_background_workers()
        super().closeEvent(event)
