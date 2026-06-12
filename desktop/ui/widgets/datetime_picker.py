"""
一体化日期时间选择器（由 Qt/C++ demo 移植）
输入框 + 弹出面板（日历 + 时分秒滚轮）
"""
from enum import IntEnum

from PySide6.QtCore import (
    Qt, Signal, QDate, QTime, QDateTime, QEvent, Property, QTimer,
    QPropertyAnimation, QEasingCurve, QRectF,
)
from PySide6.QtGui import QPainter, QPen, QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QDialog, QLineEdit,
    QLabel, QFrame, QGraphicsDropShadowEffect, QStackedWidget,
)

from qfluentwidgets import (
    PushButton, PrimaryPushButton, FluentIcon, isDarkTheme, themeColor,
    TransparentToolButton, ToolTipFilter, ToolTipPosition,
)
from qfluentwidgets.components.date_time.calendar_view import (
    CalendarViewBase, DayCalendarView, MonthCalendarView, YearCalendarView,
    DayScrollView, MonthScrollView, YearScrollView,
)


CALENDAR_YEAR_SPAN = 5


def _theme_colors() -> dict[str, str]:
    """与客资详情页 / 表单控件保持一致的主题色。"""
    is_dark = isDarkTheme()
    return {
        "bg": "#272727" if is_dark else "#fdfdfd",
        "card": "#303030" if is_dark else "#f5f5f5",
        "input_bg": "#333333" if is_dark else "#ffffff",
        "border": "#404040" if is_dark else "#d0d0d0",
        "text": "#eeeeee" if is_dark else "#333333",
        "text_sub": "#9c9c9c" if is_dark else "#8c8c8c",
        "hover_bg": "#404040" if is_dark else "#f3f3f3",
        "accent": themeColor().name(),
    }


class CompactDayScrollView(DayScrollView):
    """Fluent 日历日视图：以 anchor 为中心 ±5 年（复用官方滚动逻辑，避免空白）。"""

    def __init__(self, parent=None, anchor: QDate | None = None):
        self._compact_anchor = anchor or QDate.currentDate()
        super().__init__(parent)

    def _initItems(self):
        anchor = self._compact_anchor
        self.minYear = anchor.year() - CALENDAR_YEAR_SPAN
        self.maxYear = anchor.year() + CALENDAR_YEAR_SPAN
        super()._initItems()

    def scrollToPage(self, page: int):
        max_page = (self.maxYear - self.minYear + 1) * 12 - 1
        if not 0 <= page <= max_page:
            return
        super().scrollToPage(page)

    def contains_date(self, date: QDate) -> bool:
        return self.minYear <= date.year() <= self.maxYear

    def reanchor(self, date: QDate):
        if self.contains_date(date):
            self.setDate(date)
            return
        self._compact_anchor = date
        self.clear()
        self._initItems()
        self.setDate(date)


class _CompactScrollMixin:
    """±5 年窗口的滚动视图基类混入。"""

    _compact_anchor: QDate

    def contains_date(self, date: QDate) -> bool:
        return self.minYear <= date.year() <= self.maxYear

    def _apply_year_window(self):
        anchor = self._compact_anchor
        self.minYear = anchor.year() - CALENDAR_YEAR_SPAN
        self.maxYear = anchor.year() + CALENDAR_YEAR_SPAN

    def reanchor(self, date: QDate):
        if self.contains_date(date):
            self.scrollToDate(date)
            return
        self._compact_anchor = date
        self.clear()
        self._initItems()
        self.scrollToDate(date)


class CompactMonthScrollView(_CompactScrollMixin, MonthScrollView):
    def __init__(self, parent=None, anchor: QDate | None = None):
        self._compact_anchor = anchor or QDate.currentDate()
        super().__init__(parent)

    def _initItems(self):
        self._apply_year_window()
        year_count = self.maxYear - self.minYear + 1
        self.months = [
            self.tr("Jan"), self.tr("Feb"), self.tr("Mar"), self.tr("Apr"),
            self.tr("May"), self.tr("Jun"), self.tr("Jul"), self.tr("Aug"),
            self.tr("Sep"), self.tr("Oct"), self.tr("Nov"), self.tr("Dec"),
        ]
        self.addItems(self.months * year_count)
        for i in range(12 * year_count):
            year = i // 12 + self.minYear
            month = i % 12 + 1
            item = self.item(i)
            item.setData(Qt.ItemDataRole.UserRole, QDate(year, month, 1))
            item.setSizeHint(self.gridSize())
            if year == self.currentDate.year() and month == self.currentDate.month():
                self.delegate.setCurrentIndex(self.indexFromItem(item))


