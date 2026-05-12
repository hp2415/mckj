"""RPA 发送进度提示弹窗；提供「中断」以协作取消后台 RPA 线程。"""

import threading

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QDialog, QVBoxLayout

from qfluentwidgets import BodyLabel, CaptionLabel, IndeterminateProgressRing, PushButton


class RpaProgressDialog(QDialog):
    def __init__(self, parent=None, *, title: str = "正在发送", detail: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(320)
        self.setMinimumHeight(160)
        # 关闭按钮可见，方便用户在 RPA 卡住时主动放弃
        self.setWindowFlag(Qt.WindowCloseButtonHint, True)

        self._cancel_event = threading.Event()
        # 区分「代码完成后关闭弹窗」与「用户主动关闭弹窗」：
        # 后者要把 cancel_event 置位以通知 RPA 线程退出；
        # 前者只是收尾，绝不能反过来把成功的发送误判为中断。
        self._completed = False

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        lab_title = BodyLabel(title)
        lab_title.setWordWrap(True)
        layout.addWidget(lab_title)

        if detail:
            lab = CaptionLabel(detail)
            lab.setWordWrap(True)
            layout.addWidget(lab)

        ring = IndeterminateProgressRing(self)
        ring.setFixedSize(22, 22)
        ring.setStrokeWidth(2)
        ring.start()
        layout.addWidget(ring)

        self._btn_cancel = PushButton("中断")
        self._btn_cancel.setToolTip("停止本次微信自动化，避免误操作到错误联系人")
        self._btn_cancel.clicked.connect(self._on_cancel_clicked)
        layout.addWidget(self._btn_cancel)

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    def mark_completed(self) -> None:
        """通知弹窗：RPA 已经收到结果，接下来的 close() 不应被当作用户取消。"""
        self._completed = True

    def _on_cancel_clicked(self):
        self._cancel_event.set()
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.setText("正在中断…")

    def closeEvent(self, event: QCloseEvent) -> None:
        # 只有在 RPA 还没出结果时，关闭弹窗才视为「用户主动放弃」；
        # 一旦 mark_completed() 被调用，关闭弹窗只是单纯收尾，不能再
        # 反向触发 cancel_event，否则会把成功的发送误判成「已中断」。
        if not self._completed:
            self._cancel_event.set()
        super().closeEvent(event)

