"""
AI 聊天核心组件：QuickTextEdit / ChatActionToolbar / ChatBubble / AIChatWidget
对应 UI_implementation.md Phase 4 — AI 聊天界面改造
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QScrollArea,
    QPushButton, QLabel, QFrame, QApplication,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect,
)
from PySide6.QtCore import Qt, Signal, QSize, QTimer
from PySide6.QtGui import QKeyEvent, QColor

from qfluentwidgets import (
    TransparentToolButton, FluentIcon,
)


class QuickTextEdit(QTextEdit):
    """
    专用 IM 输入框：Enter 发送，Ctrl+Enter 换行。
    """
    enter_pressed = Signal()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            if event.modifiers() == Qt.ControlModifier:
                # Ctrl+Enter: 换行
                super().keyPressEvent(event)
            else:
                # Enter: 发送
                self.enter_pressed.emit()
        else:
            super().keyPressEvent(event)


class ChatActionToolbar(QFrame):
    """
    气泡下方的操作工具栏：仅在悬停时显示。
    """
    copy_requested = Signal()
    like_requested = Signal()
    dislike_requested = Signal()
    regenerate_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatActionToolbar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(10)

        # 定义四枚极简图标
        self.btn_copy = self._create_btn("📋", "复制回复", self.copy_requested)
        self.btn_like = self._create_btn("👍", "有帮助", self.like_requested)
        self.btn_dislike = self._create_btn("👎", "不满意", self.dislike_requested)
        self.btn_redo = self._create_btn("🔄", "重新生成", self.regenerate_requested)

        layout.addWidget(self.btn_copy)
        layout.addWidget(self.btn_like)
        layout.addWidget(self.btn_dislike)
        layout.addWidget(self.btn_redo)
        layout.addStretch()

    def _create_btn(self, icon, tooltip, signal):
        btn = QPushButton(icon)
        btn.setObjectName("ActionIconBtn")
        btn.setFixedSize(18, 18)
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(signal.emit)
        return btn


class ChatBubble(QWidget):
    """
    单个聊天气泡组件：支持悬停工具栏 (仅 AI 回复)。
    """
    copy_triggered = Signal(str)        # 用于本地剪贴板
    copy_event_triggered = Signal(int)  # 用于云端采纳记录 (msg_id)
    feedback_triggered = Signal(int, int)  # (msg_id, rating)
    regenerate_triggered = Signal()

    def __init__(self, text, is_user=False, msg_id=None, rating=0, parent=None):
        super().__init__(parent)
        self.is_user = is_user
        self.msg_id = msg_id
        self.current_rating = rating

        self.main_v_layout = QVBoxLayout(self)
        self.main_v_layout.setContentsMargins(6, 4, 6, 4)
        self.main_v_layout.setSpacing(2)

        # 1. 气泡层 (水平布局控制对齐)
        self.bubble_h_layout = QHBoxLayout()
        self.bubble_h_layout.setContentsMargins(0, 0, 0, 0)

        self.label = QLabel(text)
        self.label.setWordWrap(True)
        # 取消直接复制：移除交互标志，使用户必须点击工具栏复制按钮
        self.label.setTextInteractionFlags(Qt.NoTextInteraction)

        # 微光投影
        self.shadow = QGraphicsDropShadowEffect(self.label)
        self.shadow.setBlurRadius(8)
        self.shadow.setXOffset(0)
        self.shadow.setYOffset(1)
        self.shadow.setColor(QColor(0, 0, 0, 15))
        self.label.setGraphicsEffect(self.shadow)

        common_style = "padding: 8px 12px; border-radius: 8px; font-size: 13px; line-height: 1.4;"
        if is_user:
            self.bubble_h_layout.addStretch()
            self.label.setStyleSheet(f"{common_style} background-color: #95ec69; color: #000;")
            self.bubble_h_layout.addWidget(self.label)
        else:
            self.label.setStyleSheet(
                f"{common_style} background-color: #ffffff; color: #1f1f1f; border: 1px solid #ebebeb;"
            )
            self.bubble_h_layout.addWidget(self.label)
            self.bubble_h_layout.addStretch()

        self.main_v_layout.addLayout(self.bubble_h_layout)

        # 2. 工具栏层 (仅非用户消息显示)
        self.toolbar = None
        if not is_user:
            self.toolbar = ChatActionToolbar()
            # 使用透明度滤镜实现"预留空间"但不显示，避免抖动
            self.opacity_effect = QGraphicsOpacityEffect(self.toolbar)
            self.opacity_effect.setOpacity(0.0)
            self.toolbar.setGraphicsEffect(self.opacity_effect)

            # 绑定信号转发
            self.toolbar.copy_requested.connect(lambda: self._handle_copy())
            self.toolbar.like_requested.connect(lambda: self._emit_feedback(1))
            self.toolbar.dislike_requested.connect(lambda: self._emit_feedback(-1))
            self.toolbar.regenerate_requested.connect(self.regenerate_triggered.emit)

            # 工具栏对齐气泡左侧，并预留固定高度
            toolbar_layout = QHBoxLayout()
            toolbar_layout.setContentsMargins(8, 2, 0, 4)
            toolbar_layout.addWidget(self.toolbar)
            toolbar_layout.addStretch()
            self.main_v_layout.addLayout(toolbar_layout)

            # 如果存在历史评价，初始化按钮状态
            if rating != 0:
                self._apply_rating_ui(rating)

    def _apply_rating_ui(self, rating):
        """根据评分值点亮图标视觉 (1, -1, 0)"""
        if not self.toolbar:
            return

        self.toolbar.btn_like.setProperty("active", "true" if rating == 1 else "false")
        self.toolbar.btn_dislike.setProperty("active", "true" if rating == -1 else "false")

        self.toolbar.btn_like.style().unpolish(self.toolbar.btn_like)
        self.toolbar.btn_like.style().polish(self.toolbar.btn_like)
        self.toolbar.btn_dislike.style().unpolish(self.toolbar.btn_dislike)
        self.toolbar.btn_dislike.style().polish(self.toolbar.btn_dislike)

    def _handle_copy(self):
        self.copy_triggered.emit(self.label.text())
        if self.msg_id:
            self.copy_event_triggered.emit(self.msg_id)

        # 点击反馈：将图标变色并锁定
        self.toolbar.btn_copy.setProperty("active", "true")
        self.toolbar.btn_copy.style().unpolish(self.toolbar.btn_copy)
        self.toolbar.btn_copy.style().polish(self.toolbar.btn_copy)

    def _emit_feedback(self, rating):
        if self.msg_id:
            # 如果点击的是已选中的，则视为取消评价 (0)
            target_rating = 0 if self.current_rating == rating else rating
            self.current_rating = target_rating
            self.feedback_triggered.emit(self.msg_id, target_rating)
            self._apply_rating_ui(target_rating)

    def enterEvent(self, event):
        """鼠标进入时：透明度渐现"""
        if self.toolbar and not self.is_user:
            self.opacity_effect.setOpacity(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开时：恢复透明"""
        if self.toolbar:
            self.opacity_effect.setOpacity(0.0)
        super().leaveEvent(event)

    def append_text(self, new_text):
        self.label.setText(self.label.text() + new_text)
        # 寻找并通知父级滚动条刷新
        p = self.parentWidget()
        while p and not hasattr(p, "scroll_to_bottom"):
            p = p.parentWidget()
        if p:
            p.scroll_to_bottom()


