from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, 
    QPushButton, QListWidget, QListWidgetItem, QLabel, QFrame,
    QStackedWidget, QScrollArea, QFormLayout, QTextEdit,
    QApplication, QMessageBox, QComboBox, QDateEdit, QDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QListView, QGraphicsDropShadowEffect
)
from PySide6.QtCore import Qt, Signal, QSize, QTimer, QPoint, QEvent
from PySide6.QtGui import QPixmap, QFont, QClipboard, QKeyEvent, QColor, QStandardItemModel, QStandardItem

class SearchTag(QFrame):
    """搜索标签组件：展示关键词并支持删除"""
    removed = Signal(str)
    
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setObjectName("SearchTag")
        self.text = text
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)
        
        lbl = QLabel(text)
        lbl.setStyleSheet("font-size: 12px; font-weight: 500;")
        
        btn_close = QPushButton("×")
        btn_close.setObjectName("TagCloseBtn")
        btn_close.setFixedSize(16, 16)
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(lambda: self.removed.emit(self.text))
        
        layout.addWidget(lbl)
        layout.addWidget(btn_close)

class TagLineEdit(QLineEdit):
    """支持退格删除标签的自定义输入框"""
    backspace_pressed = Signal()
    
    def keyPressEvent(self, event):
        # 核心逻辑：只有在【按下前】已经是空，且【不是长按连发】的情况下，才发射删标签信号
        if event.key() == Qt.Key_Backspace and self.text() == "" and not event.isAutoRepeat():
            self.backspace_pressed.emit()
            return
        super().keyPressEvent(event)

class TagSearchWidget(QFrame):
    """标签搜索容器：支持多关键词叠加检索"""
    search_triggered = Signal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TagSearchContainer")
        self.tags = []
        
        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(10, 2, 10, 2)
        self.main_layout.setSpacing(8)
        
        # 标签流式容器
        self.tag_area = QWidget()
        self.tag_layout = QHBoxLayout(self.tag_area)
        self.tag_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_layout.setSpacing(8)
        self.main_layout.addWidget(self.tag_area)
        
        self.edit = TagLineEdit()
        self.edit.setObjectName("InnerSearchInput")
        self.edit.setPlaceholderText("搜索关键词...")
        self.edit.setFrame(False)
        self.edit.returnPressed.connect(self._on_return_pressed)
        self.edit.backspace_pressed.connect(self._on_backspace_on_empty)
        self.main_layout.addWidget(self.edit)
        
        self.main_layout.addStretch()

    def _on_backspace_on_empty(self):
        """当输入框为空且按下退格时，删除最后一个标签"""
        if self.tags:
            self.remove_tag(self.tags[-1])

    def _on_return_pressed(self):
        txt = self.edit.text().strip()
        if txt and txt not in self.tags:
            self.add_tag(txt)
            self.edit.clear()
            self.emit_search()
        elif not txt:
            # 如果输入框为空按下回车，也触发一次搜索（用于刷新）
            self.emit_search()

    def add_tag(self, text):
        tag = SearchTag(text)
        tag.removed.connect(self.remove_tag)
        self.tag_layout.addWidget(tag)
        self.tags.append(text)
        self.edit.setPlaceholderText("") # 有标签后减少提示文字

    def remove_tag(self, text):
        if text in self.tags:
            self.tags.remove(text)
            # 重新渲染标签区
            for i in range(self.tag_layout.count()):
                w = self.tag_layout.itemAt(i).widget()
                if w and hasattr(w, "text") and w.text == text:
                    w.deleteLater()
                    break
            if not self.tags:
                self.edit.setPlaceholderText("添加筛选关键词...")
            self.emit_search()

    def emit_search(self):
        # 拼接所有标签发往后端
        self.search_triggered.emit(self.text())

    def text(self):
        """兼容性接口：返回所有标签组合后的字符串"""
        return " ".join(self.tags)

    def clear_all(self):
        self.tags = []
        while self.tag_layout.count():
            item = self.tag_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self.edit.clear()
        self.edit.setPlaceholderText("关键词、供应商...")

