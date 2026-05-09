"""声明当前本机登录的销售微信号（wxid），用于发微信前串号拦截。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QListWidgetItem

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ListWidget,
    PrimaryPushButton,
    PushButton,
)


class LocalWechatClaimDialog(QDialog):
    """从已绑定列表中选择一条作为「本机当前登录的销售微信号」。"""

    def __init__(self, parent=None, rows: list | None = None, preferred_sales_wechat_id: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("声明本机微信")
        self.setMinimumWidth(320)
        self._selected_sw: str | None = None
        self._rows = rows or []

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        hint = CaptionLabel(
            "请选择当前电脑微信已登录的销售微信号（须与客户列表行的业务微信一致后再发送）。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.list_w = ListWidget(self)
        self.list_w.setMinimumHeight(180)
        preferred = (preferred_sales_wechat_id or "").strip()
        preferred_row = -1
        for i, r in enumerate(self._rows):
            sw = str(r.get("sales_wechat_id") or "").strip()
            als = str(r.get("alias_name") or "").strip()
            lab = str(r.get("label") or "").strip()
            disp = als or lab or sw
            line = f"{disp}" + (f"  ({sw})" if sw and disp != sw else "")
            it = QListWidgetItem(line)
            it.setData(Qt.UserRole, sw)
            self.list_w.addItem(it)
            if preferred and sw == preferred:
                preferred_row = i
        if preferred_row >= 0:
            self.list_w.setCurrentRow(preferred_row)

        layout.addWidget(self.list_w)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_ok = PrimaryPushButton("确认")
        self.btn_cancel = PushButton("取消")
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_ok)
        layout.addLayout(btn_row)

        self.btn_ok.clicked.connect(self._on_ok)
        self.btn_cancel.clicked.connect(self.reject)

    def _on_ok(self):
        it = self.list_w.currentItem()
        if not it:
            return
        sw = it.data(Qt.UserRole)
        self._selected_sw = (sw or "").strip() or None
        if self._selected_sw:
            self.accept()

    def selected_sales_wechat_id(self) -> str | None:
        return self._selected_sw
