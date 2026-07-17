"""回访悬浮列表弹层（当日待回访 + 逾期回访）。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QPoint
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import BodyLabel, CaptionLabel, StrongBodyLabel, isDarkTheme

from ui.app_fonts import style_label
from ui.widgets.callback_card import CallbackCardWidget


class CallbackListPopup(QFrame):
    """点击「回访列表」后弹出的悬浮面板。"""

    open_chat_requested = Signal(dict)
    done_requested = Signal(dict)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("CallbackListPopup")
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        # Popup 顶层窗口需显式开启，样式表 background 才会绘制
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(300)
        self.setMaximumWidth(360)
        self.setMaximumHeight(480)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        self.title_lbl = StrongBodyLabel("回访提醒")
        head.addWidget(self.title_lbl)
        head.addStretch(1)
        self.count_lbl = CaptionLabel("")
        head.addWidget(self.count_lbl)
        root.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("CallbackListScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.body = QWidget()
        self.body.setObjectName("CallbackListBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(6)
        self.empty_lbl = BodyLabel("暂无待回访")
        self.empty_lbl.setAlignment(Qt.AlignCenter)
        self.body_layout.addWidget(self.empty_lbl)
        self.body_layout.addStretch(1)
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll, 1)

        self._cards: list[CallbackCardWidget] = []
        self._section_labels: list[CaptionLabel] = []
        self._apply_theme_style()

    @staticmethod
    def _split_items(items: list[dict]) -> tuple[list[dict], list[dict]]:
        today_rows: list[dict] = []
        past_rows: list[dict] = []
        for it in items or []:
            if it.get("past_day"):
                past_rows.append(it)
            else:
                today_rows.append(it)
        return today_rows, past_rows

    def _clear_body(self):
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            w = item.widget()
            if w is not None and w is not self.empty_lbl:
                w.deleteLater()
        self._cards.clear()
        self._section_labels.clear()

    def _add_section_title(self, text: str) -> CaptionLabel:
        lbl = CaptionLabel(text)
        lbl.setObjectName("CallbackSectionTitle")
        self.body_layout.addWidget(lbl)
        self._section_labels.append(lbl)
        return lbl

    def _add_card(self, it: dict):
        card = CallbackCardWidget(it, self.body)
        card.open_chat_requested.connect(self._on_open_chat)
        card.done_requested.connect(self._on_done)
        self.body_layout.addWidget(card)
        self._cards.append(card)

    def set_items(self, items: list[dict] | None):
        rows = list(items or [])
        self._clear_body()

        if not rows:
            self.count_lbl.setText("")
            self.empty_lbl.setText("暂无待回访")
            self.empty_lbl.show()
            self.body_layout.addWidget(self.empty_lbl)
            self.body_layout.addStretch(1)
            self._apply_theme_style()
            return

        today_rows, past_rows = self._split_items(rows)
        total = len(rows)
        parts: list[str] = [f"{total} 条"]
        if past_rows:
            parts.append(f"{len(past_rows)} 逾期")
        due_today = sum(1 for it in today_rows if it.get("overdue"))
        if due_today:
            parts.append(f"{due_today} 已到点")
        self.count_lbl.setText(" · ".join(parts))
        self.empty_lbl.hide()

        if past_rows:
            self._add_section_title(f"逾期回访（{len(past_rows)}）")
            for it in past_rows:
                self._add_card(it)

        if today_rows:
            self._add_section_title(f"当日待回访（{len(today_rows)}）")
            for it in today_rows:
                self._add_card(it)

        self.body_layout.addStretch(1)
        self._apply_theme_style()

    def _on_open_chat(self, item: dict):
        self.open_chat_requested.emit(item)
        self.hide()

    def _on_done(self, item: dict):
        self.done_requested.emit(item)
        # 弹层内立即移除该卡；若空则关闭
        scp_id = int((item or {}).get("scp_id") or 0)
        keep = [
            c.item
            for c in self._cards
            if int((c.item or {}).get("scp_id") or 0) != scp_id
        ]
        self.set_items(keep)
        if not keep:
            self.hide()

    def popup_near(self, anchor: QWidget):
        """在按钮附近弹出。"""
        self.adjustSize()
        n = max(1, len(self._cards))
        section_extra = 28 * len(self._section_labels)
        h = min(480, 48 + section_extra + n * 118)
        self.resize(max(300, self.width()), h)
        pos = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        # 尽量不超出屏幕右缘
        screen = anchor.screen().availableGeometry() if anchor.screen() else None
        if screen is not None:
            if pos.x() + self.width() > screen.right():
                pos.setX(max(screen.left(), screen.right() - self.width() - 8))
            if pos.y() + self.height() > screen.bottom():
                pos.setY(max(screen.top(), anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 4))
        self.move(pos)
        self.show()
        self.raise_()
        self.activateWindow()

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg = "#2b2b2b" if is_dark else "#ffffff"
        border = "rgba(255,255,255,0.14)" if is_dark else "rgba(0,0,0,0.10)"
        section_fg = "rgba(255,255,255,0.55)" if is_dark else "rgba(0,0,0,0.45)"
        scroll_handle = "rgba(255,255,255,0.25)" if is_dark else "rgba(128,128,128,0.45)"
        scroll_handle_hover = "rgba(255,255,255,0.38)" if is_dark else "rgba(128,128,128,0.65)"
        self.setStyleSheet(
            f"""
            QFrame#CallbackListPopup {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 10px;
            }}
            QScrollArea#CallbackListScroll {{
                background: transparent;
                border: none;
            }}
            QScrollArea#CallbackListScroll QWidget#qt_scrollarea_viewport,
            QWidget#CallbackListBody {{
                background: transparent;
                background-color: transparent;
                border: none;
            }}
            QScrollArea#CallbackListScroll QScrollBar:vertical {{
                background: transparent;
                width: 6px;
                margin: 2px 2px 2px 0px;
            }}
            QScrollArea#CallbackListScroll QScrollBar::handle:vertical {{
                background: {scroll_handle};
                border-radius: 3px;
                min-height: 28px;
            }}
            QScrollArea#CallbackListScroll QScrollBar::handle:vertical:hover {{
                background: {scroll_handle_hover};
            }}
            QScrollArea#CallbackListScroll QScrollBar::add-line:vertical,
            QScrollArea#CallbackListScroll QScrollBar::sub-line:vertical {{
                height: 0px;
                border: none;
                background: transparent;
            }}
            QScrollArea#CallbackListScroll QScrollBar::add-page:vertical,
            QScrollArea#CallbackListScroll QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            QLabel#CallbackSectionTitle {{
                color: {section_fg};
                padding: 4px 0 2px 0;
            }}
            """
        )
        # 清掉 viewport / body 的系统默认白底调色板
        for w in (self.scroll.viewport(), self.body):
            w.setAutoFillBackground(False)
            w.setAttribute(Qt.WA_StyledBackground, True)
            w.setStyleSheet("background: transparent; background-color: transparent;")
        style_label(self.title_lbl, "section")
        style_label(self.count_lbl, "caption")
        style_label(self.empty_lbl, "empty")
        for card in self._cards:
            card._apply_theme_style()
