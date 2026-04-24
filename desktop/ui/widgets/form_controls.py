"""
通用表单控件：MultiSelectComboBox / NoScrollComboBox / CalendarPopup / DatePickerBtn
对应 UI_implementation.md Phase 3 — 客户信息表单改造
"""
from PySide6.QtWidgets import (
    QComboBox, QPushButton, QWidget, QVBoxLayout, QFrame, QCalendarWidget,
    QGraphicsDropShadowEffect,
)
from PySide6.QtCore import Qt, Signal, QDate, QTime, QEvent
from PySide6.QtGui import QStandardItemModel, QStandardItem, QColor

from qfluentwidgets import ComboBox, PushButton, isDarkTheme

PROFILE_TAG_ID_ROLE = Qt.UserRole + 64


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
        self._apply_theme_style()

    def _apply_theme_style(self):
        """注入 Fluent 风格的样式表"""
        is_dark = isDarkTheme()
        bg = "#333333" if is_dark else "#ffffff"
        border = "#404040" if is_dark else "#d0d0d0"
        text = "#eeeeee" if is_dark else "#333333"
        hover_bg = "#404040" if is_dark else "#f3f3f3"
        
        self.setStyleSheet(f"""
            QComboBox {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 5px;
                padding-left: 8px;
                color: {text};
                height: 30px;
            }}
            QComboBox:hover {{
                background-color: {hover_bg};
                border: 1px solid #0078d4;
            }}
            QComboBox::drop-down {{ border: none; }}
            QComboBox::down-arrow {{
                image: none;
                border: none;
            }}
        """)
        # 针对内部 LineEdit 的特殊覆盖
        self.lineEdit().setStyleSheet(f"background: transparent; border: none; color: {text}; padding: 0px;")

    def eventFilter(self, obj, event):
        if obj == self.lineEdit() and event.type() == QEvent.MouseButtonPress:
            if hasattr(self, '_last_hide_time') and self._last_hide_time.msecsTo(QTime.currentTime()) < 150:
                pass
            else:
                self.showPopup()
            return True
        return super().eventFilter(obj, event)

    def hidePopup(self):
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


class ProfileTagMultiSelectComboBox(MultiSelectComboBox):
    """动态标签多选：项内存 tag id（PROFILE_TAG_ID_ROLE），展示名称。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lineEdit().setPlaceholderText("点击勾选动态标签，保存后同步至云端")

    def set_tag_items(self, items: list):
        """items: {id, name, feature_note?, strategy_note?}"""
        self.model.clear()
        for t in items:
            tid = int(t["id"])
            name = (t.get("name") or "").strip() or f"#{tid}"
            item = QStandardItem(name)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            item.setData(Qt.Unchecked, Qt.CheckStateRole)
            item.setData(tid, PROFILE_TAG_ID_ROLE)
            fn = (t.get("feature_note") or "").strip()
            sn = (t.get("strategy_note") or "").strip()
            tips = []
            if fn:
                tips.append(f"特征：{fn}")
            if sn:
                tips.append(f"策略：{sn}")
            item.setToolTip("\n".join(tips) if tips else name)
            self.model.appendRow(item)
        self.lineEdit().clear()
        self._update_text()

    def get_checked_tag_ids(self) -> list[int]:
        out: list[int] = []
        for i in range(self.model.rowCount()):
            item = self.model.item(i)
            if item.checkState() == Qt.Checked:
                v = item.data(PROFILE_TAG_ID_ROLE)
                if v is not None:
                    out.append(int(v))
        return out

    def set_checked_tag_ids(self, ids: list[int]):
        id_set = {int(x) for x in ids}
        for i in range(self.model.rowCount()):
            item = self.model.item(i)
            v = item.data(PROFILE_TAG_ID_ROLE)
            if v is not None and int(v) in id_set:
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
        self._update_text()


class NoScrollComboBox(ComboBox):
    """阻止鼠标滚轮误触的下拉框"""
    def wheelEvent(self, event):
        event.ignore()


class CalendarPopup(QWidget):
    """自定义日历弹出面板，解决原生 QDateEdit 丑陋且残缺的问题"""
    date_selected = Signal(object)  # 抛出 QDate

    def __init__(self, init_date=None, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.container = QFrame()
        self.container.setObjectName("CalendarContainer")

        c_layout = QVBoxLayout(self.container)
        c_layout.setContentsMargins(2, 2, 2, 2)

        self._apply_theme_style()

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg = "#2d2d2d" if is_dark else "#ffffff"
        border = "#404040" if is_dark else "#d0d0d0"
        self.container.setStyleSheet(f"""
            QFrame#CalendarContainer {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
        """)
        
        # 注入阴影
        self.shadow = QGraphicsDropShadowEffect(self.container)
        self.shadow.setBlurRadius(15)
        self.shadow.setXOffset(0)
        self.shadow.setYOffset(4)
        self.shadow.setColor(QColor(0, 0, 0, 40))
        self.container.setGraphicsEffect(self.shadow)

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
            self.parent_btn._last_hide_time = QTime.currentTime()
        super().hideEvent(event)


class DatePickerBtn(PushButton):
    """伪装成输入框的时间选择器，全区域可点，风格统一"""
    def __init__(self, parent=None):
        super().__init__(parent)
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
        if hasattr(self, '_last_hide_time') and self._last_hide_time.msecsTo(QTime.currentTime()) < 150:
            return
        self.popup = CalendarPopup(self.current_date)
        self.popup.parent_btn = self
        self.popup.date_selected.connect(lambda d: self.setDate(d))

        pos = self.mapToGlobal(self.rect().bottomLeft())
        self.popup.move(pos.x(), pos.y() + 2)
        self.popup.show()
