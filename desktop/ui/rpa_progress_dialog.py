"""RPA 发送进度提示弹窗（非取消型，避免误操作）。"""

from PySide6.QtWidgets import QDialog, QVBoxLayout

from qfluentwidgets import BodyLabel, CaptionLabel, IndeterminateProgressRing


class RpaProgressDialog(QDialog):
    def __init__(self, parent=None, *, title: str = "正在发送", detail: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(320)
        self.setMinimumHeight(140)

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

