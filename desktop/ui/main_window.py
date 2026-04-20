"""
主窗口：MainWindow
所有可复用子组件已拆分至各自模块：
  - ui/chat_widgets.py      → QuickTextEdit / ChatActionToolbar / ChatBubble / AIChatWidget
  - ui/customer_info.py     → CustomerInfoWidget
  - ui/widgets/search.py    → SearchTag / TagLineEdit / TagSearchWidget
  - ui/widgets/product_card.py → ProductItemWidget
  - ui/widgets/form_controls.py → MultiSelectComboBox / NoScrollComboBox / CalendarPopup / DatePickerBtn
  - ui/widgets/cascader.py  → CascaderPopup / RegionCascader
"""
import ctypes
from ctypes import wintypes

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidgetItem, QLabel, QFrame,
    QStackedWidget, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QListView,QAbstractItemView,
)
from PySide6.QtCore import (
    Qt, Signal, QSize, QTimer, QSettings, QUrl,
    QPropertyAnimation, QEasingCurve, QRect, QParallelAnimationGroup,
)
from PySide6.QtGui import QColor, QGuiApplication
from logger_cfg import logger
from config_loader import cfg

from qfluentwidgets import (
    ListWidget,
    BodyLabel, CaptionLabel, SubtitleLabel, StrongBodyLabel,
    TransparentPushButton, TransparentToolButton, ToolButton,
    FluentIcon, isDarkTheme, SearchLineEdit,
    setTheme, Theme, InfoBar, InfoBarPosition,
    ToolTipFilter, ToolTipPosition
)

from ui.chat_widgets import AIChatWidget
from ui.customer_info import CustomerInfoWidget
from ui.widgets.product_card import ProductItemWidget
from ui.widgets.search import TagSearchWidget
from ui.widgets.filter_bar import ProductFilterBar
from ui.widgets.order_card import OrderCardWidget


