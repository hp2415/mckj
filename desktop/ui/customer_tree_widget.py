"""窄侧栏客户树：基于 QFluentWidgets TreeWidget，零层级缩进 + Fluent 选中样式。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget
from qfluentwidgets import TreeWidget
from qfluentwidgets.common.style_sheet import setCustomStyleSheet


_BRANCH_HIDE_QSS = """
QTreeWidget#CustomerList::branch,
QTreeView#CustomerList::branch {
    image: none;
    width: 0px;
    border: none;
    background: transparent;
}
QTreeWidget#CustomerList::item,
QTreeView#CustomerList::item {
    padding: 0px;
    margin: 0px;
    border: none;
}
"""


class CompactCustomerTreeWidget(TreeWidget):
    """Fluent 风格客户树：关闭分支缩进，把层级信息交给分组标题控件。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CustomerList")
        self.setColumnCount(1)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(0)
        self.setAnimated(True)
        self.setUniformRowHeights(False)
        self.setFocusPolicy(Qt.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setBorderVisible(False)
        self.setBorderRadius(0)
        self.viewport().setContentsMargins(0, 0, 0, 0)
        setCustomStyleSheet(self, _BRANCH_HIDE_QSS, _BRANCH_HIDE_QSS)

    def drawBranches(self, painter, rect, index):
        """不绘制默认分支线/三角，避免窄侧栏被层级缩进吃掉宽度。"""
        return

    def viewportEvent(self, event):
        # 跳过 Fluent TreeWidget 在固定 x 处的展开热区，展开/收起由整行点击处理
        if event.type() == QEvent.Type.MouseButtonPress:
            return QTreeWidget.viewportEvent(self, event)
        return super().viewportEvent(event)

    def apply_sidebar_theme(self, bg: str, text: str):
        """与侧栏配色对齐，并保留 Fluent 圆角选中条。"""
        qss = f"""
        QTreeWidget#CustomerList {{
            background-color: {bg};
            border: none;
            outline: none;
            color: {text};
        }}
        QTreeWidget#CustomerList::viewport {{
            background-color: {bg};
        }}
        """
        setCustomStyleSheet(self, _BRANCH_HIDE_QSS + qss, _BRANCH_HIDE_QSS + qss)
        self.viewport().setContentsMargins(0, 0, 0, 0)
