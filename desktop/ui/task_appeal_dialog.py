"""任务申诉弹窗（Fluent MessageBoxBase，与拨号选择等弹窗风格一致）。"""
from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import Qt, QDate
from PySide6.QtGui import QBrush, QColor, QFont, QTextCharFormat
from PySide6.QtWidgets import (
    QButtonGroup,
    QCalendarWidget,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    CaptionLabel,
    FlowLayout,
    LineEdit,
    MessageBoxBase,
    SubtitleLabel,
    ToggleButton,
    isDarkTheme,
    themeColor,
)

# ---------------------------------------------------------------------------
# 申诉原因选项：可在此调整文案与补充说明最低字数
# （0 = 选填且不设最低字数；>0 = 必填且不少于该字数）
# ---------------------------------------------------------------------------
SCHEDULE_REASON = "预约时间"
APPEAL_REASON_OPTIONS: list[tuple[str, int]] = [
    (SCHEDULE_REASON, 0),
    ("已采购", 0),
    ("不负责", 0),
    ("同事", 0),
    ("工作人员", 0),
]

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_WORKDAY_COUNT = 10


def _is_cn_workday(d: date) -> bool:
    """中国大陆工作日（周末/法定节假日休息，调休周末上班）。库不可用时退回周一至周五。"""
    try:
        from chinese_calendar import is_workday

        return bool(is_workday(d))
    except Exception:
        return d.weekday() < 5


def _next_workdays(count: int = _WORKDAY_COUNT, *, start: date | None = None) -> list[date]:
    """从 start（默认今天）起取 count 个工作日（含 start，若其本身为工作日）。"""
    cur = start or date.today()
    out: list[date] = []
    for _ in range(max(count * 4, 40)):
        if _is_cn_workday(cur):
            out.append(cur)
            if len(out) >= count:
                break
        cur += timedelta(days=1)
    return out


def _to_qdate(d: date) -> QDate:
    return QDate(d.year, d.month, d.day)


def _from_qdate(qd: QDate) -> date:
    return date(qd.year(), qd.month(), qd.day())


def _format_workday_label(d: date) -> str:
    return f"{d.isoformat()}（{_WEEKDAY_CN[d.weekday()]}）"


