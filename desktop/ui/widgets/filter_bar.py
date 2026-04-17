"""
商品高级筛选面板：ProductFilterBar (折叠版)
优化了比例，支持店铺与种类/产地的联动。
修复了弹出框定位偏移与点击失效的问题。
"""
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, 
    QFrame, QLineEdit, QCompleter, QScrollArea, QStackedWidget
)
from PySide6.QtCore import Qt, Signal, QTimer, QSize, QPoint
from PySide6.QtGui import QIntValidator, QDoubleValidator

from qfluentwidgets import (
    ComboBox, PushButton, TransparentToolButton, 
    FluentIcon, LineEdit, PrimaryPushButton, ListWidget,
    TransparentPushButton, ScrollArea, IconWidget, isDarkTheme,
    ToolTipFilter, ToolTipPosition
)

class CascaderPopup(QFrame):
    """
    Fluent 风格单列导航级联面板
    """
    selection_finished = Signal(list)

    def __init__(self, data, placeholder="全部", parent=None):
        # 5.9 Fix: 为了确保全局定位准确，不强绑定物理 Parent，由外部决定坐标
        super().__init__(None) 
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("CascaderPopup")
        self.setFixedWidth(200) 

        self.full_data = data
        self.history = [] 
        self.current_items = data
        self.path = [] 

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(4, 4, 4, 4)
        self.main_layout.setSpacing(2)

        self.header = QWidget()
        self.header_layout = QHBoxLayout(self.header)
        self.header_layout.setContentsMargins(4, 2, 4, 2)
        self.header_layout.setSpacing(8)

        self.btn_back = TransparentToolButton(FluentIcon.LEFT_ARROW)
        self.btn_back.setFixedSize(24, 24)
        self.btn_back.clicked.connect(self._go_back)
        self.header_layout.addWidget(self.btn_back)

        self.lbl_title = QLabel("选择分类")
        self.header_layout.addWidget(self.lbl_title)
        self.header_layout.addStretch()
        
        self.main_layout.addWidget(self.header)
        self.btn_back.hide() 

        self.line_sep = QFrame()
        self.line_sep.setFrameShape(QFrame.HLine)
        self.main_layout.addWidget(self.line_sep)

        self.scroll = ScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFixedHeight(260)
        self.scroll.setStyleSheet("border: none; background: transparent;")
        
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(1)
        self.list_layout.addStretch()
        
        self.scroll.setWidget(self.list_container)
        self.main_layout.addWidget(self.scroll)

        self._apply_theme_style()
        self._render_items(data)

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg = "#272727" if is_dark else "#ffffff"
        border = "#404040" if is_dark else "#d0d0d0"
        title_col = "#eeeeee" if is_dark else "#333333"
        line_col = "#444444" if is_dark else "#eeeeee"
        
        self.setStyleSheet(f"""
            QFrame#CascaderPopup {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
        """)
        self.lbl_title.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {title_col};")
        self.line_sep.setStyleSheet(f"background-color: {line_col}; margin-bottom: 2px;")

    def _render_items(self, items):
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for item_data in items:
            name = item_data.get("label", "Unknown")
            has_children = bool(item_data.get("children"))
            
            btn = TransparentPushButton(name)
            btn.setFixedHeight(32)
            is_dark = isDarkTheme()
            text_col = "#eeeeee" if is_dark else "#333333"
            hover_bg = "rgba(255, 255, 255, 0.1)" if is_dark else "rgba(0, 0, 0, 0.05)"
            
            btn.setStyleSheet(f"""
                QPushButton {{
                    text-align: left;
                    padding-left: 10px;
                    border-radius: 4px;
                    font-size: 12px;
                    color: {text_col};
                    border: none;
                    background-color: transparent;
                }}
                QPushButton:hover {{ background-color: {hover_bg}; }}
            """)
            
            if has_children:
                arrow = IconWidget(FluentIcon.CHEVRON_RIGHT)
                arrow.setFixedSize(12, 12)
                arrow_layout = QHBoxLayout(btn)
                arrow_layout.addStretch()
                arrow_layout.addWidget(arrow)
                arrow_layout.setContentsMargins(0, 0, 8, 0)
                
            btn.clicked.connect(lambda checked=False, d=item_data: self._on_item_clicked(d))
            self.list_layout.insertWidget(self.list_layout.count()-1, btn)

    def _on_item_clicked(self, item_data):
        name = item_data["label"]
        children = item_data.get("children", [])
        self.path.append(name)
        if children:
            self.history.append(self.current_items)
            self.current_items = children
            self.lbl_title.setText(name)
            self.btn_back.show()
            self._render_items(children)
        else:
            self._finalize()

    def _go_back(self):
        if self.history:
            self.current_items = self.history.pop()
            self.path.pop()
            self._render_items(self.current_items)
            if not self.history:
                self.lbl_title.setText("选择分类")
                self.btn_back.hide()
            else:
                prev_name = self.path[-1] if self.path else "选择分类"
                self.lbl_title.setText(prev_name)

    def _finalize(self):
        final_path = self.path[:]
        while len(final_path) < 3:
            final_path.append("")
        self.selection_finished.emit(final_path)
        self.close() # 彻底关闭
        self.deleteLater()


