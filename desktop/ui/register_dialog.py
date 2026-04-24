from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QMessageBox, QFrame, QTextEdit
from PySide6.QtCore import Qt, Signal
from qfluentwidgets import (
    LineEdit,
    PasswordLineEdit,
    PrimaryPushButton,
    PushButton,
    TitleLabel,
    BodyLabel,
    isDarkTheme,
)


class RegisterDialog(QDialog):
    """自助注册：账号 + 至少一个销售微信号（每行一个）。"""

    register_submitted = Signal(str, str, str, str)  # username, password, real_name, sales_blob

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("注册新账号")
        self.setFixedSize(420, 500)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        card.setObjectName("RegCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(32, 28, 32, 28)
        lay.setSpacing(12)

        title = TitleLabel("创建账号")
        title.setAlignment(Qt.AlignCenter)
        sub = BodyLabel("请填写与云客同步一致的销售微信号")
        sub.setAlignment(Qt.AlignCenter)
        sub.setObjectName("RegSubLbl")

        self.username_input = LineEdit()
        self.username_input.setPlaceholderText("登录工号 / 用户名")
        self.username_input.setFixedHeight(38)

        self.real_name_input = LineEdit()
        self.real_name_input.setPlaceholderText("真实姓名（内部展示）")
        self.real_name_input.setFixedHeight(38)

        self.password_input = PasswordLineEdit()
        self.password_input.setPlaceholderText("密码（至少 6 位）")
        self.password_input.setFixedHeight(38)

        self.sales_edit = QTextEdit()
        self.sales_edit.setPlaceholderText("销售微信号，每行一个（至少一行）\n例如：wxid_xxxx")
        self.sales_edit.setFixedHeight(120)

        self.submit_btn = PrimaryPushButton("提交注册")
        self.submit_btn.setFixedHeight(40)
        self.cancel_btn = PushButton("取消")
        self.cancel_btn.setFixedHeight(36)

        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addWidget(self.username_input)
        lay.addWidget(self.real_name_input)
        lay.addWidget(self.password_input)
        lay.addWidget(BodyLabel("销售微信号"))
        lay.addWidget(self.sales_edit)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.submit_btn)
        lay.addLayout(btn_row)

        root.addWidget(card)

        self.submit_btn.clicked.connect(self._on_submit)
        self.cancel_btn.clicked.connect(self.reject)
        self._apply_style()

    def _apply_style(self):
        is_dark = isDarkTheme()
        bg = "#1a1a1a" if is_dark else "#f0f2f5"
        card_bg = "#2d2d2d" if is_dark else "#ffffff"
        sub_color = "#aaaaaa" if is_dark else "#888888"
        self.setStyleSheet(f"QDialog {{ background-color: {bg}; }}")
        card = self.findChild(QFrame, "RegCard")
        if card:
            card.setStyleSheet(
                f"QFrame#RegCard {{ background-color: {card_bg}; border-radius: 12px; }}"
            )
        sub = self.findChild(BodyLabel, "RegSubLbl")
        if sub:
            sub.setStyleSheet(f"color: {sub_color};")

    def _on_submit(self):
        u = self.username_input.text().strip()
        p = self.password_input.text().strip()
        rn = self.real_name_input.text().strip()
        blob = self.sales_edit.toPlainText().strip()
        if not u or not p or not rn:
            QMessageBox.warning(self, "提示", "请填写用户名、密码与真实姓名。")
            return
        if len(p) < 6:
            QMessageBox.warning(self, "提示", "密码至少 6 位。")
            return
        lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
        if not lines:
            QMessageBox.warning(self, "提示", "请至少填写一个销售微信号。")
            return
        self.register_submitted.emit(u, p, rn, blob)
        # 由外层异步处理成功后再 accept

    def mark_success(self):
        self.accept()
