"""
通用表单控件：MultiSelectComboBox / NoScrollComboBox / CalendarPopup / DatePickerBtn
对应 UI_implementation.md Phase 3 — 客户信息表单改造
"""
from PySide6.QtWidgets import (
    QComboBox, QPushButton, QWidget, QVBoxLayout, QFrame, QCalendarWidget,
)
from PySide6.QtCore import Qt, Signal, QDate, QTime, QEvent
from PySide6.QtGui import QStandardItemModel, QStandardItem


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


class NoScrollComboBox(QComboBox):
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


class DatePickerBtn(QPushButton):
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
