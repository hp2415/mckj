"""畅呼外呼：选择主叫号码（同步弹窗，须在按钮点击回调中调用）。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListWidgetItem, QWidget

from qfluentwidgets import CaptionLabel, ListWidget, MessageBoxBase, SubtitleLabel


def resolve_changhu_phones(widget: QWidget | None) -> list[str]:
    win = widget.window() if widget is not None else None
    phones = getattr(win, "_mibuddy_changhu_phones", None) or []
    return [str(p).strip() for p in phones if str(p).strip()]


class ChanghuPhonePickerDialog(MessageBoxBase):
    """从米城账号绑定的畅呼号码中选择本次外呼主叫号（Fluent 遮罩弹窗）。"""

    def __init__(self, parent=None, phones: list[str] | None = None):
        super().__init__(parent)
        self._selected_phone: str | None = None
        self._phones = [str(p).strip() for p in (phones or []) if str(p).strip()]

        self.titleLabel = SubtitleLabel("选择畅呼号码", self)
        hint = CaptionLabel("请选择本次外呼使用的畅呼主叫号码：", self)
        hint.setWordWrap(True)

        self.list_w = ListWidget(self)
        self.list_w.setMinimumHeight(max(120, min(240, 44 * len(self._phones))))
        for phone in self._phones:
            it = QListWidgetItem(phone)
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


def pick_changhu_tel(parent: QWidget | None) -> str | None:
    phones = resolve_changhu_phones(parent)
    if not phones:
        return None
    if len(phones) == 1:
        return phones[0]
    host = parent.window() if parent is not None else parent
    dlg = ChanghuPhonePickerDialog(host, phones)
    if not dlg.exec():
        return None
    return dlg.selected_phone()