class ProductItemWidget(QFrame):
    """
    单个商品卡片组件：注入物理投影质感与弹性化长标题支持。
    """
    def __init__(self, product_data, parent=None):
        super().__init__(parent)
        self.product_data = product_data
        self.setObjectName("ProductCard")
        # 释放高度限制，允许长标题无限换行撑开
        self.setMinimumHeight(120)
        # 移除 setMaximumHeight 限制

        # 注入物理阴影特效
        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setBlurRadius(12)
        self.shadow.setXOffset(0)
        self.shadow.setYOffset(2)
        self.shadow.setColor(QColor(0, 0, 0, 20))
        self.setGraphicsEffect(self.shadow)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(15)

        # 1. 商品图片 
        self.img_label = QLabel()
        self.img_label.setObjectName("ProductImage")
        self.img_label.setFixedSize(110, 120) # 放大尺寸提升视觉直观度
        self.img_label.setScaledContents(True)
        self.img_label.mousePressEvent = self._on_image_clicked
        layout.addWidget(self.img_label, 0, Qt.AlignTop | Qt.AlignHCenter)

        # 2. 信息栏 (使用自适应权重分配)
        self.info_container = QWidget()
        info_layout = QVBoxLayout(self.info_container)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(4)
        
        # 商品全名 (开启换行)
        p_name = product_data.get("product_name", "未知商品")
        self.name_label = QLabel(p_name)
        self.name_label.setObjectName("ProductName")
        self.name_label.setWordWrap(True)
        self.name_label.setToolTip(p_name)
        self.name_label.mousePressEvent = self._on_name_clicked
        info_layout.addWidget(self.name_label)

        # 价格行
        price = product_data.get('price', 0.0)
        unit = product_data.get('unit', '')
        price_text = f"￥ {price}"
        if unit:
            price_text += f"/{unit}"
            
        self.price_label = QLabel(price_text)
        self.price_label.setObjectName("ProductPrice")
        info_layout.addWidget(self.price_label)

        # 供应商名 (开启辅助全称显示)
        supplier = product_data.get('supplier_name', '平台自营')
        self.supplier_label = QLabel(f"供货: {supplier}")
        self.supplier_label.setObjectName("ProductSupplier")
        self.supplier_label.setWordWrap(True)
        self.supplier_label.setToolTip(f"{supplier}")
        info_layout.addWidget(self.supplier_label)
        
        layout.addWidget(self.info_container, 1) # 信息区占据剩余宽度

    def sizeHint(self):
        """核心：确保向 QListWidget 报备正确的动态高度"""
        sh = super().sizeHint()
        # 根据内容自动计算推荐高度，但不低于 120 也不高于 220
        h = max(120, sh.height() + 20) 
        return QSize(sh.width(), min(h, 220))

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
        self.lineEdit().setPlaceholderText("可多选，不限月份...")
        
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

    def wheelEvent(self, event):
        # 拦截鼠标滚轮事件，防止误触导致内容变更
        event.ignore()

class NoScrollComboBox(QComboBox):
    """阻止鼠标滚轮误触的下拉框"""
    def wheelEvent(self, event):
        event.ignore()

