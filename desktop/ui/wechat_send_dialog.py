"""编辑后发送到微信：多行文本编辑 + 顶部摘要。"""

from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout

from qfluentwidgets import BodyLabel, CaptionLabel, TextEdit, PrimaryPushButton, PushButton


class WechatSendEditDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        original_text: str,
        summary_lines: list[str],
    ):
        super().__init__(parent)
        self.setWindowTitle("编辑后发送")
        self.resize(400, 320)

        layout = QVBoxLayout(self)
        for i, line in enumerate(summary_lines or []):
            lab = BodyLabel(line) if i == 0 else CaptionLabel(line)
            lab.setWordWrap(True)
            layout.addWidget(lab)

        self._edit = TextEdit(self)
        self._edit.setPlainText(original_text or "")
        self._edit.setMinimumHeight(160)
        layout.addWidget(self._edit, 1)

        row = QHBoxLayout()
        row.addStretch()
        btn_cancel = PushButton("取消")
        btn_ok = PrimaryPushButton("确认发送")
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        layout.addLayout(row)

        btn_cancel.clicked.connect(self.reject)
        btn_ok.clicked.connect(self.accept)

    def edited_text(self) -> str:
        return (self._edit.toPlainText() or "").strip()
