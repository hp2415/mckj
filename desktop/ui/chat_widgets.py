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
        self.btn_dislike = self._create_btn(FluentIcon.REMOVE, "不满意", self.dislike_requested)
        self.btn_redo = self._create_btn(FluentIcon.SYNC, "重新生成", self.regenerate_requested)

        layout.addWidget(self.btn_copy)
        layout.addWidget(self.btn_like)
        layout.addWidget(self.btn_dislike)
        layout.addWidget(self.btn_redo)
        layout.addStretch()


class ChatBubble(QWidget):
    """
    单个聊天气泡组件：支持悬停工具栏 (仅 AI 回复)。
    """
    copy_triggered = Signal(str)        # 用于本地剪贴板
    copy_event_triggered = Signal(int)  # 用于云端采纳记录 (msg_id)
    feedback_triggered = Signal(int, int)  # (msg_id, rating)
    regenerate_triggered = Signal(str)     # (user_query)

    def __init__(self, text, is_user=False, msg_id=None, rating=0, user_query="", parent=None):
        super().__init__(parent)
        self.is_user = is_user
        self.msg_id = msg_id
        self.current_rating = rating
        self.user_query = user_query # 关联的提问文本

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
        self._chat_model_id = self._chat_model_options[0][0]
        self._load_chat_model_from_cfg()

    def add_message(self, text, is_user=False, msg_id=None, rating=0, user_query=""):
        bubble = ChatBubble(text, is_user, msg_id, rating, user_query)

        # 绑定信号接力
        bubble.copy_triggered.connect(lambda t: QApplication.clipboard().setText(t))
        bubble.copy_event_triggered.connect(self.copy_event_triggered.emit)
        bubble.feedback_triggered.connect(self.feedback_requested.emit)
        bubble.regenerate_triggered.connect(self.regenerate_requested.emit)

        # 动态计算气泡最大宽度 (容器宽度的 90%)
        max_w = int(self.width() * 0.9)
        if max_w > 50:
            bubble.label.setMaximumWidth(max_w)

        # 检查当前是否在底部 (智能触底判断)
        bar = self.scroll_area.verticalScrollBar()
        was_at_bottom = bar.value() >= bar.maximum() - 50

        # 始终插在伸缩量上面 (即最底部)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)

        # 防抖滚动：如果 150ms 内连续调用 add_message，定时器会不断重置，仅最后一次生效
        # 触底逻辑加固 (Phase 4.5): 
        # 1. 如果是批量加载历史，完全跳过定时器（由 main.py 统一手动触底），防止后台 50 个定时器排队冲突
        if self._is_batch_loading:
            return bubble

        # 2. 对于 AI 追加，仅在用户已经在底部附近时才触发“吸附触底”
        bar = self.scroll_area.verticalScrollBar()
        is_near_bottom = bar.value() >= bar.maximum() - 30
        
        if is_user or is_near_bottom:
            # 用户提问立即触底，AI 流式追加稍作延迟防抖
            delay = 30 if is_user else 120
            self.scroll_timer.start(delay)

        return bubble

    def prepend_message(self, text, is_user=False, msg_id=None, rating=0, user_query=""):
        """在聊天区域顶部插入消息 (用于加载更早的历史记录)"""
        bubble = ChatBubble(text, is_user, msg_id, rating, user_query)
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

    def _on_scroll_value_changed(self, value):
        """当滚动条到达顶部时，触发加载更多信号"""
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
            mlabel = next(
                (lb for m, lb in self._chat_model_options if m == self._chat_model_id),
                self._chat_model_id,
            )
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
        if self._chat_model_id not in self._chat_model_ids_set():
            self._chat_model_id = parsed[0][0]
            cfg.set_runtime("ai_chat_model", self._chat_model_id)
        self._placeholder_meta_suffix = None
        self._load_chat_model_from_cfg()
        self._style_chat_toolbar_fonts()

    def _load_chat_model_from_cfg(self):
        mid = (cfg.ai_chat_model or "").strip()
        if mid not in self._chat_model_ids_set():
            mid = self._chat_model_options[0][0]
            cfg.set_runtime("ai_chat_model", mid)
        self._chat_model_id = mid
        self._placeholder_meta_suffix = None
        label = next((lb for m, lb in self._chat_model_options if m == mid), mid)
        self.chat_model_btn.setToolTip(
            f"选择对话模型（Fluent 菜单）\n当前：{label}\n与后台「画像分析」使用的 llm_model 无关"
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
        menu = CheckableMenu(parent=self, indicatorType=MenuIndicatorType.RADIO)
        group = QActionGroup(menu)
        group.setExclusive(True)
        for mid, label in self._chat_model_options:
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(mid == self._chat_model_id)
            group.addAction(act)
            act.triggered.connect(lambda checked=False, m=mid: self._set_chat_model(m))
            menu.addAction(act)
        pos = self.chat_model_btn.mapToGlobal(QPoint(0, self.chat_model_btn.height()))
        menu.exec(pos, ani=True, aniType=MenuAnimationType.DROP_DOWN)

    def _set_chat_model(self, model_id: str):
        if model_id not in self._chat_model_ids_set():
            return
        self._chat_model_id = model_id
        cfg.set_runtime("ai_chat_model", model_id)
        self._load_chat_model_from_cfg()

    def get_chat_model(self) -> str:
        return self._chat_model_id

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