class GenericCascader(PushButton):
    """通用级联触发按钮"""
    changed = Signal(list)

    def __init__(self, placeholder="选择", parent=None):
        super().__init__(parent)
        self.placeholder = placeholder
        self.data = []
        self.setText(placeholder)
        self.setMinimumWidth(80) 
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(self._show_popup)
        self.current_path = ["", "", ""]
        
        self.setToolTip(placeholder)
        self.installEventFilter(ToolTipFilter(self, 300, ToolTipPosition.BOTTOM))

    def set_data(self, data):
        self.data = data

    def _show_popup(self):
        if not self.data: return
        self.popup = CascaderPopup(self.data) # 不传 Parent 以免偏移
        self.popup.selection_finished.connect(self._on_selected)
        
        # 精确计算全局位置
        global_pos = self.mapToGlobal(QPoint(0, self.height() + 4))
        
        # 边界检测：使用主窗口的全局坐标系
        win = self.window()
        if win:
            win_rect = win.geometry()
            # 顶部和左右边界检测
            if global_pos.x() + self.popup.width() > win_rect.right():
                new_x = win_rect.right() - self.popup.width() - 8
                global_pos.setX(max(win_rect.left() + 8, new_x))
                
        self.popup.move(global_pos)
        self.popup.show()

    def _on_selected(self, path):
        self.current_path = path
        active_vals = [p for p in path if p]
        display = active_vals[-1] if active_vals else self.placeholder
        self.setText(display)
        self.setToolTip(" / ".join(active_vals) if active_vals else self.placeholder)
        self.changed.emit(path)

    def clear(self):
        self.current_path = ["", "", ""]
        self.setText(self.placeholder)