class TaskAppealDialog(MessageBoxBase):
    """选择申诉原因 + 可选补充说明；预约时间时展开日历选择工作日。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reason: str | None = None
        self._selected_label: str | None = None
        self._min_len = 0
        self._selected_schedule: date | None = None
        self._workdays = _next_workdays(_WORKDAY_COUNT)
        self._allowed_dates = set(self._workdays)

        self.titleLabel = SubtitleLabel("任务申诉", self)
        hint = CaptionLabel("请选择申诉原因（将用于优化任务分配）：", self)
        hint.setWordWrap(True)

        self._btn_host = QWidget(self)
        self._flow = FlowLayout(self._btn_host, needAni=False)
        self._flow.setContentsMargins(0, 0, 0, 0)
        self._flow.setHorizontalSpacing(8)
        self._flow.setVerticalSpacing(8)

        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)
        self._reason_btns: list[ToggleButton] = []
        for label, min_len in APPEAL_REASON_OPTIONS:
            btn = ToggleButton(label, self._btn_host)
            btn.setCheckable(True)
            btn.setProperty("appeal_label", label)
            btn.setProperty("appeal_min_len", int(min_len))
            self._btn_group.addButton(btn)
            self._flow.addWidget(btn)
            self._reason_btns.append(btn)
        self._btn_group.buttonToggled.connect(self._on_reason_toggled)

        # 预约时间：展开日历
        self._schedule_panel = QWidget(self)
        schedule_layout = QVBoxLayout(self._schedule_panel)
        schedule_layout.setContentsMargins(0, 4, 0, 0)
        schedule_layout.setSpacing(8)
        self._schedule_hint = CaptionLabel(
            "请在日历中选择预约日期（青绿可选，灰色删除线不可选）：",
            self._schedule_panel,
        )
        self._schedule_hint.setWordWrap(True)

        cal_row = QHBoxLayout()
        cal_row.setContentsMargins(0, 0, 0, 0)
        cal_row.addStretch(1)
        self._calendar_frame = QFrame(self._schedule_panel)
        self._calendar_frame.setObjectName("AppealCalendarFrame")
        frame_layout = QVBoxLayout(self._calendar_frame)
        frame_layout.setContentsMargins(6, 6, 6, 6)

        self._calendar = QCalendarWidget(self._calendar_frame)
        self._calendar.setGridVisible(False)
        self._calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self._calendar.setHorizontalHeaderFormat(QCalendarWidget.ShortDayNames)
        self._calendar.setNavigationBarVisible(True)
        self._calendar.setFirstDayOfWeek(Qt.Monday)
        self._calendar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # 尺寸由 _fit_to_parent_width 按主窗口宽度计算
        self._calendar.setFixedSize(260, 220)
        self._apply_calendar_style()
        self._configure_calendar_range()
        self._calendar.clicked.connect(self._on_calendar_clicked)
        frame_layout.addWidget(self._calendar)

        shadow = QGraphicsDropShadowEffect(self._calendar_frame)
        shadow.setBlurRadius(12)
        shadow.setXOffset(0)
        shadow.setYOffset(3)
        shadow.setColor(QColor(0, 0, 0, 28))
        self._calendar_frame.setGraphicsEffect(shadow)

        cal_row.addWidget(self._calendar_frame)
        cal_row.addStretch(1)

        self._selected_date_lbl = CaptionLabel("未选择日期", self._schedule_panel)
        self._selected_date_lbl.setAlignment(Qt.AlignCenter)
        self._update_selected_date_label()

        schedule_layout.addWidget(self._schedule_hint)
        schedule_layout.addLayout(cal_row)
        schedule_layout.addWidget(self._selected_date_lbl)
        self._schedule_panel.setVisible(False)

        self._detail_hint = CaptionLabel("", self)
        self._detail_edit = LineEdit(self)
        self._detail_edit.setClearButtonEnabled(True)
        self._detail_edit.setPlaceholderText("请先选择申诉原因")
        self._detail_edit.setEnabled(False)
        self._detail_edit.textChanged.connect(self._on_detail_changed)

        counter_row = QHBoxLayout()
        counter_row.setContentsMargins(0, 0, 0, 0)
        counter_row.addStretch(1)
        self._counter_lbl = CaptionLabel("", self)
        counter_row.addWidget(self._counter_lbl)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(hint)
        self.viewLayout.addWidget(self._btn_host)
        self.viewLayout.addWidget(self._schedule_panel)
        self.viewLayout.addWidget(self._detail_hint)
        self.viewLayout.addWidget(self._detail_edit)
        self.viewLayout.addLayout(counter_row)

        self.yesButton.setText("提交申诉")
        self.cancelButton.setText("取消")
        # MessageBoxBase 默认左右各 24，略收紧以适配默认主窗口 430 宽
        self.viewLayout.setContentsMargins(16, 20, 16, 16)
        self.buttonLayout.setContentsMargins(16, 16, 16, 16)
        self._fit_to_parent_width()
        self._sync_detail_ui()
        self.widget.adjustSize()

    def _host_window_width(self) -> int:
        """取宿主窗口可用宽度（默认主窗口约 430）。"""
        host = self.parent()
        if host is not None:
            win = host.window() if hasattr(host, "window") else host
            try:
                w = int(win.width()) if win is not None else 0
                if w > 0:
                    return w
            except Exception:
                pass
        return 430

    def _fit_to_parent_width(self):
        """弹窗宽度不超过主窗口，并等比缩小日历。"""
        host_w = self._host_window_width()
        # 左右各留一点空隙，避免贴边/溢出
        dialog_w = max(320, min(390, host_w - 40))
        self.widget.setMinimumWidth(dialog_w)
        self.widget.setMaximumWidth(dialog_w)

        # 内容区 ≈ dialog_w - viewLayout 左右边距；日历框再扣内边距
        content_w = dialog_w - 32
        cal_w = max(220, min(276, content_w - 24))
        cal_h = max(190, int(cal_w * 0.82))
        self._calendar.setFixedSize(cal_w, cal_h)

    def _calendar_palette(self) -> dict[str, str]:
        dark = isDarkTheme()
        accent = themeColor().name()
        return {
            "bg": "#2b2b2b" if dark else "#ffffff",
            "border": "#3f3f3f" if dark else "#e5e5e5",
            "text": "#f2f2f2" if dark else "#1f1f1f",
            "muted": "#6e6e6e" if dark else "#b8b8b8",
            "hover": "#3a3a3a" if dark else "#f3f3f3",
            "accent": accent,
            # 可选工作日：青绿底 + 深色字
            "available_fg": "#7dffa6" if dark else "#0f7a3a",
            "available_bg": "#1f3d2c" if dark else "#e6f7ed",
            # 不可选：淡灰底 + 更淡的字 + 删除线
            "disabled_fg": "#666666" if dark else "#c2c2c2",
            "disabled_bg": "#333333" if dark else "#f4f4f4",
            # 已选：主题色实心底 + 白字
            "selected_fg": "#ffffff",
            "selected_bg": accent,
            "label_ok": accent,
            "label_empty": "#9c9c9c" if dark else "#8c8c8c",
        }

    def _make_day_format(self, *, fg: str, bg: str, bold: bool = False, strike: bool = False) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QBrush(QColor(fg)))
        fmt.setBackground(QBrush(QColor(bg)))
        font = QFont()
        font.setBold(bold)
        font.setStrikeOut(strike)
        fmt.setFont(font)
        return fmt

    def _apply_calendar_style(self):
        p = self._calendar_palette()
        # 尚未确认选择时，避免原生选中高亮看起来像已选中
        if self._selected_schedule is None:
            sel_bg = p["available_bg"]
            sel_fg = p["available_fg"]
        else:
            sel_bg = p["selected_bg"]
            sel_fg = p["selected_fg"]
        self._calendar_frame.setStyleSheet(
            f"""
            QFrame#AppealCalendarFrame {{
                background-color: {p["bg"]};
                border: 1px solid {p["border"]};
                border-radius: 10px;
            }}
            """
        )
        self._calendar.setStyleSheet(
            f"""
            QCalendarWidget {{
                background: {p["bg"]};
                color: {p["text"]};
                border: none;
            }}
            QCalendarWidget QWidget#qt_calendar_navigationbar {{
                background: {p["bg"]};
                border: none;
            }}
            QCalendarWidget QToolButton {{
                color: {p["text"]};
                background: transparent;
                border: none;
                border-radius: 6px;
                padding: 4px 8px;
                margin: 2px;
                font-weight: 600;
            }}
            QCalendarWidget QToolButton:hover {{
                background: {p["hover"]};
            }}
            QCalendarWidget QToolButton::menu-indicator {{
                image: none;
            }}
            QCalendarWidget QSpinBox {{
                color: {p["text"]};
                background: {p["hover"]};
                border: none;
                border-radius: 4px;
                padding: 2px 6px;
            }}
            QCalendarWidget QAbstractItemView {{
                background: {p["bg"]};
                color: {p["text"]};
                selection-background-color: {sel_bg};
                selection-color: {sel_fg};
                outline: none;
                border: none;
            }}
            QCalendarWidget QAbstractItemView:enabled {{
                color: {p["text"]};
            }}
            QCalendarWidget QAbstractItemView:disabled {{
                color: {p["disabled_fg"]};
            }}
            """
        )

    def _apply_day_formats(self):
        """刷新可选 / 不可选 / 已选三种日期配色。"""
        if not self._workdays:
            return
        self._apply_calendar_style()
        p = self._calendar_palette()
        available_fmt = self._make_day_format(
            fg=p["available_fg"], bg=p["available_bg"], bold=True
        )
        disabled_fmt = self._make_day_format(
            fg=p["disabled_fg"], bg=p["disabled_bg"], strike=True
        )
        selected_fmt = self._make_day_format(
            fg=p["selected_fg"], bg=p["selected_bg"], bold=True
        )

        min_d = self._workdays[0]
        max_d = self._workdays[-1]
        cur = min_d
        while cur <= max_d:
            qd = _to_qdate(cur)
            if self._selected_schedule is not None and cur == self._selected_schedule:
                self._calendar.setDateTextFormat(qd, selected_fmt)
            elif cur in self._allowed_dates:
                self._calendar.setDateTextFormat(qd, available_fmt)
            else:
                self._calendar.setDateTextFormat(qd, disabled_fmt)
            cur += timedelta(days=1)

    def _configure_calendar_range(self):
        if not self._workdays:
            return
        min_d = self._workdays[0]
        max_d = self._workdays[-1]
        self._calendar.setMinimumDate(_to_qdate(min_d))
        self._calendar.setMaximumDate(_to_qdate(max_d))
        self._calendar.setCurrentPage(min_d.year, min_d.month)
        self._apply_day_formats()

        # 初始不视为已选：落到范围起点但不计入提交
        self._calendar.blockSignals(True)
        self._calendar.setSelectedDate(_to_qdate(min_d))
        self._calendar.blockSignals(False)

    def _reset_schedule_hint(self):
        self._schedule_hint.setText(
            "请在日历中选择预约日期（青绿可选，灰色删除线不可选）："
        )

    def _update_selected_date_label(self):
        p = self._calendar_palette()
        if self._selected_schedule is None:
            self._selected_date_lbl.setText("未选择日期")
            self._selected_date_lbl.setStyleSheet(f"color: {p['label_empty']};")
            return
        self._selected_date_lbl.setText(
            f"已选：{_format_workday_label(self._selected_schedule)}"
        )
        self._selected_date_lbl.setStyleSheet(
            f"color: {p['label_ok']}; font-weight: 600;"
        )

    def _clear_schedule_selection(self):
        self._selected_schedule = None
        self._update_selected_date_label()
        if self._workdays:
            self._apply_day_formats()
            self._calendar.blockSignals(True)
            self._calendar.setSelectedDate(_to_qdate(self._workdays[0]))
            self._calendar.setCurrentPage(
                self._workdays[0].year, self._workdays[0].month
            )
            self._calendar.blockSignals(False)
        self._reset_schedule_hint()

    def _set_schedule_panel_visible(self, visible: bool):
        if self._schedule_panel.isVisible() == visible:
            return
        self._schedule_panel.setVisible(visible)
        self._clear_schedule_selection()
        self._fit_to_parent_width()
        self.widget.adjustSize()

    def _on_reason_toggled(self, button, checked: bool):
        if not checked:
            if self._btn_group.checkedButton() is None:
                self._selected_label = None
                self._min_len = 0
                self._set_schedule_panel_visible(False)
                self._sync_detail_ui()
            return
        self._selected_label = str(button.property("appeal_label") or button.text())
        try:
            self._min_len = int(button.property("appeal_min_len") or 0)
        except (TypeError, ValueError):
            self._min_len = 0
        self._detail_edit.blockSignals(True)
        self._detail_edit.clear()
        self._detail_edit.blockSignals(False)
        self._set_schedule_panel_visible(self._selected_label == SCHEDULE_REASON)
        self._sync_detail_ui()

    def _on_calendar_clicked(self, qdate: QDate):
        if not qdate.isValid():
            return
        d = _from_qdate(qdate)
        if d not in self._allowed_dates:
            self._schedule_hint.setText(
                "该日不可选（灰色删除线），请点击青绿底的工作日"
            )
            # 回退到已选工作日或范围起点
            fallback = self._selected_schedule or (
                self._workdays[0] if self._workdays else None
            )
            if fallback is not None:
                self._calendar.blockSignals(True)
                self._calendar.setSelectedDate(_to_qdate(fallback))
                self._calendar.blockSignals(False)
            self._apply_day_formats()
            return

        self._selected_schedule = d
        self._apply_day_formats()
        self._update_selected_date_label()
        self._reset_schedule_hint()

    def _sync_detail_ui(self):
        has_reason = self._selected_label is not None
        self._detail_edit.setEnabled(has_reason)
        if not has_reason:
            self._detail_edit.setPlaceholderText("请先选择申诉原因")
            self._detail_hint.setText("")
            self._counter_lbl.setText("")
            return

        if self._selected_label == SCHEDULE_REASON:
            self._detail_edit.setPlaceholderText("选填，可补充预约说明")
            self._detail_hint.setText("补充说明：（选填）")
        elif self._min_len > 0:
            self._detail_edit.setPlaceholderText(f"必填，至少 {self._min_len} 字")
            self._detail_hint.setText(f"补充说明：（必填，至少 {self._min_len} 字）")
        else:
            self._detail_edit.setPlaceholderText("选填，不限字数")
            self._detail_hint.setText("补充说明：（选填）")
        self._on_detail_changed(self._detail_edit.text())

    def _on_detail_changed(self, text: str):
        if self._selected_label is None:
            self._counter_lbl.setText("")
            return
        n = len((text or "").strip())
        if self._min_len > 0:
            self._counter_lbl.setText(f"{n}/{self._min_len}")
        else:
            self._counter_lbl.setText(f"{n} 字" if n else "")

    def validate(self) -> bool:
        if not self._selected_label:
            self._detail_hint.setText("请先选择一个申诉原因")
            return False

        detail = (self._detail_edit.text() or "").strip()
        if self._selected_label == SCHEDULE_REASON:
            if self._selected_schedule is None:
                self._schedule_hint.setText("请先在日历中选择一个预约工作日")
                return False
            date_text = self._selected_schedule.isoformat()
            self._reason = (
                f"{SCHEDULE_REASON}：{date_text}；{detail}"
                if detail
                else f"{SCHEDULE_REASON}：{date_text}"
            )
            return True

        if self._min_len > 0 and len(detail) < self._min_len:
            self._detail_hint.setText(
                f"补充说明至少 {self._min_len} 字（当前 {len(detail)} 字）"
            )
            self._detail_edit.setFocus()
            return False
        self._reason = (
            f"{self._selected_label}：{detail}" if detail else self._selected_label
        )
        return bool(self._reason)

    def reason(self) -> str | None:
        return self._reason


def ask_task_appeal(parent: QWidget | None) -> str | None:
    """弹出申诉对话框；确认则返回申诉原因字符串，取消则返回 None。"""
    host = parent.window() if parent is not None else parent
    dlg = TaskAppealDialog(host)
    if not dlg.exec():
        return None
    reason = (dlg.reason() or "").strip()
    return reason or None