class CompactYearScrollView(_CompactScrollMixin, YearScrollView):
    def __init__(self, parent=None, anchor: QDate | None = None):
        self._compact_anchor = anchor or QDate.currentDate()
        super().__init__(parent)

    def _initItems(self):
        self._apply_year_window()
        super()._initItems()


class CompactDayCalendarView(DayCalendarView):
    """范围受限的日视图。"""

    def __init__(self, parent=None, anchor: QDate | None = None):
        CalendarViewBase.__init__(self, parent)
        self.setScrollView(CompactDayScrollView(self, anchor=anchor))


class CompactMonthCalendarView(MonthCalendarView):
    """范围受限的月视图。"""

    def __init__(self, parent=None, anchor: QDate | None = None):
        CalendarViewBase.__init__(self, parent)
        self.setScrollView(CompactMonthScrollView(self, anchor=anchor))


class CompactYearCalendarView(YearCalendarView):
    """范围受限的年视图。"""

    def __init__(self, parent=None, anchor: QDate | None = None):
        CalendarViewBase.__init__(self, parent)
        self.setScrollView(CompactYearScrollView(self, anchor=anchor))
        self.titleButton.setEnabled(False)


class CompactCalendarStack(QWidget):
    """日 / 月 / 年三级 Fluent 日历（与 CalendarPicker 交互一致）。"""

    itemClicked = Signal(QDate)

    def __init__(self, parent=None, anchor: QDate | None = None):
        super().__init__(parent)
        self._anchor = anchor or QDate.currentDate()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.stacked = QStackedWidget(self)
        self.day_view = CompactDayCalendarView(anchor=self._anchor)
        self.month_view = CompactMonthCalendarView(anchor=self._anchor)
        self.year_view = CompactYearCalendarView(anchor=self._anchor)
        self.stacked.addWidget(self.day_view)
        self.stacked.addWidget(self.month_view)
        self.stacked.addWidget(self.year_view)
        layout.addWidget(self.stacked)

        self.day_view.titleClicked.connect(self._on_day_title_clicked)
        self.month_view.titleClicked.connect(self._on_month_title_clicked)
        self.month_view.itemClicked.connect(self._on_month_item_clicked)
        self.year_view.itemClicked.connect(self._on_year_item_clicked)
        self.day_view.itemClicked.connect(self.itemClicked.emit)

    def _on_day_title_clicked(self):
        self.stacked.setCurrentWidget(self.month_view)
        self.month_view.setDate(self.day_view.currentPageDate())

    def _on_month_title_clicked(self):
        self.stacked.setCurrentWidget(self.year_view)
        self.year_view.setDate(self.month_view.currentPageDate())

    def _on_month_item_clicked(self, date: QDate):
        self.stacked.setCurrentWidget(self.day_view)
        self.day_view.scrollToDate(date)

    def _on_year_item_clicked(self, date: QDate):
        self.stacked.setCurrentWidget(self.month_view)
        self.month_view.setDate(date)

    def ensure_anchor(self, date: QDate):
        self._anchor = date
        self.stacked.setCurrentWidget(self.day_view)
        for view in (self.day_view, self.month_view, self.year_view):
            scroll = view.scrollView
            if hasattr(scroll, "reanchor"):
                scroll.reanchor(date)
            else:
                view.setDate(date)


class ChangeType(IntEnum):
    UNKNOWN = 0
    YEAR = 1
    MONTH = 2
    DAY = 3
    HOUR = 4
    MINUTE = 5
    SECOND = 6


