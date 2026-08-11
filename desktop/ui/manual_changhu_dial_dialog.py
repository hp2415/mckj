"""客资页手动拨号：数字键盘输入被叫号，点击畅呼主叫号发起外呼。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QApplication,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    LineEdit,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
    TransparentToolButton,
    FluentIcon,
    isDarkTheme,
)


def _normalize_dial_digits(text: str) -> str:
    """保留常见拨号字符，去掉空格与分隔符。"""
    allowed = set("0123456789*#+")
    return "".join(ch for ch in (text or "") if ch in allowed)


_DIAL_KEY_MAP = {
    Qt.Key_0: "0",
    Qt.Key_1: "1",
    Qt.Key_2: "2",
    Qt.Key_3: "3",
    Qt.Key_4: "4",
    Qt.Key_5: "5",
    Qt.Key_6: "6",
    Qt.Key_7: "7",
    Qt.Key_8: "8",
    Qt.Key_9: "9",
    Qt.Key_Asterisk: "*",
    Qt.Key_NumberSign: "#",
    Qt.Key_Plus: "+",
}


class ManualChanghuDialDialog(MessageBoxBase):
    """模拟拨号盘 + 畅呼主叫号列表；点击主叫号即确认外呼。"""

    def __init__(self, parent=None, phones: list[str] | None = None):
        super().__init__(parent)
        self._phones = [str(p).strip() for p in (phones or []) if str(p).strip()]
        self._selected_tel: str | None = None
        self._selected_changhu: str | None = None
        self._focus_armed = False

        self.titleLabel = SubtitleLabel("手动拨号", self)
        hint = CaptionLabel("可直接用键盘输入号码，再点击下方畅呼号码外呼：", self)
        hint.setWordWrap(True)

        self.number_edit = LineEdit(self)
        self.number_edit.setPlaceholderText("请输入电话号码")
        self.number_edit.setClearButtonEnabled(True)
        self.number_edit.setAlignment(Qt.AlignCenter)
        self.number_edit.setFocusPolicy(Qt.StrongFocus)
        num_font = QFont(self.number_edit.font())
        num_font.setPointSize(max(16, num_font.pointSize() + 4))
        num_font.setBold(True)
        self.number_edit.setFont(num_font)
        self.number_edit.setMinimumHeight(44)
        self.number_edit.textChanged.connect(self._on_number_edited)

        pad = QWidget(self)
        pad_layout = QGridLayout(pad)
        pad_layout.setContentsMargins(0, 4, 0, 4)
        pad_layout.setSpacing(8)
        keys = [
            ("1", 0, 0), ("2", 0, 1), ("3", 0, 2),
            ("4", 1, 0), ("5", 1, 1), ("6", 1, 2),
            ("7", 2, 0), ("8", 2, 1), ("9", 2, 2),
            ("*", 3, 0), ("0", 3, 1), ("#", 3, 2),
        ]
        for label, row, col in keys:
            btn = PushButton(label, pad)
            btn.setFixedHeight(40)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            # 点击拨号键不抢焦点，保证物理键盘可持续输入
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(lambda _=False, d=label: self._append_digit(d))
            pad_layout.addWidget(btn, row, col)

        back_row = QHBoxLayout()
        back_row.setSpacing(8)
        self.btn_backspace = TransparentToolButton(FluentIcon.DELETE, self)
        self.btn_backspace.setToolTip("删除末位")
        self.btn_backspace.setFixedSize(36, 36)
        self.btn_backspace.setFocusPolicy(Qt.NoFocus)
        self.btn_backspace.clicked.connect(self._backspace)
        self.btn_clear = PushButton("清空", self)
        self.btn_clear.setFixedHeight(36)
        self.btn_clear.setFocusPolicy(Qt.NoFocus)
        self.btn_clear.clicked.connect(self.number_edit.clear)
        back_row.addStretch(1)
        back_row.addWidget(self.btn_clear)
        back_row.addWidget(self.btn_backspace)

        caller_title = BodyLabel("畅呼外呼号码", self)
        self.caller_hint = CaptionLabel("", self)
        self.caller_hint.setWordWrap(True)
        self._refresh_caller_hint()

        caller_box = QWidget(self)
        caller_layout = QVBoxLayout(caller_box)
        caller_layout.setContentsMargins(0, 0, 0, 0)
        caller_layout.setSpacing(6)
        self._caller_buttons: list[PrimaryPushButton] = []
        for phone in self._phones:
            btn = PrimaryPushButton(FluentIcon.PHONE, phone, caller_box)
            btn.setFixedHeight(40)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(lambda _=False, p=phone: self._on_caller_clicked(p))
            caller_layout.addWidget(btn)
            self._caller_buttons.append(btn)
        if not self._phones:
            empty = CaptionLabel("未配置畅呼号码", caller_box)
            caller_layout.addWidget(empty)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(hint)
        self.viewLayout.addWidget(self.number_edit)
        self.viewLayout.addWidget(pad)
        self.viewLayout.addLayout(back_row)
        self.viewLayout.addWidget(caller_title)
        self.viewLayout.addWidget(self.caller_hint)
        self.viewLayout.addWidget(caller_box)

        # 外呼由点击主叫号触发，隐藏确认按钮
        self.yesButton.hide()
        self.cancelButton.setText("关闭")
        self.cancelButton.setAutoDefault(False)
        self.cancelButton.setDefault(False)
        self.cancelButton.setFocusPolicy(Qt.ClickFocus)
        self.widget.setMinimumWidth(360)
        self.widget.adjustSize()
        self._apply_pad_theme()

    def showEvent(self, event):
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        # 遮罩弹窗布局完成后再抢焦点，避免被关闭按钮抢走
        QTimer.singleShot(0, self._focus_number_edit)
        QTimer.singleShot(50, self._focus_number_edit)

    def hideEvent(self, event):
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().hideEvent(event)

    def _focus_number_edit(self):
        if not self.isVisible():
            return
        self.number_edit.setFocus(Qt.OtherFocusReason)
        self.number_edit.setCursorPosition(len(self.number_edit.text()))
        self._focus_armed = True

    def eventFilter(self, obj, event):
        if (
            event.type() == QEvent.KeyPress
            and self.isVisible()
            and self.isActiveWindow()
            and isinstance(event, QKeyEvent)
        ):
            if self._handle_dial_key(event):
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QKeyEvent):
        if self._handle_dial_key(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def _handle_dial_key(self, event: QKeyEvent) -> bool:
        """全局拦截拨号相关按键，焦点不在输入框时也能输入。"""
        if event.isAutoRepeat() and event.key() not in (Qt.Key_Backspace, Qt.Key_Delete):
            return False

        key = event.key()
        mods = event.modifiers()
        # 允许无修饰键；Ctrl/Alt 组合留给系统（如粘贴）
        if mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return False

        if key in (Qt.Key_Backspace, Qt.Key_Delete):
            # 输入框已有焦点时交给自身处理（支持选区删除）
            if QApplication.focusWidget() is self.number_edit:
                return False
            self._backspace()
            self._focus_number_edit()
            return True

        digit = _DIAL_KEY_MAP.get(key)
        if digit is None:
            text = event.text()
            if text and _normalize_dial_digits(text):
                digit = _normalize_dial_digits(text)
            else:
                return False

        # 已在输入框：让 LineEdit 正常接收，避免重复插入
        if QApplication.focusWidget() is self.number_edit:
            return False

        self._append_digit(digit)
        self._focus_number_edit()
        return True

    def _apply_pad_theme(self):
        dark = isDarkTheme()
        border = "rgba(255,255,255,0.12)" if dark else "rgba(0,0,0,0.08)"
        self.number_edit.setStyleSheet(
            f"LineEdit {{ font-size: 20px; letter-spacing: 1px; border: 1px solid {border}; }}"
        )

    def _refresh_caller_hint(self):
        tel = _normalize_dial_digits(self.number_edit.text())
        if tel:
            self.caller_hint.setText(f"将拨打：{tel}")
        else:
            self.caller_hint.setText("请先输入被叫号码")

    def _on_number_edited(self, _text: str = ""):
        # 粘贴时清洗非法字符
        raw = self.number_edit.text()
        cleaned = _normalize_dial_digits(raw)
        if cleaned != raw:
            cursor = self.number_edit.cursorPosition()
            self.number_edit.blockSignals(True)
            self.number_edit.setText(cleaned)
            self.number_edit.setCursorPosition(min(cursor, len(cleaned)))
            self.number_edit.blockSignals(False)
        self._refresh_caller_hint()

    def _append_digit(self, digit: str):
        current = _normalize_dial_digits(self.number_edit.text())
        if len(current) >= 20:
            return
        self.number_edit.setText(current + digit)
        if self._focus_armed or self.isVisible():
            self.number_edit.setFocus(Qt.OtherFocusReason)
            self.number_edit.setCursorPosition(len(self.number_edit.text()))

    def _backspace(self):
        current = _normalize_dial_digits(self.number_edit.text())
        if current:
            self.number_edit.setText(current[:-1])
        if self.isVisible():
            self.number_edit.setFocus(Qt.OtherFocusReason)
            self.number_edit.setCursorPosition(len(self.number_edit.text()))

    def dialed_tel(self) -> str:
        return _normalize_dial_digits(self.number_edit.text())

    def _on_caller_clicked(self, changhu_tel: str):
        tel = self.dialed_tel()
        caller = (changhu_tel or "").strip()
        if not tel:
            self.caller_hint.setText("请先输入被叫号码后再外呼")
            self._focus_number_edit()
            return
        if not caller:
            return
        self._selected_tel = tel
        self._selected_changhu = caller
        self.accept()

    def selected_call(self) -> tuple[str | None, str | None]:
        return self._selected_tel, self._selected_changhu
