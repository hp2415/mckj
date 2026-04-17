"""
标签搜索组件：SearchTag / TagLineEdit / TagSearchWidget
优化了布局，将筛选图标锁定在搜索框最右侧。
"""
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLineEdit, QPushButton, QLabel, QWidget
from PySide6.QtCore import Qt, Signal
from qfluentwidgets import TransparentToolButton, FluentIcon, isDarkTheme, ToolTipFilter, ToolTipPosition


class SearchTag(QFrame):
    """搜索标签组件：展示关键词并支持删除"""
    removed = Signal(str)

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setObjectName("SearchTag")
        self.text = text
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        self.lbl = QLabel(text)
        self.btn_close = QPushButton("×")
        self.btn_close.setObjectName("TagCloseBtn")
        self.btn_close.setFixedSize(14, 14)
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.clicked.connect(lambda: self.removed.emit(self.text))
        
        self._apply_theme_style()

        layout.addWidget(self.lbl)
        layout.addWidget(self.btn_close)

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        # 现代胶囊风格
        if is_dark:
            bg = "#1f3322" # 墨绿深色
            text_col = "#71d591"
            close_col = "#71d591"
        else:
            bg = "#e6f7ff" # 浅蓝/浅绿
            text_col = "#0050b3"
            close_col = "#0050b3"
            
        self.setStyleSheet(f"""
            QFrame#SearchTag {{
                background-color: {bg};
                border-radius: 12px;
                border: none;
            }}
        """)
        self.lbl.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {text_col}; background: transparent;")
        self.btn_close.setStyleSheet(f"""
            QPushButton {{
                color: {close_col};
                background: transparent;
                border: none;
                font-size: 14px;
                font-weight: bold;
            }}
            QPushButton:hover {{ color: #ff4d4f; }}
        """)


class TagLineEdit(QLineEdit):
    """支持退格删除标签的自定义输入框"""
    backspace_pressed = Signal()

    def keyPressEvent(self, event):
        # 核心逻辑：只有在【按下前】已经是空，且【不是长按连发】的情况下，才发射删标签信号
        if event.key() == Qt.Key_Backspace and self.text() == "" and not event.isAutoRepeat():
            self.backspace_pressed.emit()
            return
        super().keyPressEvent(event)


class TagSearchWidget(QFrame):
    """标签搜索容器：支持多关键词叠加检索，并固定筛选图标"""
    search_triggered = Signal(str)
    filter_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TagSearchContainer")
        self.tags = []
        self._is_filter_active = False # 状态持久化标识

        # 主布局：水平排列
        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(10, 0, 5, 0)
        self.main_layout.setSpacing(5)

        # 1. 标签展示 + 输入区 (作为弹性容器)
        self.content_area = QWidget()
        self.content_layout = QHBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(8)
        
        # 标签流式容器
        self.tag_area = QWidget()
        self.tag_layout = QHBoxLayout(self.tag_area)
        self.tag_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_layout.setSpacing(6)
        self.content_layout.addWidget(self.tag_area)

        # 输入框
        self.edit = TagLineEdit()
        self.edit.setObjectName("InnerSearchInput")
        self.edit.setPlaceholderText("搜索关键词...")
        self.edit.setFrame(False)
        self.edit.returnPressed.connect(self._on_return_pressed)
        self.edit.backspace_pressed.connect(self._on_backspace_on_empty)
        self.content_layout.addWidget(self.edit)
        
        self.main_layout.addWidget(self.content_area, 1) # 权重为1，撑满剩余空间
        
        # 2. 功能按钮区 (固定在右侧)
        self.btn_filter = TransparentToolButton(FluentIcon.FILTER)
        self.btn_filter.setToolTip("高级筛选")
        self.btn_filter.installEventFilter(ToolTipFilter(self.btn_filter, 300, ToolTipPosition.BOTTOM))
        self.btn_filter.setFixedSize(32, 32)
        self.btn_filter.setIconSize(QSize(18, 18))
        self.btn_filter.clicked.connect(self.filter_clicked.emit)
        self.main_layout.addWidget(self.btn_filter, 0) # 权重为0
        
        self._apply_theme_style()

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        border_col = "#404040" if is_dark else "#e0e0e0"
        bg_col = "#2c2c2c" if is_dark else "#fdfdfd"
        text_col = "#eeeeee" if is_dark else "#333333"
        
        self.setStyleSheet(f"""
            QFrame#TagSearchContainer {{
                background-color: {bg_col};
                border: 1px solid {border_col};
                border-radius: 8px;
            }}
        """)
        self.edit.setStyleSheet(f"color: {text_col}; font-size: 12px; background: transparent;")
        
        # 递归刷新已有标签
        for i in range(self.tag_layout.count()):
            w = self.tag_layout.itemAt(i).widget()
            if w and hasattr(w, "_apply_theme_style"):
                w._apply_theme_style()
        
        # 核心：在主题切换后，重新根据当前状态应用筛选按钮的高亮样式
        self.set_filter_active(self._is_filter_active)

    def set_filter_active(self, is_active: bool):
        """设置筛选图标的激活状态（底色和提示文字）"""
        self._is_filter_active = is_active
        if is_active:
            # 激活状态：深绿色底色，白色图标（或保持原色但有背景）
            self.btn_filter.setStyleSheet("""
                TransparentToolButton {
                    background-color: rgba(7, 193, 96, 40); 
                    border: 1px solid rgba(7, 193, 96, 100);
                    border-radius: 4px;
                }
                TransparentToolButton:hover {
                    background-color: rgba(7, 193, 96, 70);
                }
            """)
            self.btn_filter.setToolTip("已筛选")
        else:
            # 默认状态：完全透明，无边框，避免在深色主题下过于显眼
            self.btn_filter.setStyleSheet("background: transparent; border: none;")
            self.btn_filter.setToolTip("高级筛选")

    def _on_backspace_on_empty(self):
        """当输入框为空且按下退格时，删除最后一个标签"""
        if self.tags:
            self.remove_tag(self.tags[-1])

    def _on_return_pressed(self):
        txt = self.edit.text().strip()
        if txt and txt not in self.tags:
            self.add_tag(txt)
            self.edit.clear()
            self.emit_search()
        elif not txt:
            # 如果输入框为空按下回车，也触发一次搜索（用于刷新）
            self.emit_search()

    def add_tag(self, text):
        tag = SearchTag(text)
        tag.removed.connect(self.remove_tag)
        self.tag_layout.addWidget(tag)
        self.tags.append(text)
        self.edit.setPlaceholderText("")  # 有标签后减少提示文字

    def remove_tag(self, text):
        if text in self.tags:
            self.tags.remove(text)
            # 重新渲染标签区
            for i in range(self.tag_layout.count()):
                w = self.tag_layout.itemAt(i).widget()
                if w and hasattr(w, "text") and w.text == text:
                    w.deleteLater()
                    break
            if not self.tags:
                self.edit.setPlaceholderText("添加筛选关键词...")
            self.emit_search()

    def emit_search(self):
        # 拼接所有标签发往后端
        self.search_triggered.emit(self.text())

    def text(self):
        """兼容性接口：返回所有标签组合后的字符串"""
        return " ".join(self.tags)

    def clear_all(self):
        self.tags = []
        while self.tag_layout.count():
            item = self.tag_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.edit.clear()
        self.edit.setPlaceholderText("关键词、供应商...")

from PySide6.QtCore import QSize
