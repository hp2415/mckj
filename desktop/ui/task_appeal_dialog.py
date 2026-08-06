"""任务申诉弹窗（Fluent MessageBoxBase，与拨号选择等弹窗风格一致）。"""
from __future__ import annotations

from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QWidget

from qfluentwidgets import (
    CaptionLabel,
    FlowLayout,
    LineEdit,
    MessageBoxBase,
    SubtitleLabel,
    ToggleButton,
)

# ---------------------------------------------------------------------------
# 申诉原因选项：可在此调整文案与补充说明最低字数
# （0 = 选填且不设最低字数；>0 = 必填且不少于该字数）
# ---------------------------------------------------------------------------
APPEAL_REASON_OPTIONS: list[tuple[str, int]] = [
    ("频率过高", 10),
    ("时间不符", 10),
    ("已采购", 0),
    ("不负责", 0),
    ("被删除", 0),
    ("无预算", 0),
    ("同事", 0),
    ("工作人员", 0),
    ("其他", 10),
]


class TaskAppealDialog(MessageBoxBase):
    """选择申诉原因 + 可选补充说明。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reason: str | None = None
        self._selected_label: str | None = None
        self._min_len = 0

        self.titleLabel = SubtitleLabel("任务申诉", self)
        hint = CaptionLabel("请选择申诉原因（将用于优化任务分配）：", self)
        hint.setWordWrap(True)

        self._btn_host = QWidget(self)
        self._flow = FlowLayout(self._btn_host, needAni=False)
        self._flow.setContentsMargins(0, 0, 0, 0)
        self._flow.setHorizontalSpacing(8)
        self._flow.setVerticalSpacing(8)

        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)
        self._reason_btns: list[ToggleButton] = []
        for label, min_len in APPEAL_REASON_OPTIONS:
            btn = ToggleButton(label, self._btn_host)
            btn.setCheckable(True)
            btn.setProperty("appeal_label", label)
            btn.setProperty("appeal_min_len", int(min_len))
            self._btn_group.addButton(btn)
            self._flow.addWidget(btn)
            self._reason_btns.append(btn)
        self._btn_group.buttonToggled.connect(self._on_reason_toggled)

        self._detail_hint = CaptionLabel("", self)
        self._detail_edit = LineEdit(self)
        self._detail_edit.setClearButtonEnabled(True)
        self._detail_edit.setPlaceholderText("请先选择申诉原因")
        self._detail_edit.setEnabled(False)
        self._detail_edit.textChanged.connect(self._on_detail_changed)

        counter_row = QHBoxLayout()
        counter_row.setContentsMargins(0, 0, 0, 0)
        counter_row.addStretch(1)
        self._counter_lbl = CaptionLabel("", self)
        counter_row.addWidget(self._counter_lbl)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(hint)
        self.viewLayout.addWidget(self._btn_host)
        self.viewLayout.addWidget(self._detail_hint)
        self.viewLayout.addWidget(self._detail_edit)
        self.viewLayout.addLayout(counter_row)

        self.yesButton.setText("提交申诉")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(420)
        self._sync_detail_ui()
        self.widget.adjustSize()

    def _on_reason_toggled(self, button, checked: bool):
        if not checked:
            if self._btn_group.checkedButton() is None:
                self._selected_label = None
                self._min_len = 0
                self._sync_detail_ui()
            return
        self._selected_label = str(button.property("appeal_label") or button.text())
        try:
            self._min_len = int(button.property("appeal_min_len") or 0)
        except (TypeError, ValueError):
            self._min_len = 0
        self._detail_edit.blockSignals(True)
        self._detail_edit.clear()
        self._detail_edit.blockSignals(False)
        self._sync_detail_ui()

    def _sync_detail_ui(self):
        has_reason = self._selected_label is not None
        self._detail_edit.setEnabled(has_reason)
        if not has_reason:
            self._detail_edit.setPlaceholderText("请先选择申诉原因")
            self._detail_hint.setText("")
            self._counter_lbl.setText("")
            return

        if self._min_len > 0:
            self._detail_edit.setPlaceholderText(f"必填，至少 {self._min_len} 字")
            self._detail_hint.setText(f"补充说明：（必填，至少 {self._min_len} 字）")
        else:
            self._detail_edit.setPlaceholderText("选填，不限字数")
            self._detail_hint.setText("补充说明：（选填）")
        self._on_detail_changed(self._detail_edit.text())

    def _on_detail_changed(self, text: str):
        if self._selected_label is None:
            self._counter_lbl.setText("")
            return
        n = len((text or "").strip())
        if self._min_len > 0:
            self._counter_lbl.setText(f"{n}/{self._min_len}")
        else:
            self._counter_lbl.setText(f"{n} 字" if n else "")

    def validate(self) -> bool:
        if not self._selected_label:
            self._detail_hint.setText("请先选择一个申诉原因")
            return False
        detail = (self._detail_edit.text() or "").strip()
        if self._min_len > 0 and len(detail) < self._min_len:
            self._detail_hint.setText(
                f"补充说明至少 {self._min_len} 字（当前 {len(detail)} 字）"
            )
            self._detail_edit.setFocus()
            return False
        self._reason = (
            f"{self._selected_label}：{detail}" if detail else self._selected_label
        )
        return bool(self._reason)

    def reason(self) -> str | None:
        return self._reason


def ask_task_appeal(parent: QWidget | None) -> str | None:
    """弹出申诉对话框；确认则返回申诉原因字符串，取消则返回 None。"""
    host = parent.window() if parent is not None else parent
    dlg = TaskAppealDialog(host)
    if not dlg.exec():
        return None
    reason = (dlg.reason() or "").strip()
    return reason or None
