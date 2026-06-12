"""查看云客同步的微信原始聊天记录（佐证 AI 回复）。"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget, QScrollArea, QFrame, QLabel,
)
from qfluentwidgets import (
    SubtitleLabel, CaptionLabel, PrimaryPushButton, PushButton, isDarkTheme,
    IndeterminateProgressRing,
)
from ui.app_fonts import label_qss, style_label, text_palette


def _format_msg_time(row: dict) -> str:
    for key in ("send_timestamp_ms", "time_ms", "timestamp"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            ts = int(raw) / 1000
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            continue
    return ""


class WechatChatHistoryDialog(QDialog):
    """展示 raw_chat_logs 中的微信会话记录。"""

    load_more_requested = Signal()

    def __init__(
        self,
        parent=None,
        *,
        customer_label: str = "",
        rows: list | None = None,
        has_more: bool = False,
        loading: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle("微信聊天记录")
        self.setMinimumSize(420, 520)
        self.resize(480, 600)
        self._rows = list(rows or [])
        self._has_more = has_more
        self._loading = bool(loading)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)

        title = SubtitleLabel("微信聊天记录")
        root.addWidget(title)
        if customer_label:
            sub = CaptionLabel(customer_label)
            sub.setWordWrap(True)
            root.addWidget(sub)

        self._loading_panel = QWidget(self)
        loading_layout = QVBoxLayout(self._loading_panel)
        loading_layout.setContentsMargins(0, 40, 0, 40)
        loading_row = QHBoxLayout()
        loading_row.addStretch()
        self._loading_ring = IndeterminateProgressRing(self._loading_panel)
        self._loading_ring.setFixedSize(28, 28)
        self._loading_ring.setStrokeWidth(3)
        loading_row.addWidget(self._loading_ring)
        loading_row.addStretch()
        loading_layout.addLayout(loading_row)
        self._loading_label = CaptionLabel("正在加载聊天记录…")
        self._loading_label.setAlignment(Qt.AlignCenter)
        loading_layout.addWidget(self._loading_label)
        loading_layout.addStretch()
        root.addWidget(self._loading_panel, 1)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(4, 4, 4, 4)
        self._content_layout.setSpacing(8)
        self._content_layout.addStretch()
        self.scroll.setWidget(self._content)
        root.addWidget(self.scroll, 1)

        self._empty_label = CaptionLabel("暂无微信聊天记录。")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.hide()
        root.addWidget(self._empty_label)

        btn_row = QHBoxLayout()
        self.btn_load_more = PushButton("加载更多")
        self.btn_load_more.hide()
        self.btn_close = PrimaryPushButton("关闭")
        btn_row.addWidget(self.btn_load_more)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_close)
        root.addLayout(btn_row)

        self.btn_close.clicked.connect(self.accept)
        self.btn_load_more.clicked.connect(self.load_more_requested.emit)

        if self._loading:
            self._show_loading(True)
        else:
            self._render_rows(self._rows, append=False)
            self._update_load_more()
        self._apply_theme_style()

    def _show_loading(self, active: bool):
        self._loading = active
        if active:
            self._loading_panel.show()
            self.scroll.hide()
            self._empty_label.hide()
            self.btn_load_more.hide()
            self._loading_ring.start()
        else:
            self._loading_ring.stop()
            self._loading_panel.hide()
            self.scroll.show()

    def set_initial_data(self, rows: list, *, has_more: bool):
        """数据到达后填充首屏并结束加载态。"""
        self._rows = list(rows or [])
        self._has_more = has_more
        self._show_loading(False)
        self._render_rows(self._rows, append=False)
        self._update_load_more()

    def show_error(self, message: str):
        """加载失败时在对话框内展示错误，避免静默无反馈。"""
        self._show_loading(False)
        self._empty_label.setText(message or "加载失败，请稍后重试。")
        self._empty_label.show()
        self.btn_load_more.hide()

    def append_rows(self, rows: list, *, has_more: bool):
        self._rows.extend(rows or [])
        self._has_more = has_more
        self._render_rows(rows or [], append=True)
        self._update_load_more()
        self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())

    def _update_load_more(self):
        self.btn_load_more.setVisible(self._has_more)

    def _render_rows(self, rows: list, *, append: bool):
        if not append:
            while self._content_layout.count() > 1:
                item = self._content_layout.takeAt(0)
                w = item.widget()
                if w:
                    w.deleteLater()

        if not self._rows and not rows:
            self._empty_label.show()
            return
        self._empty_label.hide()

        insert_at = max(0, self._content_layout.count() - 1)
        for row in rows:
            bubble = self._make_bubble(row)
            self._content_layout.insertWidget(insert_at, bubble)
            insert_at += 1

    def _make_bubble(self, row: dict) -> QWidget:
        is_sales = int(row.get("is_send") or 0) == 1
        sender = "销售" if is_sales else "客户"
        time_str = _format_msg_time(row)
        text = (row.get("text") or "").strip() or "（无文本内容）"

        wrap = QFrame()
        wrap.setObjectName("WechatChatBubble")
        outer = QHBoxLayout(wrap)
        outer.setContentsMargins(0, 0, 0, 0)

        inner = QFrame()
        inner.setObjectName("WechatChatBubbleInner")
        v = QVBoxLayout(inner)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        meta = QLabel(f"{sender}  {time_str}".strip())
        meta.setObjectName("WechatChatMeta")
        body = QLabel(text)
        body.setObjectName("WechatChatBody")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(meta)
        v.addWidget(body)

        if is_sales:
            outer.addStretch()
            outer.addWidget(inner, 0, Qt.AlignRight)
        else:
            outer.addWidget(inner, 0, Qt.AlignLeft)
            outer.addStretch()

        self._style_bubble(wrap, inner, meta, body, is_sales)
        return wrap

    def _style_bubble(self, wrap, inner, meta, body, is_sales: bool):
        is_dark = isDarkTheme()
        if is_sales:
            # 销售方：微信风格浅绿底 + 深色正文
            bg = "#9fe870" if not is_dark else "#8fd46e"
            border = "#8ed45f" if not is_dark else "#7bc45a"
            meta_color = "#5a7340" if not is_dark else "#3d5228"
            text_color = "#1a1a1a"
        else:
            # 客户方：与聊天背景轻微色差，柔和白/灰底
            bg = "#ffffff" if not is_dark else "#2c2c2c"
            border = "#e8e8e8" if not is_dark else "#3a3a3a"
            pal = text_palette()
            meta_color = pal.tertiary
            text_color = pal.primary
        inner.setStyleSheet(
            f"QFrame#WechatChatBubbleInner {{"
            f" background-color: {bg};"
            f" border: 1px solid {border};"
            f" border-radius: 10px;"
            f"}}"
        )
        style_label(meta, "chat_meta", color=meta_color)
        style_label(body, "chat_bubble", color=text_color, extra="line-height: 1.45; padding: 0;")
        wrap.setStyleSheet("background: transparent;")

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg = "#191919" if is_dark else "#ededed"
        self.setStyleSheet(f"QDialog {{ background-color: {bg}; }}")
        self.scroll.setStyleSheet(
            f"QScrollArea {{ border: none; background-color: {bg}; }}"
        )
        self._content.setStyleSheet(f"background-color: {bg};")
