"""
AI 聊天核心组件：QuickTextEdit / ChatActionToolbar / ChatBubble / AIChatWidget
对应 UI_implementation.md Phase 4 — AI 聊天界面改造
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QScrollArea,
    QPushButton, QLabel, QFrame, QApplication,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QTimer, QPoint
from PySide6.QtGui import QKeyEvent, QColor, QAction, QActionGroup, QFont

from config_loader import cfg

from qfluentwidgets import (
    TransparentToolButton, FluentIcon, SmoothScrollArea,
    TextEdit, PrimaryPushButton, IndeterminateProgressRing,
    isDarkTheme, ComboBox, CheckableMenu,
    MenuAnimationType, MenuIndicatorType,
)

# 无后端配置时的桌面端回退（与 backend/ai/chat_models_catalog.py 默认一致）
FALLBACK_LLM_CHAT_MODEL_OPTIONS = (
    ("qwen3.5-plus", "通义千问 3.5 Plus"),
    ("deepseek-v3.2", "DeepSeek V3.2"),
    ("gpt-5.4", "GPT-5.4"),
)

SCENARIO_LABELS = {
    # 兜底：后端未返回动态场景时使用
    "general_chat": "自由对话",
    "product_recommend": "推品报价",
    "model_identity": "模型说明",
}


class QuickTextEdit(TextEdit):
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

    def _create_btn(self, icon, tooltip, signal):
        btn = TransparentToolButton(icon)
        btn.setObjectName("ActionIconBtn")
        btn.setFixedSize(24, 24)
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(signal.emit)
        return btn

    def _set_active_style(self, btn, active):
        """直接设置样式表并利用 checkable 状态确保底色 100% 显示"""
        btn.setCheckable(True)
        btn.setChecked(active)
        if active:
            # Phase 4.8: 使用具体选择器防止样式泄露给 ToolTip (悬浮文字)
            btn.setStyleSheet("""
                TransparentToolButton {
                    background-color: rgba(7, 193, 110, 0.25);
                    border: 1px solid rgba(7, 193, 110, 0.4);
                    border-radius: 4px;
                }
            """)
        else:
            btn.setStyleSheet("TransparentToolButton { background-color: transparent; border: none; }")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatActionToolbar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        # 定义四枚极简图标
        self.btn_copy = self._create_btn(FluentIcon.COPY, "复制回复", self.copy_requested)
        self.btn_like = self._create_btn(FluentIcon.HEART, "有帮助", self.like_requested)
        self.model_tag = QLabel("")
        self.model_tag.setObjectName("ModelTagLabel")
        self.model_tag.setVisible(False)
        self.btn_dislike = self._create_btn(FluentIcon.REMOVE, "不满意", self.dislike_requested)
        self.btn_redo = self._create_btn(FluentIcon.SYNC, "重新生成", self.regenerate_requested)

        layout.addWidget(self.btn_copy)
        layout.addWidget(self.btn_like)
        layout.addWidget(self.model_tag)
        layout.addWidget(self.btn_dislike)
        layout.addWidget(self.btn_redo)
        layout.addStretch()

    def set_model_tag(self, text: str):
        t = (text or "").strip()
        self.model_tag.setText(t)
        self.model_tag.setVisible(bool(t))
        # 让“模型”信息是弱化的辅助信息：与点赞按钮紧邻但不喧宾夺主
        is_dark = isDarkTheme()
        col = "#a7c0ff" if is_dark else "#3b6ea5"
        self.model_tag.setStyleSheet(
            f"QLabel#ModelTagLabel {{ color: {col}; font-size: 11px; padding: 0px 4px; }}"
        )


class ChatBubble(QWidget):
    """
    单个聊天气泡组件：支持悬停工具栏 (仅 AI 回复)。
    """
    copy_triggered = Signal(str)        # 用于本地剪贴板
    copy_event_triggered = Signal(int)  # 用于云端采纳记录 (msg_id)
    feedback_triggered = Signal(int, int)  # (msg_id, rating)
    regenerate_triggered = Signal(str)     # (user_query)
    stream_chunk_appended = Signal()  # AI 流式追加后通知外层吸底

    def __init__(
        self,
        text,
        is_user: bool = False,
        msg_id=None,
        rating: int = 0,
        user_query: str = "",
        model_tag: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.is_user = is_user
        self.msg_id = msg_id
        self.current_rating = rating
        self.user_query = user_query # 关联的提问文本
        self.model_tag = model_tag or ""

        self.main_v_layout = QVBoxLayout(self)
        self.main_v_layout.setContentsMargins(6, 4, 6, 4)
        self.main_v_layout.setSpacing(2)

        # 1. 气泡层 (水平布局控制对齐)
        self.bubble_h_layout = QHBoxLayout()
        self.bubble_h_layout.setContentsMargins(0, 0, 0, 0)
        self.bubble_h_layout.setSpacing(0)

        # 气泡容器 (Frame) - 支持内部多组件布局
        self.bubble_frame = QFrame()
        self.bubble_frame.setObjectName("BubbleFrame")
        self.bubble_layout = QVBoxLayout(self.bubble_frame)
        self.bubble_layout.setContentsMargins(10, 12, 10, 12)
        self.bubble_layout.setSpacing(8)

        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.NoTextInteraction)
        self.bubble_layout.addWidget(self.label)

        # 内置加载层 (默认隐藏)
        self.loading_widget = QWidget()
        self.loading_inner_layout = QHBoxLayout(self.loading_widget)
        self.loading_inner_layout.setContentsMargins(0, 0, 0, 0)
        self.loading_inner_layout.setSpacing(8)
        self.loading_ring = IndeterminateProgressRing(self)
        self.loading_ring.setFixedSize(14, 14)
        self.loading_ring.setStrokeWidth(2)
        self.loading_inner_layout.addWidget(self.loading_ring)
        self.loading_inner_layout.addWidget(QLabel("AI 正在思考..."))
        self.loading_inner_layout.addStretch()
        self.loading_widget.hide()
        self.bubble_layout.addWidget(self.loading_widget)

        # 微光投影
        self.shadow = QGraphicsDropShadowEffect(self.bubble_frame)
        self.shadow.setBlurRadius(8)
        self.shadow.setXOffset(0)
        self.shadow.setYOffset(1)
        self.shadow.setColor(QColor(0, 0, 0, 15))
        self.bubble_frame.setGraphicsEffect(self.shadow)

        self.main_v_layout.addLayout(self.bubble_h_layout)
        
        # 应用初始样式（包括将 bubble_frame 添加到 bubble_h_layout）
        self._apply_theme_style()

        # 情况处理：如果初始文本为空且为 AI 消息，则自动进入加载状态
        if not text and not is_user:
            self.set_loading(True)
            self.label.hide()

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
            self.toolbar.regenerate_requested.connect(self._handle_redo)

            # 工具栏对齐气泡左侧，并预留固定高度
            toolbar_layout = QHBoxLayout()
            toolbar_layout.setContentsMargins(8, 2, 0, 4)
            toolbar_layout.addWidget(self.toolbar)
            toolbar_layout.addStretch()
            self.main_v_layout.addLayout(toolbar_layout)

            # 如果存在历史评价，初始化按钮状态
            if rating != 0:
                self._apply_rating_ui(rating)

            # 模型标签（展示在点赞旁边；历史/实时均可写入）
            if self.model_tag:
                self.toolbar.set_model_tag(self.model_tag)

    def set_model_tag(self, text: str):
        """运行中更新模型标签（服务端 meta 可能回写实际模型）。"""
        self.model_tag = text or ""
        if self.toolbar:
            self.toolbar.set_model_tag(self.model_tag)

    def _apply_theme_style(self):
        """动态同步深浅主题背景与文字颜色，并确保气泡对齐正确"""
        is_dark = isDarkTheme()
        common_style = "border-radius: 10px; font-size: 13px; line-height: 1.45;"
        
        # 每次调用时，先清空 bubble_h_layout 中的内容（避免重复添加 frame）
        while self.bubble_h_layout.count():
            self.bubble_h_layout.takeAt(0)
        
        if self.is_user:
            # 用户气泡：右对齐，先展开展再添加内容
            bg_color = "#2bae60" if is_dark else "#95ec69"
            text_color = "#ffffff" if is_dark else "#1a1a1a"
            self.bubble_frame.setStyleSheet(f"QFrame#BubbleFrame {{ {common_style} background-color: {bg_color}; border: none; }}")
            self.label.setStyleSheet(f"background: transparent; color: {text_color}; font-weight: normal;")
            self.bubble_h_layout.addStretch()
            self.bubble_h_layout.addWidget(self.bubble_frame)
        else:
            # AI 气泡：左对齐，内容在左
            bg_color = "#2c2c2c" if is_dark else "#ffffff"
            text_color = "#e5e5e5" if is_dark else "#202020"
            border_color = "rgba(255, 255, 255, 0.1)" if is_dark else "rgba(0, 0, 0, 0.12)"
            self.bubble_frame.setStyleSheet(
                f"QFrame#BubbleFrame {{ {common_style} background-color: {bg_color}; color: {text_color}; border: 1px solid {border_color}; }}"
            )
            self.label.setStyleSheet(f"background: transparent; color: {text_color};")
            self.bubble_h_layout.addWidget(self.bubble_frame)
            self.bubble_h_layout.addStretch()
            
        # 更新投影颜色 (深色模式下投影应极淡)
        shadow_opacity = 5 if is_dark else 18
        self.shadow.setColor(QColor(0, 0, 0, shadow_opacity))

    def _apply_rating_ui(self, rating):
        """根据评分值点亮图标视觉 (1, -1, 0)"""
        if not self.toolbar:
            return

        self.toolbar._set_active_style(self.toolbar.btn_like, rating == 1)
        self.toolbar._set_active_style(self.toolbar.btn_dislike, rating == -1)

    def _handle_redo(self):
        self.regenerate_triggered.emit(self.user_query)

    def _handle_copy(self):
        self.copy_triggered.emit(self.label.text())
        if self.msg_id:
            self.copy_event_triggered.emit(self.msg_id)

        # 点击反馈：直接变色
        self.toolbar._set_active_style(self.toolbar.btn_copy, True)

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

    def set_loading(self, is_active: bool):
        """局部切换此气泡的加载/文本显示状态"""
        if is_active:
            self.label.hide()
            self.loading_widget.show()
            self.loading_ring.start()
        else:
            self.loading_ring.stop()
            self.loading_widget.hide()
            self.label.show()

    def show_error(self, message):
        """显示错误信息并提供重试按钮"""
        self.set_loading(False)
        self.label.setStyleSheet("background: transparent; color: #d93025; font-weight: bold;")
        self.label.setText(f"⚠️ {message}")
        
        # 如果还没创建过重试按钮，则动态注入
        if not hasattr(self, "retry_btn"):
            self.retry_btn = PrimaryPushButton(FluentIcon.SYNC, "重新回答", self)
            self.retry_btn.setFixedWidth(120)
            self.retry_btn.clicked.connect(lambda: self.regenerate_triggered.emit(self.user_query))
            self.bubble_layout.addWidget(self.retry_btn)

    def append_text(self, new_text):
        """流式追加文本，并自动关闭加载状态"""
        if self.loading_widget.isVisible():
            self.set_loading(False)
            self.label.show()

        self.label.setText(self.label.text() + new_text)
        if not self.is_user:
            self.stream_chunk_appended.emit()


class AIChatWidget(QWidget):
    """
    AI 智能对话主面板：适配窄屏，支持回车发送。
    """
    send_requested = Signal(str)
    history_requested = Signal()            # 请求历史记录
    copy_event_triggered = Signal(int)      # 复制事件信号
    feedback_requested = Signal(int, int)   # msg_id, rating
    regenerate_requested = Signal(str)      # (user_query)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.scroll_area = SmoothScrollArea()
        self.scroll_area.setObjectName("ChatScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { background-color: transparent; border: none; }")
        
        self.chat_container = QWidget()
        self.chat_container.setObjectName("ChatContainer")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.addStretch()
        self.chat_layout.setSpacing(16)

        self.scroll_area.setWidget(self.chat_container)
        layout.addWidget(self.scroll_area)
        
        # 确保整体透明，继承 MainWindow 设置的背景色
        self.setStyleSheet("QWidget { background: transparent; border: none; }")

        # 增加一个防抖定时器，用于在批量添加消息后统一触底
        self.scroll_timer = QTimer(self)
        self.scroll_timer.setSingleShot(True)
        self.scroll_timer.timeout.connect(lambda: self.scroll_to_bottom(instant=self._is_batch_loading))
        
        # Phase 4.7: 动态绑定范围变化，确保加载过程中坐标实时对齐
        self.scroll_area.verticalScrollBar().rangeChanged.connect(self._handle_range_changed)
        # 监听滚动条，实现上划加载更多
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)
        self._is_batch_loading = False
        # 流式回复吸底：仅在插入前已在底部时跟随；用户上滑后不再强拽（避免滚轮「乱飞」）
        self._follow_bottom_for_stream = False
        self._stream_scroll_debounce = QTimer(self)
        self._stream_scroll_debounce.setSingleShot(True)
        self._stream_scroll_debounce.timeout.connect(self._flush_stream_scroll_to_bottom)

        # 输入区域：已选模型/场景写在占位符第二行；工具栏紧凑排列
        input_container = QFrame()
        input_container.setObjectName("ChatInputContainer")
        input_container.setMinimumHeight(108)
        input_container.setMaximumHeight(180)
        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(8, 5, 8, 5)
        input_layout.setSpacing(4)

        self.input_edit = QuickTextEdit()
        self.input_edit.setObjectName("ChatInput")
        self.input_edit.enter_pressed.connect(self._on_send_clicked)
        # 高度约为原 52px 的 2/3（减小三分之一）
        self.input_edit.setMinimumHeight(35)
        input_layout.addWidget(self.input_edit, 1)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.addStretch(1)

        _tb_size = 26
        _model_icon = FluentIcon.ROBOT if hasattr(FluentIcon, "ROBOT") else FluentIcon.APPLICATION
        self.chat_model_btn = TransparentToolButton(_model_icon)
        self.chat_model_btn.setObjectName("ChatModelBtn")
        self.chat_model_btn.setFixedSize(_tb_size, _tb_size)
        self.chat_model_btn.clicked.connect(self._open_chat_model_menu)
        # self.chat_model_btn.hide()  # 模型选择按钮隐藏
        btn_layout.addWidget(self.chat_model_btn, 0, Qt.AlignVCenter)

        self.scenario_combo = ComboBox()
        self._scenario_options: list[tuple[str, str]] = [
            ("general_chat", "自由对话"),
            ("product_recommend", "推品报价"),
        ]
        self._scenario_label_to_key = {lb: k for k, lb in self._scenario_options}
        self._scenario_key_to_label = {k: lb for k, lb in self._scenario_options}
        self.scenario_combo.addItems([lb for _, lb in self._scenario_options])
        self.scenario_combo.setMinimumWidth(80)
        self.scenario_combo.setMaximumWidth(100)
        self.scenario_combo.setFixedHeight(_tb_size)
        self.scenario_combo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.scenario_combo.setCurrentIndex(0)
        self.scenario_combo.currentIndexChanged.connect(self._on_scenario_placeholder_refresh)
        btn_layout.addWidget(self.scenario_combo, 0, Qt.AlignVCenter)

        self.history_btn = TransparentToolButton(FluentIcon.HISTORY)
        self.history_btn.setToolTip("查看历史聊天记录")
        self.history_btn.setFixedSize(_tb_size, _tb_size)
        self.history_btn.clicked.connect(self.history_requested.emit)
        btn_layout.addWidget(self.history_btn, 0, Qt.AlignVCenter)

        self.clear_btn = TransparentToolButton(FluentIcon.BROOM)
        self.clear_btn.setToolTip("清空当前对话显示")
        self.clear_btn.setFixedSize(_tb_size, _tb_size)
        self.clear_btn.clicked.connect(self.clear)
        btn_layout.addWidget(self.clear_btn, 0, Qt.AlignVCenter)

        self.send_btn = PrimaryPushButton(FluentIcon.SEND, "发送")
        self.send_btn.setObjectName("SendBtn")
        self.send_btn.setMinimumWidth(64)
        self.send_btn.setMaximumWidth(82)
        self.send_btn.setFixedHeight(_tb_size)
        self.send_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.send_btn.clicked.connect(self._on_send_clicked)
        btn_layout.addWidget(self.send_btn, 0, Qt.AlignVCenter)

        input_layout.addLayout(btn_layout)

        self._style_chat_toolbar_fonts()

        layout.addWidget(input_container)

        self._placeholder_meta_suffix = None  # 服务端首包 meta，临时写入占位符第二行
        self._chat_model_options: list[tuple[str, str]] = list(FALLBACK_LLM_CHAT_MODEL_OPTIONS)
        self._chat_model_ids: list[str] = [self._chat_model_options[0][0]]
        self._load_chat_model_from_cfg()

    def add_message(
        self,
        text,
        is_user: bool = False,
        msg_id=None,
        rating: int = 0,
        user_query: str = "",
        model_tag: str = "",
    ):
        bubble = ChatBubble(text, is_user, msg_id, rating, user_query, model_tag=model_tag)

        # 绑定信号接力
        bubble.copy_triggered.connect(lambda t: QApplication.clipboard().setText(t))
        bubble.copy_event_triggered.connect(self.copy_event_triggered.emit)
        bubble.feedback_triggered.connect(self.feedback_requested.emit)
        bubble.regenerate_triggered.connect(self.regenerate_requested.emit)

        # 动态计算气泡最大宽度 (容器宽度的 90%)
        max_w = int(self.width() * 0.9)
        if max_w > 50:
            bubble.label.setMaximumWidth(max_w)

        # 在插入前判断是否在底部（插入后 maximum 变大，用旧值判断才准确）
        bar = self.scroll_area.verticalScrollBar()
        old_max = bar.maximum()
        old_val = bar.value()
        stick_to_bottom = old_max <= 0 or (old_max - old_val) <= 48

        # 始终插在伸缩量上面 (即最底部)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)

        # 防抖滚动：如果 150ms 内连续调用 add_message，定时器会不断重置，仅最后一次生效
        # 触底逻辑加固 (Phase 4.5): 
        # 1. 如果是批量加载历史，完全跳过定时器（由 main.py 统一手动触底），防止后台 50 个定时器排队冲突
        if self._is_batch_loading:
            return bubble

        if is_user:
            self._follow_bottom_for_stream = True
            self.scroll_timer.start(30)
        else:
            self._follow_bottom_for_stream = stick_to_bottom
            if stick_to_bottom:
                self.scroll_timer.start(120)
            bubble.stream_chunk_appended.connect(self._on_stream_chunk_appended)

        return bubble

    def prepend_message(
        self,
        text,
        is_user: bool = False,
        msg_id=None,
        rating: int = 0,
        user_query: str = "",
        model_tag: str = "",
    ):
        """在聊天区域顶部插入消息 (用于加载更早的历史记录)"""
        bubble = ChatBubble(text, is_user, msg_id, rating, user_query, model_tag=model_tag)
        bubble.copy_triggered.connect(lambda t: QApplication.clipboard().setText(t))
        bubble.copy_event_triggered.connect(self.copy_event_triggered.emit)
        bubble.feedback_triggered.connect(self.feedback_requested.emit)
        bubble.regenerate_triggered.connect(self.regenerate_requested.emit)

        # 动态计算气泡最大宽度
        max_w = int(self.width() * 0.9)
        if max_w > 50:
            bubble.label.setMaximumWidth(max_w)

        # 插入到最顶部 (跳过 index 0 的弹簧，或者如果弹簧在最后，直接插在 index 0)
        # 当前布局: [bubble1, bubble2, ..., spacer]
        # 我们想变成: [new_bubble, bubble1, bubble2, ..., spacer]
        self.chat_layout.insertWidget(0, bubble)
        return bubble

    def _on_stream_chunk_appended(self):
        """AI 流式输出：仅在用户未主动上滑离开时瞬时吸底（无动画，避免与滚轮争抢）。"""
        if self._follow_bottom_for_stream:
            self._stream_scroll_debounce.start(55)

    def _flush_stream_scroll_to_bottom(self):
        if self._follow_bottom_for_stream:
            self.scroll_to_bottom(instant=True)

    def _on_scroll_value_changed(self, value):
        """当滚动条到达顶部时，触发加载更多信号"""
        bar = self.scroll_area.verticalScrollBar()
        if not self._is_batch_loading and bar.maximum() > 0:
            if (bar.maximum() - value) > 72:
                self._follow_bottom_for_stream = False
        if value == 0 and not self._is_batch_loading:
            # 只有在已经加载过历史记录的情况下才允许自动触发上拉加载
            # 这里可以由外部逻辑控制是否启用
            pass

    def _handle_range_changed(self, min_val, max_val):
        """处理滚动条范围变化：主要用于批量加载时的自动吸底"""
        if self._is_batch_loading and not getattr(self, "_is_prepending", False):
            if hasattr(self.scroll_area, "delegate"):
                self.scroll_area.delegate.vScrollBar.scrollTo(max_val, useAni=False)
            else:
                self.scroll_area.verticalScrollBar().setValue(max_val)

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

    def scroll_to_bottom(self, instant=False):
        """将滚动条拉到最底部记录"""
        # 0. 强行杀掉正在排队的自动滚动任务
        self.scroll_timer.stop()

        # 1. 强制同步布局计算
        self.chat_container.adjustSize()
        
        bar = self.scroll_area.verticalScrollBar()
        max_val = bar.maximum()

        # 直接操作 Delegate 的专属滚动条，以重置其内部的 "__value" 状态，彻底断绝“滑动飞天”漏洞
        if hasattr(self.scroll_area, "delegate"):
            self.scroll_area.delegate.vScrollBar.scrollTo(max_val, useAni=not instant)
        else:
            if instant:
                if hasattr(self.scroll_area, "setScrollAnimation"):
                    self.scroll_area.setScrollAnimation(Qt.Vertical, 0)
                bar.setValue(max_val)
                if hasattr(self.scroll_area, "setScrollAnimation"):
                    QTimer.singleShot(50, lambda: self.scroll_area.setScrollAnimation(Qt.Vertical, 400))
            else:
                bar.setValue(max_val)

        self._is_batch_loading = False

    def _refresh_input_placeholder(self):
        line1 = "请输入问题…（Enter 发送，Ctrl+Enter 换行）"
        if self._placeholder_meta_suffix:
            line2 = self._placeholder_meta_suffix
        else:
            mlabels = []
            for mid in (self._chat_model_ids or []):
                lb = next((x for m, x in self._chat_model_options if m == mid), mid)
                if lb:
                    mlabels.append(lb)
            mlabel = "，".join(mlabels) if mlabels else "—"
            scen = self.scenario_combo.currentText() if hasattr(self, "scenario_combo") else ""
            line2 = f"已选模型：{mlabel}"
            if scen:
                line2 += f"　·　场景：{scen}"
        self.input_edit.setPlaceholderText(f"{line1}\n{line2}")

    def _on_scenario_placeholder_refresh(self, _index: int = 0):
        self._placeholder_meta_suffix = None
        self._refresh_input_placeholder()

    def _chat_model_ids_set(self) -> frozenset:
        return frozenset(m for m, _ in self._chat_model_options)

    def set_chat_model_options(self, items: list) -> None:
        """登录后由 main 注入 /api/system/configs_dict 的 llm_chat_models。"""
        if not items:
            return
        parsed: list[tuple[str, str]] = []
        for it in items:
            if isinstance(it, dict):
                mid = str(it.get("id", "")).strip()
                lbl = str(it.get("label", "") or mid).strip()
                if mid:
                    parsed.append((mid, lbl or mid))
        if not parsed:
            return
        self._chat_model_options = parsed
        # 修正已选列表：移除不再存在的模型；若为空则选第一个
        allowed = self._chat_model_ids_set()
        self._chat_model_ids = [m for m in (self._chat_model_ids or []) if m in allowed]
        if not self._chat_model_ids:
            self._chat_model_ids = [parsed[0][0]]
            cfg.set_runtime("ai_chat_model", ",".join(self._chat_model_ids))
        self._placeholder_meta_suffix = None
        self._load_chat_model_from_cfg()
        self._style_chat_toolbar_fonts()

    def apply_server_default_chat_models(self, model_ids: list[str] | str | None) -> None:
        """
        后端下发“桌面端默认选中模型”。
        仅在用户未固定本机偏好时生效（ai_chat_model_pinned=false）。
        """
        if getattr(cfg, "ai_chat_model_pinned", False):
            return
        if model_ids is None:
            return
        if isinstance(model_ids, str):
            parts = [p.strip() for p in model_ids.split(",") if p.strip()]
        else:
            parts = [str(p).strip() for p in (model_ids or []) if str(p).strip()]
        allowed = self._chat_model_ids_set()
        mids = [m for m in parts if m in allowed]
        if not mids:
            return
        self._chat_model_ids = mids
        cfg.set_runtime("ai_chat_model", ",".join(mids))
        self._load_chat_model_from_cfg()

    def _load_chat_model_from_cfg(self):
        raw = (cfg.ai_chat_model or "").strip()
        # 兼容旧版：单模型；新版：逗号分隔多模型
        parts = [p.strip() for p in raw.split(",") if p.strip()] if raw else []
        allowed = self._chat_model_ids_set()
        mids = [m for m in parts if m in allowed]
        if not mids:
            mids = [self._chat_model_options[0][0]]
            cfg.set_runtime("ai_chat_model", ",".join(mids))
        self._chat_model_ids = mids
        self._placeholder_meta_suffix = None
        first = self._chat_model_ids[0] if self._chat_model_ids else ""
        label = next((lb for m, lb in self._chat_model_options if m == first), first)
        self.chat_model_btn.setToolTip(
            f"选择对话模型（可多选）\n当前：{label}\n与后台「画像分析」使用的 llm_model 无关"
        )
        self._refresh_input_placeholder()

    def apply_server_chat_meta(self, chat_model_id: str, scenario_key: str):
        """流式首包 meta：写入输入框占位符第二行。"""
        mlabel = next((lb for m, lb in self._chat_model_options if m == chat_model_id), chat_model_id or "—")
        slabel = self._scenario_key_to_label.get(scenario_key) or SCENARIO_LABELS.get(scenario_key, scenario_key or "—")
        self._placeholder_meta_suffix = f"本轮：{mlabel} · {slabel}"
        self._refresh_input_placeholder()

    def set_scenario_options(self, scenarios: list[dict]):
        """
        动态刷新“场景下拉框”。
        scenarios: [{"scenario_key":"general_chat","name":"自由对话"}, ...]
        """
        opts: list[tuple[str, str]] = []
        for s in scenarios or []:
            k = (s.get("scenario_key") or "").strip()
            name = (s.get("name") or "").strip()
            if not k or not name:
                continue
            opts.append((k, name))
        if not opts:
            return

        self._scenario_options = opts
        self._scenario_label_to_key = {lb: k for k, lb in opts}
        self._scenario_key_to_label = {k: lb for k, lb in opts}

        cur_label = self.scenario_combo.currentText() if hasattr(self, "scenario_combo") else ""
        self.scenario_combo.blockSignals(True)
        self.scenario_combo.clear()
        self.scenario_combo.addItems([lb for _, lb in opts])
        # 尽量保持当前选择
        if cur_label and cur_label in self._scenario_label_to_key:
            idx = [lb for _, lb in opts].index(cur_label)
            self.scenario_combo.setCurrentIndex(idx)
        else:
            self.scenario_combo.setCurrentIndex(0)
        self.scenario_combo.blockSignals(False)
        self._refresh_input_placeholder()

    def get_selected_scenario_key(self) -> str:
        label = self.scenario_combo.currentText() if hasattr(self, "scenario_combo") else ""
        return self._scenario_label_to_key.get(label, "general_chat")

    def _open_chat_model_menu(self):
        # 兼容不同版本 qfluentwidgets：旧版 MenuIndicatorType 可能没有 CHECKBOX
        ind = getattr(MenuIndicatorType, "CHECKBOX", None)
        if ind is None:
            # 回退：让菜单自身用默认 indicator（视觉不重要，关键是 QAction 可多选）
            menu = CheckableMenu(parent=self)
        else:
            menu = CheckableMenu(parent=self, indicatorType=ind)
        group = QActionGroup(menu)
        group.setExclusive(False)
        for mid, label in self._chat_model_options:
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(mid in set(self._chat_model_ids or []))
            group.addAction(act)
            act.triggered.connect(lambda checked=False, m=mid: self._toggle_chat_model(m))
            menu.addAction(act)
        pos = self.chat_model_btn.mapToGlobal(QPoint(0, self.chat_model_btn.height()))
        menu.exec(pos, ani=True, aniType=MenuAnimationType.DROP_DOWN)

    def _toggle_chat_model(self, model_id: str):
        if model_id not in self._chat_model_ids_set():
            return
        cur = list(self._chat_model_ids or [])
        if model_id in cur:
            cur = [m for m in cur if m != model_id]
        else:
            cur.append(model_id)
        # 至少保留一个，避免“无模型可用”导致发送失败
        if not cur:
            cur = [self._chat_model_options[0][0]]
        self._chat_model_ids = cur
        cfg.set_runtime("ai_chat_model", ",".join(self._chat_model_ids))
        # 用户手动修改过模型选择：固定本机偏好，不再被后端默认覆盖
        if hasattr(cfg, "set_runtime"):
            cfg.set_runtime("ai_chat_model_pinned", "true")
        self._load_chat_model_from_cfg()

    def get_chat_model(self) -> str:
        """兼容旧接口：返回首选模型。"""
        return (self._chat_model_ids[0] if self._chat_model_ids else "") or ""

    def get_chat_models(self) -> list[str]:
        """新接口：返回当前勾选的所有模型（顺序保留）。"""
        return list(self._chat_model_ids or [])

    def get_chat_model_label(self, model_id: str) -> str:
        mid = (model_id or "").strip()
        if not mid:
            return ""
        return next((lb for m, lb in self._chat_model_options if m == mid), mid)

    def set_history_button_visible(self, visible: bool):
        """客户对话可拉取历史；自由对话无客户上下文时隐藏。"""
        if hasattr(self, "history_btn"):
            self.history_btn.setVisible(visible)

    def _on_send_clicked(self):
        text = self.input_edit.toPlainText().strip()
        if text:
            self.send_requested.emit(text)
            self.input_edit.clear()

    def _apply_theme_style(self):
        """刷新聊天页背景并递归刷新所有可见气泡"""
        is_dark = isDarkTheme()
        # 优化对比度：浅色模式下采用极简灰背景，使白色气泡更清晰
        bg = "#1e1e1e" if is_dark else "#f5f5f5"
        self.scroll_area.setStyleSheet(f"QScrollArea {{ background-color: {bg}; border: none; }}")
        self.chat_container.setStyleSheet(f"QWidget#ChatContainer {{ background-color: {bg}; }}")
        
        # 遍历所有气泡进行刷新
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), ChatBubble):
                item.widget()._apply_theme_style()

        self._style_chat_toolbar_fonts()

    def _style_chat_toolbar_fonts(self):
        """底部工具栏：略缩小字号，避免固定行高下文字裁切（不覆写 Fluent 颜色，仅调字体）。"""
        if not hasattr(self, "scenario_combo") or not hasattr(self, "send_btn"):
            return
        f = QFont()
        f.setPointSize(8)
        self.scenario_combo.setFont(f)
        self.send_btn.setFont(f)

    def clear(self):
        """清空所有聊天气泡，但保留加载环和伸缩量"""
        self._is_batch_loading = True
        
        # 倒序遍历，安全删除所有 ChatBubble
        for i in reversed(range(self.chat_layout.count())):
            item = self.chat_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), ChatBubble):
                w = self.chat_layout.takeAt(i).widget()
                if w:
                    w.deleteLater()
                    
        # 确保输入框也清空（如果有未发送内容）
        self.input_edit.clear()
