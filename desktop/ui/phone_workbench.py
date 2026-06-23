"""
右侧抽屉：电话工作台。
展示紧凑客户资料、任务/客户画像块、完整话术块；底部畅呼/云客外呼按钮。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.app_fonts import label_qss, style_label, text_palette
from ui.confirm_dialog import ask_confirm
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
    TransparentPushButton,
    FluentIcon,
    ToolTipFilter,
    ToolTipPosition,
    isDarkTheme,
)

from utils import mask_phone, resolve_display_phone
from phone_script_store import customer_script_key, get_phone_script_store, is_persistable_script

SCRIPT_PLACEHOLDER = "暂无已生成话术，点击下方「生成话术」可生成完整口播稿。"

TASK_KIND_LABELS: dict[str, str] = {
    "contact": "联系",
    "follow_up": "跟进",
    "close_deal": "促单",
    "revisit": "回访",
    "icebreaker": "激活",
}

# 双列字段（不含微信备注，备注独占首行）
_CUSTOMER_DETAIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("行政区划", "admin_division"),
    ("采购类型", "purchase_type"),
    ("采货月份", "purchase_months"),
    ("历史订单", "_orders_summary"),
    ("采购预算", "budget_amount"),
)


def _fmt_due_date(due) -> str:
    if not due:
        return ""
    return str(due)[:10]


def _display_text(value) -> str:
    if value is None:
        return "—"
    s = str(value).strip()
    return s if s else "—"


def _normalize_block_text(text: str) -> str:
    """去掉首尾空白及正文前导空行，避免标题与首行之间出现大块空白。"""
    s = (text or "").strip()
    while s.startswith("\n"):
        s = s[1:].lstrip("\r\t ")
    return s


def _fmt_budget(value) -> str:
    if value is None:
        return "—"
    try:
        amt = Decimal(str(value))
        return f"¥{amt:,.2f}"
    except (InvalidOperation, ValueError):
        s = str(value).strip()
        return s if s else "—"


def _fmt_orders_summary(customer: dict) -> str:
    try:
        cnt = int(customer.get("historical_order_count") or 0)
    except (TypeError, ValueError):
        cnt = 0
    amt_raw = customer.get("historical_amount")
    try:
        amt = Decimal(str(amt_raw if amt_raw is not None else 0))
        amt_s = f"¥{amt:,.2f}"
    except (InvalidOperation, ValueError):
        amt_s = _display_text(amt_raw)
    if cnt <= 0 and amt_s == "—":
        return "—"
    if cnt <= 0:
        return amt_s
    return f"{cnt}笔·{amt_s}"


def _field_value(customer: dict, field_key: str) -> str:
    if field_key == "_orders_summary":
        return _fmt_orders_summary(customer)
    if field_key == "budget_amount":
        return _fmt_budget(customer.get("budget_amount"))
    return _display_text(customer.get(field_key))


def _is_phone_allocation_task(task: dict | None) -> bool:
    if not isinstance(task, dict):
        return False
    return (task.get("contact_channel") or "").strip() == "phone"


# 画像/任务提示区最高高度，超出后区内滚动，避免挤占完整话术
_HINT_SCROLL_MAX_HEIGHT = 140
_HINT_BODY_FONT_SIZE = 10
_MIN_CONTENT_SYNC_WIDTH = 48
_LAYOUT_SYNC_MAX_ATTEMPTS = 10
_SCRIPT_FONT_MIN = 8
_SCRIPT_FONT_MAX = 24
_SCRIPT_FONT_DEFAULT = 11

# 外呼临时关闭：为 True 时底部仅显示「完成任务」，不调用畅呼/云客
PHONE_OUTBOUND_DISABLED = False


class PhoneWorkbenchWidget(QWidget):
    """电话工作台：紧凑客户资料 + 可伸缩话术区 + 底部畅呼/云客外呼。"""

    generate_script_requested = Signal()
    changhu_call_clicked = Signal(str)
    yunke_call_clicked = Signal()
    complete_task_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._customer: dict | None = None
        self._task: dict | None = None
        self._phone_raw: str = ""
        self._detail_value_labels: dict[str, CaptionLabel] = {}
        self._detail_key_labels: list[CaptionLabel] = []
        self._script_font_size = _SCRIPT_FONT_DEFAULT
        self._layout_sync_attempts = 0
        self._generating_for_key: str | None = None
        self._script_store = get_phone_script_store()
        self._layout_sync_timer = QTimer(self)
        self._layout_sync_timer.setSingleShot(True)
        self._layout_sync_timer.setInterval(50)
        self._layout_sync_timer.timeout.connect(self._deferred_content_layout_sync)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._placeholder = SubtitleLabel("请从左侧选择客户，或从任务分配进入电话主线任务。")
        self._placeholder.setWordWrap(True)
        self._placeholder.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._placeholder.setContentsMargins(12, 12, 12, 12)
        root.addWidget(self._placeholder, 1)

        self._work = QWidget()
        self._work.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        work = QVBoxLayout(self._work)
        work.setContentsMargins(8, 6, 8, 0)
        work.setSpacing(6)

        # ── 顶部：微信备注独占首行 + 双列其余字段 ──
        self._card_customer = self._make_card()
        cc = QVBoxLayout(self._card_customer)
        cc.setContentsMargins(8, 6, 8, 6)
        cc.setSpacing(4)

        self.lbl_wechat_remark_key = CaptionLabel("微信备注")
        self.lbl_wechat_remark = BodyLabel("—")
        self.lbl_wechat_remark.setWordWrap(True)
        self.lbl_wechat_remark.setTextInteractionFlags(Qt.TextSelectableByMouse)
        cc.addWidget(self.lbl_wechat_remark_key)
        cc.addWidget(self.lbl_wechat_remark)

        detail_grid = QGridLayout()
        detail_grid.setContentsMargins(0, 4, 0, 0)
        detail_grid.setHorizontalSpacing(6)
        detail_grid.setVerticalSpacing(2)
        for col in range(4):
            detail_grid.setColumnStretch(col, 1 if col % 2 == 1 else 0)

        for idx, (label_text, field_key) in enumerate(_CUSTOMER_DETAIL_FIELDS):
            block_row = idx // 2
            block_col = (idx % 2) * 2
            key_lbl = CaptionLabel(label_text)
            key_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            val_lbl = CaptionLabel("—")
            val_lbl.setWordWrap(True)
            val_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            detail_grid.addWidget(key_lbl, block_row, block_col)
            detail_grid.addWidget(val_lbl, block_row, block_col + 1)
            self._detail_key_labels.append(key_lbl)
            self._detail_value_labels[field_key] = val_lbl

        cc.addLayout(detail_grid)
        self._card_customer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        work.addWidget(self._card_customer, 0)

        # ── 任务条（仅任务分配·电话主线） ──
        self._card_task = self._make_card()
        tc = QVBoxLayout(self._card_task)
        tc.setContentsMargins(8, 5, 8, 5)
        tc.setSpacing(2)
        self.lbl_task_title = CaptionLabel("")
        self.lbl_task_title.setWordWrap(True)
        self.lbl_task_meta = CaptionLabel("")
        self.lbl_task_meta.setWordWrap(True)
        tc.addWidget(self.lbl_task_title)
        tc.addWidget(self.lbl_task_meta)
        self._card_task.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        work.addWidget(self._card_task, 0)
        self._card_task.setVisible(False)

        # ── 块1：客户画像 / 任务提示（细框，高度随正文）──
        self._card_hint = self._make_card(subtle=True)
        self._card_hint.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        hc = QVBoxLayout(self._card_hint)
        hc.setContentsMargins(8, 5, 8, 5)
        hc.setSpacing(2)

        self.lbl_task_hint = CaptionLabel("任务提示")
        hc.addWidget(self.lbl_task_hint, 0)

        self.txt_task_instruction = BodyLabel("")
        self.txt_task_instruction.setWordWrap(True)
        self.txt_task_instruction.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.txt_task_instruction.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.txt_task_instruction.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self._hint_body = QWidget()
        self._hint_body.setObjectName("PhoneWorkbenchHintBody")
        hint_body_layout = QVBoxLayout(self._hint_body)
        hint_body_layout.setContentsMargins(0, 0, 0, 0)
        hint_body_layout.setSpacing(0)
        hint_body_layout.addWidget(self.txt_task_instruction)

        self._hint_scroll = QScrollArea()
        self._hint_scroll.setObjectName("PhoneWorkbenchHintScroll")
        self._hint_scroll.setWidget(self._hint_body)
        self._hint_scroll.setWidgetResizable(False)
        self._hint_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._hint_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._hint_scroll.setFrameShape(QFrame.NoFrame)
        self._hint_scroll.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._hint_scroll.verticalScrollBar().setSingleStep(18)
        hc.addWidget(self._hint_scroll, 0)
        work.addWidget(self._card_hint, 0)

        # ── 块2：完整话术（细框，占满中部剩余区域）──
        self._card_script = self._make_card(subtle=True)
        self._card_script.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        sc = QVBoxLayout(self._card_script)
        sc.setContentsMargins(8, 5, 8, 5)
        sc.setSpacing(4)

        self.lbl_generated_header = CaptionLabel("完整话术")
        sc.addWidget(self.lbl_generated_header, 0)

        self.txt_generated_script = BodyLabel(SCRIPT_PLACEHOLDER)
        self.txt_generated_script.setWordWrap(True)
        self.txt_generated_script.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.txt_generated_script.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.txt_generated_script.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self._script_inner = QWidget()
        self._script_inner.setObjectName("PhoneWorkbenchScriptBody")
        script_inner_layout = QVBoxLayout(self._script_inner)
        script_inner_layout.setContentsMargins(0, 0, 0, 0)
        script_inner_layout.setSpacing(0)
        script_inner_layout.addWidget(self.txt_generated_script, 0)

        self._script_scroll = QScrollArea()
        self._script_scroll.setObjectName("PhoneWorkbenchScriptScroll")
        self._script_scroll.setWidget(self._script_inner)
        self._script_scroll.setWidgetResizable(False)
        self._script_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._script_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._script_scroll.setFrameShape(QFrame.NoFrame)
        self._script_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._script_scroll.verticalScrollBar().setSingleStep(20)
        sc.addWidget(self._script_scroll, 1)

        self._script_footer = QFrame()
        self._script_footer.setObjectName("PhoneWorkbenchScriptFooter")
        sf = QVBoxLayout(self._script_footer)
        sf.setContentsMargins(0, 4, 0, 0)
        sf.setSpacing(6)

        font_row = QHBoxLayout()
        font_row.setContentsMargins(0, 0, 0, 0)
        font_row.setSpacing(4)
        self.lbl_font_caption = CaptionLabel("字号")
        self.btn_font_decrease = TransparentPushButton("−")
        self.btn_font_decrease.setFixedSize(30, 26)
        self._attach_fluent_tooltip(self.btn_font_decrease, "缩小话术字号")
        self.btn_font_decrease.clicked.connect(self._decrease_script_font)
        self.lbl_font_size = CaptionLabel(str(self._script_font_size))
        self.lbl_font_size.setAlignment(Qt.AlignCenter)
        self.lbl_font_size.setFixedWidth(24)
        self.btn_font_increase = TransparentPushButton("+")
        self.btn_font_increase.setFixedSize(30, 26)
        self._attach_fluent_tooltip(self.btn_font_increase, "放大话术字号")
        self.btn_font_increase.clicked.connect(self._increase_script_font)
        font_row.addWidget(self.lbl_font_caption, 0)
        font_row.addWidget(self.btn_font_decrease, 0)
        font_row.addWidget(self.lbl_font_size, 0)
        font_row.addWidget(self.btn_font_increase, 0)
        font_row.addStretch(1)

        _robot = FluentIcon.ROBOT if hasattr(FluentIcon, "ROBOT") else FluentIcon.APPLICATION
        self.btn_generate_script = TransparentPushButton(_robot, "生成话术")
        self.btn_generate_script.setEnabled(False)
        self._attach_fluent_tooltip(
            self.btn_generate_script,
            "参考「首通电话不同场景话术」生成口播稿；不写入微信对话记录。",
        )
        self.btn_generate_script.setFixedHeight(28)
        self.btn_generate_script.clicked.connect(self.generate_script_requested.emit)
        font_row.addWidget(self.btn_generate_script, 0, Qt.AlignRight)

        sf.addLayout(font_row)
        sc.addWidget(self._script_footer, 0)

        work.addWidget(self._card_script, 1)
        work.setStretchFactor(self._card_script, 1)

        self._work.setVisible(False)
        root.addWidget(self._work, 1)

        # ── 底部外呼 ──
        self._footer = QFrame()
        self._footer.setObjectName("PhoneWorkbenchFooter")
        footer_layout = QVBoxLayout(self._footer)
        footer_layout.setContentsMargins(10, 6, 10, 10)
        footer_layout.setSpacing(6)

        call_row = QHBoxLayout()
        call_row.setSpacing(8)
        self.btn_changhu_call = PushButton("畅呼外呼")
        self.btn_changhu_call.setFixedHeight(36)
        self._attach_fluent_tooltip(
            self.btn_changhu_call,
            "通过畅呼系统拨打外呼电话",
            position=ToolTipPosition.TOP,
        )
        self.btn_changhu_call.clicked.connect(self._on_changhu_call_clicked)
        self.btn_yunke_call = PrimaryPushButton(FluentIcon.PHONE, "云客外呼")
        self.btn_yunke_call.setFixedHeight(36)
        self._attach_fluent_tooltip(
            self.btn_yunke_call,
            "通过云客系统拨打外呼电话",
            position=ToolTipPosition.TOP,
        )
        self.btn_yunke_call.clicked.connect(self._on_yunke_call_clicked)
        self.btn_complete_task = PrimaryPushButton(FluentIcon.ACCEPT, "完成任务")
        self.btn_complete_task.setFixedHeight(36)
        self._attach_fluent_tooltip(
            self.btn_complete_task,
            "外呼功能临时关闭，点击将当前电话任务标记为已完成",
            position=ToolTipPosition.TOP,
        )
        self.btn_complete_task.clicked.connect(self._on_complete_task_clicked)
        call_row.addWidget(self.btn_changhu_call, 1)
        call_row.addWidget(self.btn_yunke_call, 1)
        call_row.addWidget(self.btn_complete_task, 1)
        footer_layout.addLayout(call_row)

        root.addWidget(self._footer, 0)
        self._apply_outbound_mode()

        self.setStyleSheet("background: transparent;")
        self._work.setStyleSheet("background: transparent;")

        self._apply_script_font_size()
        self.clear()

    @property
    def current_task(self) -> dict | None:
        return self._task

    @property
    def current_customer(self) -> dict | None:
        return self._customer

    def dial_phone(self) -> str:
        return (self._phone_raw or "").strip()

    def customer_sales_wechat_id(self) -> str:
        if not isinstance(self._customer, dict):
            return ""
        return str(self._customer.get("sales_wechat_id") or "").strip()

    def _task_actionable(self) -> bool:
        if not _is_phone_allocation_task(self._task):
            return False
        status = (self._task.get("status") or "pending").strip()
        return status in ("pending", "in_progress", "overdue")

    def patch_task_status(self, status: str):
        if not isinstance(self._task, dict):
            return
        self._task = dict(self._task)
        self._task["status"] = (status or "").strip()
        if PHONE_OUTBOUND_DISABLED:
            self._refresh_complete_task_button()

    @staticmethod
    def _attach_fluent_tooltip(
        widget: QWidget,
        text: str,
        *,
        position: ToolTipPosition = ToolTipPosition.TOP,
        show_delay: int = 300,
    ):
        """使用 Fluent 主题 ToolTip，避免原生 tooltip 在抽屉内显示为黑块。"""
        widget.setToolTip(text)
        widget.installEventFilter(
            ToolTipFilter(widget, showDelay=show_delay, position=position)
        )

    @staticmethod
    def _make_card(*, subtle: bool = False) -> QFrame:
        if subtle:
            name = "PhoneWorkbenchCardSubtle"
        else:
            name = "PhoneWorkbenchCard"
        card = QFrame()
        card.setObjectName(name)
        return card

    @staticmethod
    def _wrapped_label_height(label: QLabel, width: int) -> int:
        """qfluent BodyLabel 无 setHeightForWidth，用 QTextDocument 估算换行高度。"""
        if width <= 0:
            return label.sizeHint().height()
        doc = QTextDocument()
        doc.setDocumentMargin(0)
        doc.setDefaultFont(label.font())
        doc.setPlainText(label.text() or " ")
        doc.setTextWidth(float(width))
        line_h = label.fontMetrics().height()
        return max(int(doc.size().height()) + 2, line_h + 2)

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_content_layout_sync()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._layout_sync_timer.stop()
        self._layout_sync_attempts = 0
        self._release_content_width_constraints()

    def refresh_layout(self):
        """抽屉切到电话工作台后调用，确保 viewport 已有有效宽度。"""
        self._layout_sync_timer.stop()
        self._layout_sync_attempts = 0
        if self._can_sync_content_layout():
            self._sync_hint_scroll_height()
            self._sync_script_inner_height()
        else:
            self._schedule_content_layout_sync()

    def refresh_profile_section(self) -> None:
        """画像全文按需拉取后刷新任务提示/客户画像区（与 _customer 同一 dict 引用）。"""
        if not self._customer:
            return
        self._apply_script_section()

    def _schedule_content_layout_sync(self):
        """有限次延迟重试；避免在不可见时无限创建 QTimer 导致句柄耗尽。"""
        if not self._work.isVisible():
            return
        if self._layout_sync_timer.isActive():
            return
        self._layout_sync_attempts = 0
        self._layout_sync_timer.start()

    def _deferred_content_layout_sync(self):
        if not self._work.isVisible():
            return
        if self._can_sync_content_layout():
            self._layout_sync_attempts = 0
            self._sync_hint_scroll_height()
            self._sync_script_inner_height()
            return
        self._layout_sync_attempts += 1
        if self._layout_sync_attempts < _LAYOUT_SYNC_MAX_ATTEMPTS:
            self._layout_sync_timer.start()
            return
        self._layout_sync_attempts = 0
        self._release_content_width_constraints()

    @staticmethod
    def _release_widget_width(widget: QWidget):
        widget.setMinimumWidth(0)
        widget.setMaximumWidth(16777215)

    def _release_content_width_constraints(self):
        for widget in (
            self._hint_body,
            self._script_inner,
            self.txt_task_instruction,
            self.txt_generated_script,
        ):
            self._release_widget_width(widget)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_content_layout()

    def _scroll_content_width(self, scroll: QScrollArea, fallback_widget: QWidget) -> int:
        viewport_w = scroll.viewport().width()
        if viewport_w >= _MIN_CONTENT_SYNC_WIDTH:
            return viewport_w

        scroll_w = scroll.width()
        if scroll_w >= _MIN_CONTENT_SYNC_WIDTH:
            scrollbar_w = (
                scroll.verticalScrollBar().width()
                if scroll.verticalScrollBar().isVisible()
                else 0
            )
            return max(scroll_w - scrollbar_w - 2, _MIN_CONTENT_SYNC_WIDTH)

        layout = fallback_widget.layout()
        if layout is not None:
            margins = layout.contentsMargins()
            card_w = fallback_widget.width() - margins.left() - margins.right()
            if card_w >= _MIN_CONTENT_SYNC_WIDTH:
                return card_w

        return 0

    def _can_sync_content_layout(self) -> bool:
        if not self._work.isVisible():
            return False
        if not self.isVisible():
            return False
        hint_w = self._scroll_content_width(self._hint_scroll, self._card_hint)
        script_w = self._scroll_content_width(self._script_scroll, self._card_script)
        return hint_w >= _MIN_CONTENT_SYNC_WIDTH and script_w >= _MIN_CONTENT_SYNC_WIDTH

    def _sync_hint_scroll_height(self):
        """画像/任务提示：仅正文区随换行高度变化，过长时封顶并滚动。"""
        inner_w = self._scroll_content_width(self._hint_scroll, self._card_hint)
        if inner_w < _MIN_CONTENT_SYNC_WIDTH:
            return
        body_h = self._wrapped_label_height(self.txt_task_instruction, inner_w)
        self.txt_task_instruction.setFixedWidth(inner_w)
        self.txt_task_instruction.setFixedHeight(body_h)
        self._hint_body.setFixedWidth(inner_w)
        self._hint_body.setFixedHeight(body_h)
        capped = min(body_h, _HINT_SCROLL_MAX_HEIGHT)
        self._hint_scroll.setFixedHeight(max(capped, 1))
        need_scroll = body_h > _HINT_SCROLL_MAX_HEIGHT
        self._hint_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if need_scroll else Qt.ScrollBarAlwaysOff
        )

    def _sync_script_inner_height(self):
        """完整话术：内层高度仅随正文；QScrollArea 本身占满剩余区域。"""
        inner_w = self._scroll_content_width(self._script_scroll, self._card_script)
        if inner_w < _MIN_CONTENT_SYNC_WIDTH:
            return
        body_h = self._wrapped_label_height(self.txt_generated_script, inner_w)
        self.txt_generated_script.setFixedWidth(inner_w)
        self.txt_generated_script.setFixedHeight(body_h)
        self._script_inner.setFixedWidth(inner_w)
        self._script_inner.setFixedHeight(body_h)

    def _section_title_labels(self) -> tuple[CaptionLabel, ...]:
        return (
            self.lbl_wechat_remark_key,
            self.lbl_task_hint,
            self.lbl_generated_header,
            *self._detail_key_labels,
        )

    def _decrease_script_font(self):
        if self._script_font_size <= _SCRIPT_FONT_MIN:
            return
        self._script_font_size -= 1
        self._apply_script_font_size()

    def _increase_script_font(self):
        if self._script_font_size >= _SCRIPT_FONT_MAX:
            return
        self._script_font_size += 1
        self._apply_script_font_size()

    def _apply_body_fonts(self):
        """字号仅通过 QFont 控制，避免主题切换后 stylesheet 与 fixedHeight 不一致。"""
        hint_font = self.txt_task_instruction.font()
        hint_font.setPointSize(_HINT_BODY_FONT_SIZE)
        self.txt_task_instruction.setFont(hint_font)

        script_font = self.txt_generated_script.font()
        script_font.setPointSize(self._script_font_size)
        self.txt_generated_script.setFont(script_font)

    def _sync_content_layout(self):
        """仅在 viewport 宽度有效时同步；失败时不触发定时器（由 showEvent / refresh 负责重试）。"""
        if not self._work.isVisible():
            return
        if not self._can_sync_content_layout():
            return
        self._sync_hint_scroll_height()
        self._sync_script_inner_height()

    def _apply_script_font_size(self):
        self._apply_body_fonts()
        self.lbl_font_size.setText(str(self._script_font_size))
        self.btn_font_decrease.setEnabled(self._script_font_size > _SCRIPT_FONT_MIN)
        self.btn_font_increase.setEnabled(self._script_font_size < _SCRIPT_FONT_MAX)
        self._apply_theme()
        self._sync_script_inner_height()

    @staticmethod
    def _clear_container_background(widget: QWidget):
        widget.setAutoFillBackground(False)
        widget.setStyleSheet("background: transparent; background-color: transparent; border: none;")

    @staticmethod
    def _scroll_area_stylesheet(*, dark: bool) -> str:
        """与客户列表 / 任务分配等区域一致的细窄滚动条。"""
        handle = "rgba(255, 255, 255, 0.25)" if dark else "rgba(128, 128, 128, 0.45)"
        handle_hover = "rgba(255, 255, 255, 0.38)" if dark else "rgba(128, 128, 128, 0.65)"
        return f"""
            QScrollArea#PhoneWorkbenchHintScroll,
            QScrollArea#PhoneWorkbenchScriptScroll {{
                background: transparent;
                border: none;
            }}
            QScrollArea#PhoneWorkbenchHintScroll QWidget#qt_scrollarea_viewport,
            QScrollArea#PhoneWorkbenchScriptScroll QWidget#qt_scrollarea_viewport,
            QWidget#PhoneWorkbenchHintBody,
            QWidget#PhoneWorkbenchScriptBody {{
                background: transparent;
                background-color: transparent;
                border: none;
            }}
            QScrollArea#PhoneWorkbenchHintScroll QScrollBar:vertical,
            QScrollArea#PhoneWorkbenchScriptScroll QScrollBar:vertical {{
                background: transparent;
                width: 6px;
                margin: 2px 2px 2px 0px;
            }}
            QScrollArea#PhoneWorkbenchHintScroll QScrollBar::handle:vertical,
            QScrollArea#PhoneWorkbenchScriptScroll QScrollBar::handle:vertical {{
                background: {handle};
                border-radius: 3px;
                min-height: 28px;
            }}
            QScrollArea#PhoneWorkbenchHintScroll QScrollBar::handle:vertical:hover,
            QScrollArea#PhoneWorkbenchScriptScroll QScrollBar::handle:vertical:hover {{
                background: {handle_hover};
            }}
            QScrollArea#PhoneWorkbenchHintScroll QScrollBar::add-line:vertical,
            QScrollArea#PhoneWorkbenchHintScroll QScrollBar::sub-line:vertical,
            QScrollArea#PhoneWorkbenchScriptScroll QScrollBar::add-line:vertical,
            QScrollArea#PhoneWorkbenchScriptScroll QScrollBar::sub-line:vertical {{
                height: 0px;
                border: none;
                background: transparent;
            }}
            QScrollArea#PhoneWorkbenchHintScroll QScrollBar::add-page:vertical,
            QScrollArea#PhoneWorkbenchHintScroll QScrollBar::sub-page:vertical,
            QScrollArea#PhoneWorkbenchScriptScroll QScrollBar::add-page:vertical,
            QScrollArea#PhoneWorkbenchScriptScroll QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
        """

    def _apply_transparent_body_areas(self, *, dark: bool):
        """任务提示 / 完整话术正文区：透明底 + 统一滚动条样式。"""
        scroll_style = self._scroll_area_stylesheet(dark=dark)
        self._hint_scroll.setStyleSheet(scroll_style)
        self._script_scroll.setStyleSheet(scroll_style)
        for w in (
            self._hint_scroll.viewport(),
            self._script_scroll.viewport(),
            self._hint_body,
            self._script_inner,
        ):
            self._clear_container_background(w)

    def _apply_theme_style(self):
        """供主窗口主题切换调用：刷新配色并重新同步正文高度。"""
        self._apply_theme()
        self._apply_body_fonts()
        self._sync_content_layout()

    def _apply_theme(self):
        dark = isDarkTheme()
        # 与右侧抽屉 DrawerBg 保持一致，避免主题切换后局部仍残留浅色块
        card_bg = "#272727" if dark else "#ffffff"
        card_border = "rgba(255,255,255,0.12)" if dark else "rgba(0,0,0,0.09)"
        footer_bg = "#272727" if dark else "#f5f5f5"
        footer_border = "rgba(255,255,255,0.08)" if dark else "rgba(0,0,0,0.06)"
        sub = "#999999" if dark else "#666666"
        compact_val = "#b8b8b8" if dark else "#444444"
        remark_val = "#d8d8d8" if dark else "#333333"
        footer_divider = "rgba(255,255,255,0.08)" if dark else "rgba(0,0,0,0.06)"

        card_style = f"""
            QFrame#PhoneWorkbenchCard {{
                background-color: {card_bg};
                border: 1px solid {card_border};
                border-radius: 6px;
            }}
        """
        subtle_card_style = f"""
            QFrame#PhoneWorkbenchCardSubtle {{
                background: transparent;
                border: 1px solid {card_border};
                border-radius: 6px;
            }}
        """
        for card in (self._card_customer, self._card_task):
            card.setStyleSheet(card_style)
        for card in (self._card_hint, self._card_script):
            card.setStyleSheet(subtle_card_style)

        self._apply_transparent_body_areas(dark=dark)

        self._script_footer.setStyleSheet(
            f"""
            QFrame#PhoneWorkbenchScriptFooter {{
                background: transparent;
                border-top: 1px solid {footer_divider};
            }}
            """
        )

        self._footer.setStyleSheet(
            f"""
            QFrame#PhoneWorkbenchFooter {{
                background-color: {footer_bg};
                border-top: 1px solid {footer_border};
            }}
            """
        )
        label_reset = (
            "margin: 0; padding: 0; "
            "background: transparent; background-color: transparent; border: none;"
        )
        for lbl in self._section_title_labels():
            style_label(lbl, "caption_emphasis", color=sub, extra=label_reset)

        self.txt_task_instruction.setStyleSheet(
            f"color: {remark_val}; {label_reset}"
        )
        self.txt_generated_script.setStyleSheet(
            f"color: {remark_val}; {label_reset}"
        )
        style_label(self.lbl_font_caption, "caption", color=sub, extra=label_reset)
        style_label(self.lbl_font_size, "caption_emphasis", color=sub, extra=label_reset)
        style_label(self.lbl_wechat_remark, "sidebar_primary", color=remark_val)
        style_label(self.lbl_task_title, "sidebar_primary", color=remark_val)
        style_label(self.lbl_task_meta, "caption", color=sub)
        self._placeholder.setStyleSheet(label_qss("empty", color=sub, extra="padding: 12px;"))
        for lbl in self._detail_value_labels.values():
            style_label(lbl, "caption", color=compact_val)

        if hasattr(self, "btn_font_decrease"):
            self.btn_font_decrease.setEnabled(self._script_font_size > _SCRIPT_FONT_MIN)
            self.btn_font_increase.setEnabled(self._script_font_size < _SCRIPT_FONT_MAX)
            self.lbl_font_size.setText(str(self._script_font_size))

    def _reset_customer_details(self):
        self.lbl_wechat_remark.setText("—")
        for lbl in self._detail_value_labels.values():
            lbl.setText("—")

    def _apply_customer_details(self, customer: dict):
        self._reset_customer_details()
        self.lbl_wechat_remark.setText(_display_text(customer.get("wechat_remark")))
        for _label_text, field_key in _CUSTOMER_DETAIL_FIELDS:
            lbl = self._detail_value_labels.get(field_key)
            if lbl is not None:
                lbl.setText(_field_value(customer, field_key))

    def context_key(self) -> str | None:
        return customer_script_key(self._customer)

    def _restore_generated_script(self) -> None:
        key = self.context_key()
        cached = self._script_store.get(key) if key else None
        self.set_generated_script(cached or "", persist=False)

    def clear(self):
        self._layout_sync_timer.stop()
        self._layout_sync_attempts = 0
        self._customer = None
        self._task = None
        self._phone_raw = ""
        self._placeholder.setVisible(True)
        self._work.setVisible(False)
        self._card_task.setVisible(False)
        self.txt_task_instruction.setText("")
        self._reset_customer_details()
        self.set_generate_busy(False)
        self._generating_for_key = None

    def set_context(self, customer: dict | None, task: dict | None = None):
        self._customer = customer if isinstance(customer, dict) else None
        self._task = task if isinstance(task, dict) else None
        if not self._customer:
            self.clear()
            return

        self._placeholder.setVisible(False)
        self._work.setVisible(True)
        self._apply_customer_details(self._customer)
        self._phone_raw = resolve_display_phone(self._task) or resolve_display_phone(self._customer)
        self._apply_task_section()
        self._apply_script_section()
        self._restore_generated_script()
        self._apply_theme_style()
        self._schedule_content_layout_sync()
        self.btn_generate_script.setEnabled(True)
        if PHONE_OUTBOUND_DISABLED:
            self.set_complete_task_busy(False)
        else:
            self.set_yunke_call_busy(False)
            self.set_changhu_call_busy(False)

    def set_generate_busy(self, busy: bool):
        has_customer = bool(self._customer)
        self.btn_generate_script.setEnabled(has_customer and not busy)
        self.btn_generate_script.setText("生成中…" if busy else "生成话术")

    def begin_script_generation(self):
        self._generating_for_key = self.context_key()
        self.set_generate_busy(True)
        self.txt_generated_script.setText("")
        self._apply_body_fonts()
        self._sync_script_inner_height()

    def append_script_stream(self, chunk: str):
        if self._generating_for_key != self.context_key():
            return
        if not chunk:
            return
        current = self.txt_generated_script.text() or ""
        self.txt_generated_script.setText(current + chunk)
        self._apply_body_fonts()
        self._sync_script_inner_height()
        bar = self._script_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def finish_script_generation(self, error: str | None):
        gen_key = self._generating_for_key
        self._generating_for_key = None
        self.set_generate_busy(False)

        if gen_key != self.context_key():
            return

        if error:
            self.set_generated_script(f"⚠️ {error}", persist=False)
        elif not (self.txt_generated_script.text() or "").strip():
            self._restore_generated_script()
        else:
            text = self.txt_generated_script.text() or ""
            if is_persistable_script(text) and gen_key:
                self._script_store.put(gen_key, text)

    def set_generated_script(self, text: str, *, persist: bool = True):
        t = _normalize_block_text(text)
        if t:
            self.txt_generated_script.setText(t)
        else:
            self.txt_generated_script.setText(SCRIPT_PLACEHOLDER)
        if persist:
            key = self.context_key()
            if key and is_persistable_script(t):
                self._script_store.put(key, t)
        self._apply_body_fonts()
        self._sync_script_inner_height()

    def _apply_task_section(self):
        if _is_phone_allocation_task(self._task):
            self._fill_task_card(self._task)
            self._card_task.setVisible(True)
            return
        self._card_task.setVisible(False)

    def _fill_task_card(self, task: dict):
        title = (task.get("title") or "电话跟进").strip()
        kind = TASK_KIND_LABELS.get((task.get("task_kind") or "contact").strip(), "联系")
        due = _fmt_due_date(task.get("due_date"))
        rank = task.get("priority_rank")
        self.lbl_task_title.setText(f"📞 {title}")
        meta = [p for p in (kind, f"截止{due}" if due else "", f"#{rank}" if rank is not None else "") if p]
        self.lbl_task_meta.setText(" · ".join(meta) if meta else "")

    def _apply_script_section(self):
        if _is_phone_allocation_task(self._task):
            self.lbl_task_hint.setText("任务提示")
            instr = (self._task.get("instruction") or "").strip()
            if not instr:
                instr = "该电话任务暂无具体说明，可参考下方「生成话术」或客户画像。"
        else:
            self.lbl_task_hint.setText("客户画像")
            profile = ""
            if self._customer:
                profile = (self._customer.get("ai_profile") or "").strip()
            if not profile and self._task:
                profile = (self._task.get("ai_profile") or "").strip()
            instr = profile or "暂无客户画像，请在客户资料中完成 AI 分析或手动填写画像。"

        self.txt_task_instruction.setText(_normalize_block_text(instr))
        self._apply_body_fonts()
        self._sync_content_layout()

    def _apply_outbound_mode(self):
        disabled = PHONE_OUTBOUND_DISABLED
        self.btn_changhu_call.setVisible(not disabled)
        self.btn_yunke_call.setVisible(not disabled)
        self.btn_complete_task.setVisible(disabled)
        if disabled:
            self._refresh_complete_task_button()

    def _refresh_complete_task_button(self, *, busy: bool = False):
        actionable = self._task_actionable()
        self.btn_complete_task.setEnabled(actionable and not busy)
        self.btn_complete_task.setText("处理中..." if busy else "完成任务")

    def _on_complete_task_clicked(self):
        if not self._task_actionable():
            parent = self.window()
            if parent and hasattr(parent, "show_info_bar"):
                parent.show_info_bar(
                    "warning",
                    "无法完成任务",
                    "当前没有待完成的电话主线任务。",
                )
            return
        title = (self._task.get("title") or "电话任务").strip()
        if not ask_confirm(
            self,
            "完成任务",
            f"外呼功能临时关闭。确认将「{title}」标记为已完成？",
        ):
            return
        self.set_complete_task_busy(True)
        self.complete_task_clicked.emit()

    def set_complete_task_busy(self, busy: bool):
        self._refresh_complete_task_button(busy=busy)

    def _on_changhu_call_clicked(self):
        if PHONE_OUTBOUND_DISABLED:
            self._on_complete_task_clicked()
            return
        from ui.changhu_phone_picker import pick_changhu_tel, resolve_changhu_phones

        if not self._phone_raw:
            parent = self.window()
            if parent and hasattr(parent, "show_info_bar"):
                parent.show_info_bar(
                    "warning",
                    "暂无联系电话",
                    "请先在「客户详细资料」中补充号码后再外呼。",
                )
            return
        if not resolve_changhu_phones(self):
            parent = self.window()
            if parent and hasattr(parent, "show_info_bar"):
                parent.show_info_bar(
                    "warning",
                    "畅呼外呼失败",
                    "未配置畅呼号码，请在米城账号中绑定畅呼手机号后重试",
                    duration=4000,
                )
            return
        changhu_tel = pick_changhu_tel(self)
        if not changhu_tel:
            return
        name = ""
        if isinstance(self._customer, dict):
            name = str(self._customer.get("customer_name") or "").strip()
        masked = mask_phone(self._phone_raw)
        who = f"「{name}」" if name and name != "—" else "该客户"
        if not ask_confirm(
            self,
            "畅呼外呼",
            f"确认使用畅呼号码 {changhu_tel} 拨打{who}（{masked}）？",
        ):
            return
        self.btn_changhu_call.setEnabled(False)
        self.btn_changhu_call.setText("外呼中...")
        self.changhu_call_clicked.emit(changhu_tel)

    def set_changhu_call_busy(self, busy: bool):
        has_phone = bool(self._phone_raw)
        self.btn_changhu_call.setEnabled(has_phone and not busy)
        self.btn_changhu_call.setText("外呼中..." if busy else "畅呼外呼")

    def _on_yunke_call_clicked(self):
        if PHONE_OUTBOUND_DISABLED:
            self._on_complete_task_clicked()
            return
        if not self._phone_raw:
            parent = self.window()
            if parent and hasattr(parent, "show_info_bar"):
                parent.show_info_bar(
                    "warning",
                    "暂无联系电话",
                    "请先在「客户详细资料」中补充号码后再外呼。",
                )
            return
        name = ""
        if isinstance(self._customer, dict):
            name = str(self._customer.get("customer_name") or "").strip()
        masked = mask_phone(self._phone_raw)
        who = f"「{name}」" if name and name != "—" else "该客户"
        if not ask_confirm(
            self,
            "云客外呼",
            f"确认通过云客外呼拨打{who}（{masked}）？",
        ):
            return
        self.btn_yunke_call.setEnabled(False)
        self.btn_yunke_call.setText("外呼中...")
        self.yunke_call_clicked.emit()

    def set_yunke_call_busy(self, busy: bool):
        has_phone = bool(self._phone_raw)
        self.btn_yunke_call.setEnabled(has_phone and not busy)
        self.btn_yunke_call.setText("外呼中..." if busy else "云客外呼")
