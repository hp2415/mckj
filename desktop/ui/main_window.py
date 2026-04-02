from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, 
    QPushButton, QListWidget, QListWidgetItem, QLabel, QFrame,
    QStackedWidget, QScrollArea, QFormLayout, QTextEdit
)
from PySide6.QtGui import QPixmap, QFont
from PySide6.QtCore import Qt, Signal, QSize

class ProductItemWidget(QFrame):
    """
    单个商品卡片组件。
    """
    def __init__(self, product_data, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedHeight(120)
        self.setStyleSheet("""
            QFrame { background-color: #ffffff; border: 1px solid #eeeeee; border-radius: 8px; }
            QLabel { border: none; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(15)

        self.img_label = QLabel()
        self.img_label.setFixedSize(100, 100)
        self.img_label.setStyleSheet("background-color: #f8f8f8; border-radius: 4px;")
        self.img_label.setText("Loading...")
        self.img_label.setScaledContents(True)
        layout.addWidget(self.img_label)

        info_layout = QVBoxLayout()
        self.name_label = QLabel(product_data.get("product_name", "未知商品"))
        self.name_label.setWordWrap(True)
        self.name_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #333;")
        info_layout.addWidget(self.name_label)

        self.price_label = QLabel(f"￥ {product_data.get('price', 0.0)}")
        self.price_label.setStyleSheet("font-size: 15px; color: #d4380d; font-weight: bold;")
        info_layout.addWidget(self.price_label)

        supplier = product_data.get('supplier_name', '平台自营')
        self.supplier_label = QLabel(f"供应商: {supplier}")
        self.supplier_label.setStyleSheet("font-size: 11px; color: #999;")
        info_layout.addWidget(self.supplier_label)
        
        info_layout.addStretch()
        layout.addLayout(info_layout)

    def update_image(self, pixmap):
        """异步加载完成后的图片更新回调"""
        self.img_label.setText("") # 清除 Loading 文字
        self.img_label.setPixmap(pixmap)

class CustomerInfoWidget(QWidget):
    """
    客户详情信息面板：左侧静态展示，右侧动态编辑。
    """
    save_clicked = Signal(str, dict) # 提交手机号及待更新的数据

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # 标题
        title_label = QLabel("客户档案详情")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #333;")
        layout.addWidget(title_label)

        # 分割布局：静态信息与动态信息
        
        
        # 1. 静态基础资料 (只读)
        self.static_group = QFrame()
        self.static_group.setStyleSheet("background-color: #f9f9f9; border-radius: 8px; padding: 10px;")
        static_layout = QFormLayout(self.static_group)
        
        self.lbl_name = QLabel("-")
        self.lbl_phone = QLabel("-")
        self.lbl_unit = QLabel("-")
        
        static_layout.addRow("客户姓名:", self.lbl_name)
        static_layout.addRow("联系电话:", self.lbl_phone)
        static_layout.addRow("所属单位:", self.lbl_unit)
        
        layout.addWidget(self.static_group)

        # 2. 动态跟进信息 (可编辑)
        self.dynamic_group = QWidget()
        dynamic_layout = QFormLayout(self.dynamic_group)
        
        self.edit_title = QLineEdit()
        self.edit_title.setPlaceholderText("例如：李局、张总")
        
        self.edit_budget = QLineEdit()
        self.edit_budget.setPlaceholderText("请输入预计单笔采购预算")
        
        self.edit_profile = QTextEdit()
        self.edit_profile.setPlaceholderText("记录客户性格、偏好、历史沟通重点等...")
        self.edit_profile.setMinimumHeight(150)
        
        dynamic_layout.addRow("我的称呼:", self.edit_title)
        dynamic_layout.addRow("预估预算:", self.edit_budget)
        dynamic_layout.addRow("运营画像:", self.edit_profile)
        
        layout.addWidget(self.dynamic_group)

        # 3. 操作按钮
        self.save_btn = QPushButton("保存同步至服务器")
        self.save_btn.setFixedHeight(40)
        self.save_btn.setStyleSheet("""
            QPushButton { background-color: #1890ff; color: white; border-radius: 4px; font-weight: bold; }
            QPushButton:hover { background-color: #40a9ff; }
        """)
        self.save_btn.clicked.connect(self._on_save_clicked)
        layout.addWidget(self.save_btn)
        
        layout.addStretch()
        
        self.current_phone = None

    def set_customer(self, data):
        self.current_phone = data.get("phone")
        self.lbl_name.setText(data.get("customer_name", "-"))
        self.lbl_phone.setText(data.get("phone", "-"))
        self.lbl_unit.setText(data.get("unit_name", "-"))
        
        self.edit_title.setText(data.get("title", ""))
        self.edit_budget.setText(str(data.get("budget_amount", "0.00")))
        self.edit_profile.setText(data.get("ai_profile", ""))

    def _on_save_clicked(self):
        if not self.current_phone: return
        update_data = {
            "title": self.edit_title.text().strip(),
            "budget_amount": self.edit_budget.text().strip(),
            "ai_profile": self.edit_profile.toPlainText().strip()
        }
        self.save_clicked.emit(self.current_phone, update_data)

class MainWindow(QMainWindow):
    """
    桌面端主窗口：侧边栏+多功能切换架构。
    """
    search_requested = Signal(str, int, int)
    customer_selected = Signal(dict)

    def __init__(self, username: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"微企 AI 助手 - {username}")
        self.resize(850, 650)
        self.setStyleSheet("background-color: #ffffff;")

        # --- 1. 主水平布局 ---
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.main_h_layout = QHBoxLayout(central_widget)
        self.main_h_layout.setContentsMargins(0, 0, 0, 0)
        self.main_h_layout.setSpacing(0)

        # --- 2. 左侧侧边栏 ---
        self.sidebar = QWidget()
        self.sidebar.setFixedWidth(220)
        self.sidebar.setStyleSheet("background-color: #f0f2f5; border-right: 1px solid #d9d9d9;")
        sidebar_layout = QVBoxLayout(self.sidebar)
        
        sidebar_title = QLabel("我的客户")
        sidebar_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #1f1f1f; margin-bottom: 5px;")
        sidebar_layout.addWidget(sidebar_title)

        self.customer_list = QListWidget()
        self.customer_list.setStyleSheet("""
            QListWidget { border: none; background-color: transparent; outline: 0; }
            QListWidget::item { padding: 10px; border-bottom: 1px solid #e8e8e8; }
            QListWidget::item:selected { background-color: #e6f7ff; color: #1890ff; }
        """)
        self.customer_list.itemClicked.connect(self._on_customer_item_clicked)
        sidebar_layout.addWidget(self.customer_list)
        
        # 2.1 增加退出登录按钮
        self.logout_btn = QPushButton("退出账户")
        self.logout_btn.setFlat(True)
        self.logout_btn.setCursor(Qt.PointingHandCursor)
        self.logout_btn.setStyleSheet("color: #8c8c8c; font-size: 12px; margin-top: 10px; text-decoration: underline;")
        sidebar_layout.addWidget(self.logout_btn)
        
        self.main_h_layout.addWidget(self.sidebar)

        # --- 3. 右侧功能区 ---
        self.right_panel = QWidget()
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # 3.1 导航栏
        self.nav_bar = QWidget()
        self.nav_bar.setFixedHeight(50)
        self.nav_bar.setStyleSheet("background-color: #ffffff; border-bottom: 1px solid #d9d9d9;")
        nav_layout = QHBoxLayout(self.nav_bar)
        nav_layout.setContentsMargins(15, 0, 15, 0)
        
        self.btn_chat = QPushButton("AI 对话")
        self.btn_info = QPushButton("客户资料")
        self.btn_prod = QPushButton("商品库")
        
        for btn, idx in [(self.btn_chat, 0), (self.btn_info, 1), (self.btn_prod, 2)]:
            btn.setFlat(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedSize(100, 50)
            btn.clicked.connect(lambda checked=False, i=idx: self.stack.setCurrentIndex(i))
            nav_layout.addWidget(btn)
        
        nav_layout.addStretch()
        right_layout.addWidget(self.nav_bar)

        # 3.2 堆栈容器
        self.stack = QStackedWidget()
        
        self.chat_page = QLabel("AI 智能对话模块开发中...")
        self.chat_page.setAlignment(Qt.AlignCenter)
        self.stack.addWidget(self.chat_page)

        # 升级后的客户资料页
        self.info_page = CustomerInfoWidget()
        self.stack.addWidget(self.info_page)

        # 商品检索页
        self.product_page = QWidget()
        prod_v_layout = QVBoxLayout(self.product_page)
        prod_v_layout.setContentsMargins(15, 15, 15, 15)
        
        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜产品名称或关键词...")
        self.search_input.setFixedHeight(35)
        self.search_btn = QPushButton("查询货源")
        self.search_btn.clicked.connect(self._on_search_clicked)
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.search_btn)
        prod_v_layout.addLayout(search_layout)

        self.product_list = QListWidget()
        self.product_list.setSelectionMode(QListWidget.NoSelection)
        prod_v_layout.addWidget(self.product_list)

        self.load_more_btn = QPushButton("点击加载更多...")
        self.load_more_btn.hide()
        self.load_more_btn.clicked.connect(self._on_load_more_clicked)
        prod_v_layout.addWidget(self.load_more_btn)

        self.stack.addWidget(self.product_page)
        
        right_layout.addWidget(self.stack)
        self.main_h_layout.addWidget(self.right_panel)

        self.current_skip = 0
        self.current_limit = 20

    def _on_customer_item_clicked(self, item):
        customer_data = item.data(Qt.UserRole)
        self.customer_selected.emit(customer_data)

    def update_customer_list(self, customers):
        self.customer_list.clear()
        for c in customers:
            item = QListWidgetItem(f"{c['customer_name']} ({c['phone']})")
            item.setData(Qt.UserRole, c)
            self.customer_list.addItem(item)

    def _on_search_clicked(self):
        self.current_skip = 0
        self.product_list.clear()
        self.search_requested.emit(self.search_input.text().strip(), self.current_skip, self.current_limit)

    def _on_load_more_clicked(self):
        self.current_skip += self.current_limit
        self.search_requested.emit(self.search_input.text().strip(), self.current_skip, self.current_limit)

    def add_product_card(self, product_data):
        item = QListWidgetItem(self.product_list)
        item.setSizeHint(QSize(0, 130))
        widget = ProductItemWidget(product_data)
        self.product_list.setItemWidget(item, widget)
        return widget

    def update_has_more(self, has_more: bool):
        self.load_more_btn.setVisible(has_more)
