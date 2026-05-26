"""任务分配单条任务卡片：展示任务核心信息 + 完成/跳过/恢复待办 等快捷操作。

设计要点：
- 左侧色条 + 类型徽章直观区分主线/破冰任务；
- 客户、单位、备注分行展示，方便窄侧栏 / 全宽两种容器都能看清；
- 执行说明 (instruction) 自动换行，必要时可由父级控制最大行数；
- 状态右上角徽章 + 截止时间 + 完成/跳过按钮，遵循「同行管理后台一致」的语义化色彩。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    PushButton,
    StrongBodyLabel,
    isDarkTheme,
)


TASK_KIND_LABELS: dict[str, str] = {
    "contact": "联系",
    "follow_up": "跟进",
    "close_deal": "促单",
    "revisit": "回访",
    "icebreaker": "破冰",
}

# 类型 → (前景色, 背景色) 浅/深主题共用前景，背景透明度做区分
_KIND_COLORS: dict[str, tuple[str, str]] = {
    "contact": ("#1890ff", "rgba(24,144,255,0.14)"),
    "follow_up": ("#13c2c2", "rgba(19,194,194,0.14)"),
    "close_deal": ("#fa541c", "rgba(250,84,28,0.14)"),
    "revisit": ("#722ed1", "rgba(114,46,209,0.14)"),
    "icebreaker": ("#fa8c16", "rgba(250,140,22,0.16)"),
}

_STATUS_LABELS: dict[str, str] = {
    "pending": "待办",
    "in_progress": "进行中",
    "done": "已完成",
    "skipped": "已跳过",
    "overdue": "已逾期",
}

# 状态 → (前景色, 背景色)
_STATUS_COLORS: dict[str, tuple[str, str]] = {
    "pending": ("#1890ff", "rgba(24,144,255,0.14)"),
    "in_progress": ("#faad14", "rgba(250,173,20,0.16)"),
    "done": ("#52c41a", "rgba(82,196,26,0.16)"),
    "skipped": ("#8c8c8c", "rgba(140,140,140,0.18)"),
    "overdue": ("#ff4d4f", "rgba(255,77,79,0.16)"),
}


class TaskCardWidget(QFrame):
    """单条联系任务卡片。"""

    # (task_id, op) → "done" | "skip" | "pending"
    action_triggered = Signal(int, str)
    # 点击卡片主体（非操作按钮）→ 跳转客户对话
    open_chat_requested = Signal(dict)

    def __init__(self, task: dict, parent=None):
        super().__init__(parent)
        self.task: dict = task or {}
        self.setObjectName("TaskCard")
        self.setFrameShape(QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击进入客户对话，并自动生成开场白")

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        # ── 顶行：序号 + 类型徽章 + 状态徽章 ──
        head = QHBoxLayout()
        head.setSpacing(6)
        head.setContentsMargins(0, 0, 0, 0)

        rank = self.task.get("priority_rank")
        self.rank_lbl = CaptionLabel(f"#{rank}" if rank is not None else "#-")
        self.rank_lbl.setObjectName("TaskRankLabel")
        head.addWidget(self.rank_lbl)

        self.kind_lbl = CaptionLabel(self._kind_text())
        self.kind_lbl.setObjectName("TaskKindBadge")
        head.addWidget(self.kind_lbl)

        head.addStretch(1)

        self.status_lbl = CaptionLabel(self._status_text())
        self.status_lbl.setObjectName("TaskStatusBadge")
        head.addWidget(self.status_lbl, 0, Qt.AlignRight)
        root.addLayout(head)

        # ── 客户信息 ──
        cust_name = (self.task.get("customer_name") or "").strip() or "（未登记客户）"
        self.customer_lbl = StrongBodyLabel(cust_name)
        self.customer_lbl.setWordWrap(True)
        root.addWidget(self.customer_lbl)

        sub_parts = []
        unit = (self.task.get("unit_name") or "").strip()
        if unit:
            sub_parts.append(unit)
        wxmark = (self.task.get("wechat_remark") or "").strip()
        if wxmark:
            sub_parts.append(f"备注: {wxmark}")
        if sub_parts:
            self.sub_lbl = CaptionLabel(" · ".join(sub_parts))
            self.sub_lbl.setWordWrap(True)
            root.addWidget(self.sub_lbl)
        else:
            self.sub_lbl = None

        # ── 任务标题 ──
        title = (self.task.get("title") or "").strip()
        if title:
            self.title_lbl = BodyLabel(title)
            self.title_lbl.setWordWrap(True)
            self.title_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            root.addWidget(self.title_lbl)
        else:
            self.title_lbl = None

        # ── 执行说明 ──
        instr = (self.task.get("instruction") or "").strip()
        if instr:
            self.instr_lbl = CaptionLabel(instr)
            self.instr_lbl.setWordWrap(True)
            self.instr_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            root.addWidget(self.instr_lbl)
        else:
            self.instr_lbl = None

        # ── 底部：截止日期 + 快捷操作 ──
        self._foot_layout = QHBoxLayout()
        self._foot_layout.setSpacing(6)
        self._foot_layout.setContentsMargins(0, 2, 0, 0)
        self.due_lbl = CaptionLabel("")
        self._buttons: list[QWidget] = []
        root.addLayout(self._foot_layout)
        self._rebuild_footer()
        self._apply_theme_style()

    # ── helpers ──
    def _kind_text(self) -> str:
        kind = (self.task.get("task_kind") or "contact").strip()
        return TASK_KIND_LABELS.get(kind, kind or "联系")

    def _status_text(self) -> str:
        st = (self.task.get("status") or "pending").strip()
        return _STATUS_LABELS.get(st, st)

    def _fmt_due_date(self) -> str:
        due = self.task.get("due_date")
        if not due:
            return ""
        # ContactTaskOut.due_date 来自后端 schemas，序列化后通常为 "YYYY-MM-DD"
        s = str(due)
        return s[:10]

    def _emit_action(self, op: str):
        tid = self.task.get("id")
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return
        self.action_triggered.emit(tid, op)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            w = self.childAt(pos)
            while w is not None and w is not self:
                if isinstance(w, PushButton):
                    return super().mouseReleaseEvent(event)
                w = w.parent()
            self.open_chat_requested.emit(dict(self.task))
        super().mouseReleaseEvent(event)

    def _clear_footer_actions(self):
        """仅移除 stretch 与操作按钮，保留 due_lbl（避免 deleteLater 后悬空引用）。"""
        while self._foot_layout.count() > 1:
            item = self._foot_layout.takeAt(self._foot_layout.count() - 1)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._buttons = []

    def _rebuild_footer(self):
        """重建底部操作区（状态变更时复用，避免整表销毁）。"""
        self._clear_footer_actions()
        due = self._fmt_due_date()
        self.due_lbl.setText(f"截止 {due}" if due else "无截止日期")
        if self._foot_layout.indexOf(self.due_lbl) < 0:
            self._foot_layout.insertWidget(0, self.due_lbl)
        self._foot_layout.addStretch(1)

        status = (self.task.get("status") or "pending").strip()
        if status in ("pending", "in_progress", "overdue"):
            btn_done = PushButton("完成")
            btn_done.setFixedHeight(26)
            btn_done.clicked.connect(lambda: self._emit_action("done"))
            self._foot_layout.addWidget(btn_done)
            self._buttons.append(btn_done)

            btn_skip = PushButton("跳过")
            btn_skip.setFixedHeight(26)
            btn_skip.clicked.connect(lambda: self._emit_action("skip"))
            self._foot_layout.addWidget(btn_skip)
            self._buttons.append(btn_skip)
        else:
            btn_restore = PushButton("改待办")
            btn_restore.setFixedHeight(26)
            btn_restore.setToolTip("将此任务恢复为待办状态")
            btn_restore.clicked.connect(lambda: self._emit_action("restore"))
            self._foot_layout.addWidget(btn_restore)
            self._buttons.append(btn_restore)

    def update_task(self, task: dict):
        """就地更新任务展示（完成/跳过/恢复后局部刷新）。"""
        self.task = task or {}
        rank = self.task.get("priority_rank")
        self.rank_lbl.setText(f"#{rank}" if rank is not None else "#-")
        self.kind_lbl.setText(self._kind_text())
        self.status_lbl.setText(self._status_text())
        self._rebuild_footer()
        self._apply_theme_style()
        self.updateGeometry()

    # ── theme ──
    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        # 深色模式：卡片用明显的深灰背景以便与页面背景产生层次感
        card_bg = "#2e2e2e" if is_dark else "#ffffff"
        card_border = "rgba(255,255,255,0.12)" if is_dark else "rgba(0,0,0,0.09)"
        text_main = "#e8e8e8" if is_dark else "#1a1a1a"
        text_sub = "#999999" if is_dark else "#666666"

        kind = (self.task.get("task_kind") or "contact").strip()
        kind_fg, kind_bg = _KIND_COLORS.get(kind, _KIND_COLORS["contact"])

        st = (self.task.get("status") or "pending").strip()
        st_fg, st_bg = _STATUS_COLORS.get(st, _STATUS_COLORS["pending"])

        # 卡片整体：左侧 4px 色条 + 圆角边框
        side_color = st_fg if st in ("overdue", "done", "skipped") else kind_fg
        self.setStyleSheet(
            f"""
            QFrame#TaskCard {{
                background-color: {card_bg};
                border: 1px solid {card_border};
                border-left: 4px solid {side_color};
                border-radius: 8px;
            }}
            """
        )

        self.rank_lbl.setStyleSheet(
            f"color: {text_sub}; font-weight: bold; font-size: 11px;"
        )
        self.kind_lbl.setStyleSheet(
            "QLabel#TaskKindBadge {"
            f" color: {kind_fg};"
            f" background-color: {kind_bg};"
            f" border: 1px solid {kind_fg}55;"
            " padding: 1px 8px;"
            " border-radius: 8px;"
            " font-size: 11px;"
            " font-weight: bold;"
            "}"
        )
        self.status_lbl.setStyleSheet(
            "QLabel#TaskStatusBadge {"
            f" color: {st_fg};"
            f" background-color: {st_bg};"
            f" border: 1px solid {st_fg}55;"
            " padding: 1px 8px;"
            " border-radius: 8px;"
            " font-size: 11px;"
            " font-weight: bold;"
            "}"
        )
        self.customer_lbl.setStyleSheet(
            f"color: {text_main}; font-weight: bold; font-size: 13px;"
        )
        if self.sub_lbl is not None:
            self.sub_lbl.setStyleSheet(f"color: {text_sub}; font-size: 11px;")
        if self.title_lbl is not None:
            self.title_lbl.setStyleSheet(f"color: {text_main}; font-size: 12px;")
        if self.instr_lbl is not None:
            self.instr_lbl.setStyleSheet(
                f"color: {text_sub}; font-size: 11px; line-height: 16px;"
            )
        self.due_lbl.setStyleSheet(f"color: {text_sub}; font-size: 11px;")

        # 操作按钮：深色/浅色模式下都给出明确的边框与文字颜色
        if is_dark:
            btn_fg = "#cccccc"
            btn_bg = "rgba(255,255,255,0.07)"
            btn_border = "rgba(255,255,255,0.15)"
            btn_hover = "rgba(255,255,255,0.14)"
        else:
            btn_fg = "#444444"
            btn_bg = "rgba(0,0,0,0.04)"
            btn_border = "rgba(0,0,0,0.12)"
            btn_hover = "rgba(0,0,0,0.09)"
        btn_style = (
            f"QPushButton {{ color: {btn_fg}; background-color: {btn_bg};"
            f" border: 1px solid {btn_border}; border-radius: 5px;"
            f" padding: 1px 10px; font-size: 11px; }}"
            f"QPushButton:hover {{ background-color: {btn_hover}; border-color: #07c160; }}"
            f"QPushButton:pressed {{ background-color: rgba(7,193,96,0.18);"
            f" border-color: #07c160; color: #07c160; }}"
        )
        for btn in self._buttons:
            btn.setStyleSheet(btn_style)
