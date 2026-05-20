"""
AI 聊天核心组件：QuickTextEdit / ChatActionToolbar / ChatBubble / AIChatWidget
对应 UI_implementation.md Phase 4 — AI 聊天界面改造
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QScrollArea,
    QPushButton, QLabel, QFrame, QApplication, QSplitter,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QObject, QEvent, QTimer, QPoint
from PySide6.QtGui import QKeyEvent, QColor, QAction, QActionGroup, QFont, QFontMetrics

from config_loader import cfg

from qfluentwidgets import (
    TransparentToolButton, FluentIcon, SmoothScrollArea,
    TextEdit, PrimaryPushButton, IndeterminateProgressRing,
    isDarkTheme, ComboBox, CheckableMenu,
    MenuAnimationType, MenuIndicatorType,
)

from ui.app_icons import AppIcon

# ---- Markdown -> QLabel 富文本 -----------------------------------------------
# AI 回复一般是 Markdown（**加粗**、## 标题、列表、表格、代码块…）。
# QLabel 在 Qt.RichText 模式下支持 HTML 子集，足以渲染常见 Markdown 排版。
# 转换库使用纯 Python 的 `markdown` —— 无额外原生依赖，PyInstaller 自动打包。
try:
    import markdown as _markdown  # type: ignore
    _MD_AVAILABLE = True
except Exception:  # pragma: no cover
    _markdown = None
    _MD_AVAILABLE = False

# 为 QLabel 内嵌的 QTextDocument 提供轻量级排版样式。
# 注意：Qt 富文本只识别极小的 CSS 子集（颜色、背景、字体、内外边距等），
# 这里只用受支持的属性，避免出现 "未渲染但布局变怪" 的诡异表现。
_BUBBLE_MD_CSS = """
<style>
  h1, h2, h3, h4, h5, h6 { margin: 6px 0 4px 0; font-weight: 600; }
  h1 { font-size: 16px; }
  h2 { font-size: 15px; }
  h3 { font-size: 14px; }
  h4, h5, h6 { font-size: 13px; }
  p  { margin: 4px 0; }
  ul, ol { margin: 4px 0 4px 18px; }
  li { margin: 1px 0; }
  pre  { background-color: rgba(127,127,127,0.18); padding: 6px 8px; font-family: Consolas, 'Cascadia Mono', monospace; }
  code { background-color: rgba(127,127,127,0.18); padding: 1px 4px; font-family: Consolas, 'Cascadia Mono', monospace; }
  pre code { background-color: transparent; padding: 0; }
  blockquote { border-left: 3px solid rgba(127,127,127,0.45); padding-left: 8px; margin: 4px 0; color: gray; }
  table { border-collapse: collapse; margin: 4px 0; }
  th, td { border: 1px solid rgba(127,127,127,0.45); padding: 2px 6px; }
  hr { border: 0; border-top: 1px solid rgba(127,127,127,0.45); }
  a { text-decoration: underline; }