class CustomerItemWidget(QWidget):
    """自定义客户列表项：双行展示 (单位 + 姓名/电话)"""
    def __init__(self, customer_data, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        # 增加右边距 (从 10 增加到 22) 以预留滚动条空间，防止重叠
        layout.setContentsMargins(10, 5, 22, 5)
        layout.setSpacing(2)
        
        # 第一行：单位名称
        unit_name = customer_data.get("unit_name") or customer_data.get("unit_type") or "未知单位"
        self.unit_lbl = BodyLabel(unit_name)
        
        # 第二行：姓名 + 脱敏电话
        name = customer_data.get("customer_name") or "未知"
        phone = str(customer_data.get("phone") or "")
        masked_phone = ""
        if len(phone) >= 7:
            masked_phone = f"{phone[:2]}**{phone[-2:]}"
        elif phone:
            masked_phone = phone
            
        self.info_lbl = CaptionLabel(f"{name} | {masked_phone}")
        
        # --- 走马灯逻辑初始化 ---

        # --- 走马灯逻辑初始化 ---
        self.full_unit = unit_name
        self.full_info = f"{name} | {phone}" # 悬浮时显示原始电话
        # 为防止重叠滚动条，默认显示字数缩减为 7
        self.display_unit = unit_name[:7] + "..." if len(unit_name) > 7 else unit_name
        self.display_info = f"{name} | {masked_phone}"
        
        self.unit_lbl.setText(self.display_unit)
        self.info_lbl.setText(self.display_info)
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_marquee)
        self.offset = 0
        
        # 存储搜索文本 (单位 + 姓名 + 原始电话)
        self.search_text = f"{unit_name} {name} {phone}".lower()
        
        layout.addWidget(self.unit_lbl)
        layout.addWidget(self.info_lbl)
        
        # 应用初始样式
        self._apply_theme_style()

    def _apply_theme_style(self):
        """动态适配深浅主题文字颜色"""
        is_dark = isDarkTheme()
        unit_color = "#eeeeee" if is_dark else "#333333"
        info_color = "#aaaaaa" if is_dark else "#666666"
        self.unit_lbl.setStyleSheet(f"font-weight: bold; color: {unit_color}; font-size:11px;")
        self.info_lbl.setStyleSheet(f"color: {info_color}; font-size:11px;")

    def enterEvent(self, event):
        """鼠标进入：如果文字过长，启动走马灯"""
        # 相应调整阈值
        if len(self.full_unit) > 7 or len(self.full_info) > 10: 
            self.offset = 0
            self.timer.start(250)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标移开：重置文字"""
        self.timer.stop()
        self.unit_lbl.setText(self.display_unit)
        self.info_lbl.setText(self.display_info)
        super().leaveEvent(event)

    def _update_marquee(self):
        """同步滚动逻辑"""
        self.offset += 1
        
        # 滚动显示逻辑 (单位名)
        if len(self.full_unit) > 7:
            text = self.full_unit + "   "
            idx = self.offset % len(text)
            self.unit_lbl.setText((text + text)[idx:idx+7])
        
        # 滚动显示逻辑 (姓名电话)
        if len(self.full_info) > 10: # 侧边栏宽度限制
            text = self.full_info + "   "
            idx = self.offset % len(text)
            self.info_lbl.setText((text + text)[idx:idx+10])

class MainWindow(QMainWindow):
    """
    主窗口：极致窄屏适配 (430x720)。
    导航：极窄全局左侧导航栏 + 中央内容区 + 右侧可展开抽屉。
    """
    search_requested = Signal(str, int, int)
    customer_selected = Signal(dict)
    sync_triggered = Signal()          # 手动触发同步信号
    upload_wechat_clicked = Signal()   # 手动触发导入微信流水库
    tab_changed = Signal(int)          # 标签切换信号
    order_history_requested = Signal(int)  # 请求加载订单流水（传入 customer_id）
    filter_requested = Signal(dict, int, int) # [filters, skip, limit]
    shop_metadata_refresh_requested = Signal(str) # 联动信号：传递店铺名

    def __init__(self, username: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"微企 AI - {username}")
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

        self.btn_nav_chat = create_nav_btn(FluentIcon.CHAT, "客户对话")
        self.btn_nav_shop = create_nav_btn(FluentIcon.SHOPPING_CART, "商品货源")

        self.btn_import_wechat = create_nav_btn(FluentIcon.DICTIONARY_ADD, "导入微信聊天记录")
        self.btn_import_wechat.clicked.connect(self.upload_wechat_clicked.emit)

        self.btn_snap_wechat = create_nav_btn(FluentIcon.PIN, "窗口收纳吸附")
        self.btn_snap_wechat.setContextMenuPolicy(Qt.CustomContextMenu)
        self.btn_snap_wechat.customContextMenuRequested.connect(self._start_calibration)
        self.btn_snap_wechat.clicked.connect(self._toggle_snap)

        self.btn_theme_toggle = create_nav_btn(FluentIcon.CONSTRACT, "切换主题模式")
        self.btn_theme_toggle.clicked.connect(self._toggle_theme)

        self.logout_btn = create_nav_btn(FluentIcon.POWER_BUTTON, "安全退出")

        nav_v_layout.addWidget(self.btn_nav_chat)
        nav_v_layout.addWidget(self.btn_nav_shop)
        nav_v_layout.addStretch()
        nav_v_layout.addWidget(self.btn_import_wechat)
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
        self.sidebar = QWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(110)
        self._apply_sidebar_style()
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(0, 8, 0, 8)
        sidebar_layout.setSpacing(6)

        # 客户搜索框
        self.customer_search = SearchLineEdit()
        self.customer_search.setPlaceholderText("搜索客户...")
        self.customer_search.setFixedWidth(100)
        self.customer_search.textChanged.connect(self._filter_customers)
        sidebar_layout.addWidget(self.customer_search, 0, Qt.AlignHCenter)

        self.customer_list = ListWidget()
        self.customer_list.setObjectName("CustomerList")
        self.customer_list.setFocusPolicy(Qt.NoFocus)
        self.customer_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.customer_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.customer_list.itemClicked.connect(self._on_customer_item_clicked)
        sidebar_layout.addWidget(self.customer_list)
        # 移除 sidebar_layout.addStretch() 以允许 ListWidget 铺满垂直空间

        chat_module_layout.addWidget(self.sidebar)

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
        self.lbl_header_unit.setStyleSheet("font-size: 13px;")
        self.lbl_header_unit.setFixedWidth(200) # 限制宽度防止抖动
        
        self.lbl_header_info = CaptionLabel("")
        self.lbl_header_info.setStyleSheet("font-size: 11px;")
        
        hi_layout.addWidget(self.lbl_header_unit)
        hi_layout.addWidget(self.lbl_header_info)
        action_layout.addWidget(self.header_info_container)

        action_layout.addStretch()

        self.btn_action_phone = TransparentToolButton(FluentIcon.PHONE)
        self.btn_action_phone.setToolTip("电话记录")
        self.btn_action_phone.installEventFilter(ToolTipFilter(self.btn_action_phone, 300, ToolTipPosition.BOTTOM))

        self.btn_action_order = TransparentToolButton(FluentIcon.SHOPPING_CART)
        self.btn_action_order.setToolTip("订单信息")
        self.btn_action_order.installEventFilter(ToolTipFilter(self.btn_action_order, 300, ToolTipPosition.BOTTOM))

        self.btn_action_info = TransparentToolButton(FluentIcon.PEOPLE)
        self.btn_action_info.setToolTip("客户详细资料")
        self.btn_action_info.installEventFilter(ToolTipFilter(self.btn_action_info, 300, ToolTipPosition.BOTTOM))

        action_layout.addWidget(self.btn_action_phone)
        action_layout.addWidget(self.btn_action_order)
        action_layout.addWidget(self.btn_action_info)

        chat_area_layout.addWidget(self.action_bar)

        self.chat_page = AIChatWidget()
        chat_area_layout.addWidget(self.chat_page)

        chat_module_layout.addWidget(self.chat_area)
        chat_module_layout.setStretch(0, 0)
        chat_module_layout.setStretch(1, 1)
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
        prod_layout.addWidget(self.product_list)

        self.load_more_btn = TransparentPushButton(FluentIcon.CHEVRON_DOWN_MED, "展开更多货源")
        self.load_more_btn.setCursor(Qt.PointingHandCursor)
        self.load_more_btn.clicked.connect(self._on_load_more_clicked)
        self._load_more_item = None

        self.center_stack.addWidget(self.product_page)

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

        # 电话与订单空页面占位
        phone_page = QWidget()
        p_l = QVBoxLayout(phone_page)
        self.phone_label = SubtitleLabel("请先选择左侧客户")
        self.phone_label.setAlignment(Qt.AlignCenter)
        p_l.addWidget(self.phone_label)

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
        self.drawer_stack.addWidget(phone_page)
        self.drawer_stack.addWidget(self.order_page)

        drawer_bg_layout.addWidget(self.drawer_stack)
        drawer_layout.addWidget(self.drawer_bg)
        self.root_h_layout.addWidget(self.drawer_widget)

        # ── 信号连接 ──
        self.btn_nav_chat.clicked.connect(lambda: self._on_tab_changed(0))
        self.btn_nav_shop.clicked.connect(lambda: self._on_tab_changed(2))

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
        self._collapsed_width = 430  # 基础收起状态宽度
        
        # 最后统一应用样式，确保所有子控件已创建
        self._apply_content_style()
        self._on_tab_changed(0)

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
        _titles = {0: "客户详细资料", 1: "联系电话", 2: "历史订单流水"}

        # 如果已展开：点击同一个图标 → 收起；不同图标 → 切换内容
        if self._drawer_open:
            if self.drawer_stack.currentIndex() == index:
                self._drawer_open = False
            else:
                self.drawer_stack.setCurrentIndex(index)
                self.drawer_title.setText(_titles.get(index, "详细信息"))
                QTimer.singleShot(50, self._force_refresh_all_layouts)
                return
        else:
            self.drawer_stack.setCurrentIndex(index)
            self.drawer_title.setText(_titles.get(index, "详细信息"))
            self._drawer_open = True

        drawer_target = 350 if self._drawer_open else 0
        window_target = self._collapsed_width + drawer_target

        self.setMinimumWidth(min(self._collapsed_width, window_target))
        self.setMaximumWidth(16777215)
        self.drawer_widget.setMinimumWidth(0)
        self.drawer_widget.setMaximumWidth(350)

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
            if not self._drawer_open:
                self.drawer_widget.setMaximumWidth(0)
                self.setMaximumWidth(430)
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
        
        # 极致防丢：如果探测到的宽度异常（如抽屉未开或正在动画），则根据当前抽屉状态强制设定安全渲染宽度
        if self._drawer_open and viewport_w < 200:
            target_width = 320 # 标准 350 宽度下的安全内容区
        elif not self._drawer_open:
            target_width = 320 # 预案宽度
        else:
            target_width = viewport_w
            
        if not orders:
            # 当数据为空时展示占位提示
            item = QListWidgetItem(self.order_list)
            placeholder = QLabel("暂无订单记录")
            is_dark = isDarkTheme()
            placeholder.setStyleSheet(f"color: {'#888888' if is_dark else '#999999'}; font-size: 13px; margin-top: 50px;")
            placeholder.setAlignment(Qt.AlignCenter)
            item.setSizeHint(QSize(target_width, 150))
            self.order_list.addItem(item)
            self.order_list.setItemWidget(item, placeholder)
            return

        for order in orders:
            item = QListWidgetItem(self.order_list)
            widget = OrderCardWidget(order)
            
            # 锁定宽度适配容器，留出足够的余位防止横向溢出
            widget.setFixedWidth(target_width - 20)
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
        self.resizeEvent(None)

    def _on_tab_changed(self, index):
        """切换全局导航模块（chat_module=index 0, product_page=index 1 in center_stack）"""
        if index == 0:
            self.center_stack.setCurrentIndex(0)
        elif index == 2:  # 外部逻辑仍传 2（商品），内部映射到 center_stack index 1
            self.center_stack.setCurrentIndex(1)
            # 切换到商品时，自动合上右侧详情面板
            if self._drawer_open:
                self._toggle_drawer(self.drawer_stack.currentIndex())
            # 延迟触发界面的全面重绘，解决初始进入时宽度为0导致的产品名不换行问题
            QTimer.singleShot(100, self._force_refresh_all_layouts)

        self.tab_changed.emit(index)

    def switch_tab(self, index):
        self._on_tab_changed(index)

    def _on_customer_item_clicked(self, item):
        customer_data = item.data(Qt.UserRole)
        self.customer_selected.emit(customer_data)
        
        # --- 对话页头实时信息同步 ---
        unit = customer_data.get("unit_name") or customer_data.get("unit_type") or "未知单位"
        name = customer_data.get("customer_name") or "未知"
        phone = str(customer_data.get("phone") or "")
        
        # 限制名称长度防止挤压布局 (15字)
        display_unit = unit[:15] + "..." if len(unit) > 15 else unit
        self.lbl_header_unit.setText(display_unit)
        self.lbl_header_info.setText(f"{name} | {phone}")
        # ---------------------------

        # 动态更新电话面板内容
        phone_number = customer_data.get("phone") if customer_data else None
        if phone_number:
            self.phone_label.setText(f"☎ 联系电话：\n\n{phone_number}")
        else:
            self.phone_label.setText("该客户暂无联系方式")

        # 自动触发订单流水加载（不管抽屉是否展开，先预加载数据）
        customer_id = customer_data.get("id") if customer_data else None
        if customer_id:
            self.order_history_requested.emit(customer_id)

    def update_customer_list(self, customers):
        # 1. 记忆当前选中
        current_phone = None
        sel_item = self.customer_list.currentItem()
        if sel_item:
            current_phone = sel_item.data(Qt.UserRole).get("phone")

        self.customer_list.clear()

        target_item = None
        for c in customers:
            item = QListWidgetItem(self.customer_list)
            item.setData(Qt.UserRole, c)
            
            # 使用自定义 Widget
            widget = CustomerItemWidget(c)
            item.setSizeHint(widget.sizeHint())
            self.customer_list.addItem(item)
            self.customer_list.setItemWidget(item, widget)

            if current_phone and c.get("phone") == current_phone:
                target_item = item

        # 2. 智能恢复选中状态
        if target_item:
            self.customer_list.setCurrentItem(target_item)
        else:
            self.customer_list.clearSelection()
            self.customer_list.setCurrentRow(-1)

    # ── 商品列表管理 ───────────────────────────────────────────────────────────

    def _on_search_clicked(self, keyword=""):
        # 先解除按钮的父子关系，防止随 clear() 被 Qt 自动销毁
        self.load_more_btn.setParent(None)
        self.product_list.clear()
        self._load_more_item = None

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
        """将『点击加载』按钮集成到列表流末尾"""
        if has_more:
            if self._load_more_item:
                row = self.product_list.row(self._load_more_item)
                if row >= 0:
                    self.product_list.takeItem(row)
                self._load_more_item = None

            wrapper = QWidget()
            w_layout = QHBoxLayout(wrapper)
            w_layout.setContentsMargins(0, 5, 0, 10)
            w_layout.addStretch()
            w_layout.addWidget(self.load_more_btn)
            w_layout.addStretch()

            self._load_more_item = QListWidgetItem(self.product_list)
            self._load_more_item.setSizeHint(QSize(0, 60))
            self.product_list.setItemWidget(self._load_more_item, wrapper)
            self.load_more_btn.show()
        else:
            if self._load_more_item:
                row = self.product_list.row(self._load_more_item)
                if row >= 0:
                    self.load_more_btn.setParent(None)
                    self.product_list.takeItem(row)
                self._load_more_item = None

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
                    # 动态适配：留出 25px 空间（6px 滚动条 + 边距 + 容错）
                    widget_target_w = o_width - 25
                    if widget_target_w > 50:
                        w.setFixedWidth(widget_target_w) 
                    w.adjustSize()
                    item.setSizeHint(w.sizeHint())

    def _apply_global_nav_style(self):
        """侧边导航栏样式"""
        is_dark = isDarkTheme()
        bg = "#1a1a1a" if is_dark else "#20252b"
        border = "#101010" if is_dark else "rgba(0,0,0,0.1)"
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
        is_dark = isDarkTheme()
        bg = "#202020" if is_dark else "#f6f6f6"
        border = "#2a2a2a" if is_dark else "rgba(0, 0, 0, 0.05)"
        self.sidebar.setStyleSheet(f"QWidget#Sidebar {{ background-color: {bg}; border-right: 1px solid {border}; }}")

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
            unit_col = "#eeeeee" if is_dark else "#333333"
            info_col = "#aaaaaa" if is_dark else "#666666"
            self.lbl_header_unit.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {unit_col};")
            self.lbl_header_info.setStyleSheet(f"font-size: 11px; color: {info_col};")
        
        # 应用于聊天容器和商品主页
        style = f"background-color: {bg}; border: none;"
        if hasattr(self, "chat_area"):
            self.chat_area.setStyleSheet(f"QWidget#ChatArea {{ {style} }}")
        if hasattr(self, "product_page"):
            self.product_page.setStyleSheet(f"QWidget#ProductPage {{ {style} }}")
        
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
        if hasattr(self, "customer_list"): 
            self.customer_list.setStyleSheet(list_style)
            self.customer_list.viewport().setContentsMargins(0, 0, 0, 0)
        if hasattr(self, "order_list"): 
            self.order_list.setStyleSheet(list_style)
            self.order_list.viewport().setContentsMargins(0, 0, 0, 0)

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
        
        # 刷新详情页与容器样式
        self.info_page._apply_theme_style()
        self.chat_page._apply_theme_style()
        self.search_input._apply_theme_style()
        self.filter_bar._apply_theme_style()
        
        # --- 增量刷新：遍历所有动态列表项并热刷新其内部样式 ---
        all_lists = [self.customer_list, self.product_list, self.order_list]
        for list_widget in all_lists:
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                widget = list_widget.itemWidget(item)
                if widget and hasattr(widget, "_apply_theme_style"):
                    widget._apply_theme_style()
                elif widget and hasattr(widget, "_apply_theme_styles"): # OrderCardWidget 使用的是复数形式
                    widget._apply_theme_styles()
        
        
        # 强制在主题切换后进行一次全局对齐，防止绘制残影或布局错位
        QTimer.singleShot(50, self._force_refresh_all_layouts)
        
        logger.info(f"Theme switched to: {'DARK' if is_dark else 'LIGHT'} (Settings Saved)")

    def _filter_customers(self, text):
        """根据搜索框文字过滤客户列表 (支持单位、姓名、电话)"""
        kw = text.strip().lower()
        for i in range(self.customer_list.count()):
            item = self.customer_list.item(i)
            widget = self.customer_list.itemWidget(item)
            if widget and hasattr(widget, "search_text"):
                match = kw in widget.search_text
                item.setHidden(not match)
