"""Fluent 风格确认对话框（电话外呼、查看号码等场景统一使用）。"""
from __future__ import annotations

from PySide6.QtWidgets import QWidget

from qfluentwidgets import MessageBox


def ask_confirm(
    parent: QWidget | None,
    title: str,
    content: str,
    *,
    yes_text: str = "确认",
    cancel_text: str = "取消",
) -> bool:
    """弹出 Fluent 确认框，避免系统 QMessageBox 在抽屉/弹窗内显示为黑底。"""
    host = parent.window() if parent is not None else parent
    box = MessageBox(title, content, host)
    box.yesButton.setText(yes_text)
    box.cancelButton.setText(cancel_text)
    return bool(box.exec())
