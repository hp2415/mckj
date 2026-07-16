"""统一的 Fluent 文本右键复制菜单（QLabel / QLineEdit / QTextEdit）。

避免原生菜单在深色主题或父级 QSS 下全黑，并与各处交互保持一致。
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QAction, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QTextEdit,
    QToolTip,
    QWidget,
)
from qfluentwidgets import MenuAnimationType, RoundMenu

_FILTER_ATTR = "_text_copy_menu_filter"

OnCopyCallback = Callable[[str], None]
GetFullTextCallback = Callable[[], str]


class _TextCopyMenuFilter(QObject):
    """在目标收到 ContextMenu 之前拦截，避免与原生菜单叠出两个弹窗。"""

    def __init__(
        self,
        parent=None,
        on_copy: Optional[OnCopyCallback] = None,
        get_full_text: Optional[GetFullTextCallback] = None,
    ):
        super().__init__(parent)
        self.on_copy = on_copy
        self.get_full_text = get_full_text

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.ContextMenu and isinstance(watched, QWidget):
            _hide_floating_tips()
            show_text_copy_menu(
                watched,
                event.pos(),
                on_copy=self.on_copy,
                get_full_text=self.get_full_text,
            )
            return True
        return False


def _hide_floating_tips() -> None:
    QToolTip.hideText()
    for w in QApplication.topLevelWidgets():
        name = w.__class__.__name__
        if name in ("ToolTip", "TransparentToolTip") and w.isVisible():
            w.hide()


def _label_plain_text(label: QLabel) -> str:
    """取 QLabel 可见纯文本；RichText 不要用 HTML 源码长度/内容。"""
    raw = label.text() or ""
    if label.textFormat() == Qt.TextFormat.PlainText:
        return raw
    doc = QTextDocument()
    doc.setHtml(raw)
    return doc.toPlainText()


def _selected_and_full_text(
    widget: QWidget,
    get_full_text: Optional[GetFullTextCallback] = None,
) -> tuple[str, str, bool]:
    """返回 (选中文本, 复制用全文, 是否可编辑)。"""
    if isinstance(widget, QLabel):
        selected = (widget.selectedText() or "").replace("\u2029", "\n")
        default_full = _label_plain_text(widget)
        editable = False
    elif isinstance(widget, QLineEdit):
        selected = widget.selectedText() or ""
        default_full = widget.text() or ""
        editable = not widget.isReadOnly()
    elif isinstance(widget, QTextEdit):
        cursor = widget.textCursor()
        selected = (cursor.selectedText() or "").replace("\u2029", "\n")
        default_full = widget.toPlainText() or ""
        editable = not widget.isReadOnly()
    else:
        selected, default_full, editable = "", "", False

    if get_full_text is not None:
        full_text = get_full_text() or ""
    else:
        full_text = default_full
    return selected, full_text, editable


def _select_all(widget: QWidget) -> None:
    if isinstance(widget, QLabel):
        # setSelection 按文档字符位，必须用纯文本长度，不能用 HTML 源码长度
        plain = _label_plain_text(widget)
        widget.setSelection(0, len(plain))
    elif isinstance(widget, (QLineEdit, QTextEdit)):
        widget.selectAll()


def _cut_selection(widget: QWidget) -> None:
    if isinstance(widget, QLineEdit):
        widget.cut()
    elif isinstance(widget, QTextEdit):
        widget.cut()


def _paste_clipboard(widget: QWidget) -> None:
    if isinstance(widget, QLineEdit):
        widget.paste()
    elif isinstance(widget, QTextEdit):
        widget.paste()


def enable_text_copy_menu(
    widget: QWidget,
    *,
    on_copy: Optional[OnCopyCallback] = None,
    get_full_text: Optional[GetFullTextCallback] = None,
) -> None:
    """为可选/可编辑文本控件启用统一 Fluent 右键菜单。

    get_full_text: 可选，覆盖「复制全部」的内容（如 AI 气泡返回未渲染 Markdown）。
    """
    existing = getattr(widget, _FILTER_ATTR, None)
    if isinstance(existing, _TextCopyMenuFilter):
        if on_copy is not None:
            existing.on_copy = on_copy
        if get_full_text is not None:
            existing.get_full_text = get_full_text
        return

    widget.setContextMenuPolicy(Qt.CustomContextMenu)
    filt = _TextCopyMenuFilter(
        widget, on_copy=on_copy, get_full_text=get_full_text
    )
    widget.installEventFilter(filt)
    setattr(widget, _FILTER_ATTR, filt)


def enable_selectable_label_menu(
    label: QLabel,
    *,
    on_copy: Optional[OnCopyCallback] = None,
    get_full_text: Optional[GetFullTextCallback] = None,
) -> None:
    """兼容旧接口。"""
    enable_text_copy_menu(label, on_copy=on_copy, get_full_text=get_full_text)


def show_selectable_label_menu(
    label: QLabel, pos, *, on_copy=None, get_full_text=None
) -> None:
    """兼容旧接口。"""
    show_text_copy_menu(
        label, pos, on_copy=on_copy, get_full_text=get_full_text
    )


def show_text_copy_menu(
    widget: QWidget,
    pos,
    *,
    on_copy: Optional[OnCopyCallback] = None,
    get_full_text: Optional[GetFullTextCallback] = None,
) -> None:
    selected, full_text, editable = _selected_and_full_text(
        widget, get_full_text=get_full_text
    )
    if isinstance(widget, QLabel):
        has_selectable_content = bool(_label_plain_text(widget))
    elif isinstance(widget, QLineEdit):
        has_selectable_content = bool(widget.text())
    elif isinstance(widget, QTextEdit):
        has_selectable_content = bool(widget.toPlainText())
    else:
        has_selectable_content = bool(full_text)

    can_paste = editable and bool(QApplication.clipboard().text())
    parent = widget.window() or widget
    menu = RoundMenu(parent=parent)

    if editable:
        cut_action = QAction("剪切", menu)
        cut_action.setEnabled(bool(selected))

        def on_cut():
            if not selected:
                return
            text = selected
            _cut_selection(widget)
            if on_copy:
                on_copy(text)

        cut_action.triggered.connect(on_cut)
        menu.addAction(cut_action)

    copy_action = QAction("复制", menu)
    copy_action.setEnabled(bool(selected))

    def do_copy():
        if selected:
            QApplication.clipboard().setText(selected)
            if on_copy:
                on_copy(selected)

    copy_action.triggered.connect(do_copy)
    menu.addAction(copy_action)

    if editable:
        paste_action = QAction("粘贴", menu)
        paste_action.setEnabled(can_paste)
        paste_action.triggered.connect(lambda: _paste_clipboard(widget))
        menu.addAction(paste_action)

    copy_all = QAction("复制全部", menu)
    copy_all.setEnabled(bool(full_text))

    def do_copy_all():
        QApplication.clipboard().setText(full_text)
        if on_copy:
            on_copy(full_text)

    copy_all.triggered.connect(do_copy_all)
    menu.addAction(copy_all)

    select_all = QAction("全选", menu)
    select_all.setEnabled(has_selectable_content)
    select_all.triggered.connect(lambda: _select_all(widget))
    menu.addAction(select_all)

    menu.exec(
        widget.mapToGlobal(pos),
        ani=True,
        aniType=MenuAnimationType.DROP_DOWN,
    )