class CascaderPopup(QWidget):
    """三级联动浮窗面板，模仿 Web 端的多列级联下拉"""
    selection_finished = Signal(str, str, str)

    def __init__(self, pca_data, init_p=None, init_c=None, init_d=None, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("CascaderPopup")
        
        self.pca_data = pca_data
        self.prov_str = init_p or ""
        self.city_str = init_c or ""
        self.dist_str = init_d or ""

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

        # 自动定位到现有地区
        self._auto_select_initial()

    def _auto_select_initial(self):
        """如果存在初始值，自动触发联级展开"""
        if not self.prov_str: return
        
        # 1. 找省
        items = self.list_prov.findItems(self.prov_str, Qt.MatchExactly)
        if items:
            self.list_prov.setCurrentItem(items[0])
            self._on_prov_clicked(items[0])
            
            # 2. 找市
            if self.city_str:
                c_items = self.list_city.findItems(self.city_str, Qt.MatchExactly)
                if c_items:
                    self.list_city.setCurrentItem(c_items[0])
                    self._on_city_clicked(c_items[0])
                    
                    # 3. 找区
                    if self.dist_str:
                        d_items = self.list_dist.findItems(self.dist_str, Qt.MatchExactly)
                        if d_items:
                            self.list_dist.setCurrentItem(d_items[0])
                            self.list_dist.scrollToItem(d_items[0])

    def _on_prov_clicked(self, item):
        self.prov_str = item.text()
        # 仅当没有初始值时才清空后续，防止自动定位失败
        if not self.city_str:
            self.city_str = ""
            self.dist_str = ""
            self.list_city.clear()
            self.list_dist.clear()
        else:
            # 如果是为了自动定位，我们只刷列表但不清空已存的值
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
        if not self.dist_str:
            self.dist_str = ""
            self.list_dist.clear()
        else:
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
        self.container.setObjectName("CalendarContainer")
        
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
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(self._show_popup)
        self._current_value = ""

    def _show_popup(self):
        from PySide6.QtCore import QTime
        if hasattr(self, '_last_hide_time') and self._last_hide_time.msecsTo(QTime.currentTime()) < 150:
            return
            
        # 将当前已选的省市区解析出来传给弹窗，实现开窗定位
        p, c, d = "", "", ""
        parts = self._current_value.split("-")
        if len(parts) >= 3:
            p, c, d = parts[0], parts[1], parts[2]
            
        self.popup = CascaderPopup(self.pca_data, p, c, d)
        self.popup.parent_btn = self
        self.popup.selection_finished.connect(self._on_selected)
        
        pos = self.mapToGlobal(self.rect().bottomLeft())
        self.popup.move(pos.x(), pos.y() + 2)
        self.popup.show()

    def _on_selected(self, prov, city, dist):
        self._current_value = f"{prov}-{city}-{dist}"
        self.update_display_text(prov, city, dist)
        
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
            self.update_display_text(parts[0], parts[1], parts[2])
        else:
            self.setText(division_str)

    def update_display_text(self, prov, city, dist):
        """智能截断：如果全称太长，保留省和区（中间省略），确保末端可见"""
        full_text = f"{prov} / {city} / {dist}"
        self.setToolTip(full_text)
        
        # 阈值：若超过 12 个字符（含斜杠），执行中间省略
        if len(full_text) > 12:
            short_text = f"{prov} / ... / {dist}"
            self.setText(short_text)
        else:
            self.setText(full_text)

class CustomerInfoWidget(QWidget):
    """
    客户详情信息面板：视觉风格大一统。
    """
    save_clicked = Signal(str, dict)
    history_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 10, 15, 10) # 压缩外边距
        layout.setSpacing(10)

        header = QLabel("客户全息档案")
        header.setObjectName("InfoHeader")
        layout.addWidget(header)

        # 统一表单样式
        self.form_container = QFrame()
        self.form_container.setObjectName("FormContainer")
        form_layout = QFormLayout(self.form_container)
        form_layout.setLabelAlignment(Qt.AlignRight)
        form_layout.setVerticalSpacing(6) # 极致压缩表单间距
        form_layout.setHorizontalSpacing(15)

        # 1. 核心只读
        self.edit_name = QLineEdit()
        self.edit_name.setReadOnly(True)
        self.edit_phone = QLineEdit()
        self.edit_phone.setReadOnly(True)

        # 2. 动态选项与组件
        self.combo_unit = NoScrollComboBox()
        self.combo_unit.setPlaceholderText("请选择所属单位...")
        self.combo_purchase_type = NoScrollComboBox()
        self.combo_purchase_type.setPlaceholderText("请选择采购模式...")
        self.edit_wechat_remark = QLineEdit()
        self.edit_wechat_remark.setPlaceholderText("填入客户的微信备注")
        
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
        self.btn_historical_amount.setObjectName("HistoryAmountBtn")
        self.btn_historical_amount.setFlat(True)
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
        form_layout.addRow("微信备注:", self.edit_wechat_remark)
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
        self.save_btn.setObjectName("SaveBtn")
        self.save_btn.setFixedHeight(36)
        self.save_btn.clicked.connect(self._on_save_clicked)
        layout.addWidget(self.save_btn)
        
        layout.addStretch()
        self.current_phone = None

    def populate_combo_boxes(self, configs_dict):
        """填充后台字典下发的数据 (原生占位符模式)"""
        self.combo_unit.clear()
        self.combo_unit.addItems(configs_dict.get("unit_type_choices", []))
        self.combo_unit.setCurrentIndex(-1) # 默认不选中
        
        self.combo_purchase_type.clear()
        self.combo_purchase_type.addItems(configs_dict.get("purchase_type_choices", []))
        self.combo_purchase_type.setCurrentIndex(-1) # 默认不选中
        
        months = [f"{i}月" for i in range(1, 13)]
        self.combo_purchase_months.model.clear()
        self.combo_purchase_months.addItemsChecked(months)
        self.combo_purchase_months.lineEdit().clear() # 确保初始化为空

    def set_customer(self, data):
        self.current_phone = data.get("phone")
        self.edit_name.setText(data.get("customer_name", "-"))
        self.edit_phone.setText(data.get("phone", "-"))
        
        # 下拉框赋值优化：原生负索引模式
        u_idx = self.combo_unit.findText(data.get("unit_type", ""))
        self.combo_unit.setCurrentIndex(u_idx)
        
        self.combo_division.setCurrentText(data.get("admin_division", "") or "")
        
        p_idx = self.combo_purchase_type.findText(data.get("purchase_type", ""))
        self.combo_purchase_type.setCurrentIndex(p_idx)
        
        # 多选框：根据数据长度自适应
        months_str = data.get("purchase_months", "") or ""
        months_list = [m.strip() for m in months_str.split(",") if m.strip()]
        self.combo_purchase_months.set_checked_items(months_list)
        if not months_list:
            self.combo_purchase_months.lineEdit().clear()
        
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
        self.edit_wechat_remark.setText(data.get("wechat_remark", ""))

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
            "ai_profile": self.edit_profile.toPlainText().strip(),
            "wechat_remark": self.edit_wechat_remark.text().strip()
        }
        self.save_clicked.emit(self.current_phone, update_data)

