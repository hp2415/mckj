"""桌面端「活动群发」页面。"""
from __future__ import annotations

import html

from PySide6.QtCore import QItemSelectionModel, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SearchLineEdit,
    SmoothScrollDelegate,
    SpinBox,
    StrongBodyLabel,
    SubtitleLabel,
    isDarkTheme,
)

from ui.app_fonts import style_label, text_palette


_STATUS_LABELS = {
    "pending": "待发送",
    "sent": "已发送",
    "failed": "失败",
    "skipped": "已跳过",
}

_COL_CHECK = 0
_COL_REMARK = 1
_COL_UNIT = 2
_COL_STATUS = 3
_COL_SCRIPT = 4
_COL_ERROR = 5
_COL_ACTIONS = 6


_COL_MIN_DEFAULT = 48
_COL_MIN_ACTIONS = 176
_COL_MIN_SCRIPT = 100

# 与后端 campaign_blast_llm.CHUNK_SIZE 对齐：桌面端按此分批请求，避免一次长连接超时
SCRIPT_GEN_BATCH_SIZE = 8

# 长文本悬浮提示最大宽度（px），避免话术/错误详情单行拉得过长
_TOOLTIP_MAX_WIDTH = 420
# 悬浮竖向滚动条贴在表格右侧，列宽计算时预留，避免挡住「操作」按钮
_OVERLAY_VSCROLL_RESERVE = 12


def _wrapping_tooltip(text: str, max_width: int = _TOOLTIP_MAX_WIDTH) -> str:
    """将纯文本转为可换行的富文本 ToolTip（Qt 纯文本提示默认不折行）。"""
    text = (text or "").strip()
    if not text:
        return ""
    escaped = html.escape(text).replace("\n", "<br>")
    # 使用固定 width（而非 max-width），QToolTip 内部 QLabel 才能稳定按宽度折行
    return (
        f'<div style="width:{int(max_width)}px;'
        f' white-space:pre-wrap; word-wrap:break-word;">{escaped}</div>'
    )


def _enable_resizable_columns(
    table: QTableWidget,
    defaults: dict[int, int],
    *,
    fixed_cols: tuple[int, ...] = (0,),
    stretch_col: int | None = None,
    col_mins: dict[int, int] | None = None,
):
    """勾选列固定，其余可拖；窗口缩放时弹性列吃剩余宽度。最后一列用 Fixed，避免窗口缩小被 Qt 挤没。"""
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.Interactive)
    header.setMinimumSectionSize(36)
    header.setStretchLastSection(False)
    header.setCascadingSectionResizes(False)
    header.setSectionsClickable(True)
    n = table.columnCount()
    mins = [_COL_MIN_DEFAULT] * n
    mins[0] = 40
    if n >= 7:
        mins[_COL_SCRIPT] = _COL_MIN_SCRIPT
        mins[_COL_ACTIONS] = _COL_MIN_ACTIONS
        mins[_COL_ERROR] = 72
        mins[_COL_UNIT] = 64
        mins[_COL_STATUS] = 64
        mins[_COL_REMARK] = 72
    elif n > 1:
        mins[n - 1] = 72
    if col_mins:
        for col, m in col_mins.items():
            if 0 <= col < n:
                mins[col] = int(m)
    for col in fixed_cols:
        header.setSectionResizeMode(col, QHeaderView.Fixed)
        header.resizeSection(col, defaults.get(col, 40))
        if 0 <= col < n:
            mins[col] = int(defaults.get(col, 40))
    for col, width in defaults.items():
        if col not in fixed_cols:
            header.resizeSection(col, width)
    last = n - 1
    if stretch_col is not None and 0 <= int(stretch_col) < last:
        # 最后一列仍可通过其左边界拖动（见 _trade_user_column_resize），但不让 Qt 在窗口缩放时挤压它
        header.setSectionResizeMode(last, QHeaderView.Fixed)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.TextElideMode.ElideRight)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    table.setMinimumWidth(0)
    table.setProperty("_blast_flex_col", stretch_col)
    table.setProperty("_blast_fixed_cols", list(fixed_cols))
    table.setProperty("_blast_col_mins", mins)


def _viewport_width(table: QTableWidget) -> int:
    vw = table.viewport().width()
    if vw <= 0:
        extra = table.verticalScrollBar().sizeHint().width() if table.verticalScrollBar().isVisible() else 0
        vw = max(0, table.width() - extra)
    delegate = getattr(table, "_smooth_scroll", None)
    bar = getattr(delegate, "vScrollBar", None) if delegate is not None else None
    if bar is not None and bar.maximum() > 0 and bar.isVisible():
        vw = max(0, vw - _OVERLAY_VSCROLL_RESERVE)
    return vw


def _table_col_meta(table: QTableWidget) -> tuple[int, int, tuple[int, ...], list[int]]:
    n = table.columnCount()
    raw_flex = table.property("_blast_flex_col")
    flex_col = int(raw_flex) if raw_flex is not None else max(0, n - 1)
    raw_fixed = table.property("_blast_fixed_cols")
    fixed_cols = tuple(raw_fixed) if isinstance(raw_fixed, list) else (0,)
    raw_mins = table.property("_blast_col_mins")
    mins = list(raw_mins) if isinstance(raw_mins, list) and len(raw_mins) == n else [_COL_MIN_DEFAULT] * n
    flex_col = max(0, min(n - 1, flex_col))
    return n, flex_col, fixed_cols, mins


def _min_for(col: int, mins: list[int], fixed_cols: tuple[int, ...]) -> int:
    if col in fixed_cols:
        return mins[col] if 0 <= col < len(mins) else 40
    if 0 <= col < len(mins):
        return max(36, int(mins[col]))
    return _COL_MIN_DEFAULT


def _apply_section_sizes(header: QHeaderView, sizes: list[int], fixed_cols: tuple[int, ...]):
    header.blockSignals(True)
    try:
        for i, w in enumerate(sizes):
            header.resizeSection(i, max(1, int(w)))
    finally:
        header.blockSignals(False)


def _restore_cell_widgets(table: QTableWidget):
    """列被挤出视口时 Qt 会把 cellWidget hide，宽度恢复后需重新 show。"""
    model = table.model()
    if model is None:
        return
    for row in range(table.rowCount()):
        for col in range(table.columnCount()):
            w = table.cellWidget(row, col)
            if w is None:
                continue
            rect = table.visualRect(model.index(row, col))
            if rect.isValid() and not rect.isEmpty():
                w.setGeometry(rect)
            w.setVisible(True)


