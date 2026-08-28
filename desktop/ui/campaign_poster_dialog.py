"""活动海报外发预览：Fluent 遮罩弹窗，避免原生 QDialog 标题栏在左上角闪一帧。"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    MessageBoxBase,
    SubtitleLabel,
    TransparentToolButton,
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


def _decode_preview(path: str, width: int, height: int) -> QImage | None:
    """在工作线程解码并缩放（QImage 线程安全；不要在此创建 QPixmap）。"""
    if not path or not os.path.isfile(path):
        return None
    image = QImage()
    if not image.load(path) or image.isNull():
        return None
    scaled = image.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return scaled if scaled and not scaled.isNull() else None


class CampaignPosterPreviewDialog(MessageBoxBase):
    """遮罩弹窗：无系统最小化/关闭按钮，铺满父窗口，不会在左上角闪原生标题栏。"""

    _preview_ready = Signal(int, int, str, object)

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
        self._campaigns = [c for c in (campaigns or []) if c]
        self._index = 0
        self._accepting = False
        self._load_timer: QTimer | None = None
        self._load_attempts = 0
        self._preview_cache: dict[str, QPixmap] = {}
        self._decode_token = 0
        self._shown = False

        title = "活动图片预览" if self._preview_only else "发送活动海报"
        self.titleLabel = SubtitleLabel(title, self)
        default_hint = (
            "可切换活动查看海报预览。"
            if self._preview_only
            else "确认后将通过本机微信发送海报。"
        )
        self._hint = CaptionLabel(customer_hint or default_hint, self)
        self._hint.setWordWrap(True)

        if self._campaigns:
            idx = int(initial_index or 0)
            if idx < 0 or idx >= len(self._campaigns):
                idx = 0
            self._index = idx

        self._nav = QWidget(self)
        nav_l = QHBoxLayout(self._nav)
        nav_l.setContentsMargins(0, 0, 0, 0)
        nav_l.setSpacing(4)
        self._btn_prev = TransparentToolButton(FluentIcon.CARE_LEFT_SOLID, self._nav)
        self._btn_next = TransparentToolButton(FluentIcon.CARE_RIGHT_SOLID, self._nav)
        for btn in (self._btn_prev, self._btn_next):
            btn.setFixedSize(28, 28)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setAutoRaise(True)
        self._campaign_label = BodyLabel("", self._nav)
        self._campaign_label.setWordWrap(True)
        nav_l.addWidget(self._btn_prev)
        nav_l.addWidget(self._campaign_label, 1)
        nav_l.addWidget(self._btn_next)
        self._nav.setVisible(len(self._campaigns) > 1)
        self._btn_prev.clicked.connect(lambda: self._shift_campaign(-1))
        self._btn_next.clicked.connect(lambda: self._shift_campaign(1))

        self._name = BodyLabel("", self)
        self._name.setWordWrap(True)
        self._window = CaptionLabel("", self)
        self._window.setWordWrap(True)

        self._img = QLabel(self)
        self._img.setObjectName("PosterPreviewImage")
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setScaledContents(False)
        self._img.setFocusPolicy(Qt.NoFocus)
        self._img.setText("正在加载海报…")
        self._preview_w = 320
        self._preview_h = 240

        self.viewLayout.setContentsMargins(16, 16, 16, 12)
        self.viewLayout.setSpacing(8)
        self.buttonLayout.setContentsMargins(16, 12, 16, 16)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self._hint)
        self.viewLayout.addWidget(self._nav)
        self.viewLayout.addWidget(self._name)
        self.viewLayout.addWidget(self._window)
        self.viewLayout.addWidget(self._img, 0, Qt.AlignHCenter)

        if self._preview_only:
            self.yesButton.hide()
            self.cancelButton.setText("关闭")
        else:
            self.yesButton.setText("发送")
            self.cancelButton.setText("取消")
            self.yesButton.setEnabled(False)
        self.yesButton.setAutoDefault(False)
        self.yesButton.setDefault(False)
        self.cancelButton.setAutoDefault(False)
        self.cancelButton.setDefault(False)

        self._preview_ready.connect(self._on_preview_ready)
        self._sync_nav_label()
        self._render_meta()
        self._apply_img_style()
        self._fit_to_parent()
        self.widget.adjustSize()

    def open(self):
        """等鼠标松开再显示，避免打开弹窗的那次点击落到遮罩/按钮上立刻关掉。"""
        if QApplication.mouseButtons() != Qt.NoButton:
            QTimer.singleShot(16, self.open)
            return
        super().open()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._shown:
            self._shown = True
            self._fit_to_parent()
            QTimer.singleShot(0, self._request_preview)

    def _host_size(self) -> tuple[int, int]:
        """默认主窗口约 430×720；超出则按实际宿主收紧。"""
        host = self.parent()
        if host is not None:
            win = host.window() if hasattr(host, "window") else host
            try:
                w = int(win.width()) if win is not None else 0
                h = int(win.height()) if win is not None else 0
                if w > 0 and h > 0:
                    return w, h
            except Exception:
                pass
        return 430, 720

    def _fit_to_parent(self):
        """卡片宽高都限制在宿主窗口内，避免默认 430 宽主窗口裁切。"""
        host_w, host_h = self._host_size()
        dialog_w = max(300, min(390, host_w - 40))
        self.widget.setMinimumWidth(dialog_w)
        self.widget.setMaximumWidth(dialog_w)
        self.widget.setMaximumHeight(max(360, host_h - 32))

        content_w = dialog_w - 32
        self._hint.setMaximumWidth(content_w)
        self._name.setMaximumWidth(content_w)
        self._window.setMaximumWidth(content_w)

        img_w = content_w
        img_h = int(img_w * 0.75)
        reserved = 16 + 12 + 28 + 52 + 22 + 18 + 81 + 8 * 5 + 20
        if self._nav.isVisible():
            reserved += 32
        max_img_h = max(140, host_h - 40 - reserved)
        if img_h > max_img_h:
            img_h = max_img_h
            img_w = max(160, min(content_w, int(img_h * 4 / 3)))
        self._preview_w = img_w
        self._preview_h = img_h
        self._img.setFixedSize(img_w, img_h)

    def validate(self) -> bool:
        if self._preview_only or self._accepting:
            return False
        pix = self._img.pixmap()
        if pix is None or pix.isNull():
            return False
        self._accepting = True
        return True

    def _shift_campaign(self, delta: int):
        if len(self._campaigns) <= 1:
            return
        self._index = (self._index + int(delta)) % len(self._campaigns)
        self._sync_nav_label()
        self._render_meta()
        self._request_preview()

    def _sync_nav_label(self):
        n = len(self._campaigns)
        if n <= 1:
            self._campaign_label.setText("")
            return
        self._campaign_label.setText(f"{self._index + 1} / {n}")

    def _current(self) -> dict:
        if not self._campaigns:
            return {}
        if self._index < 0 or self._index >= len(self._campaigns):
            return self._campaigns[0]
        return self._campaigns[self._index]

    def _render_meta(self):
        camp = self._current()
        self._name.setText(str(camp.get("name") or "未命名活动"))
        self._window.setText(_fmt_window(camp.get("start_at"), camp.get("end_at")))

    def _stop_load_watch(self):
        if self._load_timer is not None and self._load_timer.isActive():
            self._load_timer.stop()

    def _start_load_watch(self):
        if self._load_timer is None:
            self._load_timer = QTimer(self)
            self._load_timer.setInterval(200)
            self._load_timer.timeout.connect(self._on_load_tick)
        if not self._load_timer.isActive():
            self._load_attempts = 0
            self._load_timer.start()

    def _on_load_tick(self):
        self._load_attempts += 1
        local = str(self._current().get("_local_image") or "").strip()
        if local and os.path.isfile(local) and os.path.getsize(local) > 0:
            self._stop_load_watch()
            self._request_preview()
            return
        if self._load_attempts >= 150:
            self._stop_load_watch()
            self._img.setText("海报预览加载失败")
            self._img.setPixmap(QPixmap())
            self.yesButton.setEnabled(False)

    def _request_preview(self):
        camp = self._current()
        local = str(camp.get("_local_image") or "").strip()
        if local in self._preview_cache:
            self._apply_pixmap(self._preview_cache[local])
            return
        if not local or not os.path.isfile(local) or os.path.getsize(local) <= 0:
            self._img.setPixmap(QPixmap())
            self._img.setText("正在加载海报…")
            self.yesButton.setEnabled(False)
            self._start_load_watch()
            return

        self._stop_load_watch()
        self._img.setPixmap(QPixmap())
        self._img.setText("正在加载海报…")
        self.yesButton.setEnabled(False)
        token = self._decode_token + 1
        self._decode_token = token
        path = local
        index = self._index

        def _work():
            image = _decode_preview(path, self._preview_w, self._preview_h)
            self._preview_ready.emit(token, index, path, image)

        threading.Thread(target=_work, name="poster-preview-decode", daemon=True).start()

    def _on_preview_ready(self, token: int, index: int, path: str, image: object):
        if token != self._decode_token or index != self._index:
            return
        if not isinstance(image, QImage) or image.isNull():
            self._img.setText("海报预览加载失败")
            self._img.setPixmap(QPixmap())
            self.yesButton.setEnabled(False)
            return
        pix = QPixmap.fromImage(image)
        if path:
            self._preview_cache[path] = pix
        self._apply_pixmap(pix)

    def _apply_pixmap(self, pix: QPixmap):
        if pix is None or pix.isNull():
            self._img.setText("海报预览加载失败")
            self._img.setPixmap(QPixmap())
            self.yesButton.setEnabled(False)
            return
        box = self._img.size()
        if not pix.isNull() and (pix.width() > box.width() or pix.height() > box.height()):
            pix = pix.scaled(box, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._img.setText("")
        self._img.setPixmap(pix)
        if not self._preview_only:
            self.yesButton.setEnabled(True)

    def selected_index(self) -> int:
        return int(self._index)

    def selected_campaign(self) -> dict:
        return dict(self._current())

    def _apply_img_style(self):
        self._img.setStyleSheet(
            "QLabel#PosterPreviewImage { background-color: rgba(0,0,0,0.06);"
            " border: 1px solid rgba(0,0,0,0.12); border-radius: 8px; }"
        )

    def reject(self):
        self._stop_load_watch()
        super().reject()

    def accept(self):
        self._stop_load_watch()
        super().accept()