class MainWindow(QMainWindow):
    """
    桌面端主窗口：侧边栏+多功能切换架构。
    """
    search_requested = Signal(str, int, int)
    customer_selected = Signal(dict)
    upload_wechat_clicked = Signal()

    def __init__(self, username: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"微企 AI 助手 - {username}")
        self.resize(850, 650)
        self.setObjectName("MainWindow")

        # --- 1. 主水平布局 ---
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.main_h_layout = QHBoxLayout(central_widget)
        self.main_h_layout.setContentsMargins(0, 0, 0, 0)
        self.main_h_layout.setSpacing(0)

        # --- 2. 左侧侧边栏 ---
        self.sidebar = QWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(220)
        sidebar_layout = QVBoxLayout(self.sidebar)
        
        sidebar_title = QLabel("我的客户")
        sidebar_title.setObjectName("SidebarTitle")
        sidebar_layout.addWidget(sidebar_title)

        self.customer_list = QListWidget()
        self.customer_list.setObjectName("CustomerList")
        self.customer_list.itemClicked.connect(self._on_customer_item_clicked)
        sidebar_layout.addWidget(self.customer_list)
        
        # 2.1 增加退出登录按钮与微信导入按钮
        self.btn_import_wechat = QPushButton("导入微信聊天记录")
        self.btn_import_wechat.setObjectName("ImportBtn")
        self.btn_import_wechat.setFlat(True)
        self.btn_import_wechat.clicked.connect(self.upload_wechat_clicked.emit)
        sidebar_layout.addWidget(self.btn_import_wechat)
        
        self.logout_btn = QPushButton("退出账户")
        self.logout_btn.setObjectName("LogoutBtn")
        self.logout_btn.setFlat(True)
        sidebar_layout.addWidget(self.logout_btn)
        
        self.main_h_layout.addWidget(self.sidebar)

        # --- 3. 右侧功能区 ---
        self.right_panel = QWidget()
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # 3.1 导航栏
        self.nav_bar = QWidget()
        self.nav_bar.setObjectName("NavBar")
        self.nav_bar.setFixedHeight(50)
        nav_layout = QHBoxLayout(self.nav_bar)
        nav_layout.setContentsMargins(15, 0, 15, 0)
        
        self.btn_chat = QPushButton("AI 对话")
        self.btn_info = QPushButton("客户资料")
        self.btn_prod = QPushButton("商品库")
        
        self.nav_buttons = [self.btn_chat, self.btn_info, self.btn_prod]
        
        for btn, idx in zip(self.nav_buttons, range(3)):
            btn.setObjectName("NavBtn")
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
            active = "true" if i == index else "false"
            btn.setProperty("active", active)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

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
        self.label.setObjectName("ChatBubbleLabel")
        self.label.setProperty("is_user", "true" if is_user else "false")
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        
        # 增加微光投影
        self.shadow = QGraphicsDropShadowEffect(self.label)
        self.shadow.setBlurRadius(8)
        self.shadow.setXOffset(0)
        self.shadow.setYOffset(1)
        self.shadow.setColor(QColor(0, 0, 0, 15))
        self.label.setGraphicsEffect(self.shadow)
        
        if is_user:
            layout.addStretch()
            layout.addWidget(self.label)
        else:
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
        self.scroll_area.setObjectName("ChatScrollArea")
        self.scroll_area.setWidgetResizable(True)
        
        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.addStretch() # 将消息顶到底部
        self.chat_layout.setSpacing(15) # 增加气泡间距，更有呼吸感
        self.chat_layout.setContentsMargins(15, 10, 15, 20)
        
        self.scroll_area.setWidget(self.chat_container)
        layout.addWidget(self.scroll_area)

        # 2. 底部输入区域
        input_container = QFrame()
        input_container.setObjectName("ChatInputContainer")
        input_container.setFixedHeight(120)
        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(15, 10, 15, 10)
        
        self.input_edit = QTextEdit()
        self.input_edit.setObjectName("ChatInput")
        self.input_edit.setPlaceholderText("请输入您的问题... (Ctrl + Enter 发送)")
        input_layout.addWidget(self.input_edit)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.send_btn = QPushButton("发送提问")
        self.send_btn.setObjectName("SendBtn")
        self.send_btn.setFixedSize(90, 32)
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

