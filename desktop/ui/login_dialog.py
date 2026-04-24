from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QMessageBox, QWidget, QFrame
from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QColor

from qfluentwidgets import (
    LineEdit, PasswordLineEdit, PrimaryPushButton, HyperlinkButton,
    TitleLabel, BodyLabel, setTheme, Theme,
    isDarkTheme
)
from qfluentwidgets import FluentStyleSheet


class LoginDialog(QDialog):
    """
    现代化登录对话框 —— QFluentWidgets 风格。
    """
    login_requested = Signal(str, str)
    open_register_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 微信助手 - 账号登录")
        self.setFixedSize(380, 340)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowMinimizeButtonHint)

        # ── 根容器 ──────────────────────────────────────────────
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 卡片面板 ────────────────────────────────────────────
        card = QFrame()
        card.setObjectName("LoginCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(40, 36, 40, 36)
        card_layout.setSpacing(16)

        # 标题
        self.title_lbl = TitleLabel("欢迎登录")
        self.title_lbl.setAlignment(Qt.AlignCenter)

        # 副标题
        self.sub_lbl = BodyLabel("AI 微企助手")
        self.sub_lbl.setAlignment(Qt.AlignCenter)
        self.sub_lbl.setObjectName("SubtitleLbl")

        # 账号输入
        self.username_input = LineEdit()
        self.username_input.setPlaceholderText("请输入员工账号")
        self.username_input.setFixedHeight(40)
        self.username_input.setClearButtonEnabled(True)

        # 密码输入
        self.password_input = PasswordLineEdit()
        self.password_input.setPlaceholderText("请输入登录密码")
        self.password_input.setFixedHeight(40)

        # 登录按钮
        self.login_btn = PrimaryPushButton("立即验证并登录")
        self.login_btn.setFixedHeight(42)
        self.login_btn.setCursor(Qt.PointingHandCursor)

        # 组装
        card_layout.addWidget(self.title_lbl)
        card_layout.addWidget(self.sub_lbl)
        card_layout.addSpacing(4)
        card_layout.addWidget(self.username_input)
        card_layout.addWidget(self.password_input)
        card_layout.addSpacing(4)
        card_layout.addWidget(self.login_btn)

        # HyperlinkButton(url, text) 需合法签名；无外链时用 parent 构造 + 空 QUrl，避免点击打开浏览器
        self.register_link = HyperlinkButton(self)
        self.register_link.setText("没有账号？注册")
        self.register_link.setUrl(QUrl())
        self.register_link.setCursor(Qt.PointingHandCursor)
        card_layout.addWidget(self.register_link, alignment=Qt.AlignCenter)

        root.addWidget(card)

        # ── 信号 ────────────────────────────────────────────────
        self.login_btn.clicked.connect(self._handle_login_click)
        self.register_link.clicked.connect(self.open_register_requested.emit)
        self.password_input.returnPressed.connect(self._handle_login_click)
        self.username_input.returnPressed.connect(self.password_input.setFocus)

        # ── 样式 ────────────────────────────────────────────────
        self._apply_style()

    def _apply_style(self):
        """根据当前主题应用背景色与卡片样式。"""
        is_dark = isDarkTheme()
        if is_dark:
            bg = "#1a1a1a"
            card_bg = "#2d2d2d"
            sub_color = "#aaaaaa"
            title_color = "#ffffff"
        else:
            bg = "#f0f2f5"
            card_bg = "#ffffff"
            sub_color = "#888888"
            title_color = "#1a1a1a"

        self.setStyleSheet(f"QDialog {{ background-color: {bg}; }}")
        card = self.findChild(QFrame, "LoginCard")
        if card:
            card.setStyleSheet(
                f"""
                QFrame#LoginCard {{
                    background-color: {card_bg};
                    border-radius: 12px;
                }}
                """
            )
        self.sub_lbl.setStyleSheet(f"color: {sub_color};")
        self.title_lbl.setStyleSheet(f"color: {title_color};")

    def _handle_login_click(self):
        username = self.username_input.text().strip()
        password = self.password_input.text().strip()
        if not username or not password:
            QMessageBox.warning(self, "提示", "请完整填写账号和密码！")
            return
        self.login_requested.emit(username, password)
