"""
标签搜索组件：SearchTag / TagLineEdit / TagSearchWidget
对应 UI_implementation.md Phase 5 — 商品页改造
"""
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLineEdit, QPushButton, QLabel, QWidget
from PySide6.QtCore import Qt, Signal


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

        lbl = QLabel(text)
        lbl.setStyleSheet("font-size: 12px; font-weight: 500;")

        btn_close = QPushButton("×")
        btn_close.setObjectName("TagCloseBtn")
        btn_close.setFixedSize(16, 16)
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(lambda: self.removed.emit(self.text))

        layout.addWidget(lbl)
        layout.addWidget(btn_close)


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
    """标签搜索容器：支持多关键词叠加检索"""
    search_triggered = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TagSearchContainer")
        self.tags = []

        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(10, 2, 10, 2)
        self.main_layout.setSpacing(8)

        # 标签流式容器
        self.tag_area = QWidget()
        self.tag_layout = QHBoxLayout(self.tag_area)
        self.tag_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_layout.setSpacing(8)
        self.main_layout.addWidget(self.tag_area)

        self.edit = TagLineEdit()
        self.edit.setObjectName("InnerSearchInput")
        self.edit.setPlaceholderText("搜索关键词...")
        self.edit.setFrame(False)
        self.edit.returnPressed.connect(self._on_return_pressed)
        self.edit.backspace_pressed.connect(self._on_backspace_on_empty)
        self.main_layout.addWidget(self.edit)

        self.main_layout.addStretch()

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
