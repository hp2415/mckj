"""回访提醒卡片（当日 / 往日逾期）。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    PushButton,
    StrongBodyLabel,
    isDarkTheme,
)

from ui.app_fonts import badge_qss, compact_button_qss, style_label


class CallbackCardWidget(QFrame):
    """单条回访提醒。"""

    open_chat_requested = Signal(dict)
    done_requested = Signal(dict)

    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        self.item: dict = item or {}
        self.setObjectName("CallbackCard")
        self.setFrameShape(QFrame.NoFrame)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        head.setContentsMargins(0, 0, 0, 0)

        past_day = bool(self.item.get("past_day"))
        overdue = bool(self.item.get("overdue"))
        if past_day:
            badge_text = "逾期"
        elif overdue:
            badge_text = "已到点"
        else:
            badge_text = "待回访"
        self.badge_lbl = CaptionLabel(badge_text)
        self.badge_lbl.setObjectName("CallbackBadge")
        head.addWidget(self.badge_lbl)

        time_text = self._format_time(self.item.get("callback_at"), past_day=past_day)
        self.time_lbl = CaptionLabel(time_text)
        self.time_lbl.setObjectName("CallbackTime")
        head.addWidget(self.time_lbl)
        head.addStretch(1)
        root.addLayout(head)

        name = (self.item.get("customer_name") or "").strip() or "（未登记客户）"
        self.customer_lbl = StrongBodyLabel(name)
        self.customer_lbl.setWordWrap(True)
        root.addWidget(self.customer_lbl)

        note = (self.item.get("callback_note") or "").strip()
        if note:
            self.note_lbl = BodyLabel(note)
            self.note_lbl.setWordWrap(True)
            self.note_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            root.addWidget(self.note_lbl)
        else:
            self.note_lbl = None

        foot = QHBoxLayout()
        foot.setSpacing(6)
        foot.setContentsMargins(0, 2, 0, 0)
        foot.addStretch(1)
        self.btn_chat = PushButton("去联系")
        self.btn_chat.setFixedHeight(26)
        self.btn_chat.clicked.connect(lambda: self.open_chat_requested.emit(dict(self.item)))
        foot.addWidget(self.btn_chat)
        self.btn_done = PushButton("已处理")
        self.btn_done.setFixedHeight(26)
        self.btn_done.clicked.connect(lambda: self.done_requested.emit(dict(self.item)))
        foot.addWidget(self.btn_done)
        root.addLayout(foot)

        self._apply_theme_style()

    @staticmethod
    def _format_time(raw, *, past_day: bool = False) -> str:
        text = str(raw or "").strip()
        if not text:
            return "时刻未知"
        text = text.replace("T", " ")
        if past_day and len(text) >= 16:
            return text[5:16]  # MM-DD HH:MM
        if len(text) >= 16:
            return text[11:16]
        return text

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        # 深色下略抬升卡片底，与弹层底板拉开层次
        card_bg = "#333333" if is_dark else "#ffffff"
        card_border = "rgba(255,255,255,0.12)" if is_dark else "rgba(0,0,0,0.09)"
        past_day = bool(self.item.get("past_day"))
        overdue = bool(self.item.get("overdue"))
        if past_day:
            side = "#cf1322"
            badge_fg, badge_bg = "#cf1322", "rgba(207,19,34,0.16)"
        elif overdue:
            side = "#ff4d4f"
            badge_fg, badge_bg = "#ff4d4f", "rgba(255,77,79,0.16)"
        else:
            side = "#fa8c16"
            badge_fg, badge_bg = "#fa8c16", "rgba(250,140,22,0.16)"

        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"""
            QFrame#CallbackCard {{
                background-color: {card_bg};
                border: 1px solid {card_border};
                border-left: 4px solid {side};
                border-radius: 8px;
            }}
            """
        )
        self.badge_lbl.setStyleSheet(
            f"QLabel#CallbackBadge {{ {badge_qss(badge_fg, badge_bg)} }}"
        )
        style_label(self.time_lbl, "caption")
        style_label(self.customer_lbl, "body_emphasis")
        if self.note_lbl is not None:
            style_label(self.note_lbl, "caption", extra="line-height: 16px;")

        if is_dark:
            btn_fg, btn_bg = "#cccccc", "rgba(255,255,255,0.07)"
            btn_border, btn_hover = "rgba(255,255,255,0.15)", "rgba(255,255,255,0.14)"
        else:
            btn_fg, btn_bg = "#444444", "rgba(0,0,0,0.04)"
            btn_border, btn_hover = "rgba(0,0,0,0.12)", "rgba(0,0,0,0.09)"
        btn_style = compact_button_qss(
            fg=btn_fg,
            bg=btn_bg,
            border=btn_border,
            hover_bg=btn_hover,
            hover_border="#07c160",
        )
        self.btn_chat.setStyleSheet(btn_style)
        self.btn_done.setStyleSheet(btn_style)
