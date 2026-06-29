"""客户外呼：从多个联系电话中选择本次拨打号码（同步弹窗，须在按钮点击回调中调用）。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListWidgetItem, QWidget

from qfluentwidgets import CaptionLabel, ListWidget, MessageBoxBase, SubtitleLabel

from utils import mask_phone


class CustomerPhonePickerDialog(MessageBoxBase):
    """客户有多个联系电话时，选择本次外呼的被叫号码。"""

    def __init__(self, parent=None, phones: list[str] | None = None):
        super().__init__(parent)
        self._selected_phone: str | None = None
        self._phones = [str(p).strip() for p in (phones or []) if str(p).strip()]

        self.titleLabel = SubtitleLabel("选择拨打号码", self)
        hint = CaptionLabel("该客户有多个联系电话，请选择本次外呼号码：", self)
        hint.setWordWrap(True)

        self.list_w = ListWidget(self)
        self.list_w.setMinimumHeight(max(120, min(240, 44 * len(self._phones))))
        for phone in self._phones:
            it = QListWidgetItem(mask_phone(phone))
            it.setData(Qt.UserRole, phone)
            self.list_w.addItem(it)
        if self._phones:
            self.list_w.setCurrentRow(0)
        self.list_w.itemDoubleClicked.connect(lambda _it: self.yesButton.click())

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(hint)
        self.viewLayout.addWidget(self.list_w)

        self.yesButton.setText("确认")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(360)
        self.widget.adjustSize()

    def validate(self) -> bool:
        it = self.list_w.currentItem()
        if not it:
            return False
        phone = str(it.data(Qt.UserRole) or it.text() or "").strip()
        if not phone:
            return False
        self._selected_phone = phone
        return True

    def selected_phone(self) -> str | None:
        return self._selected_phone


def pick_customer_phone(parent: QWidget | None, phones: list[str]) -> str | None:
    cleaned = [str(p).strip() for p in phones if str(p).strip()]
    if not cleaned:
        return None
    if len(cleaned) == 1:
        return cleaned[0]
    host = parent.window() if parent is not None else parent
    dlg = CustomerPhonePickerDialog(host, cleaned)
    if not dlg.exec():
        return None
    return dlg.selected_phone()