class ChatActionToolbar(QFrame):
    """
    气泡下方的操作工具栏：仅在悬停时显示。
    """
    copy_requested = Signal()
    like_requested = Signal()
    dislike_requested = Signal()
    regenerate_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatActionToolbar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(10)
        
        # 定义四枚极简图标
        self.btn_copy = self._create_btn("📋", "复制回复", self.copy_requested)
        self.btn_like = self._create_btn("👍", "有帮助", self.like_requested)
        self.btn_dislike = self._create_btn("👎", "不满意", self.dislike_requested)
        self.btn_redo = self._create_btn("🔄", "重新生成", self.regenerate_requested)
        
        layout.addWidget(self.btn_copy)
        layout.addWidget(self.btn_like)
        layout.addWidget(self.btn_dislike)
        layout.addWidget(self.btn_redo)
        layout.addStretch()

    def _create_btn(self, icon, tooltip, signal):
        btn = QPushButton(icon)
        btn.setObjectName("ActionIconBtn")
        btn.setFixedSize(18, 18) # 图标进一步缩小
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(signal.emit)
        return btn

class ChatBubble(QWidget):
    """
    单个聊天气泡组件：支持悬停工具栏 (仅 AI 回复)。
    """
    copy_triggered = Signal(str)     # 用于本地剪贴板
    copy_event_triggered = Signal(int) # 用于云端采纳记录 (msg_id)
    feedback_triggered = Signal(int, int) # (msg_id, rating)
    regenerate_triggered = Signal()

    def __init__(self, text, is_user=False, msg_id=None, rating=0, parent=None):
        super().__init__(parent)
        self.is_user = is_user
        self.msg_id = msg_id
        # 记录当前评价状态
        self.current_rating = rating
        
        self.main_v_layout = QVBoxLayout(self)
        self.main_v_layout.setContentsMargins(6, 4, 6, 4)
        self.main_v_layout.setSpacing(2)
        
        # 1. 气泡层 (水平布局控制对齐)
        self.bubble_h_layout = QHBoxLayout()
        self.bubble_h_layout.setContentsMargins(0, 0, 0, 0)
        
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        # 取消直接复制：移除交互标志，使用户必须点击工具栏复制按钮
        self.label.setTextInteractionFlags(Qt.NoTextInteraction)
        
        # 微光投影
        self.shadow = QGraphicsDropShadowEffect(self.label)
        self.shadow.setBlurRadius(8)
        self.shadow.setXOffset(0)
        self.shadow.setYOffset(1)
        self.shadow.setColor(QColor(0, 0, 0, 15))
        self.label.setGraphicsEffect(self.shadow)
        
        common_style = "padding: 8px 12px; border-radius: 8px; font-size: 13px; line-height: 1.4;"
        if is_user:
            self.bubble_h_layout.addStretch()
            self.label.setStyleSheet(f"{common_style} background-color: #95ec69; color: #000;")
            self.bubble_h_layout.addWidget(self.label)
        else:
            self.label.setStyleSheet(f"{common_style} background-color: #ffffff; color: #1f1f1f; border: 1px solid #ebebeb;")
            self.bubble_h_layout.addWidget(self.label)
            self.bubble_h_layout.addStretch()
            
        self.main_v_layout.addLayout(self.bubble_h_layout)
        
        # 2. 工具栏层 (仅非用户消息显示)
        self.toolbar = None
        if not is_user:
            self.toolbar = ChatActionToolbar()
            # 关键：使用透明度滤镜实现“预留空间”但不显示，避免抖动
            from PySide6.QtWidgets import QGraphicsOpacityEffect
            self.opacity_effect = QGraphicsOpacityEffect(self.toolbar)
            self.opacity_effect.setOpacity(0.0) # 初始透明
            self.toolbar.setGraphicsEffect(self.opacity_effect)
            
            # 绑定信号转发
            self.toolbar.copy_requested.connect(lambda: self._handle_copy())
            self.toolbar.like_requested.connect(lambda: self._emit_feedback(1))
            self.toolbar.dislike_requested.connect(lambda: self._emit_feedback(-1))
            self.toolbar.regenerate_requested.connect(self.regenerate_triggered.emit)
            
            # 工具栏对齐气泡左侧，并预留固定高度
            toolbar_layout = QHBoxLayout()
            toolbar_layout.setContentsMargins(8, 2, 0, 4)
            toolbar_layout.addWidget(self.toolbar)
            toolbar_layout.addStretch()
            self.main_v_layout.addLayout(toolbar_layout)
            
            # 如果存在历史评价，初始化按钮状态 (NEW)
            if rating != 0:
                self._apply_rating_ui(rating)

    def _apply_rating_ui(self, rating):
        """根据评分值点亮图标视觉 (1, -1, 0)"""
        if not self.toolbar: return
        
        # 先清除样式的 active 属性
        self.toolbar.btn_like.setProperty("active", "true" if rating == 1 else "false")
        self.toolbar.btn_dislike.setProperty("active", "true" if rating == -1 else "false")
        
        # 强制刷新样式表
        self.toolbar.btn_like.style().unpolish(self.toolbar.btn_like)
        self.toolbar.btn_like.style().polish(self.toolbar.btn_like)
        self.toolbar.btn_dislike.style().unpolish(self.toolbar.btn_dislike)
        self.toolbar.btn_dislike.style().polish(self.toolbar.btn_dislike)

    def _handle_copy(self):
        self.copy_triggered.emit(self.label.text())
        if self.msg_id:
            self.copy_event_triggered.emit(self.msg_id)
        
        # 点击反馈：将图标变色并锁定
        self.toolbar.btn_copy.setProperty("active", "true")
        self.toolbar.btn_copy.style().unpolish(self.toolbar.btn_copy)
        self.toolbar.btn_copy.style().polish(self.toolbar.btn_copy)

    def _emit_feedback(self, rating):
        if self.msg_id:
            # 如果点击的是已选中的，则视为取消评价 (0)
            target_rating = 0 if self.current_rating == rating else rating
            self.current_rating = target_rating
            
            self.feedback_triggered.emit(self.msg_id, target_rating)
            self._apply_rating_ui(target_rating)

    def enterEvent(self, event):
        """鼠标进入时：透明度渐现"""
        if self.toolbar and not self.is_user:
            self.opacity_effect.setOpacity(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开时：恢复透明 (如果没评价过)"""
        # 如果已经有了 active 状态，我们可以选择让它继续半透明显示，或者完全隐藏
        # 这里选择恢复隐藏，但保留 active 属性供下次悬停查看
        if self.toolbar:
            self.opacity_effect.setOpacity(0.0)
        super().leaveEvent(event)

    def append_text(self, new_text):
        self.label.setText(self.label.text() + new_text)
        # 寻找并通知父级滚动条刷新
        p = self.parentWidget()
        while p and not hasattr(p, "scroll_to_bottom"):
            p = p.parentWidget()
        if p:
            p.scroll_to_bottom()

class AIChatWidget(QWidget):
    """
    AI 智能对话主面板：适配窄屏，支持回车发送。
    """
    send_requested = Signal(str)
    copy_event_triggered = Signal(int) # 新增：复制事件信号
    feedback_requested = Signal(int, int) # msg_id, rating
    regenerate_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("ChatScrollArea")
        self.scroll_area.setWidgetResizable(True)
        
        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.addStretch()
        self.chat_layout.setSpacing(12)
        
        self.scroll_area.setWidget(self.chat_container)
        layout.addWidget(self.scroll_area)
        
        # 监听滚动条范围变化，实现自动触底
        self.scroll_area.verticalScrollBar().rangeChanged.connect(self.scroll_to_bottom)

        # 输入区域 (IM 风格)
        input_container = QFrame()
        input_container.setObjectName("ChatInputContainer")
        input_container.setFixedHeight(130)
        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(10, 5, 10, 5)

        self.input_edit = QuickTextEdit()
        self.input_edit.setObjectName("ChatInput")
        self.input_edit.setPlaceholderText("请输入问题... (Enter 发送, Ctrl+Enter 换行)")
        self.input_edit.enter_pressed.connect(self._on_send_clicked)
        input_layout.addWidget(self.input_edit)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("SendBtn")
        self.send_btn.setFixedSize(76, 40)
        self.send_btn.clicked.connect(self._on_send_clicked)
        btn_layout.addWidget(self.send_btn)
        input_layout.addLayout(btn_layout)

        layout.addWidget(input_container)

    def add_message(self, text, is_user=False, msg_id=None, rating=0):
        bubble = ChatBubble(text, is_user, msg_id, rating)
        
        # 绑定信号接力
        bubble.copy_triggered.connect(lambda t: QApplication.clipboard().setText(t))
        bubble.copy_event_triggered.connect(self.copy_event_triggered.emit) # 接力上报信号
        bubble.feedback_triggered.connect(self.feedback_requested.emit)
        bubble.regenerate_triggered.connect(self.regenerate_requested.emit)

        # 动态计算气泡最大宽度 (容器宽度的 90%)
        max_w = int(self.width() * 0.9)
        if max_w > 50:
            bubble.label.setMaximumWidth(max_w)
            
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        # 强制执行布局刷新并滚动
        QTimer.singleShot(50, self.scroll_to_bottom)
        return bubble

    def resizeEvent(self, event):
        """窗口缩放时，动态调整所有已有气泡的最大宽度"""
        super().resizeEvent(event)
        new_max_w = int(self.width() * 0.8)
        if new_max_w < 100: return
        
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), ChatBubble):
                item.widget().label.setMaximumWidth(new_max_w)

    def scroll_to_bottom(self):
        """将滚动条拉到最底部记录"""
        bar = self.scroll_area.verticalScrollBar()
        bar.setValue(bar.maximum())

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
    upload_wechat_clicked = Signal() # 手动触发导入微信流水库

    def __init__(self, username: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"微企 AI - {username}")
        # setMinimumSize开启自由缩放测试模式
        # self.setMinimumSize(300, 600)
        self.setFixedSize(400, 720)
        self.resize(400, 720) 
        self.setObjectName("MainWindow")

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.main_h_layout = QHBoxLayout(central_widget)
        self.main_h_layout.setContentsMargins(0, 0, 0, 0)
        self.main_h_layout.setSpacing(0)

        # 1. 侧边栏 (极简模式)
        self.sidebar = QWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(110)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(0, 10, 0, 10)
        
        # 客户列表
        self.customer_list = QListWidget()
        self.customer_list.setObjectName("CustomerList")
        # 优化：禁止自动获取焦点，防止初始化时自动选中首项
        self.customer_list.setFocusPolicy(Qt.NoFocus)
        self.customer_list.itemClicked.connect(self._on_customer_item_clicked)
        sidebar_layout.addWidget(self.customer_list)
        
        self.btn_import_wechat = QPushButton("导入微信聊天记录")
        self.btn_import_wechat.setFlat(True)
        self.btn_import_wechat.setCursor(Qt.PointingHandCursor)
        self.btn_import_wechat.setStyleSheet("color: #1890ff; font-size: 11px; margin-bottom: 5px;")
        self.btn_import_wechat.clicked.connect(self.upload_wechat_clicked.emit)
        sidebar_layout.addWidget(self.btn_import_wechat)
        
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
        self.nav_bar.setObjectName("NavBar")
        self.nav_bar.setFixedHeight(45)
        nav_layout = QHBoxLayout(self.nav_bar)
        nav_layout.setContentsMargins(5, 0, 5, 0)
        nav_layout.setSpacing(2)
        
        self.btn_chat = QPushButton("对话")
        self.btn_info = QPushButton("画像")
        self.btn_prod = QPushButton("商品")
        
        self.tabs = [self.btn_chat, self.btn_info, self.btn_prod]
        for btn, idx in zip(self.tabs, range(3)):
            btn.setObjectName("NavBtn")
            btn.setFlat(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedSize(80, 45)
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
        # 核心：侧边边距设为 0，实现全宽平铺
        prod_layout.setContentsMargins(0, 5, 0, 0)
        prod_layout.setSpacing(0)

        # 头部搜索区 (带内填充，保持美感)
        header_container = QWidget()
        header_layout = QVBoxLayout(header_container)
        header_layout.setContentsMargins(15, 5, 15, 5)
        header_layout.setSpacing(5)

        self.search_input = TagSearchWidget()
        self.search_input.search_triggered.connect(self._on_search_clicked)
        header_layout.addWidget(self.search_input)

        # 3.2.1 同步状态面板
        sync_panel = QHBoxLayout()
        sync_panel.setContentsMargins(5, 0, 5, 0)
        self.sync_status_lbl = QLabel("云端货源状态加载中...")
        self.sync_status_lbl.setObjectName("SyncStatus")
        sync_panel.addWidget(self.sync_status_lbl)

        sync_panel.addStretch()
        self.btn_sync_now = QPushButton("同步")
        self.btn_sync_now.setObjectName("SyncBtn")
        self.btn_sync_now.setFixedSize(40, 20)
        self.btn_sync_now.hide()
        self.btn_sync_now.clicked.connect(self.sync_triggered.emit)
        sync_panel.addWidget(self.btn_sync_now)
        header_layout.addLayout(sync_panel)
        
        prod_layout.addWidget(header_container)
        
        self.product_list = QListWidget()
        self.product_list.setObjectName("ProductList")
        # 彻底关闭横向滚动条，并强制项宽度随窗口动态调整
        self.product_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.product_list.setResizeMode(QListView.Adjust)
        self.product_list.setSpacing(0)
        prod_layout.addWidget(self.product_list)
        
        # 预制加载按钮
        self.load_more_btn = QPushButton("--- 展开更多货源 ---")
        self.load_more_btn.setObjectName("LoadMoreBtn")
        self.load_more_btn.setCursor(Qt.PointingHandCursor)
        self.load_more_btn.clicked.connect(self._on_load_more_clicked)
        self._load_more_item = None 

        self.stack.addWidget(self.chat_page)
        self.stack.addWidget(self.info_page)
        self.stack.addWidget(self.product_page)
        right_layout.addWidget(self.stack)
        self.main_h_layout.addWidget(self.right_panel)

        self._on_tab_changed(0) 

    def _on_tab_changed(self, index):
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self.tabs):
            active = "true" if i == index else "false"
            btn.setProperty("active", active)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def switch_tab(self, index):
        self._on_tab_changed(index)

    def _on_customer_item_clicked(self, item):
        self.customer_selected.emit(item.data(Qt.UserRole))

    def update_customer_list(self, customers):
        # 1. 记忆当前选中：在清空前记下当前客户的唯一手机号
        current_phone = None
        sel_item = self.customer_list.currentItem()
        if sel_item:
            current_phone = sel_item.data(Qt.UserRole).get("phone")
            
        self.customer_list.clear()
        
        target_item = None
        for c in customers:
            # 缩窄模式：仅显示姓名，手机号作为副文本
            name = c.get('customer_name', '未知')
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, c)
            item.setToolTip(f"手机: {c.get('phone')}")
            self.customer_list.addItem(item)
            
            # 2. 如果手机号匹配，记录该项位置
            if current_phone and c.get("phone") == current_phone:
                target_item = item
            
        # 3. 智能恢复选中状态
        if target_item:
            self.customer_list.setCurrentItem(target_item)
        else:
            # 确保首次加载或异步更新且未匹配到之前项时，清空选中
            self.customer_list.clearSelection()
            self.customer_list.setCurrentRow(-1)

    def _on_search_clicked(self, keyword=""):
        # 先解除按钮的父子关系，防止随 clear() 被 Qt 自动销毁
        self.load_more_btn.setParent(None)
        self.product_list.clear()
        self._load_more_item = None # 彻底重置状态
        
        # 始终读取完整的标签组合作为关键词
        final_kw = keyword if keyword else self.search_input.text()
        self.search_requested.emit(final_kw, 0, 20)

    def _on_load_more_clicked(self):
        # 修正：排除『加载更多』按钮占用的行数，确保 skip 为 20, 40, 60...
        actual_count = self.product_list.count()
        if self._load_more_item:
            actual_count -= 1
        self.search_requested.emit(self.search_input.text(), actual_count, 20)

    def add_product_card(self, product_data):
        # 如果存在加载更多项，由于它是永远排在最后的，我们需要先移除再插入新商品，或者直接在末尾前插入
        row = self.product_list.count()
        if self._load_more_item:
            row -= 1
            
        widget = ProductItemWidget(product_data)
        item = QListWidgetItem()
        self.product_list.insertItem(row, item)
        
        # 精准计算高度，但不锁死宽度
        target_width = self.product_list.viewport().width()
        if target_width < 100: target_width = 400 # 初始状态兜底
        
        widget.setFixedWidth(target_width)
        widget.adjustSize()
        # 锁定高度防止抖动，但允许宽度随 ResizeMode 自由拉伸
        h = widget.sizeHint().height()
        item.setSizeHint(QSize(0, h)) 
        widget.setMinimumWidth(0) # 释放宽度约束
        widget.setMaximumWidth(16777215)
        
        self.product_list.setItemWidget(item, widget)
        return widget

    def update_has_more(self, has_more):
        """将『点击加载』按钮集成到列表流末尾，并使用容器纠正扭曲"""
        if has_more:
            # 如果已经存在，我们需要先提取它，确保它被移动到最后一行
            if self._load_more_item:
                row = self.product_list.row(self._load_more_item)
                if row >= 0:
                    self.product_list.takeItem(row)
                self._load_more_item = None # 重置以触发重新创建
            
            # 创建包装容器解决按钮拉伸扭曲问题
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
            # 如果没有更多了，且按钮还在，则将其安全移除
            if self._load_more_item:
                row = self.product_list.row(self._load_more_item)
                if row >= 0:
                    self.load_more_btn.setParent(None) # 资产剥离
                    self.product_list.takeItem(row)
                self._load_more_item = None

    def resizeEvent(self, event):
        """核心 Liquid Layout：当窗口拉伸时，强制刷新商品卡片的高度"""
        super().resizeEvent(event)
        
        # 1. 刷新商品列表
        new_width = self.product_list.viewport().width()
        if new_width > 50:
            for i in range(self.product_list.count()):
                item = self.product_list.item(i)
                widget = self.product_list.itemWidget(item)
                if widget and isinstance(widget, ProductItemWidget):
                    widget.setFixedWidth(new_width)
                    widget.adjustSize()
                    # 更新 Item 的 SizeHint 否则会留白或重叠
                    h = widget.sizeHint().height()
                    item.setSizeHint(QSize(0, h))
                    # 计算完高度后释放宽度锁定，允许其继续弹性展示
                    widget.setMinimumWidth(0)
                    widget.setMaximumWidth(16777215)