class RollingTimeWidget(QWidget):
    """时分秒滚轮选择控件"""

    timeUpdated = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("RollingTimeWidget")
        self.setFixedSize(52, 120)
        self.min_range = 0
        self.max_range = 0
        self.number_size = 12
        self.rolling_judgment = False
        self.current_pos_y = 0
        self.current_time = 0
        self._pos_y_shifting = 0

        self.rolling_ani = QPropertyAnimation(self, b"posYShifting")
        self.rolling_ani.setDuration(500)
        self.rolling_ani.setEasingCurve(QEasingCurve.Type.OutCirc)
        self.rolling_ani.finished.connect(self._on_animation_finished)

        self._apply_theme()

    def _apply_theme(self):
        c = _theme_colors()
        self.setStyleSheet(f"""
            RollingTimeWidget {{
                background: {c["card"]};
                border: 1px solid {c["border"]};
                border-radius: 4px;
            }}
        """)

    def setTimeRange(self, min_val: int, max_val: int):
        self.min_range = min_val
        self.max_range = max_val
        self.update()

    def getPosYShifting(self) -> int:
        return self._pos_y_shifting

    def setPosYShifting(self, value: int):
        self._pos_y_shifting = value
        self.update()

    posYShifting = Property(int, getPosYShifting, setPosYShifting)

    def setCurrTimeVal(self, val: int):
        self.current_time = max(self.min_range, min(self.max_range, val))
        self.roll_animation()
        self.update()

    def getCurrTimeVal(self) -> int:
        return self.current_time

    def _on_animation_finished(self):
        self.timeUpdated.emit(self.current_time)

    def mousePressEvent(self, event):
        self.rolling_ani.stop()
        self.rolling_judgment = True
        self.current_pos_y = int(event.position().y())

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta > 0 and self.current_time > self.min_range:
            self._pos_y_shifting = self.height() // 4
        elif delta < 0 and self.current_time < self.max_range:
            self._pos_y_shifting = -self.height() // 4
        self.roll_animation()
        self.update()

    def mouseMoveEvent(self, event):
        if not self.rolling_judgment:
            return
        y = int(event.position().y())
        if (self.current_time == self.min_range and y >= self.current_pos_y) or (
            self.current_time == self.max_range and y <= self.current_pos_y
        ):
            return
        self._pos_y_shifting = y - self.current_pos_y
        self.update()

    def mouseReleaseEvent(self, event):
        if self.rolling_judgment:
            self.rolling_judgment = False
            self.roll_animation()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        h = self.height()
        quarter = h // 4

        if self._pos_y_shifting >= quarter and self.current_time > self.min_range:
            self.current_pos_y += quarter
            self._pos_y_shifting -= quarter
            self.current_time -= 1

        if self._pos_y_shifting <= -quarter and self.current_time < self.max_range:
            self.current_pos_y -= quarter
            self._pos_y_shifting += quarter
            self.current_time += 1

        self._draw_number(painter, self.current_time, self._pos_y_shifting)

        if self.current_time != self.min_range:
            self._draw_number(painter, self.current_time - 1, self._pos_y_shifting - quarter)
        if self.current_time != self.max_range:
            self._draw_number(painter, self.current_time + 1, self._pos_y_shifting + quarter)

        line_color = QColor(120, 120, 120, 80)
        painter.setPen(QPen(line_color, 1))
        painter.drawLine(0, h * 3 // 8, self.width(), h * 3 // 8)
        painter.drawLine(0, h * 5 // 8, self.width(), h * 5 // 8)

    def _draw_number(self, painter: QPainter, value: int, offset: int):
        h = self.height()
        size = max(10, (h - abs(offset)) // self.number_size)
        transparency = max(40, 255 - 510 * abs(offset) // h)
        text_h = h // 2 - 3 * abs(offset) // 5
        y = h // 2 + offset - text_h // 2

        font = QFont(self.font())
        font.setPixelSize(size)
        painter.setFont(font)

        c = _theme_colors()
        is_center = abs(offset) < h // 16
        if is_center:
            color = QColor(c["accent"])
        else:
            sub = QColor(c["text_sub"])
            sub.setAlpha(transparency)
            color = sub
        painter.setPen(color)
        painter.drawText(QRectF(0, y, self.width(), text_h), Qt.AlignmentFlag.AlignCenter, str(value).zfill(2))

    def roll_animation(self):
        h = self.height()
        eighth = h // 8

        if self._pos_y_shifting > eighth:
            self.rolling_ani.setStartValue(eighth - self._pos_y_shifting)
            self.rolling_ani.setEndValue(0)
            self.current_time = max(self.min_range, self.current_time - 1)
        elif self._pos_y_shifting > -eighth:
            self.rolling_ani.setStartValue(self._pos_y_shifting)
            self.rolling_ani.setEndValue(0)
        elif self._pos_y_shifting < -eighth:
            self.rolling_ani.setStartValue(-eighth - self._pos_y_shifting)
            self.rolling_ani.setEndValue(0)
            self.current_time = min(self.max_range, self.current_time + 1)

        self.rolling_ani.start()
        self.update()


class DateTimePickerDlg(QDialog):
    """日历 + 时分秒滚轮弹出面板"""

    timeUpdated = Signal(QDateTime)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._cur_datetime = QDateTime.currentDateTime()
        self._selected_date = QDate.currentDate()
        self._init_ui()

    def _init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        self.container = QFrame()
        self.container.setObjectName("DateTimePickerContainer")
        root = QVBoxLayout(self.container)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        self.header_frame = QFrame()
        self.header_frame.setObjectName("DateTimePickerHeader")
        header_layout = QHBoxLayout(self.header_frame)
        header_layout.setContentsMargins(8, 6, 8, 6)
        header_layout.setSpacing(16)

        self.year_label = QLabel()
        self.week_label = QLabel()
        self.month_label = QLabel()
        self.day_label = QLabel()
        for lbl in (self.year_label, self.week_label, self.month_label, self.day_label):
            lbl.setObjectName("DateTimePickerHeaderLabel")
            header_layout.addWidget(lbl)
        header_layout.addStretch()
        root.addWidget(self.header_frame)

        body = QHBoxLayout()
        body.setSpacing(12)

        self.calendar_stack = CompactCalendarStack(self.container)
        self.calendar_stack.itemClicked.connect(self._on_day_clicked)
        body.addWidget(self.calendar_stack, 0, Qt.AlignmentFlag.AlignTop)

        time_frame = QFrame()
        time_frame.setObjectName("DateTimePickerTimeFrame")
        time_layout = QHBoxLayout(time_frame)
        time_layout.setContentsMargins(4, 0, 4, 0)
        time_layout.setSpacing(4)

        self.hour_select = RollingTimeWidget()
        self.minute_select = RollingTimeWidget()
        self.second_select = RollingTimeWidget()
        self.hour_select.setTimeRange(0, 23)
        self.minute_select.setTimeRange(0, 59)
        self.second_select.setTimeRange(0, 59)

        for widget, sep in (
            (self.hour_select, ":"),
            (self.minute_select, ":"),
            (self.second_select, None),
        ):
            time_layout.addWidget(widget)
            if sep:
                sep_lbl = QLabel(sep)
                sep_lbl.setObjectName("DateTimePickerSep")
                time_layout.addWidget(sep_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        body.addWidget(time_frame, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(body)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.cancel_btn = PushButton("取消")
        self.ok_btn = PrimaryPushButton("确定")
        self.cancel_btn.clicked.connect(self.hide)
        self.ok_btn.clicked.connect(self._on_ok_clicked)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.ok_btn)
        root.addLayout(btn_row)

        outer.addWidget(self.container)
        self._apply_theme()

    def _apply_theme(self):
        c = _theme_colors()
        self.container.setStyleSheet(f"""
            QFrame#DateTimePickerContainer {{
                background-color: {c["bg"]};
                border: 1px solid {c["border"]};
                border-radius: 8px;
            }}
            QFrame#DateTimePickerHeader {{
                background-color: {c["card"]};
                border-radius: 6px;
            }}
            QLabel#DateTimePickerHeaderLabel {{
                color: {c["text"]};
                font-weight: bold;
                font-size: 13px;
                background: transparent;
            }}
            QLabel#DateTimePickerSep {{
                color: {c["text"]};
                font-size: 18px;
                font-weight: bold;
                padding: 0 2px;
                background: transparent;
            }}
            QFrame#DateTimePickerTimeFrame {{
                background: transparent;
            }}
        """)

        shadow = QGraphicsDropShadowEffect(self.container)
        shadow.setBlurRadius(15)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 40))
        self.container.setGraphicsEffect(shadow)

        for w in (self.hour_select, self.minute_select, self.second_select):
            w._apply_theme()

    def _get_selected_date(self) -> QDate:
        scroll = self.calendar_stack.day_view.scrollView
        idx = scroll.delegate.selectedIndex
        if idx.isValid():
            item = scroll.item(idx.row())
            if item:
                date = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(date, QDate) and date.isValid():
                    return date
        return self._selected_date

    def setCurDateTime(self, dt: QDateTime):
        if not dt.isValid():
            return
        self._cur_datetime = dt
        self._selected_date = dt.date()
        self.calendar_stack.ensure_anchor(dt.date())
        self.hour_select.setCurrTimeVal(dt.time().hour())
        self.minute_select.setCurrTimeVal(dt.time().minute())
        self.second_select.setCurrTimeVal(dt.time().second())
        self._set_label_time(dt.date())

    def showEvent(self, event):
        super().showEvent(event)
        # 弹窗完成布局后再定位到目标月份，避免首次打开滚动位置错误
        date = self._selected_date
        QTimer.singleShot(0, lambda: self.calendar_stack.ensure_anchor(date))

    def _set_label_time(self, date: QDate):
        week_days = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        self.year_label.setText(str(date.year()))
        self.week_label.setText(week_days[date.dayOfWeek()])
        self.month_label.setText(f"{date.month():02d}月")
        self.day_label.setText(f"{date.day():02d}日")

    def _on_day_clicked(self, date: QDate):
        self._selected_date = date
        self._set_label_time(date)

    def _update_datetime_part(self, change_type: ChangeType, val: int):
        if val < 0:
            return
        current_date = self._cur_datetime.date()
        current_time = self._cur_datetime.time()

        if change_type == ChangeType.YEAR:
            current_date = QDate(val, current_date.month(), current_date.day())
        elif change_type == ChangeType.MONTH:
            current_date = QDate(current_date.year(), val, current_date.day())
        elif change_type == ChangeType.DAY:
            current_date = QDate(current_date.year(), current_date.month(), val)
        elif change_type == ChangeType.HOUR:
            current_time = QTime(val, current_time.minute(), current_time.second())
        elif change_type == ChangeType.MINUTE:
            current_time = QTime(current_time.hour(), val, current_time.second())
        elif change_type == ChangeType.SECOND:
            current_time = QTime(current_time.hour(), current_time.minute(), val)

        self._cur_datetime = QDateTime(current_date, current_time)

    def _on_ok_clicked(self):
        current_date = self._get_selected_date()
        self._cur_datetime.setDate(current_date)
        self._update_datetime_part(ChangeType.HOUR, self.hour_select.getCurrTimeVal())
        self._update_datetime_part(ChangeType.MINUTE, self.minute_select.getCurrTimeVal())
        self._update_datetime_part(ChangeType.SECOND, self.second_select.getCurrTimeVal())
        self.timeUpdated.emit(self._cur_datetime)
        self.hide()

    def dateTime(self) -> QDateTime:
        return self._cur_datetime


class CustomDateTimeEdit(QFrame):
    """只读展示 + 占位提示，点击弹出日期时间面板（不可手动编辑删除）。"""

    btnClicked = Signal()
    clearRequested = Signal()
    PLACEHOLDER_TEXT = "请选择回访时间"
    DISPLAY_FORMAT = "yyyy-MM-dd HH:mm:ss"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CustomDateTimeEdit")
        self._datetime = QDateTime()
        self.setFixedHeight(30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(2)

        self.line_edit = QLineEdit(self)
        self.line_edit.setReadOnly(True)
        self.line_edit.setFrame(False)
        self.line_edit.setPlaceholderText(self.PLACEHOLDER_TEXT)
        self.line_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.line_edit.installEventFilter(self)
        layout.addWidget(self.line_edit, 1)

        self.clear_btn = TransparentToolButton(FluentIcon.CLOSE, self)
        self.clear_btn.setFixedSize(22, 22)
        self.clear_btn.setToolTip("清除回访时间")
        self.clear_btn.installEventFilter(
            ToolTipFilter(self.clear_btn, 300, ToolTipPosition.BOTTOM)
        )
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        self.clear_btn.hide()
        layout.addWidget(self.clear_btn)

        self._calendar_slot = QWidget(self)
        self._calendar_slot.setFixedSize(22, 22)
        self._calendar_slot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._calendar_slot)

        self.setEmpty()
        self._apply_theme()

    def eventFilter(self, obj, event):
        if obj is self.line_edit and event.type() == QEvent.Type.MouseButtonPress:
            self.btnClicked.emit()
            return True
        return super().eventFilter(obj, event)

    def isEmpty(self) -> bool:
        return not self._datetime.isValid()

    def setEmpty(self):
        self._datetime = QDateTime()
        self.line_edit.clear()
        self.clear_btn.hide()
        self._apply_theme()

    def setDateTimeValue(self, dt: QDateTime):
        if dt.isValid():
            self._datetime = dt
            self.line_edit.setText(dt.toString(self.DISPLAY_FORMAT))
            self.clear_btn.show()
        else:
            self.setEmpty()
            return
        self._apply_theme()

    def _on_clear_clicked(self):
        self.clearRequested.emit()

    def dateTime(self) -> QDateTime:
        return self._datetime

    def _apply_theme(self):
        c = _theme_colors()
        text_color = c["text"]
        self.setStyleSheet(f"""
            QFrame#CustomDateTimeEdit {{
                background-color: {c["input_bg"]};
                border: 1px solid {c["border"]};
                border-radius: 5px;
            }}
        """)
        self.line_edit.setStyleSheet(f"""
            background: transparent;
            border: none;
            color: {text_color};
            padding: 0;
        """)
        palette = self.line_edit.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["text_sub"]))
        self.line_edit.setPalette(palette)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.btnClicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def setCustomSize(self, width: int, height: int):
        self.setFixedSize(width, height)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        icon_size = 14
        slot = self._calendar_slot.geometry()
        rect = QRectF(
            slot.x() + (slot.width() - icon_size) / 2,
            slot.y() + (slot.height() - icon_size) / 2,
            icon_size,
            icon_size,
        )
        FluentIcon.CALENDAR.render(painter, rect)


class DateTimePicker(QWidget):
    """一体化日期时间选择器

    弹出面板（含 Fluent 日历）在首次点击时才创建，避免打开详情页时预加载大量日期项。
    """

    datetimeChanged = Signal(QDateTime)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cur_datetime = QDateTime()
        self._picker_dlg: DateTimePickerDlg | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.custom_edit = CustomDateTimeEdit(self)
        self.custom_edit.btnClicked.connect(self._on_edit_clicked)
        self.custom_edit.clearRequested.connect(self._on_clear_requested)

        layout.addWidget(self.custom_edit)

    def clear(self):
        """重置为未选择状态，显示占位提示。"""
        self._cur_datetime = QDateTime()
        self.custom_edit.setEmpty()

    def _on_clear_requested(self):
        self.clear()
        self.datetimeChanged.emit(QDateTime())

    def _ensure_picker_dlg(self) -> DateTimePickerDlg:
        if self._picker_dlg is None:
            self._picker_dlg = DateTimePickerDlg(self)
            self._picker_dlg.timeUpdated.connect(self._on_picker_time_updated)
            self._picker_dlg.installEventFilter(self)
        return self._picker_dlg

    def _on_edit_clicked(self):
        init_dt = (
            self._cur_datetime
            if self._cur_datetime.isValid()
            else QDateTime.currentDateTime()
        )
        self.custom_edit._apply_theme()
        dlg = self._ensure_picker_dlg()
        dlg._apply_theme()
        dlg.setCurDateTime(init_dt)
        global_pos = self.custom_edit.mapToGlobal(self.custom_edit.rect().bottomLeft())
        dlg.move(global_pos.x(), global_pos.y() + 2)
        dlg.show()

    def _on_picker_time_updated(self, dt: QDateTime):
        if not dt.isValid():
            return
        self.custom_edit.setDateTimeValue(dt)
        self._cur_datetime = dt
        self.datetimeChanged.emit(dt)

    def eventFilter(self, watched, event):
        if (
            self._picker_dlg is not None
            and watched is self._picker_dlg
            and event.type() == QEvent.Type.WindowDeactivate
        ):
            self._picker_dlg.hide()
            return True
        return super().eventFilter(watched, event)

    def getDateTime(self) -> QDateTime:
        if self.custom_edit.isEmpty():
            return QDateTime()
        dt = self.custom_edit.dateTime()
        return dt if dt.isValid() else self._cur_datetime

    def setDateTime(self, dt: QDateTime):
        if not dt.isValid():
            self.clear()
            return
        self._cur_datetime = dt
        self.custom_edit.setDateTimeValue(dt)

    datetime = Property(QDateTime, getDateTime, setDateTime)

    def setFixedWidth(self, width: int):
        super().setFixedWidth(width)
        self.custom_edit.setCustomSize(width, self.custom_edit.height())
