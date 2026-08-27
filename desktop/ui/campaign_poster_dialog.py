"""活动海报外发预览：展示活动信息与海报图，确认后再走 RPA。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    PrimaryPushButton,
    PushButton,
    isDarkTheme,
)


def _fmt_window(start_at: str | None, end_at: str | None) -> str:
    def _one(raw: str | None) -> str:
        s = (raw or "").strip()
        if not s:
            return "—"
        s = s.replace("T", " ")
        if len(s) >= 16:
            return s[:16]
        return s

    return f"{_one(start_at)} ～ {_one(end_at)}"


class CampaignPosterPreviewDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        campaigns: list[dict],
        customer_hint: str = "",
        preview_only: bool = False,
        initial_index: int = 0,
    ):
        super().__init__(parent)
        self._preview_only = bool(preview_only)
        self.setWindowTitle("活动图片预览" if self._preview_only else "发送活动海报")
        self.resize(440, 460)
        self._campaigns = [dict(c) for c in (campaigns or []) if c]
        self._index = 0
        self._accepting = False

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        default_hint = (
            "可切换活动查看海报预览。"
            if self._preview_only
            else "确认后将通过本机微信发送海报。"
        )
        self._hint = CaptionLabel(customer_hint or default_hint)
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        self._combo = ComboBox(self)
        self._combo.setPlaceholderText("选择活动")
        for item in self._campaigns:
            name = str(item.get("name") or f"活动#{item.get('id') or ''}")
            self._combo.addItem(name)
        self._combo.setVisible(len(self._campaigns) > 1)
        if self._campaigns:
            idx = int(initial_index or 0)
            if idx < 0 or idx >= len(self._campaigns):
                idx = 0
            self._index = idx
            self._combo.setCurrentIndex(idx)
        self._combo.currentIndexChanged.connect(self._on_campaign_changed)
        layout.addWidget(self._combo)

        self._name = BodyLabel("")
        self._name.setWordWrap(True)
        layout.addWidget(self._name)

        self._window = CaptionLabel("")
        self._window.setWordWrap(True)
        layout.addWidget(self._window)

        self._img = QLabel(self)
        self._img.setObjectName("PosterPreviewImage")
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setMinimumHeight(240)
        self._img.setMaximumHeight(320)
        self._img.setScaledContents(False)
        layout.addWidget(self._img, 1)

        row = QHBoxLayout()
        row.addStretch()
        self._btn_cancel = PushButton("关闭" if self._preview_only else "取消")
        self._btn_ok = PrimaryPushButton("发送")
        row.addWidget(self._btn_cancel)
        row.addWidget(self._btn_ok)
        self._btn_ok.setVisible(not self._preview_only)
        layout.addLayout(row)

        self._btn_cancel.clicked.connect(self.reject)
        self._btn_ok.clicked.connect(self._on_confirm_send)
        self._apply_theme_style()
        self._render_current()

    def _on_campaign_changed(self, index: int):
        if 0 <= int(index) < len(self._campaigns):
            self._index = int(index)
            self._render_current()

    def _current(self) -> dict:
        if not self._campaigns:
            return {}
        if self._index < 0 or self._index >= len(self._campaigns):
            return self._campaigns[0]
        return self._campaigns[self._index]

    def _render_current(self):
        camp = self._current()
        name = str(camp.get("name") or "未命名活动")
        self._name.setText(name)
        self._window.setText(_fmt_window(camp.get("start_at"), camp.get("end_at")))
        local = str(camp.get("_local_image") or "").strip()
        pix = QPixmap(local) if local else QPixmap()
        if pix.isNull():
            self._img.setText("海报预览加载失败")
            self._img.setPixmap(QPixmap())
            self._btn_ok.setEnabled(False)
        else:
            self._img.setText("")
            scaled = pix.scaled(
                400,
                300,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            self._img.setPixmap(scaled)
            self._btn_ok.setEnabled(True)

    def selected_index(self) -> int:
        return int(self._index)

    def selected_campaign(self) -> dict:
        return dict(self._current())

    def _on_confirm_send(self):
        if self._accepting:
            return
        pix = self._img.pixmap()
        if pix is None or pix.isNull():
            return
        self._accepting = True
        self._btn_ok.setEnabled(False)
        self._btn_cancel.setEnabled(False)
        self.accept()

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg = "#1a1a1a" if is_dark else "#f0f2f5"
        text = "#ffffff" if is_dark else "#1a1a1a"
        sub = "#aaaaaa" if is_dark else "#888888"
        frame = "rgba(255,255,255,0.12)" if is_dark else "rgba(0,0,0,0.12)"
        img_bg = "#2c2c2c" if is_dark else "#ffffff"
        self.setStyleSheet(
            f"QDialog {{ background-color: {bg}; color: {text}; }}"
            f" QLabel#PosterPreviewImage {{ background-color: {img_bg};"
            f" border: 1px solid {frame}; border-radius: 8px; color: {sub}; }}"
        )
        self._hint.setStyleSheet(f"color: {sub};")
        self._window.setStyleSheet(f"color: {sub};")
        self._name.setStyleSheet(f"color: {text};")