</style>
"""


def _md_to_html(text: str) -> str:
    """将 Markdown 转为 QLabel 可渲染的 HTML 片段。

    - 流式追加场景下会被频繁调用，要求转换本身轻量、无副作用。
    - markdown 未安装时回退到“转义后 <br> 换行”的纯文本，仍优于直接显示 ## 与 **。
    """
    src = text or ""
    if not src:
        return ""
    if _MD_AVAILABLE:
        try:
            body = _markdown.markdown(
                src,
                extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
                output_format="html",
            )
        except Exception:
            import html as _html
            body = _html.escape(src).replace("\n", "<br>")
    else:
        import html as _html
        body = _html.escape(src).replace("\n", "<br>")
    return _BUBBLE_MD_CSS + body


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
    "auto": "自动",
}


# 路由器"自动"选项的特殊 key：桌面端把它原样传给后端，
# 后端 SceneRouter 看到 "auto" 时视为"无 hint"，全权决策。
AUTO_SCENARIO_KEY = "auto"
AUTO_SCENARIO_LABEL = "自动"

# [TEMP-NO-AUTO] 临时屏蔽"自动"场景选项用于本次打包，打包完搜索本标记移除/置 False 即可恢复。
_HIDE_AUTO_SCENARIO = False


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


class ChatActionToolbar(QObject):
    """
    气泡上方/下方的操作工具栏控制器：
      - top_bar：气泡上方，左侧点赞/踩、中间模型名称、右侧重新生成
      - bottom_bar：气泡下方，左侧复制、右侧编辑发送/发送
    本身不是可见控件，仅承载按钮、模型标签与对外信号。两条工具条作为子控件由
    ChatBubble 直接加入垂直布局。
    """
    copy_requested = Signal()
    like_requested = Signal()
    dislike_requested = Signal()
    regenerate_requested = Signal()
    send_wechat_requested = Signal()
    edit_send_wechat_requested = Signal()

    def _create_btn(self, icon, tooltip, signal, size: int = 24):
        btn = TransparentToolButton(icon)
        btn.setObjectName("ActionIconBtn")
        btn.setFixedSize(size, size)
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
        self._model_tag_full = ""

        self.btn_copy = self._create_btn(FluentIcon.COPY, "复制回复", self.copy_requested)
        # 使用项目自带 SVG（心 / 心碎）替代默认的 ♥ / ✕，更贴合“情绪式反馈”交互
        self.btn_like = self._create_btn(AppIcon.HEART, "有帮助", self.like_requested)
        self.btn_dislike = self._create_btn(AppIcon.HEART_BROKEN, "不满意", self.dislike_requested)
        self.btn_redo = self._create_btn(FluentIcon.SYNC, "重新生成", self.regenerate_requested)

        self.btn_send_wechat = self._create_btn(
            AppIcon.SEND_WECHAT, "发送到微信", self.send_wechat_requested, size=26
        )
        _edit_icon = FluentIcon.EDIT if hasattr(FluentIcon, "EDIT") else FluentIcon.SYNC
        self.btn_edit_send_wechat = self._create_btn(
            _edit_icon, "编辑后发送到微信", self.edit_send_wechat_requested, size=26
        )

        self.model_tag = QLabel("")
        self.model_tag.setObjectName("ModelTagLabel")
        self.model_tag.setVisible(False)
        self.model_tag.setWordWrap(False)
        self.model_tag.setTextInteractionFlags(Qt.NoTextInteraction)
        self.model_tag.setAlignment(Qt.AlignCenter)

        self.top_bar = self._build_top_bar()
        self.bottom_bar = self._build_bottom_bar()

        self.top_bar.installEventFilter(self)

    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("ChatActionToolbarTop")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setStyleSheet("QFrame#ChatActionToolbarTop { background: transparent; border: none; }")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(4, 0, 4, 2)
        layout.setSpacing(4)

        left_wrap = QWidget(bar)
        left_l = QHBoxLayout(left_wrap)
        left_l.setContentsMargins(0, 0, 0, 0)
        left_l.setSpacing(4)
        left_l.addWidget(self.btn_like)
        left_l.addWidget(self.btn_dislike)

        right_wrap = QWidget(bar)
        right_l = QHBoxLayout(right_wrap)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(4)
        right_l.addWidget(self.btn_redo)

        layout.addWidget(left_wrap, 0, Qt.AlignLeft)
        layout.addStretch(1)
        layout.addWidget(self.model_tag, 0, Qt.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(right_wrap, 0, Qt.AlignRight)
        return bar

    def _build_bottom_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("ChatActionToolbarBottom")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setStyleSheet("QFrame#ChatActionToolbarBottom { background: transparent; border: none; }")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        left_wrap = QWidget(bar)
        left_l = QHBoxLayout(left_wrap)
        left_l.setContentsMargins(0, 0, 0, 0)
        left_l.setSpacing(4)
        left_l.addWidget(self.btn_copy)

        right_wrap = QWidget(bar)
        right_l = QHBoxLayout(right_wrap)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(2)
        # 需求：发送按钮与编辑发送按钮交换位置（编辑发送在左、发送在右）
        right_l.addWidget(self.btn_edit_send_wechat)
        right_l.addWidget(self.btn_send_wechat)

        layout.addWidget(left_wrap, 0)
        layout.addStretch(1)
        layout.addWidget(right_wrap, 0)
        return bar

    def set_model_tag(self, text: str):
        t = (text or "").strip()
        self._model_tag_full = t
        self.model_tag.setVisible(bool(t))
        # 让“模型”信息是弱化的辅助信息：放在顶部居中，不喧宾夺主
        is_dark = isDarkTheme()
        col = "#a7c0ff" if is_dark else "#3b6ea5"
        self.model_tag.setStyleSheet(
            f"QLabel#ModelTagLabel {{ color: {col}; font-size: 11px; padding: 0px 4px; }}"
        )
        self._apply_model_tag_elide()

    def _apply_model_tag_elide(self):
        full = (self._model_tag_full or "").strip()
        if not full:
            self.model_tag.setText("")
            return
        # 中间区域宽度受两侧按钮挤压，按容器宽度的一半给出安全可视长度
        bar_w = max(120, self.top_bar.width())
        max_w = max(60, int(bar_w * 0.5) - 8)
        fm = QFontMetrics(self.model_tag.font())
        self.model_tag.setText(fm.elidedText(full, Qt.ElideRight, max_w))

    def eventFilter(self, obj, event):
        if obj is self.top_bar and event.type() == QEvent.Resize:
            if self.model_tag.isVisible():
                self._apply_model_tag_elide()
        return super().eventFilter(obj, event)

    def set_action_widgets_visible(self, visible: bool) -> None:
        """切换工具条内部按钮的可见性。

        bar 本身保留固定高度始终占位，只是内部按钮被显隐 —— 既保证 hover
        体验顺滑（不跳行），又彻底不依赖 QGraphicsOpacityEffect。
        模型标签 (self.model_tag) 不在此控制：它有内容时常驻显示，
        作为弱化的辅助信息。
        """
        widgets = [
            self.btn_copy,
            self.btn_like,
            self.btn_dislike,
            self.btn_redo,
            self.btn_send_wechat,
            self.btn_edit_send_wechat,
        ]
        for w in widgets:
            try:
                w.setVisible(visible)
            except RuntimeError:
                # 控件 C++ 端已销毁（极端竞态），忽略即可
                pass


class ChatBubble(QWidget):
    """
    单个聊天气泡组件：支持悬停工具栏 (仅 AI 回复)。
    """
    copy_triggered = Signal(str)        # 用于本地剪贴板
    copy_event_triggered = Signal(int)  # 用于云端采纳记录 (msg_id)
    feedback_triggered = Signal(int, int)  # (msg_id, rating)
    regenerate_triggered = Signal(str)     # (user_query)
    stream_chunk_appended = Signal()  # AI 流式追加后通知外层吸底
    send_wechat_requested = Signal(object, str)  # (msg_id, bubble_text)
    edit_send_wechat_requested = Signal(object, str)

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

        # 气泡列容器：纵向堆叠 [上工具条] + [气泡] + [下工具条]。
        # 关键设计：把 toolbar.top_bar / bottom_bar 放进这个 QVBoxLayout 里，
        # 由 Qt 自身的布局体系保证三者宽度一致 = 气泡宽度。
        # —— 这样工具条天然就"锚定在气泡左右两端"，
        #    无需任何 Python 端的 eventFilter / setFixedWidth / QTimer 同步逻辑
        #    （那条路径已被验证会在批量渲染历史消息时触发 PySide6 段错误闪退）。
        self.bubble_column = QWidget()
        self.bubble_column_layout = QVBoxLayout(self.bubble_column)
        self.bubble_column_layout.setContentsMargins(0, 0, 0, 0)
        self.bubble_column_layout.setSpacing(2)

        # 气泡容器 (Frame) - 支持内部多组件布局
        # 不主动设置 SizePolicy：让 QFrame 用默认的 Preferred/Preferred，
        # 这样 bubble_frame 在 bubble_column 这个 QVBoxLayout 里会
        # 自动撑满列宽（= 工具条宽度），保证三者**端到端对齐**。
        # 短消息时气泡略宽于文字内容，但工具条图标会"严丝合缝"贴住
        # 气泡左右边缘 —— 这正是本次改动的核心诉求。
        self.bubble_frame = QFrame()
        self.bubble_frame.setObjectName("BubbleFrame")
        self.bubble_layout = QVBoxLayout(self.bubble_frame)
        self.bubble_layout.setContentsMargins(10, 12, 10, 12)
        self.bubble_layout.setSpacing(8)

        # 原始文本缓存：
        # - 用户气泡：与 label 显示一致；
        # - AI 气泡：保存“未渲染的 Markdown”，复制 / 发送微信时使用，
        #   避免把 <p><strong>… 这些 HTML 源码塞进剪贴板或微信。
        self._raw_text = text or ""
        self.label = QLabel()
        self.label.setWordWrap(True)
        if is_user:
            # 用户发出的内容支持鼠标选中复制，方便回溯或转发
            self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.label.setTextFormat(Qt.PlainText)
        else:
            # AI 回复按 Markdown -> HTML 渲染，保留对 **/##/列表/代码块 的正确排版
            self.label.setTextInteractionFlags(Qt.NoTextInteraction)
            self.label.setTextFormat(Qt.RichText)
        self._render_label()
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

        # 气泡帧加入列容器；AI 气泡的上下工具条将在下方 if not is_user 分支里
        # 通过 insertWidget(0, ...) / addWidget(...) 加到同一个 QVBoxLayout 里。
        self.bubble_column_layout.addWidget(self.bubble_frame)

        self.main_v_layout.addLayout(self.bubble_h_layout)
        
        # 应用初始样式（包括将 bubble_column 添加到 bubble_h_layout）
        self._apply_theme_style()

        # 情况处理：如果初始文本为空且为 AI 消息，则自动进入加载状态
        if not text and not is_user:
            self.set_loading(True)
            self.label.hide()

        # 2. 工具栏层 (仅非用户消息显示)
        self.toolbar = None
        if not is_user:
            self.toolbar = ChatActionToolbar(self)
            # 不再使用 QGraphicsOpacityEffect 控制可见性 ——
            # 同一气泡上同时挂 QGraphicsDropShadowEffect + 2 个
            # QGraphicsOpacityEffect 时，加载历史 (短时间内创建大量气泡)
            # 会触发 PySide6 已知的 effect 渲染段错误，表现为“点击历史
            # 聊天记录后整个客户端无报错闪退”。
            #
            # 改用：上下工具条永远占位（固定高度），只切换内部按钮的
            # 显隐 —— 既保留 hover 时的“无跳动”体验，又规避 GraphicsEffect。
            _bar_h = 28
            self.toolbar.top_bar.setFixedHeight(_bar_h)
            self.toolbar.bottom_bar.setFixedHeight(_bar_h)
            self.toolbar.set_action_widgets_visible(False)

            # 绑定信号转发
            self.toolbar.copy_requested.connect(lambda: self._handle_copy())
            self.toolbar.like_requested.connect(lambda: self._emit_feedback(1))
            self.toolbar.dislike_requested.connect(lambda: self._emit_feedback(-1))
            self.toolbar.regenerate_requested.connect(self._handle_redo)
            self.toolbar.send_wechat_requested.connect(self._on_send_wechat)
            self.toolbar.edit_send_wechat_requested.connect(self._on_edit_send_wechat)

            # 上下工具条直接加入 bubble_column 这个 QVBoxLayout：
            # Qt 的 QVBoxLayout 默认让所有子控件占满列宽，因此
            # top_bar / bubble_frame / bottom_bar 三者天然等宽。
            # —— 工具条始终"贴着气泡左右边缘"，
            #    而不再随页面边距飘到聊天面板两端，
            #    且不依赖 eventFilter + setFixedWidth（已被验证会闪退）。
            self.bubble_column_layout.insertWidget(0, self.toolbar.top_bar)
            self.bubble_column_layout.addWidget(self.toolbar.bottom_bar)

            # 如果存在历史评价，初始化按钮状态
            if rating != 0:
                self._apply_rating_ui(rating)

            # 模型标签（展示在气泡上方居中；历史/实时均可写入）
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
        
        # 每次调用时，先清空 bubble_h_layout 中的内容（避免重复添加 column）
        while self.bubble_h_layout.count():
            self.bubble_h_layout.takeAt(0)
        
        if self.is_user:
            # 用户气泡：右对齐
            bg_color = "#2bae60" if is_dark else "#95ec69"
            text_color = "#ffffff" if is_dark else "#1a1a1a"
            self.bubble_frame.setStyleSheet(f"QFrame#BubbleFrame {{ {common_style} background-color: {bg_color}; border: none; }}")
            self.label.setStyleSheet(f"background: transparent; color: {text_color}; font-weight: normal;")
            self.bubble_h_layout.addStretch()
            self.bubble_h_layout.addWidget(self.bubble_column)
        else:
            # AI 气泡：左对齐，工具条天然与气泡同宽（由 bubble_column 的 QVBoxLayout 保证）
            bg_color = "#2c2c2c" if is_dark else "#ffffff"
            text_color = "#e5e5e5" if is_dark else "#202020"
            border_color = "rgba(255, 255, 255, 0.1)" if is_dark else "rgba(0, 0, 0, 0.12)"
            self.bubble_frame.setStyleSheet(
                f"QFrame#BubbleFrame {{ {common_style} background-color: {bg_color}; color: {text_color}; border: 1px solid {border_color}; }}"
            )
            self.label.setStyleSheet(f"background: transparent; color: {text_color};")
            self.bubble_h_layout.addWidget(self.bubble_column)
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

    def _render_label(self):
        """根据 is_user 决定按纯文本还是 Markdown 渲染当前 _raw_text。"""
        if self.is_user:
            self.label.setText(self._raw_text or "")
        else:
            self.label.setText(_md_to_html(self._raw_text or ""))

    def get_raw_text(self) -> str:
        """返回未渲染的原始文本（AI 气泡即原始 Markdown）。"""
        return self._raw_text or ""

    def _handle_redo(self):
        self.regenerate_triggered.emit(self.user_query)

    def _on_send_wechat(self):
        # 微信不支持 Markdown 渲染，但发原始 Markdown 优于发 HTML 源码；
        # 用户若要纯文本可在“编辑后发送”里再调整。
        self.send_wechat_requested.emit(self.msg_id, self._raw_text)

    def _on_edit_send_wechat(self):
        self.edit_send_wechat_requested.emit(self.msg_id, self._raw_text)

    def _handle_copy(self):
        self.copy_triggered.emit(self._raw_text)
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
        """鼠标进入时：显示上下工具条内部按钮（高度常占，无跳动）"""
        if self.toolbar and not self.is_user:
            try:
                self.toolbar.set_action_widgets_visible(True)
            except Exception:
                pass
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开时：隐藏内部按钮，bar 自身仍占位"""
        if self.toolbar:
            try:
                self.toolbar.set_action_widgets_visible(False)
            except Exception:
                pass
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
        # 错误提示按纯文本展示，避免 message 中混入 Markdown 标点导致渲染异常
        self.label.setTextFormat(Qt.PlainText)
        self._raw_text = f"⚠️ {message}"
        self.label.setText(self._raw_text)

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

        # 关键：维护原始 Markdown 缓存，再整体重新渲染。
        # 流式过程中遇到尚未闭合的 ** / ``` 等会暂时显得粗糙，
        # 但只要后续 chunk 补齐就会立即转为正确的富文本，符合主流 LLM 客户端体验。
        self._raw_text = (self._raw_text or "") + (new_text or "")
        self._render_label()
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
    wechat_send_requested = Signal(object, str)
    wechat_edit_send_requested = Signal(object, str)
    cleared = Signal()                      # 对话窗口被清空信号

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
        input_container.setMinimumHeight(80)
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
        self.chat_model_btn.hide()  # 模型选择按钮隐藏
        btn_layout.addWidget(self.chat_model_btn, 0, Qt.AlignVCenter)

        self.scenario_combo = ComboBox()
        # 默认置顶"自动"，由后端 SceneRouter 全权决策；
        # 用户也可在下拉里手动锁定一个场景作为强 hint。
        self._scenario_options: list[tuple[str, str]] = [
            (AUTO_SCENARIO_KEY, AUTO_SCENARIO_LABEL),
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
        self.scenario_combo.setToolTip("选择本轮使用的提示词场景；选『自动』由后台路由器决定。")
        self.scenario_combo.currentIndexChanged.connect(self._on_scenario_placeholder_refresh)
        self.scenario_combo.hide()  # 场景选择按钮隐藏
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

        self.input_splitter = QSplitter(Qt.Vertical, self)
        self.input_splitter.setObjectName("ChatInputSplitter")
        self.input_splitter.setHandleWidth(4)
        self.input_splitter.setChildrenCollapsible(False)
        self.input_splitter.addWidget(self.scroll_area)
        self.input_splitter.addWidget(input_container)
        self.input_splitter.setStretchFactor(0, 1)
        self.input_splitter.setStretchFactor(1, 0)
        self.input_splitter.splitterMoved.connect(self._on_input_splitter_moved)
        layout.addWidget(self.input_splitter)

        QTimer.singleShot(0, self._apply_chat_input_height)
        self._apply_theme_style()

        self._placeholder_meta_suffix = None  # 服务端首包 meta，临时写入占位符第二行
        self._chat_model_options: list[tuple[str, str]] = list(FALLBACK_LLM_CHAT_MODEL_OPTIONS)
        self._chat_model_ids: list[str] = [self._chat_model_options[0][0]]
        self._load_chat_model_from_cfg()

    def _apply_chat_input_height(self):
        if not hasattr(self, "input_splitter") or self.input_splitter is None:
            return
        saved_h = cfg.chat_input_height
        total_h = self.height()
        if total_h < 150:
            return
        input_h = max(80, min(total_h - 100, saved_h))
        chat_h = max(50, total_h - input_h)
        self.input_splitter.setSizes([chat_h, input_h])

    def _on_input_splitter_moved(self, pos: int, index: int):
        if not hasattr(self, "input_splitter") or self.input_splitter is None:
            return
        sizes = self.input_splitter.sizes()
        if len(sizes) < 2 or sizes[1] <= 0:
            return
        new_h = max(80, int(sizes[1]))
        if new_h == getattr(cfg, "chat_input_height", None):
            return
        try:
            cfg.set_runtime("chat_input_height", str(new_h))
        except Exception:
            pass

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
        bubble.send_wechat_requested.connect(self.wechat_send_requested.emit)
        bubble.edit_send_wechat_requested.connect(self.wechat_edit_send_requested.emit)

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
        bubble.send_wechat_requested.connect(self.wechat_send_requested.emit)
        bubble.edit_send_wechat_requested.connect(self.wechat_edit_send_requested.emit)

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
        后端下发"桌面端默认选中模型"。
        始终以管理后台 desktop_default_chat_models 为准：每次启动覆盖本机当前选择，
        本机内的勾选切换仅在当前会话生效，不再持久化"固定偏好"。
        """
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
            f"选择对话模型（可多选，仅本次会话生效；默认值由管理后台下发）\n当前：{label}\n与后台「画像分析」使用的 llm_model 无关"
        )
        self._refresh_input_placeholder()

    def apply_server_chat_meta(
        self,
        chat_model_id: str,
        scenario_key: str,
        auxiliary_scenarios: list | None = None,
    ):
        """流式首包 meta：写入输入框占位符第二行。"""
        mlabel = next((lb for m, lb in self._chat_model_options if m == chat_model_id), chat_model_id or "—")
        slabel = self._scenario_key_to_label.get(scenario_key) or SCENARIO_LABELS.get(scenario_key, scenario_key or "—")
        aux = [k for k in (auxiliary_scenarios or []) if k]
        if aux:
            aux_labels = [
                self._scenario_key_to_label.get(k) or SCENARIO_LABELS.get(k, k)
                for k in aux
            ]
            slabel = f"{slabel} + {' + '.join(aux_labels)}"
        self._placeholder_meta_suffix = f"本轮：{mlabel} · {slabel}"
        self._refresh_input_placeholder()

    def set_scenario_options(self, scenarios: list[dict]):
        """
        动态刷新“场景下拉框”。
        scenarios: [{"scenario_key":"general_chat","name":"自由对话"}, ...]

        无论后端给什么列表，首项都强制为"自动"（由 SceneRouter 决策），
        然后才是后端返回的具体场景。
        """
        # [TEMP-NO-AUTO] 本次打包临时屏蔽"自动"选项，恢复时删除整个 if 分支即可。
        if _HIDE_AUTO_SCENARIO:
            opts: list[tuple[str, str]] = []
        else:
            opts: list[tuple[str, str]] = [(AUTO_SCENARIO_KEY, AUTO_SCENARIO_LABEL)]
        for s in scenarios or []:
            k = (s.get("scenario_key") or "").strip()
            name = (s.get("name") or "").strip()
            if not k or not name or k == AUTO_SCENARIO_KEY:
                continue
            opts.append((k, name))
        # 仅"自动"一项时仍允许刷新（视觉上保持选项更新）
        if len(opts) <= 1 and getattr(self, "scenario_combo", None) is None:
            return

        self._scenario_options = opts
        self._scenario_label_to_key = {lb: k for k, lb in opts}
        self._scenario_key_to_label = {k: lb for k, lb in opts}

        cur_label = self.scenario_combo.currentText() if hasattr(self, "scenario_combo") else ""
        self.scenario_combo.blockSignals(True)
        self.scenario_combo.clear()
        self.scenario_combo.addItems([lb for _, lb in opts])
        # 尽量保持当前选择；找不到则回到首项（屏蔽"自动"时即为"客户沟通"/"内部问答"）
        if cur_label and cur_label in self._scenario_label_to_key:
            idx = [lb for _, lb in opts].index(cur_label)
            self.scenario_combo.setCurrentIndex(idx)
        else:
            # [TEMP-NO-AUTO] 优先选中 general_chat（客户沟通），找不到再回退到首项
            default_idx = 0
            if _HIDE_AUTO_SCENARIO:
                for i, (k, _lb) in enumerate(opts):
                    if k == "general_chat":
                        default_idx = i
                        break
            self.scenario_combo.setCurrentIndex(default_idx)
        self.scenario_combo.blockSignals(False)
        self._refresh_input_placeholder()

    def get_selected_scenario_key(self) -> str:
        label = self.scenario_combo.currentText() if hasattr(self, "scenario_combo") else ""
        return self._scenario_label_to_key.get(label, AUTO_SCENARIO_KEY)

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
        # 至少保留一个，避免"无模型可用"导致发送失败
        if not cur:
            cur = [self._chat_model_options[0][0]]
        self._chat_model_ids = cur
        # 会话级勾选：仅更新内存，不持久化；下次启动仍以管理后台 desktop_default_chat_models 为准
        self._placeholder_meta_suffix = None
        first = self._chat_model_ids[0] if self._chat_model_ids else ""
        label = next((lb for m, lb in self._chat_model_options if m == first), first)
        self.chat_model_btn.setToolTip(
            f"选择对话模型（可多选，仅本次会话生效；默认值由管理后台下发）\n当前：{label}\n与后台「画像分析」使用的 llm_model 无关"
        )
        self._refresh_input_placeholder()

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
        
        if hasattr(self, "input_splitter") and self.input_splitter is not None:
            handle_color = "rgba(255,255,255,0.10)" if is_dark else "rgba(0,0,0,0.08)"
            hover_color = "rgba(7,193,96,0.45)"
            self.input_splitter.setStyleSheet(f"""
                QSplitter#ChatInputSplitter::handle {{
                    background-color: {handle_color};
                }}
                QSplitter#ChatInputSplitter::handle:hover {{
                    background-color: {hover_color};
                }}
            """)
        
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
        self.cleared.emit()