class ProductFilterBar(QFrame):
    """
    商品高级筛选面板：增加了联动支持与布局调优
    """
    filter_changed = Signal(dict)
    metadata_refresh_requested = Signal(str) # 联动信号：传递当前店铺名

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProductFilterBar")
        
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        from PySide6.QtGui import QColor
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(15)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 30))
        self.setGraphicsEffect(shadow)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 8, 10, 4) # 底部边距从 8 调优为 4，减小与列表的间隙
        main_layout.setSpacing(6)

        # 第一行：店铺 + 种类 (5.9 比例调整: 5.5 : 4.5)
        row1 = QHBoxLayout()
        row1.setSpacing(5)
        row1.addWidget(QLabel("店铺:"))
        self.shop_combo = ComboBox()
        self.shop_combo.setPlaceholderText("全部")
        self.shop_combo.setMinimumWidth(100)
        self.shop_combo.currentIndexChanged.connect(self._on_shop_selected) # 连接联动事件
        row1.addWidget(self.shop_combo, 55)

        row1.addSpacing(5)
        
        row1.addWidget(QLabel("种类:"))
        self.cat_cascader = GenericCascader("不限")
        row1.addWidget(self.cat_cascader, 45)
        main_layout.addLayout(row1)

        # 第二行：产地 + 价格 (5.9 比例调整: 4.5 : 5.5)
        row2 = QHBoxLayout()
        row2.setSpacing(5)
        row2.addWidget(QLabel("产地:"))
        self.org_cascader = GenericCascader("不限")
        row2.addWidget(self.org_cascader, 40) # 减小一点产地占比 (45 -> 40)

        row2.addSpacing(10) # 增大间距

        row2.addWidget(QLabel("价格:"))
        self.min_price = LineEdit()
        self.min_price.setPlaceholderText("起")
        self.min_price.setFixedWidth(55) # 增加宽度 (45 -> 55)
        self.min_price.setValidator(QDoubleValidator(0, 99999, 2))
        row2.addWidget(self.min_price)
        
        row2.addWidget(QLabel("-"))
        
        self.max_price = LineEdit()
        self.max_price.setPlaceholderText("止")
        self.max_price.setFixedWidth(55) # 增加宽度 (45 -> 55)
        self.max_price.setValidator(QDoubleValidator(0, 99999, 2))
        row2.addWidget(self.max_price)
        main_layout.addLayout(row2)

        # 第三行：操作区
        row3 = QHBoxLayout()
        row3.setContentsMargins(0, 2, 0, 0)
        row3.addStretch()
        self.btn_reset = TransparentToolButton(FluentIcon.DELETE)
        self.btn_reset.setToolTip("清空重置")
        self.btn_reset.installEventFilter(ToolTipFilter(self.btn_reset, 300, ToolTipPosition.BOTTOM))
        self.btn_reset.setFixedSize(28, 28)
        self.btn_reset.clicked.connect(self.reset_all)
        row3.addWidget(self.btn_reset)

        self.btn_apply = PrimaryPushButton("确定筛选")
        self.btn_apply.setFixedSize(80, 32)
        # 针对按钮的微调：加粗、更圆、稍微加宽
        self.btn_apply.setStyleSheet("""
            PrimaryPushButton {
                font-size: 11px;
                font-weight: 600;
                border-radius: 6px;
                padding: 0px 12px;
            }
        """)
        self.btn_apply.clicked.connect(self._on_apply_clicked)
        row3.addWidget(self.btn_apply)
        main_layout.addLayout(row3)
        
        # 记录投影效果以便后续刷新
        self.shadow = shadow
        self._apply_theme_style()

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        if is_dark:
            bg = "#2c2c2c"
            border = "#404040"
            lbl_col = "#aaaaaa"
            shadow_op = 10 
        else:
            bg = "#ffffff"
            border = "#e0e0e0"
            lbl_col = "#555555"
            shadow_op = 30

        self.setStyleSheet(f"""
            QFrame#ProductFilterBar {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 12px;
                margin: 5px 12px;
            }}
            QLabel {{ color: {lbl_col}; font-size: 11px; font-weight: bold; }}
            QPushButton {{ color: {lbl_col}; font-size: 11px; border: none; background: transparent; text-align: left; }}
        """)
        
        if hasattr(self, "shadow") and self.shadow:
            from PySide6.QtGui import QColor
            self.shadow.setColor(QColor(0, 0, 0, shadow_op))

    def _on_shop_selected(self, index):
        """当店铺选择改变时，触发元数据刷新信号"""
        txt = self.shop_combo.currentText()
        shop_name = "" if txt == "全部店铺" or txt == "全部" else txt
        self.metadata_refresh_requested.emit(shop_name)

    def set_metadata(self, suppliers, categories, origins, update_shop=True):
        """支持选择性更新数据，联动时通常只更新种类和产地"""
        if update_shop:
            self.shop_combo.blockSignals(True) # 防止更新时触发联动循环
            self.shop_combo.clear()
            self.shop_combo.addItem("全部店铺", "")
            for s in sorted(suppliers): self.shop_combo.addItem(s, s) # 增加排序
            self.shop_combo.blockSignals(False)

        self.cat_cascader.set_data(categories)
        self.org_cascader.set_data(origins)

    def reset_all(self):
        self.shop_combo.setCurrentIndex(0)
        self.cat_cascader.clear()
        self.org_cascader.clear()
        self.min_price.clear()
        self.max_price.clear()
        self._on_apply_clicked()

    def _on_apply_clicked(self):
        cat = self.cat_cascader.current_path
        org = self.org_cascader.current_path
        shop_text = self.shop_combo.currentText()
        supplier_val = "" if shop_text == "全部店铺" or shop_text == "全部" else shop_text

        self.filter_changed.emit({
            "supplier_name": supplier_val,
            "cat1": cat[0], "cat2": cat[1], "cat3": cat[2],
            "province": org[0], "city": org[1], "district": org[2],
            "min_price": float(self.min_price.text()) if self.min_price.text() else None,
            "max_price": float(self.max_price.text()) if self.max_price.text() else None
        })
