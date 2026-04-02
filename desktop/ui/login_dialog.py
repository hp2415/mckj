from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QHBoxLayout, QMessageBox
)
from PySide6.QtCore import Qt, Signal

class LoginDialog(QDialog):
    """
    简洁的登录对话框。
    """
    login_requested = Signal(str, str) # 发出 (username, password) 信号

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 微信助手 - 账号登录")
        self.setFixedSize(320, 240)
        self.setWindowFlags(Qt.Window | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        # 标题图/标志
        title = QLabel("欢迎登录后台系统")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 18px; font-weight: bold; margin-bottom: 10px;")
        layout.addWidget(title)

        # 账号输入
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("请输入员工账号")
        self.username_input.setFixedHeight(35)
        self.username_input.setStyleSheet("padding: 5px; border-radius: 4px; border: 1px solid #ccc;")
        layout.addWidget(self.username_input)

        # 密码输入
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("请输入登录密码")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setFixedHeight(35)
        self.password_input.setStyleSheet("padding: 5px; border-radius: 4px; border: 1px solid #ccc;")
        layout.addWidget(self.password_input)

        # 登录按钮
        self.login_btn = QPushButton("立即验证并登录")
        self.login_btn.setFixedHeight(40)
        self.login_btn.setCursor(Qt.PointingHandCursor)
        self.login_btn.setStyleSheet("""
            QPushButton {
                background-color: #007bff;
                color: white;
                font-weight: bold;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #0056b3;
            }
            QPushButton:pressed {
                background-color: #004085;
            }
        """)
        self.login_btn.clicked.connect(self._handle_login_click)
        layout.addWidget(self.login_btn)

    def _handle_login_click(self):
        username = self.username_input.text().strip()
        password = self.password_input.text().strip()
        
        if not username or not password:
            QMessageBox.warning(self, "提示", "请完整填写账号和密码！")
            return
            
        self.login_requested.emit(username, password)
        # 注意：此处不关闭窗口，等待外部 API 回调后再进行 accept() 或 reject()
