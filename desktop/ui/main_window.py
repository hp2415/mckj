from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, 
    QPushButton, QListWidget, QListWidgetItem, QLabel, QFrame,
    QStackedWidget, QScrollArea, QFormLayout, QTextEdit,
    QApplication, QMessageBox, QComboBox, QDateEdit, QDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QListView
)
from PySide6.QtGui import QPixmap, QFont, QClipboard, QKeyEvent
from PySide6.QtCore import Qt, Signal, QSize

class ProductItemWidget(QFrame):
    """
    单个商品卡片组件。
    支持点击图片复制图像，点击名称复制『名称+链接』。
    """
    def __init__(self, product_data, parent=None):
        super().__init__(parent)
        self.product_data = product_data
        self.setFixedHeight(110)
        self.setStyleSheet("""
            QFrame { background-color: #ffffff; border: 1px solid #f0f0f0; border-radius: 6px; }
            QFrame:hover { border: 1px solid #1890ff; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        # 1. 商品图片 (点击复制图片)
        self.img_label = QLabel()
        self.img_label.setFixedSize(90, 90)
        self.img_label.setCursor(Qt.PointingHandCursor)
        self.img_label.setToolTip("点击复制图片")
        self.img_label.setStyleSheet("background-color: #f5f5f5; border-radius: 4px;")
        self.img_label.setScaledContents(True)
        self.img_label.mousePressEvent = self._on_image_clicked
        layout.addWidget(self.img_label)

        # 2. 信息栏 (点击名称复制文本)
        info_layout = QVBoxLayout()
        self.name_label = QLabel(product_data.get("product_name", "未知商品"))
        self.name_label.setWordWrap(True)
        self.name_label.setCursor(Qt.PointingHandCursor)
        self.name_label.setToolTip("点击复制: 商品名+链接")
        self.name_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #1890ff; text-decoration: underline;")
        self.name_label.mousePressEvent = self._on_name_clicked
        info_layout.addWidget(self.name_label)

        self.price_label = QLabel(f"￥ {product_data.get('price', 0.0)}")
        self.price_label.setStyleSheet("font-size: 14px; color: #ff4d4f; font-weight: bold;")
        info_layout.addWidget(self.price_label)

        supplier = product_data.get('supplier_name', '平台自营')
        self.supplier_label = QLabel(f"供货: {supplier}")
        self.supplier_label.setStyleSheet("font-size: 11px; color: #bfbfbf;")
        info_layout.addWidget(self.supplier_label)
        
        info_layout.addStretch()
        layout.addLayout(info_layout)

    def _on_image_clicked(self, event):
        """点击图片：复制原始图像至剪贴板"""
        pixmap = self.img_label.pixmap()
        if pixmap and not pixmap.isNull():
            QApplication.clipboard().setPixmap(pixmap)
            self.setStyleSheet("background-color: #e6f7ff; border: 1px solid #1890ff;") # 临时变色反馈
            QTimer.singleShot(200, lambda: self.setStyleSheet("background-color: #ffffff; border: 1px solid #f0f0f0;"))

    def _on_name_clicked(self, event):
        """点击名称：复制『名称+链接』至剪贴板"""
        name = self.product_data.get("product_name", "")
        url = self.product_data.get("product_url", "暂无外部链接")
        text = f"{name}\n{url}"
        QApplication.clipboard().setText(text)
        self.setStyleSheet("background-color: #f6ffed; border: 1px solid #52c41a;") # 临时绿变色
        QTimer.singleShot(200, lambda: self.setStyleSheet("background-color: #ffffff; border: 1px solid #f0f0f0;"))

    def update_image(self, pixmap):
        self.img_label.setPixmap(pixmap)

from PySide6.QtGui import QStandardItemModel, QStandardItem
from PySide6.QtCore import QEvent

class MultiSelectComboBox(QComboBox):
    """自定义带复选框的下拉多选组件"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().installEventFilter(self)
        
        self.model = QStandardItemModel()
        self.setModel(self.model)
        
        self.view().pressed.connect(self._handle_item_pressed)
        
    def eventFilter(self, obj, event):
        if obj == self.lineEdit() and event.type() == QEvent.MouseButtonPress:
            from PySide6.QtCore import QTime
            if hasattr(self, '_last_hide_time') and self._last_hide_time.msecsTo(QTime.currentTime()) < 150:
                pass
            else:
                self.showPopup()
            return True
        return super().eventFilter(obj, event)
        
    def hidePopup(self):
        from PySide6.QtCore import QTime
        self._last_hide_time = QTime.currentTime()
        super().hidePopup()
        
    def _handle_item_pressed(self, index):
        item = self.model.itemFromIndex(index)
        if item.checkState() == Qt.Checked:
            item.setCheckState(Qt.Unchecked)
        else:
            item.setCheckState(Qt.Checked)
        self._update_text()
        
    def _update_text(self):
        checked_items = []
        for i in range(self.model.rowCount()):
            item = self.model.item(i)
            if item.checkState() == Qt.Checked:
                checked_items.append(item.text())
        self.lineEdit().setText(", ".join(checked_items))

    def addItemsChecked(self, items):
        for text in items:
            item = QStandardItem(text)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            item.setData(Qt.Unchecked, Qt.CheckStateRole)
            self.model.appendRow(item)

    def get_checked_items(self):
        checked_items = []
        for i in range(self.model.rowCount()):
            item = self.model.item(i)
            if item.checkState() == Qt.Checked:
                checked_items.append(item.text())
        return checked_items

    def set_checked_items(self, items):
        for i in range(self.model.rowCount()):
            item = self.model.item(i)
            if item.text() in items:
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
        self._update_text()

class CascaderPopup(QWidget):
    """三级联动浮窗面板，模仿 Web 端的多列级联下拉"""
    selection_finished = Signal(str, str, str)

    def __init__(self, pca_data, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setStyleSheet("""
            QWidget { background-color: #ffffff; border: 1px solid #d9d9d9; border-radius: 4px; }
            QListWidget { border: none; outline: 0; }
            QListWidget::item { padding: 8px 12px; }
            QListWidget::item:hover { background-color: #f5f5f5; }
            QListWidget::item:selected { background-color: #e6f7ff; color: #1890ff; font-weight: bold; }
        """)
        
        self.pca_data = pca_data
        self.prov_str = ""
        self.city_str = ""
        self.dist_str = ""

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.list_prov = QListWidget()
        self.list_city = QListWidget()
        self.list_dist = QListWidget()
        
        for lw in (self.list_prov, self.list_city, self.list_dist):
            lw.setFixedWidth(160)
            lw.setFixedHeight(280)
            layout.addWidget(lw)
            
        # 分割线
        self.line1 = QFrame()
        self.line1.setFrameShape(QFrame.VLine)
        self.line1.setStyleSheet("color: #e8e8e8;")
        layout.insertWidget(1, self.line1)
        
        self.line2 = QFrame()
        self.line2.setFrameShape(QFrame.VLine)
        self.line2.setStyleSheet("color: #e8e8e8;")
        layout.insertWidget(3, self.line2)
        
        self.list_city.hide()
        self.list_dist.hide()
        self.line1.hide()
        self.line2.hide()

        if self.pca_data:
            self.list_prov.addItems(list(self.pca_data.keys()))

        self.list_prov.itemClicked.connect(self._on_prov_clicked)
        self.list_city.itemClicked.connect(self._on_city_clicked)
        self.list_dist.itemClicked.connect(self._on_dist_clicked)

    def _on_prov_clicked(self, item):
        self.prov_str = item.text()
        self.city_str = ""
        self.dist_str = ""
        self.list_city.clear()
        self.list_dist.clear()
        
        self.list_dist.hide()
        self.line2.hide()
        
        if self.prov_str in self.pca_data:
            self.list_city.addItems(list(self.pca_data[self.prov_str].keys()))
            self.list_city.show()
            self.line1.show()
        else:
            self.list_city.hide()
            self.line1.hide()

    def _on_city_clicked(self, item):
        self.city_str = item.text()
        self.dist_str = ""
        self.list_dist.clear()
        
        if self.prov_str in self.pca_data and self.city_str in self.pca_data[self.prov_str]:
            self.list_dist.addItems(self.pca_data[self.prov_str][self.city_str])
            self.list_dist.show()
            self.line2.show()
        else:
            self.list_dist.hide()
            self.line2.hide()

    def _on_dist_clicked(self, item):
        self.dist_str = item.text()
        self.selection_finished.emit(self.prov_str, self.city_str, self.dist_str)
        self.hide()
        
    def hideEvent(self, event):
        if hasattr(self, "parent_btn") and self.parent_btn:
            from PySide6.QtCore import QTime
            self.parent_btn._last_hide_time = QTime.currentTime()
        super().hideEvent(event)

class CalendarPopup(QWidget):
    """自定义日历弹出面板，解决原生 QDateEdit 丑陋且残缺的问题"""
    date_selected = Signal(object) # 抛出 QDate
    
    def __init__(self, init_date=None, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.container = QFrame()
        self.container.setStyleSheet("""
            QFrame { background-color: #ffffff; border: 1px solid #d9d9d9; border-radius: 4px; }
            QCalendarWidget QWidget#qt_calendar_navigationbar { background-color: #f5f5f5; border-bottom: 1px solid #e8e8e8; }
            QCalendarWidget QToolButton { color: #333; font-weight: bold; font-size: 13px; }
            QCalendarWidget QMenu { background-color: white; color: black; }
            QCalendarWidget QSpinBox { background-color: white; border: 1px solid #ccc; border-radius: 2px; }
            QCalendarWidget QAbstractItemView:enabled { color: #333; selection-background-color: #1890ff; selection-color: white; font-size: 13px; }
            QCalendarWidget QAbstractItemView:disabled { color: #ccc; }
        """)
        
        c_layout = QVBoxLayout(self.container)
        c_layout.setContentsMargins(2, 2, 2, 2)
        
        from PySide6.QtWidgets import QCalendarWidget
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        if init_date:
            self.calendar.setSelectedDate(init_date)
            
        self.calendar.clicked.connect(self._on_click)
        c_layout.addWidget(self.calendar)
        layout.addWidget(self.container)
        
    def _on_click(self, date):
        self.date_selected.emit(date)
        self.hide()
        
    def hideEvent(self, event):
        if hasattr(self, "parent_btn") and self.parent_btn:
            from PySide6.QtCore import QTime
            self.parent_btn._last_hide_time = QTime.currentTime()
        super().hideEvent(event)

class DatePickerBtn(QPushButton):
    """伪装成输入框的时间选择起，全区域可点，风格统一"""
    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtCore import QDate
        self.current_date = QDate.currentDate()
        self.update_text()
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("""
            QPushButton {
                text-align: left; padding: 4px; background-color: #ffffff;
                border: 1px solid #d9d9d9; border-radius: 4px; color: #1f1f1f; font-size: 13px; min-height: 24px;
            }
            QPushButton:hover { border-color: #40a9ff; }
        """)
        self.clicked.connect(self._show_popup)

    def setDate(self, qdate):
        self.current_date = qdate
        self.update_text()

    def date(self):
        return self.current_date

    def update_text(self):
        self.setText(self.current_date.toString("yyyy-MM-dd"))
        
    def _show_popup(self):
        from PySide6.QtCore import QTime
        if hasattr(self, '_last_hide_time') and self._last_hide_time.msecsTo(QTime.currentTime()) < 150: return
        self.popup = CalendarPopup(self.current_date)
        self.popup.parent_btn = self
        self.popup.date_selected.connect(lambda d: self.setDate(d))
        
        pos = self.mapToGlobal(self.rect().bottomLeft())
        self.popup.move(pos.x(), pos.y() + 2)
        self.popup.show()

class RegionCascader(QPushButton):
    """
    触发级联选择的按钮框，伪装成 QLineEdit 的样式
    """
    def __init__(self, pca_data, parent=None):
        super().__init__(parent)
        self.pca_data = pca_data
        self.setText("请选择省/市/区")
        self.setStyleSheet("""
            QPushButton {
                text-align: left;
                padding: 4px;
                background-color: #ffffff;
                border: 1px solid #d9d9d9;
                border-radius: 4px;
                color: #1f1f1f;
                font-size: 13px;
                min-height: 24px;
            }
            QPushButton:hover { border-color: #40a9ff; }
        """)
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(self._show_popup)
        self._current_value = ""

    def _show_popup(self):
        from PySide6.QtCore import QTime
        if hasattr(self, '_last_hide_time') and self._last_hide_time.msecsTo(QTime.currentTime()) < 150:
            return
            
        self.popup = CascaderPopup(self.pca_data)
        self.popup.parent_btn = self
        self.popup.selection_finished.connect(self._on_selected)
        
        pos = self.mapToGlobal(self.rect().bottomLeft())
        self.popup.move(pos.x(), pos.y() + 2)
        self.popup.show()

    def _on_selected(self, prov, city, dist):
        self._current_value = f"{prov}-{city}-{dist}"
        self.setText(f"{prov} / {city} / {dist}")
        self.setStyleSheet(self.styleSheet() + "QPushButton { color: #1f1f1f; }")
        
    def currentText(self):
        return self._current_value
        
    def setCurrentText(self, division_str):
        if not division_str:
            self._current_value = ""
            self.setText("请选择省/市/区")
            return
            
        self._current_value = division_str
        parts = division_str.split("-")
        if len(parts) >= 3:
            self.setText(f"{parts[0]} / {parts[1]} / {parts[2]}")
            self.setStyleSheet(self.styleSheet() + "QPushButton { color: #1f1f1f; }")
        else:
            self.setText(division_str)

class CustomerInfoWidget(QWidget):
    """
    客户详情信息面板：视觉风格大一统。
    """
    save_clicked = Signal(str, dict)
    history_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        header = QLabel("客户全息档案")
        header.setStyleSheet("font-size: 15px; font-weight: bold; color: #1f1f1f;")
        layout.addWidget(header)

        # 统一表单样式
        self.form_container = QFrame()
        self.form_container.setStyleSheet("""
            QLineEdit, QTextEdit, QComboBox { 
                border: 1px solid #d9d9d9; border-radius: 4px; padding: 4px; 
                background-color: #ffffff; font-size: 13px; min-height: 24px; color: #1f1f1f;
            }
            QLineEdit:read-only { background-color: #f5f5f5; color: #8c8c8c; }
        """)
        form_layout = QFormLayout(self.form_container)
        form_layout.setLabelAlignment(Qt.AlignRight)

        # 1. 核心只读
        self.edit_name = QLineEdit()
        self.edit_name.setReadOnly(True)
        self.edit_phone = QLineEdit()
        self.edit_phone.setReadOnly(True)

        # 2. 动态选项与组件
        self.combo_unit = QComboBox()
        self.combo_purchase_type = QComboBox()
        
        # 加载本地城市数据
        self._pca_data = {}
        import os, json
        pca_path = os.path.join(os.path.dirname(__file__), "..", "pca.json")
        if os.path.exists(pca_path):
            with open(pca_path, "r", encoding="utf-8") as f:
                self._pca_data = json.load(f)
                
        # 网页级联选择器风格菜单
        self.combo_division = RegionCascader(self._pca_data)
        
        self.edit_contact_date = DatePickerBtn()
        
        self.combo_purchase_months = MultiSelectComboBox()
        
        self.btn_historical_amount = QPushButton("0.00 元")
        self.btn_historical_amount.setFlat(True)
        self.btn_historical_amount.setCursor(Qt.PointingHandCursor)
        self.btn_historical_amount.setStyleSheet("color: #1890ff; text-decoration: underline; text-align: left;")
        self.btn_historical_amount.clicked.connect(lambda: self.history_clicked.emit(self.current_phone) if self.current_phone else None)

        # 3. 业务主观字段
        self.edit_title = QLineEdit()
        self.edit_title.setPlaceholderText("例如：李局、张总")
        self.edit_budget = QLineEdit()
        self.edit_budget.setPlaceholderText("预计单笔采购预算")
        self.edit_profile = QTextEdit()
        self.edit_profile.setPlaceholderText("性格、偏好、历史沟通记录...")
        self.edit_profile.setMinimumHeight(100)

        form_layout.addRow("真实姓名:", self.edit_name)
        form_layout.addRow("联系电话:", self.edit_phone)
        form_layout.addRow("所属单位:", self.combo_unit)
        form_layout.addRow("行政区划:", self.combo_division)
        form_layout.addRow("建联日期:", self.edit_contact_date)
        form_layout.addRow("采购类型:", self.combo_purchase_type)
        form_layout.addRow("采货月份:", self.combo_purchase_months)
        form_layout.addRow("历史总额:", self.btn_historical_amount)
        form_layout.addRow("当前称呼:", self.edit_title)
        form_layout.addRow("采购预算:", self.edit_budget)
        form_layout.addRow("私域画像:", self.edit_profile)

        layout.addWidget(self.form_container)

        self.save_btn = QPushButton("保存全部跟进信息")
        self.save_btn.setFixedHeight(36)
        self.save_btn.setStyleSheet("""
            QPushButton { background-color: #1890ff; color: white; border-radius: 4px; font-weight: bold; }
            QPushButton:hover { background-color: #40a9ff; }
        """)
        self.save_btn.clicked.connect(self._on_save_clicked)
        layout.addWidget(self.save_btn)
        
        layout.addStretch()
        self.current_phone = None

    def populate_combo_boxes(self, configs_dict):
        """填充后台字典下发的数据"""
        self.combo_unit.clear()
        self.combo_unit.addItems(configs_dict.get("unit_type_choices", []))
        
        self.combo_purchase_type.clear()
        self.combo_purchase_type.addItems(configs_dict.get("purchase_type_choices", []))
        
        months = [f"{i}月" for i in range(1, 13)]
        self.combo_purchase_months.model.clear()
        self.combo_purchase_months.addItemsChecked(months)

    def set_customer(self, data):
        self.current_phone = data.get("phone")
        self.edit_name.setText(data.get("customer_name", "-"))
        self.edit_phone.setText(data.get("phone", "-"))
        
        self.combo_unit.setCurrentText(data.get("unit_type", "") or "")
        
        self.combo_division.setCurrentText(data.get("admin_division", "") or "")
        self.combo_purchase_type.setCurrentText(data.get("purchase_type", "") or "")
        
        months_str = data.get("purchase_months", "") or ""
        self.combo_purchase_months.set_checked_items([m.strip() for m in months_str.split(",") if m.strip()])
        
        contact_dt = data.get("contact_date")
        if contact_dt:
            from PySide6.QtCore import QDate
            try:
                # 解析如 '2026-04-03'
                year, month, day = map(int, contact_dt.split("-"))
                self.edit_contact_date.setDate(QDate(year, month, day))
            except: pass
            
        hist_amt = data.get("historical_amount", 0.0)
        hist_cnt = data.get("historical_order_count", 0)
        self.btn_historical_amount.setText(f"¥{hist_amt} ({hist_cnt}笔)")
        
        self.edit_title.setText(data.get("title", ""))
        self.edit_budget.setText(str(data.get("budget_amount", "0.00")))
        self.edit_profile.setText(data.get("ai_profile", ""))

    def _on_save_clicked(self):
        if not self.current_phone: return
        
        update_data = {
            "unit_type": self.combo_unit.currentText(),
            "admin_division": self.combo_division.currentText(),
            "purchase_type": self.combo_purchase_type.currentText(),
            "purchase_months": ", ".join(self.combo_purchase_months.get_checked_items()),
            "contact_date": self.edit_contact_date.date().toString("yyyy-MM-dd"),
            "title": self.edit_title.text().strip(),
            "budget_amount": self.edit_budget.text().strip() or "0",
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
        
        self.nav_buttons = [self.btn_chat, self.btn_info, self.btn_prod]
        
        for btn, idx in zip(self.nav_buttons, range(3)):
            btn.setFlat(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedSize(100, 50)
            btn.clicked.connect(lambda checked=False, i=idx: self.switch_tab(i))
            nav_layout.addWidget(btn)
            
        self.switch_tab(0) # 默认态
        nav_layout.addStretch()
        right_layout.addWidget(self.nav_bar)

    def switch_tab(self, index):
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self.nav_buttons):
            if i == index:
                btn.setStyleSheet("color: #1890ff; font-weight: bold; border-bottom: 2px solid #1890ff;")
            else:
                btn.setStyleSheet("color: #595959; font-weight: normal; border-bottom: none;")

class ChatBubble(QWidget):
    """
    单个聊天气泡组件。
    支持左/右对齐，具备圆角与阴影感。
    """
    def __init__(self, text, is_user=False, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        
        # 气泡样式设定
        common_style = "padding: 10px; border-radius: 10px; font-size: 13px; line-height: 1.4;"
        if is_user:
            # 用户消息：靠右，深蓝色背景
            layout.addStretch()
            self.label.setStyleSheet(f"{common_style} background-color: #007bff; color: white;")
            layout.addWidget(self.label)
        else:
            # AI 消息：靠左，浅灰色背景
            self.label.setStyleSheet(f"{common_style} background-color: #f4f4f5; color: #333;")
            layout.addWidget(self.label)
            layout.addStretch()

    def append_text(self, new_text):
        """流式追加文本"""
        self.label.setText(self.label.text() + new_text)

class AIChatWidget(QWidget):
    """
    AI 智能对话主面板：包含消息滚动流与底部输入框。
    """
    send_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 1. 消息显示区域 (滚动)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("border: none; background-color: #ffffff;")
        
        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.addStretch() # 将消息顶到底部
        self.chat_layout.setSpacing(10)
        
        self.scroll_area.setWidget(self.chat_container)
        layout.addWidget(self.scroll_area)

        # 2. 底部输入区域
        input_container = QFrame()
        input_container.setFixedHeight(120)
        input_container.setStyleSheet("background-color: #ffffff; border-top: 1px solid #e8e8e8;")
        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(15, 10, 15, 10)

        self.input_edit = QTextEdit()
        self.input_edit.setPlaceholderText("请输入您的问题... (Ctrl + Enter 发送)")
        self.input_edit.setStyleSheet("border: none; font-size: 13px;")
        input_layout.addWidget(self.input_edit)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.send_btn = QPushButton("发送提问")
        self.send_btn.setFixedSize(90, 32)
        self.send_btn.setStyleSheet("""
            QPushButton { background-color: #007bff; color: white; border: none; border-radius: 4px; font-weight: bold; }
            QPushButton:hover { background-color: #0056b3; }
        """)
        self.send_btn.clicked.connect(self._on_send_clicked)
        btn_layout.addWidget(self.send_btn)
        input_layout.addLayout(btn_layout)

        layout.addWidget(input_container)

    def add_message(self, text, is_user=False):
        """向 UI 添加一个气泡"""
        bubble = ChatBubble(text, is_user)
        # 插入到 Stretch 之前
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        # 自动滚动到底部 (通过单次定时器安全置后，严禁使用 processEvents)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        ))
        return bubble

    def _on_send_clicked(self):
        text = self.input_edit.toPlainText().strip()
        if text:
            self.send_requested.emit(text)
            self.input_edit.clear()

    def clear(self):
        """清除聊天历史记录"""
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

from PySide6.QtCore import Qt, Signal, QSize, QTimer

class QuickTextEdit(QTextEdit):
    """
    专用 IM 输入框：Enter 发送，Ctrl+Enter 换行。
    """
    enter_pressed = Signal()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            if event.modifiers() == Qt.ControlModifier:
                # Ctrl+Enter: 换行
                super().keyPressEvent(event)
            else:
                # Enter: 发送
                self.enter_pressed.emit()
        else:
            super().keyPressEvent(event)

class ChatBubble(QWidget):
    """
    单个聊天气泡组件：适配窄屏。
    """
    def __init__(self, text, is_user=False, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label.setMaximumWidth(280) # 窄屏适配
        
        common_style = "padding: 8px 12px; border-radius: 8px; font-size: 13px; line-height: 1.4;"
        if is_user:
            layout.addStretch()
            self.label.setStyleSheet(f"{common_style} background-color: #95ec69; color: #000;")
            layout.addWidget(self.label)
        else:
            self.label.setStyleSheet(f"{common_style} background-color: #ffffff; color: #1f1f1f; border: 1px solid #ebebeb;")
            layout.addWidget(self.label)
            layout.addStretch()

    def append_text(self, new_text):
        self.label.setText(self.label.text() + new_text)

class AIChatWidget(QWidget):
    """
    AI 智能对话主面板：适配窄屏，支持回车发送。
    """
    send_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("border: none; background-color: #f5f5f5;")
        
        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.addStretch()
        self.chat_layout.setSpacing(12)
        
        self.scroll_area.setWidget(self.chat_container)
        layout.addWidget(self.scroll_area)

        # 输入区域 (IM 风格)
        input_container = QFrame()
        input_container.setFixedHeight(130)
        input_container.setStyleSheet("background-color: #ffffff; border-top: 1px solid #e8e8e8;")
        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(10, 5, 10, 5)

        self.input_edit = QuickTextEdit()
        self.input_edit.setPlaceholderText("请输入问题... (Enter 发送, Ctrl+Enter 换行)")
        self.input_edit.setStyleSheet("border: none; font-size: 13px;")
        self.input_edit.enter_pressed.connect(self._on_send_clicked)
        input_layout.addWidget(self.input_edit)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.send_btn = QPushButton("发送")
        self.send_btn.setFixedSize(60, 28)
        self.send_btn.setStyleSheet("""
            QPushButton { background-color: #07c160; color: white; border-radius: 4px; font-size: 12px; }
            QPushButton:hover { background-color: #06ad56; }
        """)
        self.send_btn.clicked.connect(self._on_send_clicked)
        btn_layout.addWidget(self.send_btn)
        input_layout.addLayout(btn_layout)

        layout.addWidget(input_container)

    def add_message(self, text, is_user=False):
        bubble = ChatBubble(text, is_user)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        QApplication.processEvents()
        self.scroll_area.verticalScrollBar().setValue(self.scroll_area.verticalScrollBar().maximum())
        return bubble

    def _on_send_clicked(self):
        text = self.input_edit.toPlainText().strip()
        if text:
            self.send_requested.emit(text)
            self.input_edit.clear()

    def clear(self):
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

class MainWindow(QMainWindow):
    """
    主窗口：极致窄屏适配 (400x720)。
    """
    search_requested = Signal(str, int, int)
    customer_selected = Signal(dict)
    sync_triggered = Signal() # 手动触发同步信号

    def __init__(self, username: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"微企 AI - {username}")
        self.setFixedSize(400, 720) # 锁定窄屏尺寸
        self.setStyleSheet("background-color: #ffffff;")

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.main_h_layout = QHBoxLayout(central_widget)
        self.main_h_layout.setContentsMargins(0, 0, 0, 0)
        self.main_h_layout.setSpacing(0)

        # 1. 侧边栏 (极简模式)
        self.sidebar = QWidget()
        self.sidebar.setFixedWidth(130)
        self.sidebar.setStyleSheet("background-color: #2e2e2e; border-right: 1px solid #1a1a1a;")
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(0, 10, 0, 10)
        
        # 客户列表
        self.customer_list = QListWidget()
        self.customer_list.setStyleSheet("""
            QListWidget { border: none; background-color: transparent; outline: 0; }
            QListWidget::item { padding: 12px 10px; color: #b0b0b0; border-bottom: 1px solid #3a3a3a; font-size: 13px; }
            QListWidget::item:selected { background-color: #3d3d3d; color: #ffffff; border-left: 3px solid #07c160; }
        """)
        self.customer_list.itemClicked.connect(self._on_customer_item_clicked)
        sidebar_layout.addWidget(self.customer_list)
        
        self.logout_btn = QPushButton("安全退出")
        self.logout_btn.setFlat(True)
        self.logout_btn.setStyleSheet("color: #666; font-size: 11px; margin-bottom: 10px;")
        sidebar_layout.addWidget(self.logout_btn)
        
        self.main_h_layout.addWidget(self.sidebar)

        # 2. 右侧功能区
        self.right_panel = QWidget()
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # 导航栏 (极简选项卡)
        self.nav_bar = QWidget()
        self.nav_bar.setFixedHeight(45)
        self.nav_bar.setStyleSheet("background-color: #ffffff; border-bottom: 1px solid #f0f0f0;")
        nav_layout = QHBoxLayout(self.nav_bar)
        nav_layout.setContentsMargins(5, 0, 5, 0)
        nav_layout.setSpacing(2)
        
        self.btn_chat = QPushButton("对话")
        self.btn_info = QPushButton("画像")
        self.btn_prod = QPushButton("商品")
        
        self.tabs = [self.btn_chat, self.btn_info, self.btn_prod]
        for btn, idx in zip(self.tabs, range(3)):
            btn.setFlat(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedSize(80, 45)
            btn.setStyleSheet("QPushButton { color: #666; font-size: 13px; font-weight: bold; border-bottom: 3px solid transparent; }")
            btn.clicked.connect(lambda checked=False, i=idx: self._on_tab_changed(i))
            nav_layout.addWidget(btn)
        nav_layout.addStretch()
        right_layout.addWidget(self.nav_bar)

        self.stack = QStackedWidget()
        self.chat_page = AIChatWidget()
        self.info_page = CustomerInfoWidget()
        
        # 商品检索页
        self.product_page = QWidget()
        prod_layout = QVBoxLayout(self.product_page)
        prod_layout.setContentsMargins(10, 10, 10, 10)
        prod_layout.setSpacing(5)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("找货源...")
        self.search_input.setStyleSheet("height: 30px; border: 1px solid #eee; border-radius: 15px; padding-left: 10px;")
        self.search_input.returnPressed.connect(self._on_search_clicked)
        prod_layout.addWidget(self.search_input)

        # 3.2.1 同步状态面板 (新加)
        sync_panel = QHBoxLayout()
        sync_panel.setContentsMargins(5, 0, 5, 0)
        self.sync_status_lbl = QLabel("云端货源状态加载中...")
        self.sync_status_lbl.setStyleSheet("font-size: 11px; color: #8c8c8c;")
        sync_panel.addWidget(self.sync_status_lbl)

        sync_panel.addStretch()
        self.btn_sync_now = QPushButton("同步")
        self.btn_sync_now.setFixedSize(40, 20)
        self.btn_sync_now.setCursor(Qt.PointingHandCursor)
        self.btn_sync_now.setStyleSheet("""
            QPushButton { background-color: #f0f0f0; border: 1px solid #d9d9d9; border-radius: 4px; font-size: 10px; color: #595959; }
            QPushButton:hover { background-color: #e6f7ff; border: 1px solid #1890ff; color: #1890ff; }
        """)
        self.btn_sync_now.hide() # 默认隐藏，由 main.py 根据权限开启
        self.btn_sync_now.clicked.connect(self.sync_triggered.emit)
        sync_panel.addWidget(self.btn_sync_now)
        prod_layout.addLayout(sync_panel)
        
        self.product_list = QListWidget()
        self.product_list.setStyleSheet("""
            QListWidget { border: none; background-color: transparent; outline: 0; }
            QScrollBar:vertical {
                border: none; background: transparent; width: 6px; margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #d9d9d9; min-height: 20px; border-radius: 3px;
            }
            QScrollBar::handle:vertical:hover { background: #bfbfbf; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)
        prod_layout.addWidget(self.product_list)
        
        self.load_more_btn = QPushButton("--- 展开更多货源 ---")
        self.load_more_btn.hide()
        self.load_more_btn.setFlat(True)
        self.load_more_btn.setCursor(Qt.PointingHandCursor)
        self.load_more_btn.setStyleSheet("""
            QPushButton { 
                color: #bfbfbf; font-size: 11px; padding: 10px; border: none; 
            }
            QPushButton:hover { color: #1890ff; }
        """)
        self.load_more_btn.clicked.connect(self._on_load_more_clicked)
        prod_layout.addWidget(self.load_more_btn)

        self.stack.addWidget(self.chat_page)
        self.stack.addWidget(self.info_page)
        self.stack.addWidget(self.product_page)
        right_layout.addWidget(self.stack)
        self.main_h_layout.addWidget(self.right_panel)

        self._on_tab_changed(0) 

    def _on_tab_changed(self, index):
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self.tabs):
            if i == index:
                btn.setStyleSheet("QPushButton { color: #07c160; font-size: 13px; font-weight: bold; border-bottom: 3px solid #07c160; }")
            else:
                btn.setStyleSheet("QPushButton { color: #666; font-size: 13px; font-weight: bold; border-bottom: 3px solid transparent; }")

    def switch_tab(self, index):
        self._on_tab_changed(index)

    def _on_customer_item_clicked(self, item):
        self.customer_selected.emit(item.data(Qt.UserRole))

    def update_customer_list(self, customers):
        self.customer_list.clear()
        for c in customers:
            # 缩窄模式：仅显示姓名，手机号作为副文本
            name = c.get('customer_name', '未知')
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, c)
            item.setToolTip(f"手机: {c.get('phone')}")
            self.customer_list.addItem(item)

    def _on_search_clicked(self):
        self.product_list.clear()
        self.search_requested.emit(self.search_input.text().strip(), 0, 20)

    def _on_load_more_clicked(self):
        self.search_requested.emit(self.search_input.text().strip(), self.product_list.count(), 20)

    def add_product_card(self, product_data):
        item = QListWidgetItem(self.product_list)
        item.setSizeHint(QSize(0, 120))
        widget = ProductItemWidget(product_data)
        self.product_list.setItemWidget(item, widget)
        return widget

    def update_has_more(self, has_more):
        self.load_more_btn.setVisible(has_more)