class AIChatWidget(QWidget):
    """
    AI 智能对话主面板：适配窄屏，支持回车发送。
    """
    send_requested = Signal(str)
    copy_event_triggered = Signal(int)      # 复制事件信号
    feedback_requested = Signal(int, int)   # msg_id, rating
    regenerate_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("ChatScrollArea")
        self.scroll_area.setWidgetResizable(True)

        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.addStretch()
        self.chat_layout.setSpacing(12)

        self.scroll_area.setWidget(self.chat_container)
        layout.addWidget(self.scroll_area)

        # 监听滚动条范围变化，实现自动触底
        self.scroll_area.verticalScrollBar().rangeChanged.connect(self.scroll_to_bottom)

        # 输入区域 (IM 风格)
        input_container = QFrame()
        input_container.setObjectName("ChatInputContainer")
        input_container.setFixedHeight(130)
        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(10, 5, 10, 5)

        self.input_edit = QuickTextEdit()
        self.input_edit.setObjectName("ChatInput")
        self.input_edit.setPlaceholderText("请输入问题... (Enter 发送, Ctrl+Enter 换行)")
        self.input_edit.enter_pressed.connect(self._on_send_clicked)
        input_layout.addWidget(self.input_edit)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("SendBtn")
        self.send_btn.setFixedSize(76, 40)
        self.send_btn.clicked.connect(self._on_send_clicked)
        btn_layout.addWidget(self.send_btn)
        input_layout.addLayout(btn_layout)

        layout.addWidget(input_container)

    def add_message(self, text, is_user=False, msg_id=None, rating=0):
        bubble = ChatBubble(text, is_user, msg_id, rating)

        # 绑定信号接力
        bubble.copy_triggered.connect(lambda t: QApplication.clipboard().setText(t))
        bubble.copy_event_triggered.connect(self.copy_event_triggered.emit)
        bubble.feedback_triggered.connect(self.feedback_requested.emit)
        bubble.regenerate_triggered.connect(self.regenerate_requested.emit)

        # 动态计算气泡最大宽度 (容器宽度的 90%)
        max_w = int(self.width() * 0.9)
        if max_w > 50:
            bubble.label.setMaximumWidth(max_w)

        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        QTimer.singleShot(50, self.scroll_to_bottom)
        return bubble

    def resizeEvent(self, event):
        """窗口缩放时，动态调整所有已有气泡的最大宽度"""
        super().resizeEvent(event)
        new_max_w = int(self.width() * 0.8)
        if new_max_w < 100:
            return

        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), ChatBubble):
                item.widget().label.setMaximumWidth(new_max_w)

    def scroll_to_bottom(self):
        """将滚动条拉到最底部记录"""
        bar = self.scroll_area.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_send_clicked(self):
        text = self.input_edit.toPlainText().strip()
        if text:
            self.send_requested.emit(text)
            self.input_edit.clear()

    def clear(self):
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