def _schedule_restore_cell_widgets(table: QTableWidget):
    _restore_cell_widgets(table)
    QTimer.singleShot(0, lambda t=table: _restore_cell_widgets(t))


def _fill_flex_into_sizes(
    sizes: list[int],
    *,
    n: int,
    flex_col: int,
    fixed_cols: tuple[int, ...],
    mins: list[int],
    vw: int,
    protect_col: int | None = None,
):
    """使各列达到最小宽，并由弹性列吃掉与视口的差额。"""
    for i in range(n):
        sizes[i] = max(int(sizes[i]), _min_for(i, mins, fixed_cols))
    if vw <= 0:
        return
    min_flex = _min_for(flex_col, mins, fixed_cols)
    last = n - 1
    others = sum(sizes[i] for i in range(n) if i != flex_col)
    remain = vw - others
    if remain >= min_flex:
        sizes[flex_col] = remain
        return
    sizes[flex_col] = min_flex
    overflow = sum(sizes) - vw
    shrinkable = [
        i
        for i in range(n)
        if i not in fixed_cols and i != flex_col and i != last and i != protect_col
    ]
    for i in reversed(shrinkable):
        if overflow <= 0:
            break
        room = sizes[i] - _min_for(i, mins, fixed_cols)
        take = min(max(0, room), overflow)
        sizes[i] -= take
        overflow -= take
    if overflow > 0 and protect_col is not None and protect_col != flex_col:
        room = sizes[protect_col] - _min_for(protect_col, mins, fixed_cols)
        take = min(max(0, room), overflow)
        sizes[protect_col] -= take
        overflow -= take
    others = sum(sizes[i] for i in range(n) if i != flex_col)
    sizes[flex_col] = max(min_flex, vw - others)


def _fill_flex_to_viewport(table: QTableWidget):
    """仅在窗口缩放时调用：多余/不足都由弹性列承担，尽量不压操作列。"""
    n, flex_col, fixed_cols, mins = _table_col_meta(table)
    if n <= 0:
        return
    vw = _viewport_width(table)
    if vw <= 0:
        return
    header = table.horizontalHeader()
    sizes = [header.sectionSize(i) for i in range(n)]
    _fill_flex_into_sizes(
        sizes, n=n, flex_col=flex_col, fixed_cols=fixed_cols, mins=mins, vw=vw
    )
    _apply_section_sizes(header, sizes, fixed_cols)


def _trade_user_column_resize(table: QTableWidget, logical_index: int, old_size: int, new_size: int):
    """用户拖列时只改目标列，差额全部由弹性列（发送话术）承担，不与邻列互换。"""
    n, flex_col, fixed_cols, mins = _table_col_meta(table)
    if n <= 0 or logical_index in fixed_cols:
        return
    header = table.horizontalHeader()
    delta = int(new_size) - int(old_size)
    if delta == 0:
        return
    sizes = [header.sectionSize(i) for i in range(n)]
    last = n - 1
    # Qt 已经把 logical_index 改成 new_size。默认只动这一列；
    # 弹性列的右边界 = 下一列的左边界，改下一列；最后一列没有右边界，改最后一列。
    if logical_index == flex_col:
        target = logical_index + 1
        while target < n and target in fixed_cols:
            target += 1
        if target >= n:
            _fill_flex_into_sizes(
                sizes, n=n, flex_col=flex_col, fixed_cols=fixed_cols, mins=mins,
                vw=_viewport_width(table),
            )
            _apply_section_sizes(header, sizes, fixed_cols)
            return
        sizes[logical_index] = int(old_size)
        sizes[target] = sizes[target] - delta
    elif logical_index == last - 1 and last != flex_col:
        sizes[logical_index] = int(old_size)
        target = last
        sizes[target] = sizes[target] - delta
    else:
        target = logical_index
        sizes[target] = int(new_size)

    min_t = _min_for(target, mins, fixed_cols)
    vw = _viewport_width(table)
    min_flex = _min_for(flex_col, mins, fixed_cols)
    other_sum = sum(sizes[i] for i in range(n) if i not in (flex_col, target))
    if vw > 0:
        max_t = max(min_t, vw - min_flex - other_sum)
        sizes[target] = min(max(min_t, sizes[target]), max_t)
    else:
        sizes[target] = max(min_t, sizes[target])
    _fill_flex_into_sizes(
        sizes,
        n=n,
        flex_col=flex_col,
        fixed_cols=fixed_cols,
        mins=mins,
        vw=vw,
        protect_col=target,
    )
    _apply_section_sizes(header, sizes, fixed_cols)


def _check_item(*, user_data=None) -> QTableWidgetItem:
    item = QTableWidgetItem("")
    item.setFlags(
        Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
    )
    item.setCheckState(Qt.CheckState.Unchecked)
    if user_data is not None:
        item.setData(Qt.UserRole, user_data)
    item.setTextAlignment(Qt.AlignCenter)
    return item


