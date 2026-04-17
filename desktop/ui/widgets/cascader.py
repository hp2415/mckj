"""
省市区三级联动组件：CascaderPopup / RegionCascader
对应 UI_implementation.md Phase 3 — 客户信息表单改造
"""
from PySide6.QtWidgets import QWidget, QHBoxLayout, QFrame
from PySide6.QtCore import Qt, Signal, QTime

from qfluentwidgets import ListWidget, PushButton, isDarkTheme


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

        self.list_prov = ListWidget()
        self.list_city = ListWidget()
        self.list_dist = ListWidget()

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
        layout.insertWidget(3, self.line2)
        
        self._apply_theme_style()

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg = "#272727" if is_dark else "#ffffff"
        border = "#404040" if is_dark else "#d0d0d0"
        line_col = "#444444" if is_dark else "#e8e8e8"
        
        self.setStyleSheet(f"""
            QWidget#CascaderPopup {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            QListWidget {{
                background-color: transparent;
                border: none;
                outline: none;
            }}
        """)
        self.line1.setStyleSheet(f"background-color: {line_col}; border: none;")
        self.line2.setStyleSheet(f"background-color: {line_col}; border: none;")

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
        if not self.prov_str:
            return

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
            self.parent_btn._last_hide_time = QTime.currentTime()
        super().hideEvent(event)


class RegionCascader(PushButton):
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