class _BlastSelectTable(QTableWidget):
    """行点击：选中/再点取消（已选行再点只取消该行）；勾选列点击只切换该行且保留其它已选。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._hover_row = -1
        self._deselect_row: int | None = None
        self._press_pos = None
        self._fitting_cols = False
        self.apply_chrome()
        self._smooth_scroll = SmoothScrollDelegate(self, useAni=False)
        self._patch_overlay_scrollbar()
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.verticalScrollBar().setSingleStep(24)
        self.verticalScrollBar().rangeChanged.connect(self._on_vscroll_range)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_overlay_scrollbar()
        if self.property("_blast_flex_col") is None:
            return
        self._fit_columns()

    def _on_vscroll_range(self, *_):
        if self.property("_blast_flex_col") is None:
            return
        self._fit_columns()

    def _patch_overlay_scrollbar(self):
        """Fluent 默认滚动条盖住整表（含表头）；改到表头下方，并避免表头右侧空白露白。"""
        d = getattr(self, "_smooth_scroll", None)
        if d is None:
            return
        bar = d.vScrollBar
        bar.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        bar.setAutoFillBackground(False)
        table = self

        def _adjust_pos(size):
            header = table.horizontalHeader()
            top = header.height() if header is not None and header.isVisible() else 0
            top = max(1, top)
            bar.resize(12, max(0, size.height() - top - 1))
            bar.move(size.width() - 13, top)

        bar._adjustPos = _adjust_pos
        self._layout_overlay_scrollbar()

    def _layout_overlay_scrollbar(self):
        d = getattr(self, "_smooth_scroll", None)
        if d is None:
            return
        d.vScrollBar._adjustPos(self.size())
        d.vScrollBar.raise_()
        d.hScrollBar.raise_()

    def _fit_columns(self):
        if self._fitting_cols:
            return
        self._fitting_cols = True
        try:
            _fill_flex_to_viewport(self)
            _schedule_restore_cell_widgets(self)
            self._layout_overlay_scrollbar()
        finally:
            self._fitting_cols = False

    def apply_chrome(self):
        """表格底色 / 字色 / 选中底色随主题刷新。"""
        if isDarkTheme():
            bg = "#2a2a2a"
            text = "#ebebeb"
            header_bg = "#323232"
            header_text = "#b3b3b3"
            grid = "#3a3a3a"
            selected = "#454545"
            selected_text = "#f2f2f2"
        else:
            bg = "#ffffff"
            text = "#141414"
            header_bg = "#f0f1f3"
            header_text = "#3d3d3d"
            grid = "#e5e5e5"
            selected = "#DBDBDB"
            selected_text = "#222222"
        self.setStyleSheet(
            f"""
            QTableWidget {{
                background-color: {bg};
                color: {text};
                gridline-color: {grid};
                border: 1px solid {grid};
                outline: none;
            }}
            QTableWidget::item {{
                color: {text};
            }}
            QHeaderView {{
                background-color: {header_bg};
            }}
            QHeaderView::section {{
                background-color: {header_bg};
                color: {header_text};
                border: none;
                border-right: 1px solid {grid};
                border-bottom: 1px solid {grid};
                padding: 4px 6px;
            }}
            QTableCornerButton::section {{
                background-color: {header_bg};
                border: none;
                border-bottom: 1px solid {grid};
            }}
            QAbstractScrollArea::corner {{
                background-color: {header_bg};
                border: none;
            }}
            QTableWidget::item:selected {{
                background-color: {selected};
                color: {selected_text};
            }}
            QTableWidget::item:selected:active {{
                background-color: {selected};
                color: {selected_text};
            }}
            QTableWidget QScrollBar:vertical {{
                width: 0px;
                background: transparent;
            }}
            QTableWidget QScrollBar:horizontal {{
                height: 0px;
                background: transparent;
            }}
            """
        )
        self._layout_overlay_scrollbar()
        d = getattr(self, "_smooth_scroll", None)
        if d is not None:
            d.vScrollBar.update()
            d.vScrollBar.handle.update()

    def mousePressEvent(self, event: QMouseEvent):
        self._deselect_row = None
        self._press_pos = None
        if event.button() == Qt.MouseButton.LeftButton:
            mods = event.modifiers()
            shift_ctrl = mods & (
                Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier
            )
            idx = self.indexAt(event.pos())
            if idx.isValid() and not shift_ctrl:
                # 勾选列：只切换当前行，不丢掉其它已选
                if idx.column() == 0:
                    self._toggle_row(idx.row())
                    self.setFocus(Qt.FocusReason.MouseFocusReason)
                    event.accept()
                    return
                sm = self.selectionModel()
                if sm and sm.isRowSelected(idx.row(), self.rootIndex()):
                    # 已选行再点：松手后只取消该行，避免 Qt 先清空其它选中
                    self._deselect_row = idx.row()
                    self._press_pos = event.pos()
                    self.setFocus(Qt.FocusReason.MouseFocusReason)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        super().mouseMoveEvent(event)
        idx = self.indexAt(event.pos())
        self._set_hover_row(idx.row() if idx.isValid() else -1)

    def mouseReleaseEvent(self, event: QMouseEvent):
        deselect = self._deselect_row
        press = self._press_pos
        self._deselect_row = None
        self._press_pos = None
        if (
            event.button() == Qt.MouseButton.LeftButton
            and deselect is not None
            and press is not None
            and (event.pos() - press).manhattanLength() <= 6
        ):
            sm = self.selectionModel()
            model = self.model()
            if sm is not None and model is not None:
                sm.select(
                    model.index(deselect, 0),
                    QItemSelectionModel.SelectionFlag.Deselect
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
            self._hover_row = -1
            self._set_hover_row(deselect)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _toggle_row(self, row: int):
        sm = self.selectionModel()
        model = self.model()
        if sm is None or model is None or row < 0:
            return
        sm.select(
            model.index(row, 0),
            QItemSelectionModel.SelectionFlag.Toggle
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        self._hover_row = -1
        self._set_hover_row(row)

    def leaveEvent(self, event):
        self._set_hover_row(-1)
        super().leaveEvent(event)

    def _set_hover_row(self, row: int):
        if self._hover_row == row:
            return
        self._clear_hover_bg(self._hover_row)
        self._hover_row = row
        if row < 0:
            return
        sm = self.selectionModel()
        if sm and sm.isRowSelected(row, self.rootIndex()):
            return
        alpha = 56 if isDarkTheme() else 40
        brush = QBrush(QColor(128, 128, 128, alpha))
        for col in range(self.columnCount()):
            item = self.item(row, col)
            if item is not None:
                item.setBackground(brush)

    def _clear_hover_bg(self, row: int):
        if row < 0:
            return
        for col in range(self.columnCount()):
            item = self.item(row, col)
            if item is not None:
                item.setBackground(QBrush())


class _EditScriptDialog(QDialog):
    def __init__(self, parent: QWidget | None, *, title: str, text: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(480, 320)
        layout = QVBoxLayout(self)
        self.edit = QTextEdit()
        self.edit.setPlainText(text or "")
        layout.addWidget(self.edit, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def script_text(self) -> str:
        return self.edit.toPlainText().strip()


class _AddRecipientsDialog(QDialog):
    """从销售号好友中检索，Excel 式拖选后批量加入。"""

    search_requested = Signal(str, int, object, str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        sales_wechat_id: str = "",
        campaign_id: int = 0,
        job_id: int | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("CampaignBlastAddDialog")
        self.setWindowTitle("添加客户到外发名单")
        self.resize(640, 520)
        self._sales_wechat_id = sales_wechat_id
        self._campaign_id = campaign_id
        self._job_id = job_id
        self._syncing = False

        layout = QVBoxLayout(self)
        self.hint_lbl = CaptionLabel(
            "检索当前执行微信号下的客户。单击/拖选/Shift 连选，Ctrl 点选，勾选后加入。"
        )
        layout.addWidget(self.hint_lbl)

        self.search = SearchLineEdit()
        self.search.setPlaceholderText("搜索昵称 / 备注 / 手机，回车或点搜索")
        self.search.searchSignal.connect(self._on_search)
        self.search.returnPressed.connect(self._on_search_return)
        layout.addWidget(self.search)

        hdr = QHBoxLayout()
        self.chk_select_all = CheckBox("全选当前结果")
        self.chk_select_all.toggled.connect(self._on_select_all)
        hdr.addWidget(self.chk_select_all)
        hdr.addStretch()
        layout.addLayout(hdr)

        self.table = _BlastSelectTable(0, 4)
        self.table.setHorizontalHeaderLabels(["选", "昵称", "备注", "单位性质"])
        _enable_resizable_columns(
            self.table,
            {0: 40, 1: 180, 2: 200, 3: 90},
            stretch_col=2,
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table, 1)
        self.table.horizontalHeader().sectionResized.connect(self._on_header_resized)

        self.status_lbl = CaptionLabel("")
        layout.addWidget(self.status_lbl)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("加入选中")
        self.buttons.accepted.connect(self._try_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._apply_theme_style()

    def _apply_theme_style(self):
        """深浅主题下统一弹窗背景与文字对比度。"""
        is_dark = isDarkTheme()
        if is_dark:
            bg = "#2b2b2b"
            border = "#3f3f3f"
            btn_bg = "#3a3a3a"
            btn_border = "#555555"
            btn_hover = "#454545"
            btn_text = "#f2f2f2"
        else:
            bg = "#ffffff"
            border = "#e5e5e5"
            btn_bg = "#f5f5f5"
            btn_border = "#d0d0d0"
            btn_hover = "#ebebeb"
            btn_text = "#1f1f1f"
        pal = text_palette()
        self.setStyleSheet(
            f"""
            QDialog#CampaignBlastAddDialog {{
                background-color: {bg};
                color: {pal.primary};
            }}
            QDialogButtonBox QPushButton {{
                background-color: {btn_bg};
                color: {btn_text};
                border: 1px solid {btn_border};
                border-radius: 6px;
                padding: 6px 14px;
                min-width: 72px;
            }}
            QDialogButtonBox QPushButton:hover {{
                background-color: {btn_hover};
            }}
            """
        )
        style_label(self.hint_lbl, "caption", color=pal.secondary)
        style_label(self.status_lbl, "caption", color=pal.tertiary)
        if hasattr(self, "table") and self.table is not None:
            self.table.apply_chrome()

    def _try_accept(self):
        if not self.selected_raw_customer_ids():
            self.status_lbl.setText("请先点选或拖选要加入的客户")
            return
        self.accept()

    def _on_search(self, kw: str):
        self.search_requested.emit(
            self._sales_wechat_id,
            self._campaign_id,
            self._job_id,
            (kw or "").strip(),
        )

    def _on_search_return(self):
        self._on_search(self.search.text())

    def _on_select_all(self, checked: bool):
        if self._syncing:
            return
        self._syncing = True
        try:
            if checked:
                self.table.selectAll()
            else:
                self.table.clearSelection()
            self._sync_checks_from_selection()
        finally:
            self._syncing = False

    def _on_selection_changed(self):
        if self._syncing:
            return
        self._syncing = True
        try:
            self._sync_checks_from_selection()
            total = self.table.rowCount()
            n = len(self.selected_raw_customer_ids())
            self.chk_select_all.setChecked(total > 0 and n == total)
        finally:
            self._syncing = False

    def _sync_checks_from_selection(self):
        sm = self.table.selectionModel()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if not item:
                continue
            selected = bool(sm and sm.isRowSelected(row, self.table.rootIndex()))
            item.setCheckState(
                Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked
            )

    def set_candidates(self, items: list[dict], total: int):
        self._syncing = True
        try:
            self.table.setRowCount(0)
            self.chk_select_all.setChecked(False)
            for it in items or []:
                row = self.table.rowCount()
                self.table.insertRow(row)
                rid = (it.get("raw_customer_id") or "").strip()
                self.table.setItem(row, 0, _check_item(user_data=rid))
                name = (it.get("display_name") or "").strip()
                remark = (it.get("remark") or "").strip()
                ut = (it.get("unit_type") or "").strip()
                name_item = QTableWidgetItem(name)
                name_item.setData(Qt.UserRole, rid)
                name_item.setToolTip(name)
                self.table.setItem(row, 1, name_item)
                remark_item = QTableWidgetItem(remark)
                remark_item.setToolTip(remark)
                self.table.setItem(row, 2, remark_item)
                self.table.setItem(row, 3, QTableWidgetItem(ut))
            shown = len(items or [])
            extra = ""
            if total > shown:
                extra = "；仅显示前 {} 条，请用搜索缩小范围".format(shown)
            self.status_lbl.setText(f"共 {total} 条可添加，当前显示 {shown} 条{extra}")
        finally:
            self._syncing = False
            self._fit_table()

    def selected_raw_customer_ids(self) -> list[str]:
        out: list[str] = []
        sm = self.table.selectionModel()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            selected = bool(sm and sm.isRowSelected(row, self.table.rootIndex()))
            checked = bool(item and item.checkState() == Qt.CheckState.Checked)
            if not (selected or checked):
                continue
            rid = str((item.data(Qt.UserRole) if item else "") or "").strip()
            if rid:
                out.append(rid)
        return out

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_table()

    def _on_header_resized(self, logical_index: int, old_size: int, new_size: int):
        if self.table._fitting_cols:
            return
        self.table._fitting_cols = True
        try:
            _trade_user_column_resize(self.table, logical_index, old_size, new_size)
            _schedule_restore_cell_widgets(self.table)
        finally:
            self.table._fitting_cols = False

    def _fit_table(self):
        self.table._fit_columns()


class CampaignBlastWidget(QFrame):
    """活动群发主页面。"""

    page_activated = Signal()
    create_job_requested = Signal(str, str, int, int)
    load_current_job_requested = Signal(str, int)
    running_campaigns_requested = Signal(str)
    add_recipients_dialog_search = Signal(str, int, object, str)
    add_recipients_requested = Signal(int, object)
    delete_recipients_requested = Signal(int, object)
    generate_scripts_requested = Signal(int, object)
    patch_script_requested = Signal(int, int, str)
    retry_failed_requested = Signal(int)
    start_send_requested = Signal(object)
    send_single_requested = Signal(int, int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CampaignBlastPage")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self._job: dict | None = None
        self._sending = False
        self._script_generating = False
        self._script_gen_cancel = False
        self._add_dialog: _AddRecipientsDialog | None = None
        self._sales_id_by_index: list[str] = []
        self._campaign_id_by_index: list[int] = []
        self._cancel_flag = False
        self._syncing_selection = False
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        title_row = QHBoxLayout()
        self.title_lbl = SubtitleLabel("活动群发")
        title_row.addWidget(self.title_lbl)
        title_row.addStretch()
        self.lbl_progress = CaptionLabel("")
        title_row.addWidget(self.lbl_progress)
        root.addLayout(title_row)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.lbl_sales = BodyLabel("执行微信号")
        filter_row.addWidget(self.lbl_sales)
        self.sales_combo = ComboBox()
        self.sales_combo.setMinimumWidth(0)
        self.sales_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.sales_combo.setPlaceholderText("请选择销售微信号")
        self.sales_combo.currentIndexChanged.connect(self._on_sales_or_campaign_changed)
        filter_row.addWidget(self.sales_combo, 1)

        self.lbl_unit = BodyLabel("单位性质")
        filter_row.addWidget(self.lbl_unit)
        self.unit_combo = ComboBox()
        self.unit_combo.setMinimumWidth(0)
        self.unit_combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.unit_combo.currentTextChanged.connect(self._on_unit_changed)
        filter_row.addWidget(self.unit_combo)

        self.lbl_campaign = BodyLabel("活动")
        filter_row.addWidget(self.lbl_campaign)
        self.campaign_combo = ComboBox()
        self.campaign_combo.setMinimumWidth(0)
        self.campaign_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.campaign_combo.currentIndexChanged.connect(self._on_sales_or_campaign_changed)
        filter_row.addWidget(self.campaign_combo, 1)
        root.addLayout(filter_row)

        gen_row = QHBoxLayout()
        gen_row.setSpacing(8)
        self.lbl_limit = BodyLabel("数量")
        gen_row.addWidget(self.lbl_limit)
        self.spin_limit = SpinBox()
        self.spin_limit.setRange(1, 500)
        self.spin_limit.setValue(100)
        self.spin_limit.setMinimumWidth(140)
        self.spin_limit.setFixedHeight(34)
        self.spin_limit.setAccelerated(True)
        gen_row.addWidget(self.spin_limit)

        self.btn_gen_list = PrimaryPushButton("生成外发名单")
        self.btn_gen_list.clicked.connect(self._on_gen_list)
        gen_row.addWidget(self.btn_gen_list)
        gen_row.addStretch()
        root.addLayout(gen_row)

        tbl_hdr = QHBoxLayout()
        self.lbl_list_title = StrongBodyLabel("外发名单")
        tbl_hdr.addWidget(self.lbl_list_title)
        tbl_hdr.addStretch()
        self.chk_select_all = CheckBox("全选")
        self.chk_select_all.toggled.connect(self._on_select_all)
        tbl_hdr.addWidget(self.chk_select_all)
        self.btn_add = PushButton("添加")
        self.btn_del = PushButton("删除选中")
        self.btn_add.clicked.connect(self._on_add)
        self.btn_del.clicked.connect(self._on_delete)
        tbl_hdr.addWidget(self.btn_add)
        tbl_hdr.addWidget(self.btn_del)
        root.addLayout(tbl_hdr)

        self.table = _BlastSelectTable(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["选", "客户备注", "单位性质", "发送状态", "发送话术", "错误详情", "操作"]
        )
        _enable_resizable_columns(
            self.table,
            {
                _COL_CHECK: 40,
                _COL_REMARK: 130,
                _COL_UNIT: 80,
                _COL_STATUS: 80,
                _COL_SCRIPT: 380,
                _COL_ERROR: 140,
                _COL_ACTIONS: 196,
            },
            stretch_col=_COL_SCRIPT,
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.table.horizontalHeader().sectionResized.connect(self._on_header_resized)
        root.addWidget(self.table, 1)

        action_row = QHBoxLayout()
        self.btn_gen_scripts = PrimaryPushButton("生成话术")
        self.btn_gen_scripts.clicked.connect(self._on_gen_scripts)
        action_row.addWidget(self.btn_gen_scripts)

        self.btn_send = PrimaryPushButton("确认批量发送")
        self.btn_send.clicked.connect(self._on_send)
        action_row.addWidget(self.btn_send)

        self.btn_retry = PushButton("重试失败项")
        self.btn_retry.clicked.connect(self._on_retry)
        self.btn_retry.setVisible(False)
        action_row.addWidget(self.btn_retry)

        self.btn_cancel_send = PushButton("取消发送")
        self.btn_cancel_send.clicked.connect(self._on_cancel_send)
        self.btn_cancel_send.setVisible(False)
        action_row.addWidget(self.btn_cancel_send)

        action_row.addStretch()
        self.lbl_stats = CaptionLabel("")
        action_row.addWidget(self.lbl_stats)
        root.addLayout(action_row)

        prog_row = QHBoxLayout()
        prog_row.setSpacing(8)
        self.script_progress = ProgressBar()
        self.script_progress.setRange(0, 100)
        self.script_progress.setValue(0)
        self.script_progress.setFixedHeight(6)
        self.script_progress.setVisible(False)
        prog_row.addWidget(self.script_progress, 1)
        self.lbl_script_progress = CaptionLabel("")
        self.lbl_script_progress.setVisible(False)
        prog_row.addWidget(self.lbl_script_progress)
        root.addLayout(prog_row)

        self._apply_theme_style()
        self._refresh_actions()

    def sizeHint(self) -> QSize:
        return QSize(0, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 240)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_table()

    def _on_header_resized(self, logical_index: int, old_size: int, new_size: int):
        if self.table._fitting_cols:
            return
        self.table._fitting_cols = True
        try:
            _trade_user_column_resize(self.table, logical_index, old_size, new_size)
            _schedule_restore_cell_widgets(self.table)
        finally:
            self.table._fitting_cols = False

    def _fit_table(self):
        if not hasattr(self, "table"):
            return
        self.table._fit_columns()

    def _apply_theme_style(self):
        """主题切换时同步页面背景与标签字色（须由主窗口 _toggle_theme 调用）。"""
        is_dark = isDarkTheme()
        bg = "#272727" if is_dark else "#f5f6f8"
        pal = text_palette()
        self.setStyleSheet(
            f"""
            QFrame#CampaignBlastPage {{
                background-color: {bg};
            }}
            """
        )
        style_label(self.title_lbl, "section", color=pal.primary)
        style_label(self.lbl_progress, "caption", color=pal.tertiary)
        for lbl in (
            self.lbl_sales,
            self.lbl_unit,
            self.lbl_campaign,
            self.lbl_limit,
        ):
            style_label(lbl, "body", color=pal.secondary)
        style_label(self.lbl_list_title, "body_emphasis", color=pal.primary)
        style_label(self.lbl_stats, "caption", color=pal.tertiary)
        if hasattr(self, "lbl_script_progress"):
            style_label(self.lbl_script_progress, "caption", color=pal.tertiary)
        if hasattr(self, "table") and self.table is not None:
            self.table.apply_chrome()
            # 清掉悬浮残留底色，避免切主题后灰块残留
            hover = getattr(self.table, "_hover_row", -1)
            if hasattr(self.table, "_clear_hover_bg"):
                self.table._clear_hover_bg(hover)
                self.table._hover_row = -1

    # 兼容旧调用名
    def _apply_theme(self):
        self._apply_theme_style()
        if isinstance(self.table, _BlastSelectTable):
            self.table.apply_chrome()

    def on_page_activated(self):
        self.page_activated.emit()

    def set_sales_options(self, bindings: list[dict]):
        current_sw = self.current_sales_wechat_id()
        self.sales_combo.blockSignals(True)
        self.sales_combo.clear()
        self._sales_id_by_index = []
        rows = sorted(
            bindings or [],
            key=lambda r: (not bool(r.get("is_primary")), str(r.get("label") or "")),
        )
        for r in rows:
            sw = str(r.get("sales_wechat_id") or "").strip()
            if not sw:
                continue
            nick = str(r.get("nickname") or r.get("alias_name") or "").strip()
            label = str(r.get("label") or "").strip()
            shown = nick or label or sw
            star = " ★" if r.get("is_primary") else ""
            self.sales_combo.addItem(f"{shown}{star}", userData=sw)
            self._sales_id_by_index.append(sw)
        if current_sw:
            idx = self._find_sales_index(current_sw)
            if idx >= 0:
                self.sales_combo.setCurrentIndex(idx)
        elif self.sales_combo.count() > 0:
            self.sales_combo.setCurrentIndex(0)
        self.sales_combo.blockSignals(False)
    def _find_sales_index(self, sw: str) -> int:
        sw = (sw or "").strip()
        for i, sid in enumerate(self._sales_id_by_index):
            if sid == sw:
                return i
        for i in range(self.sales_combo.count()):
            if str(self.sales_combo.itemData(i) or "").strip() == sw:
                return i
        return -1

    def set_unit_types(self, choices: list[str]):
        cur = self.unit_combo.currentText()
        self.unit_combo.clear()
        for ut in [str(x).strip() for x in (choices or []) if str(x).strip()]:
            self.unit_combo.addItem(ut, userData=ut)
        if cur:
            idx = self._find_unit_index(cur)
            if idx >= 0:
                self.unit_combo.setCurrentIndex(idx)

    def _find_unit_index(self, ut: str) -> int:
        for i in range(self.unit_combo.count()):
            if (self.unit_combo.itemData(i) or self.unit_combo.itemText(i)) == ut:
                return i
        return -1

    def set_running_campaigns(self, items: list[dict]):
        cur = self.current_campaign_id()
        self.campaign_combo.blockSignals(True)
        self.campaign_combo.clear()
        self._campaign_id_by_index = []
        for it in items or []:
            cid = int(it.get("id") or 0)
            if not cid:
                continue
            name = (it.get("name") or f"活动#{cid}").strip()
            if not it.get("has_active_posters"):
                name += "（无海报）"
            self.campaign_combo.addItem(name, userData=cid)
            self._campaign_id_by_index.append(cid)
        if cur:
            idx = self._find_campaign_index(cur)
            if idx >= 0:
                self.campaign_combo.setCurrentIndex(idx)
        self.campaign_combo.blockSignals(False)
        self._emit_load_current()

    def _find_campaign_index(self, cid: int) -> int:
        for i, x in enumerate(self._campaign_id_by_index):
            if int(x) == int(cid):
                return i
        for i in range(self.campaign_combo.count()):
            try:
                if int(self.campaign_combo.itemData(i) or 0) == int(cid):
                    return i
            except (TypeError, ValueError):
                continue
        return -1

    def apply_job(self, job: dict | None):
        self._job = job
        self._render_table()
        self._refresh_actions()

    def set_busy(self, busy: bool, message: str = ""):
        generating = bool(getattr(self, "_script_generating", False))
        for w in (
            self.btn_gen_list,
            self.btn_add,
            self.btn_del,
            self.btn_gen_scripts,
            self.btn_send,
            self.btn_retry,
            self.sales_combo,
            self.unit_combo,
            self.campaign_combo,
            self.spin_limit,
        ):
            w.setEnabled(not busy and not generating)
        if not generating:
            self.lbl_progress.setText(message if busy else "")

    def begin_script_generation(self, total: int):
        self._script_generating = True
        self._script_gen_cancel = False
        total = max(0, int(total))
        self.script_progress.setVisible(True)
        self.script_progress.setRange(0, max(1, total))
        self.script_progress.setValue(0)
        self.lbl_script_progress.setVisible(True)
        self.lbl_script_progress.setText(f"0/{total}")
        self.btn_cancel_send.setText("停止生成")
        self.btn_cancel_send.setVisible(True)
        self.set_busy(True, "正在按批生成话术…")
        self.lbl_progress.setText("正在按批生成话术…")

    def set_script_gen_progress(
        self,
        *,
        done: int,
        total: int,
        generated: int,
        failed: int,
        message: str = "",
    ):
        total = max(0, int(total))
        done = max(0, min(int(done), total if total else int(done)))
        self.script_progress.setVisible(True)
        self.script_progress.setRange(0, max(1, total))
        self.script_progress.setValue(done if total else 0)
        msg = message or (
            f"已处理 {done}/{total} · 成功 {generated} · 失败 {failed}"
        )
        self.lbl_script_progress.setVisible(True)
        self.lbl_script_progress.setText(msg)
        self.lbl_progress.setText(msg)

    def end_script_generation(self):
        self._script_generating = False
        self._script_gen_cancel = False
        if not self._sending:
            self.btn_cancel_send.setVisible(False)
            self.btn_cancel_send.setText("取消发送")
        self.set_busy(False)
        missing = self.recipient_ids_needing_scripts()
        recipients = (self._job or {}).get("recipients") or []
        open_recs = [r for r in recipients if (r.get("status") or "") != "sent"]
        total_open = len(open_recs)
        if missing and total_open > 0:
            done = max(0, total_open - len(missing))
            self.script_progress.setVisible(True)
            self.script_progress.setRange(0, max(1, total_open))
            self.script_progress.setValue(done)
            self.lbl_script_progress.setVisible(True)
            self.lbl_script_progress.setText(f"已生成 {done}/{total_open}，可继续")
            self.lbl_progress.setText("")
        else:
            self.script_progress.setVisible(False)
            self.lbl_script_progress.setVisible(False)
        self._refresh_actions()

    def is_script_gen_cancelled(self) -> bool:
        return bool(self._script_gen_cancel)

    def recipient_ids_needing_scripts(self) -> list[int]:
        """未发送且尚无话术的名单行（用于续传）。"""
        out: list[int] = []
        for rec in (self._job or {}).get("recipients") or []:
            st = (rec.get("status") or "").strip()
            if st == "sent":
                continue
            if (rec.get("script_text") or "").strip():
                continue
            try:
                rid = int(rec.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if rid > 0:
                out.append(rid)
        return out

    def set_sending(self, sending: bool):
        self._sending = sending
        if sending:
            self._cancel_flag = False
            self.btn_cancel_send.setText("取消发送")
        self.btn_cancel_send.setVisible(sending or self._script_generating)
        self.btn_send.setEnabled(not sending)
        self.btn_gen_list.setEnabled(not sending and not self._script_generating)
        self.btn_gen_scripts.setEnabled(not sending and not self._script_generating)
        self.btn_add.setEnabled(not sending and not self._script_generating)
        self.btn_del.setEnabled(not sending and not self._script_generating)
        self.table.setEnabled(not sending)

    def set_send_progress(
        self,
        *,
        current: int,
        total: int,
        success: int,
        failed: int,
        message: str = "",
    ):
        self.lbl_progress.setText(
            message or f"发送中 {current}/{total} · 成功 {success} · 失败 {failed}"
        )
        self.lbl_stats.setText(
            f"成功 {success} · 失败 {failed} · 剩余 {max(0, total - current)}"
        )

    def current_sales_wechat_id(self) -> str:
        data = self.sales_combo.currentData()
        if data:
            return str(data).strip()
        idx = self.sales_combo.currentIndex()
        if 0 <= idx < len(self._sales_id_by_index):
            return self._sales_id_by_index[idx]
        return ""

    def current_unit_type(self) -> str:
        data = self.unit_combo.currentData()
        if data:
            return str(data).strip()
        return (self.unit_combo.currentText() or "").strip()

    def current_campaign_id(self) -> int:
        data = self.campaign_combo.currentData()
        if data is not None:
            try:
                return int(data)
            except (TypeError, ValueError):
                pass
        idx = self.campaign_combo.currentIndex()
        if 0 <= idx < len(self._campaign_id_by_index):
            return int(self._campaign_id_by_index[idx])
        return 0

    def current_job_id(self) -> int | None:
        if not self._job:
            return None
        try:
            return int(self._job.get("id") or 0) or None
        except (TypeError, ValueError):
            return None

    def get_job_for_send(self) -> dict | None:
        return self._job

    def list_limit(self) -> int:
        return int(self.spin_limit.value())

    def checked_recipient_ids(self) -> list[int]:
        out: list[int] = []
        sm = self.table.selectionModel()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, _COL_CHECK)
            selected = bool(sm and sm.isRowSelected(row, self.table.rootIndex()))
            checked = bool(item and item.checkState() == Qt.CheckState.Checked)
            if not (selected or checked):
                continue
            try:
                out.append(int(item.data(Qt.UserRole)))
            except (TypeError, ValueError, AttributeError):
                pass
        return out

    def mark_send_finished(self, job: dict | None):
        self.set_sending(False)
        self.apply_job(job)
        stats = (job or {}).get("stats") or {}
        failed = int(stats.get("failed") or 0)
        self.btn_retry.setVisible(failed > 0)

    def _render_table(self):
        bar = self.table.verticalScrollBar()
        scroll = int(bar.value()) if bar is not None else 0
        self._syncing_selection = True
        try:
            self.table.setRowCount(0)
            self.chk_select_all.setChecked(False)

            recipients = (self._job or {}).get("recipients") or []
            for rec in recipients:
                row = self.table.rowCount()
                self.table.insertRow(row)
                rid = int(rec.get("id") or 0)

                self.table.setItem(row, _COL_CHECK, _check_item(user_data=rid))

                remark = (rec.get("remark") or rec.get("display_name") or "").strip()
                remark_item = QTableWidgetItem(remark)
                remark_item.setToolTip(remark)
                self.table.setItem(row, _COL_REMARK, remark_item)
                self.table.setItem(
                    row, _COL_UNIT, QTableWidgetItem((rec.get("unit_type") or "").strip())
                )
                st = (rec.get("status") or "pending").strip()
                self.table.setItem(
                    row,
                    _COL_STATUS,
                    QTableWidgetItem(_STATUS_LABELS.get(st, st)),
                )
                script = (rec.get("script_text") or "").strip()
                script_item = QTableWidgetItem(script)
                script_item.setToolTip(_wrapping_tooltip(script))
                self.table.setItem(row, _COL_SCRIPT, script_item)
                err = (rec.get("error_message") or "").strip()
                err_item = QTableWidgetItem(err)
                err_item.setToolTip(_wrapping_tooltip(err))
                self.table.setItem(row, _COL_ERROR, err_item)

                action_w = QWidget()
                action_w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
                action_w.setMinimumWidth(_COL_MIN_ACTIONS - 8)
                action_l = QHBoxLayout(action_w)
                action_l.setContentsMargins(4, 2, 4, 2)
                action_l.setSpacing(6)
                btn_edit = PushButton("修改话术")
                btn_edit.setFixedHeight(28)
                btn_edit.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
                btn_edit.clicked.connect(lambda _=False, r=rid: self._on_edit_row(r))
                action_l.addWidget(btn_edit)
                can_send = st in ("pending", "failed") and script and rec.get("poster_id")
                btn_send = PushButton("重试" if st == "failed" else "发送")
                btn_send.setFixedHeight(28)
                btn_send.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
                btn_send.setEnabled(bool(can_send) and not self._sending)
                btn_send.clicked.connect(lambda _=False, r=rid: self._on_send_row(r))
                action_l.addWidget(btn_send)
                self.table.setCellWidget(row, _COL_ACTIONS, action_w)

            stats = (self._job or {}).get("stats") or {}
            sent = int(stats.get("sent") or 0)
            failed = int(stats.get("failed") or 0)
            pending = int(stats.get("pending") or 0)
            self.lbl_stats.setText(f"成功 {sent} · 失败 {failed} · 待发送 {pending}")
            self.btn_retry.setVisible(failed > 0 and not self._sending)
        finally:
            self._syncing_selection = False
            self._fit_table()
            if bar is not None:
                bar.setValue(scroll)
                QTimer.singleShot(0, lambda v=scroll: self.table.verticalScrollBar().setValue(v))

    def _refresh_actions(self):
        has_job = bool(self._job and self._job.get("recipients"))
        busy = self._sending or self._script_generating
        self.btn_gen_scripts.setEnabled(has_job and not busy)
        self.btn_add.setEnabled(bool(self.current_job_id()) and not busy)
        self.btn_del.setEnabled(has_job and not busy)

        missing = self.recipient_ids_needing_scripts() if has_job else []
        recipients = (self._job or {}).get("recipients") or []
        has_any_script = any(
            (r.get("script_text") or "").strip()
            for r in recipients
            if (r.get("status") or "") != "sent"
        )
        if missing and has_any_script:
            self.btn_gen_scripts.setText(f"继续生成({len(missing)})")
        else:
            self.btn_gen_scripts.setText("生成话术")

        camp_ok = False
        cid = self.current_campaign_id()
        idx = self._find_campaign_index(cid) if cid else -1
        if idx >= 0:
            camp_ok = "无海报" not in self.campaign_combo.itemText(idx)
        sendable = [
            r
            for r in recipients
            if (r.get("status") or "") in ("pending", "failed")
            and (r.get("script_text") or "").strip()
            and r.get("poster_id")
        ]
        self.btn_send.setEnabled(bool(sendable) and camp_ok and not busy)

    def _sync_checks_from_selection(self):
        sm = self.table.selectionModel()
        n = 0
        total = self.table.rowCount()
        for row in range(total):
            item = self.table.item(row, _COL_CHECK)
            if not item:
                continue
            selected = bool(sm and sm.isRowSelected(row, self.table.rootIndex()))
            item.setCheckState(
                Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked
            )
            if selected:
                n += 1
        self.chk_select_all.blockSignals(True)
        self.chk_select_all.setChecked(total > 0 and n == total)
        self.chk_select_all.blockSignals(False)

    def _on_select_all(self, checked: bool):
        if self._syncing_selection:
            return
        self._syncing_selection = True
        try:
            if checked:
                self.table.selectAll()
            else:
                self.table.clearSelection()
            self._sync_checks_from_selection()
        finally:
            self._syncing_selection = False

    def _on_table_selection_changed(self):
        if self._syncing_selection:
            return
        self._syncing_selection = True
        try:
            self._sync_checks_from_selection()
        finally:
            self._syncing_selection = False

    def _on_unit_changed(self, _text: str):
        ut = self.current_unit_type()
        if ut:
            self.running_campaigns_requested.emit(ut)

    def _on_sales_or_campaign_changed(self, _idx: int = 0):
        self._emit_load_current()

    def _emit_load_current(self):
        sw = self.current_sales_wechat_id()
        cid = self.current_campaign_id()
        if sw and cid:
            self.load_current_job_requested.emit(sw, cid)
        else:
            self.apply_job(None)

    def _on_gen_list(self):
        sw = self.current_sales_wechat_id()
        ut = self.current_unit_type()
        cid = self.current_campaign_id()
        if not sw or not ut or not cid:
            return
        self.create_job_requested.emit(sw, ut, cid, self.list_limit())

    def set_add_dialog_candidates(self, items: list[dict], total: int):
        if self._add_dialog is not None:
            self._add_dialog.set_candidates(items, total)

    def _on_add(self):
        jid = self.current_job_id()
        sw = self.current_sales_wechat_id()
        cid = self.current_campaign_id()
        if not jid or not sw or not cid:
            return
        dlg = _AddRecipientsDialog(
            self,
            sales_wechat_id=sw,
            campaign_id=cid,
            job_id=jid,
        )
        self._add_dialog = dlg
        dlg.search_requested.connect(self.add_recipients_dialog_search.emit)
        dlg.search_requested.emit(sw, cid, jid, "")
        if dlg.exec() != QDialog.Accepted:
            self._add_dialog = None
            return
        ids = dlg.selected_raw_customer_ids()
        self._add_dialog = None
        if ids:
            self.add_recipients_requested.emit(jid, ids)

    def _on_delete(self):
        jid = self.current_job_id()
        if not jid:
            return
        rids = self.checked_recipient_ids()
        if not rids:
            self.lbl_progress.setText("请先点选或拖选要删除的客户")
            return
        self.delete_recipients_requested.emit(jid, rids)

    def _on_gen_scripts(self):
        jid = self.current_job_id()
        if not jid:
            return
        sel = self.checked_recipient_ids()
        if sel:
            self.generate_scripts_requested.emit(jid, sel)
            return
        missing = self.recipient_ids_needing_scripts()
        if not missing:
            self.lbl_progress.setText("话术已全部生成；如需重写请勾选后点击生成话术")
            return
        self.generate_scripts_requested.emit(jid, missing)

    def _recipient_script(self, recipient_id: int) -> str:
        for rec in (self._job or {}).get("recipients") or []:
            if int(rec.get("id") or 0) == int(recipient_id):
                return (rec.get("script_text") or "").strip()
        return ""

    def _on_edit_row(self, recipient_id: int):
        jid = self.current_job_id()
        if not jid:
            return
        dlg = _EditScriptDialog(
            self,
            title="修改话术",
            text=self._recipient_script(recipient_id),
        )
        if dlg.exec() == QDialog.Accepted:
            self.patch_script_requested.emit(jid, int(recipient_id), dlg.script_text())

    def _on_send_row(self, recipient_id: int):
        jid = self.current_job_id()
        if jid:
            self.send_single_requested.emit(int(jid), int(recipient_id))

    def _on_send(self):
        if not self._job:
            return
        checked = self.checked_recipient_ids()
        payload = dict(self._job)
        if checked:
            payload["_recipient_ids"] = checked
        self.start_send_requested.emit(payload)

    def _on_retry(self):
        jid = self.current_job_id()
        if jid:
            self.retry_failed_requested.emit(jid)

    def _on_cancel_send(self):
        if self._script_generating:
            self._script_gen_cancel = True
            self.lbl_progress.setText("正在停止生成…")
            return
        self._cancel_flag = True

    def is_send_cancelled(self) -> bool:
        return self._cancel_flag
