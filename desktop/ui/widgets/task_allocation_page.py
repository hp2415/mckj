"""桌面端「任务分配」模块主页面。

布局 (从上到下)：
1. 顶部工具栏：销售微信号下拉 + 周期切换 (日/周/月) + 刷新按钮
2. 周期与批次信息行（period_start ~ period_end · 批次 #ID · 状态）
3. 统计卡片：本批任务 / 主线 / 激活 / 待办 / 完成率（含进度条）
4. 筛选栏：可多选，已选项以标签卡片展示；类型（微信/电话/激活）互斥，状态（待办/完成）互斥，可组合
5. 任务卡片列表 (TaskCardWidget) —— 与管理后台「联系任务列表」字段一致，但更易读

数据流：
- MainWindow / DesktopApp 调用 `set_sales_options()` 把当前用户名下绑定的销售微信号灌进下拉框；
- 用户切换销售/周期 / 点击刷新 → 发出 `request_overview` 信号，由 DesktopApp 调 API 拉取；
- DesktopApp 拿到后端响应后调用 `set_overview_data()` 渲染统计卡和列表；
- 列表中的 完成 / 跳过 按钮通过 TaskCardWidget.action_triggered 上抛 → `task_action_requested`，
  由 DesktopApp 调对应 API，再回调 `set_overview_data()` 刷新。
"""
from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import Qt, QSize, Signal, QTimer
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    ListWidget,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    ToolButton,
    isDarkTheme,
)

_TASK_LIST_RENDER_BATCH = 8

from ui.app_fonts import label_qss, style_label, text_palette
from ui.widgets.search import SearchTag
from ui.widgets.task_card import TaskCardWidget
from ui.widgets.skeleton import CardListSkeletonPanel


_PERIODS: list[tuple[str, str]] = [
    ("daily", "日任务"),
    ("weekly", "周任务"),
    ("monthly", "月进度"),
]

_TASK_FILTER_META: dict[str, str] = {
    "wechat": "微信主线",
    "phone": "电话主线",
    "ice": "仅激活",
    "pending": "仅待办",
    "done": "仅完成",
}
_TYPE_FILTER_KEYS = frozenset({"wechat", "phone", "ice"})
_STATUS_FILTER_KEYS = frozenset({"pending", "done"})
_FILTER_DISPLAY_ORDER = ("wechat", "phone", "ice", "pending", "done")

_STAT_CARD_ACCENTS: dict[str, str] = {
    "total": "#576b95",
    "wechat": "#07c160",
    "phone": "#722ed1",
    "ice": "#fa8c16",
    "pending": "#1890ff",
    "done": "#52c41a",
}


def _bindings_signature(bindings: Iterable[dict]) -> tuple[tuple[str, bool], ...]:
    rows = []
    for r in bindings or []:
        sw = str(r.get("sales_wechat_id") or "").strip()
        if sw:
            rows.append((sw, bool(r.get("is_primary"))))
    return tuple(rows)


class _StatCard(QFrame):
    """统计卡片：上方大数字 + 下方小标题；可点击时与筛选栏联动。"""

    clicked = Signal()

    def __init__(
        self,
        title: str,
        *,
        card_key: str = "",
        clickable: bool = True,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setObjectName("TaskStatCard")
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(56)
        self._card_key = card_key
        self._clickable = clickable
        self._active = False
        self._hovered = False
        self._accent = _STAT_CARD_ACCENTS.get(card_key, "#07c160")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        self.value_lbl = StrongBodyLabel("—")
        self.value_lbl.setAlignment(Qt.AlignCenter)
        self.value_lbl.setObjectName("TaskStatValue")
        self.title_lbl = CaptionLabel(title)
        self.title_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.value_lbl)
        layout.addWidget(self.title_lbl)

        if clickable:
            self.setCursor(Qt.PointingHandCursor)
            tip = {
                "total": "查看全部任务（清除筛选）",
                "wechat": "筛选微信主线任务",
                "phone": "筛选电话主线任务",
                "ice": "筛选激活任务",
                "pending": "筛选待办任务",
                "done": "筛选已完成任务",
            }.get(card_key, "")
            if tip:
                self.setToolTip(tip)

        self._apply_theme_style()

    def set_value(self, text: str):
        self.value_lbl.setText(text)

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self._apply_theme_style()

    def enterEvent(self, event):
        if self._clickable:
            self._hovered = True
            self._apply_theme_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._clickable:
            self._hovered = False
            self._apply_theme_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if self._clickable and event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        base_bg = "#333333" if is_dark else "#ffffff"
        base_border = "rgba(255,255,255,0.12)" if is_dark else "rgba(0,0,0,0.09)"
        title_color = "#999999" if is_dark else "#666666"
        value_color = "#e8e8e8" if is_dark else "#1a1a1a"
        accent = self._accent

        if self._active:
            bg_alpha = "33" if is_dark else "26"
            bg = f"#{bg_alpha}{accent.lstrip('#')}"
            border = f"2px solid {accent}"
            value_color = accent
            title_color = accent
        elif self._hovered and self._clickable:
            bg_alpha = "22" if is_dark else "14"
            bg = f"#{bg_alpha}{accent.lstrip('#')}"
            border = f"1px solid {accent}"
        else:
            bg = base_bg
            border = f"1px solid {base_border}"

        self.setStyleSheet(
            f"""
            QFrame#TaskStatCard {{
                background-color: {bg};
                border: {border};
                border-radius: 8px;
            }}
            """
        )
        style_label(self.value_lbl, "stat_value", color=value_color)
        style_label(self.title_lbl, "stat_label", color=title_color)


class TaskFilterWidget(QFrame):
    """任务筛选：多选标签 + 添加下拉；类型组与状态组各自互斥。"""

    selection_changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("TaskFilterBar")
        self._selected: set[str] = set()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        self.tag_area = QWidget()
        self.tag_layout = QHBoxLayout(self.tag_area)
        self.tag_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_layout.setSpacing(6)
        layout.addWidget(self.tag_area, 1)

        self.hint_lbl = CaptionLabel("全部")
        self.hint_lbl.setObjectName("TaskFilterHint")
        self.tag_layout.addWidget(self.hint_lbl)

        self.add_combo = ComboBox()
        self.add_combo.setFixedHeight(26)
        self.add_combo.setMinimumWidth(108)
        self.add_combo.setToolTip("添加筛选条件（类型与状态可组合）")
        self.add_combo.currentIndexChanged.connect(self._on_add_combo_changed)
        layout.addWidget(self.add_combo, 0)

        self._sync_ui()
        self._apply_theme_style()

    def selected_keys(self) -> frozenset[str]:
        return frozenset(self._selected)

    def _ordered_selected(self) -> list[str]:
        return [k for k in _FILTER_DISPLAY_ORDER if k in self._selected]

    def add_filter(self, key: str) -> None:
        key = (key or "").strip()
        if key not in _TASK_FILTER_META:
            return
        if key in _TYPE_FILTER_KEYS:
            self._selected -= _TYPE_FILTER_KEYS
        if key in _STATUS_FILTER_KEYS:
            self._selected -= _STATUS_FILTER_KEYS
        self._selected.add(key)
        self._sync_ui()
        self.selection_changed.emit()

    def remove_filter(self, key: str) -> None:
        key = (key or "").strip()
        if key not in self._selected:
            return
        self._selected.discard(key)
        self._sync_ui()
        self.selection_changed.emit()

    def clear_all(self) -> None:
        if not self._selected:
            return
        self._selected.clear()
        self._sync_ui()
        self.selection_changed.emit()

    def _on_add_combo_changed(self, _idx: int) -> None:
        key = self.add_combo.currentData()
        self.add_combo.blockSignals(True)
        self.add_combo.setCurrentIndex(0)
        self.add_combo.blockSignals(False)
        if not key:
            return
        self.add_filter(str(key))

    def _clear_tags(self) -> None:
        while self.tag_layout.count():
            item = self.tag_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _rebuild_tags(self) -> None:
        self._clear_tags()
        keys = self._ordered_selected()
        if not keys:
            self.hint_lbl = CaptionLabel("全部")
            self.hint_lbl.setObjectName("TaskFilterHint")
            self.tag_layout.addWidget(self.hint_lbl)
            self._style_hint_label()
            return
        for key in keys:
            label = _TASK_FILTER_META[key]
            tag = SearchTag(label)
            tag.removed.connect(lambda _text, k=key: self.remove_filter(k))
            self.tag_layout.addWidget(tag)
        self.tag_layout.addStretch(1)

    def _rebuild_combo(self) -> None:
        self.add_combo.blockSignals(True)
        self.add_combo.clear()
        self.add_combo.addItem("添加筛选…", userData=None)
        for key in _FILTER_DISPLAY_ORDER:
            if key not in self._selected:
                self.add_combo.addItem(_TASK_FILTER_META[key], userData=key)
        self.add_combo.setCurrentIndex(0)
        self.add_combo.blockSignals(False)

    def _sync_ui(self) -> None:
        self._rebuild_tags()
        self._rebuild_combo()

    def _style_hint_label(self) -> None:
        self.hint_lbl.setStyleSheet(label_qss("caption", extra="padding: 2px 0;"))

    def _apply_theme_style(self) -> None:
        is_dark = isDarkTheme()
        border_col = "#404040" if is_dark else "#e0e0e0"
        bg_col = "#2c2c2c" if is_dark else "#fdfdfd"
        self.setStyleSheet(
            f"""
            QFrame#TaskFilterBar {{
                background-color: {bg_col};
                border: 1px solid {border_col};
                border-radius: 8px;
            }}
            """
        )
        if self._selected:
            for i in range(self.tag_layout.count()):
                w = self.tag_layout.itemAt(i).widget()
                if w and hasattr(w, "_apply_theme_style"):
                    w._apply_theme_style()
        else:
            self._style_hint_label()


class TaskAllocationWidget(QFrame):
    """任务分配主页面。"""

    # 用户希望刷新数据 → (sales_wechat_id, period, page, page_size, status)
    # status: None 表示不筛；字符串时传给后端 /api/tasks/overview?status=
    request_overview = Signal(str, str, int, int, object)
    # 用户点击申诉/改待办 → (task_id, op, payload)
    task_action_requested = Signal(int, str, object)
    # 点击任务卡片 → 打开对应客户对话
    task_open_customer_chat = Signal(dict)
    # 激活卡片发微信 → (task, edit_mode)
    task_wechat_send_requested = Signal(dict, bool)
    # 电话主线 → 打开客户电话面板
    task_open_customer_phone = Signal(dict)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("TaskAllocationPage")
        self.setFrameShape(QFrame.NoFrame)
        self._sales_options: list[dict] = []
        self._items: list[dict] = []
        self._period: str = "daily"
        self._loading: bool = False
        self._last_meta: dict = {}
        self._view_mode: str = ""
        self._page: int = 1
        self._page_size: int = 0
        self._status_filter: Optional[str] = None
        self._total_items: int = 0
        self._append_mode: bool = False
        self._bindings_sig: tuple[tuple[str, bool], ...] | None = None
        self._last_fetch_key: tuple | None = None
        self._cards_by_id: dict[int, TaskCardWidget] = {}
        self._width_sync_timer = QTimer(self)
        self._width_sync_timer.setSingleShot(True)
        self._width_sync_timer.setInterval(50)
        self._width_sync_timer.timeout.connect(self._sync_card_widths)
        self._inflight_fetch_key: tuple | None = None
        self._render_gen = 0
        self._render_queue: list[dict] = []
        self._render_deferred = False
        self._list_render_active = False
        self._module_entered_once = False
        self._pending_initial_fetch = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # ── 信息栏容器（固定高度，避免空列表时被纵向拉伸）──
        self.info_container = QFrame()
        self.info_container.setObjectName("TaskInfoContainer")
        self.info_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.info_container.setStyleSheet("QFrame#TaskInfoContainer { background: transparent; border: none; }")
        info_layout = QVBoxLayout(self.info_container)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(8)

        # ── 标题 ──
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        self.title_lbl = SubtitleLabel("任务分配")
        title_row.addWidget(self.title_lbl)
        title_row.addStretch(1)
        self.btn_refresh = ToolButton(FluentIcon.SYNC)
        self.btn_refresh.setToolTip("刷新当前销售在该周期的任务列表")
        self.btn_refresh.setFixedSize(30, 30)
        self.btn_refresh.setIconSize(QSize(16, 16))
        self.btn_refresh.clicked.connect(lambda: self._emit_request(force=True))
        title_row.addWidget(self.btn_refresh)
        info_layout.addLayout(title_row)

        # ── 工具栏：销售号 + 周期 ──
        toolbar = QFrame()
        toolbar.setObjectName("TaskToolbar")
        tb_layout = QVBoxLayout(toolbar)
        tb_layout.setContentsMargins(10, 8, 10, 8)
        tb_layout.setSpacing(6)

        sw_row = QHBoxLayout()
        sw_row.setSpacing(6)
        self._lbl_sw = CaptionLabel("销售微信")
        sw_row.addWidget(self._lbl_sw)
        self.sales_combo = ComboBox()
        self.sales_combo.setPlaceholderText("请选择销售微信号")
        self.sales_combo.currentIndexChanged.connect(self._on_sales_changed)
        self.sales_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        sw_row.addWidget(self.sales_combo, 1)
        tb_layout.addLayout(sw_row)

        period_row = QHBoxLayout()
        period_row.setSpacing(6)
        self._lbl_period = CaptionLabel("周期")
        period_row.addWidget(self._lbl_period)
        self._period_buttons: dict[str, PushButton] = {}
        for key, label in _PERIODS:
            btn = PushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(26)
            btn.clicked.connect(lambda _=False, k=key: self._on_period_clicked(k))
            self._period_buttons[key] = btn
            period_row.addWidget(btn)
        period_row.addStretch(1)
        tb_layout.addLayout(period_row)

        toolbar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        info_layout.addWidget(toolbar)
        self._toolbar_frame = toolbar
        self._set_period_active(self._period)

        # ── 周期/批次信息行（QLabel + RichText，保证 HTML span 可靠渲染）──
        self.meta_lbl = QLabel("请选择销售微信号以加载任务列表。")
        self.meta_lbl.setTextFormat(Qt.RichText)
        self.meta_lbl.setWordWrap(True)
        self.meta_lbl.setObjectName("TaskMetaLabel")
        info_layout.addWidget(self.meta_lbl)

        # ── 统计卡片 ──
        cards_row = QHBoxLayout()
        cards_row.setSpacing(6)
        self.card_total = _StatCard("本批任务", card_key="total")
        self.card_wechat = _StatCard("微信主线", card_key="wechat")
        self.card_phone = _StatCard("电话主线", card_key="phone")
        self.card_ice = _StatCard("激活", card_key="ice")
        self.card_pending = _StatCard("待办", card_key="pending")
        self.card_rate = _StatCard("完成率", card_key="done")
        self._stat_cards: dict[str, _StatCard] = {
            "total": self.card_total,
            "wechat": self.card_wechat,
            "phone": self.card_phone,
            "ice": self.card_ice,
            "pending": self.card_pending,
            "done": self.card_rate,
        }
        for key, card in self._stat_cards.items():
            card.clicked.connect(lambda k=key: self._on_stat_card_clicked(k))
        for c in (self.card_total, self.card_wechat, self.card_phone, self.card_ice, self.card_pending, self.card_rate):
            cards_row.addWidget(c, 1, Qt.AlignTop)
        info_layout.addLayout(cards_row)

        root.addWidget(self.info_container, 0)

        # 完成率进度条
        self.progress_bar = QFrame()
        self.progress_bar.setObjectName("TaskProgressBar")
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        pb_layout = QHBoxLayout(self.progress_bar)
        pb_layout.setContentsMargins(0, 0, 0, 0)
        pb_layout.setSpacing(0)
        self.progress_inner = QFrame()
        self.progress_inner.setObjectName("TaskProgressInner")
        self.progress_inner.setFixedHeight(6)
        pb_layout.addWidget(self.progress_inner, 0, Qt.AlignLeft)
        pb_layout.addStretch(1)
        root.addWidget(self.progress_bar, 0)

        # ── 筛选：多选标签 + 添加下拉 ──
        self.filter_bar = QWidget()
        self.filter_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        filter_row = QHBoxLayout(self.filter_bar)
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(6)
        self._lbl_filter = CaptionLabel("筛选")
        filter_row.addWidget(self._lbl_filter)
        self.task_filter = TaskFilterWidget()
        self.task_filter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.task_filter.selection_changed.connect(self._on_filter_selection_changed)
        filter_row.addWidget(self.task_filter, 1)

        # ── 收起/展开按钮 ──
        self.btn_toggle_info = ToolButton(FluentIcon.UP)
        self.btn_toggle_info.setToolTip("收起上方信息栏")
        self.btn_toggle_info.setFixedSize(26, 26)
        self.btn_toggle_info.setIconSize(QSize(14, 14))
        self.btn_toggle_info.clicked.connect(self._on_toggle_info_clicked)
        filter_row.addWidget(self.btn_toggle_info)

        root.addWidget(self.filter_bar, 0)

        # ── 列表区域（独占剩余空间；空状态提示固定在内容区顶部）──
        self.list_area = QWidget()
        self.list_area.setObjectName("TaskListArea")
        list_area_layout = QVBoxLayout(self.list_area)
        list_area_layout.setContentsMargins(0, 0, 0, 0)
        list_area_layout.setSpacing(6)

        self.task_list = ListWidget()
        self.task_list.setObjectName("TaskList")
        self.task_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.task_list.setFocusPolicy(Qt.NoFocus)
        self.task_list.setResizeMode(QListView.Adjust)
        self.task_list.setSpacing(6)
        self.task_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.task_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.task_list.verticalScrollBar().setSingleStep(18)
        list_area_layout.addWidget(self.task_list, 1)

        self.btn_load_more = PushButton("加载更多（+50）")
        self.btn_load_more.setFixedHeight(28)
        self.btn_load_more.clicked.connect(self._on_load_more_clicked)
        self.btn_load_more.hide()
        list_area_layout.addWidget(self.btn_load_more, 0)

        self.empty_container = QWidget()
        self.empty_container.setObjectName("TaskEmptyContainer")
        empty_layout = QVBoxLayout(self.empty_container)
        empty_layout.setContentsMargins(0, 24, 0, 0)
        self.empty_lbl = BodyLabel("暂无任务数据")
        self.empty_lbl.setAlignment(Qt.AlignCenter)
        self.empty_lbl.setObjectName("TaskEmptyLabel")
        empty_layout.addWidget(self.empty_lbl, 0, Qt.AlignHCenter | Qt.AlignTop)
        empty_layout.addStretch(1)
        list_area_layout.addWidget(self.empty_container, 1)
        self.empty_container.hide()

        self._list_loading_overlay = QWidget(self.list_area)
        self._list_loading_overlay.setObjectName("TaskListLoadingOverlay")
        overlay_layout = QVBoxLayout(self._list_loading_overlay)
        overlay_layout.setContentsMargins(0, 0, 0, 0)
        overlay_layout.setSpacing(0)
        self._list_loading_skeleton = CardListSkeletonPanel(
            card_style="task",
            row_count=4,
            row_spacing=8,
            margins=(8, 16, 8, 8),
            parent=self._list_loading_overlay,
        )
        overlay_layout.addWidget(self._list_loading_skeleton, 1)
        self._list_loading_overlay.hide()

        root.addWidget(self.list_area, 1)
        self._root_layout = root

        # 默认「仅待办」：控件就绪后再设置；阻塞信号避免首屏尚未拉数时误触空状态
        self.task_filter.blockSignals(True)
        self.task_filter.add_filter("pending")
        self.task_filter.blockSignals(False)
        self._sync_stat_card_active_state()

        self._apply_theme_style()
        self._update_stats(total=0, wechat=0, phone=0, ice=0, pending=0, rate=0.0)

    # ── 对外 API ──
    def set_sales_options(self, bindings: Iterable[dict]):
        """灌入当前用户绑定的销售微信号列表。

        bindings 每项支持字段：sales_wechat_id / alias_name / label / is_primary。
        """
        bindings = list(bindings or [])
        # 主号优先排前面，方便默认选中
        bindings.sort(key=lambda x: (0 if x.get("is_primary") else 1, x.get("id") or 0))
        self._sales_options = bindings
        new_sig = _bindings_signature(bindings)
        bindings_changed = new_sig != self._bindings_sig
        self._bindings_sig = new_sig

        current_sw = self.current_sales_wechat_id()
        self.sales_combo.blockSignals(True)
        self.sales_combo.clear()
        for r in bindings:
            sw = str(r.get("sales_wechat_id") or "").strip()
            if not sw:
                continue
            alias = str(r.get("alias_name") or "").strip()
            label = str(r.get("label") or "").strip()
            shown = alias or label or sw
            tail = f" ({sw})" if alias and sw and alias != sw else ""
            star = " ★" if r.get("is_primary") else ""
            self.sales_combo.addItem(f"{shown}{tail}{star}", userData=sw)
        # 优先恢复先前选择，否则选第一个 (= 主号)
        if current_sw:
            idx = self._find_index_by_sw(current_sw)
            if idx >= 0:
                self.sales_combo.setCurrentIndex(idx)
        elif self.sales_combo.count() > 0:
            self.sales_combo.setCurrentIndex(0)
        self.sales_combo.blockSignals(False)

        if self.sales_combo.count() == 0:
            self.meta_lbl.setText("当前账号未绑定销售微信号，请先在「销售微信号」页面添加。")
            self._clear_list_with_placeholder("当前账号未绑定销售微信号")
        elif bindings_changed or not self._items:
            # 仅进入任务页后才拉取，避免在设置页同步绑定时抢网络
            if self._module_entered_once:
                self._emit_request()
            else:
                self._pending_initial_fetch = True

    def current_sales_wechat_id(self) -> str:
        data = self.sales_combo.currentData()
        if data:
            return str(data).strip()
        return ""

    def current_period(self) -> str:
        return self._period

    def set_overview_data(self, payload: dict):
        """渲染后端 `/api/tasks/overview` 返回的 data 字段。"""
        self._inflight_fetch_key = None
        self._loading = False
        self._last_fetch_key = self._current_fetch_key()
        self._hide_list_loading_overlay()
        self.btn_refresh.setEnabled(True)
        if not isinstance(payload, dict):
            payload = {}
        stats = payload.get("stats") or {}
        items = list(payload.get("items") or [])
        self._view_mode = str(payload.get("view_mode") or "")
        self._total_items = int(payload.get("total_items") or 0)
        append_now = bool(self._append_mode and self._items)
        if append_now:
            seen = {int(it.get("id") or 0) for it in self._items}
            for it in items:
                tid = int(it.get("id") or 0)
                if tid and tid not in seen:
                    self._items.append(it)
                    seen.add(tid)
        else:
            self._items = items
        self._last_meta = {
            "period_type": payload.get("period_type") or self._period,
            "period_start": payload.get("period_start"),
            "period_end": payload.get("period_end"),
            "batch_id": payload.get("batch_id"),
            "batch_status": payload.get("batch_status"),
            "view_mode": payload.get("view_mode"),
            "snapshot": payload.get("snapshot") or {},
        }

        total = int(stats.get("total") or self._total_items or len(self._items))
        wechat, phone, ice = self._channel_counts(self._items)
        pending = sum(
            1
            for it in self._items
            if (it.get("status") or "") in ("pending", "in_progress", "overdue")
        )
        rate = float(stats.get("completion_rate") or 0.0)
        is_month_progress = payload.get("view_mode") == "month_progress"
        self.card_total.title_lbl.setText("本月任务" if is_month_progress else "本批任务")
        self._update_stats(total=total, wechat=wechat, phone=phone, ice=ice, pending=pending, rate=rate)
        self._update_meta_line(stats=stats)
        if append_now:
            self._append_task_list(items)
        else:
            self._schedule_task_list_rebuild()
        self._append_mode = False
        self._update_load_more_button()

    def patch_task_status(self, task_id: int, status: str) -> bool:
        """本地更新单条任务状态，避免操作后整表重拉/重建。"""
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            return False
        status = (status or "").strip()
        if not status:
            return False
        updated = False
        for it in self._items:
            if int(it.get("id") or 0) == tid:
                it["status"] = status
                updated = True
                break
        if not updated:
            return False
        task_data = next((it for it in self._items if int(it.get("id") or 0) == tid), None)
        card = self._cards_by_id.get(tid)
        if card is not None and task_data is not None:
            card.update_task(task_data)
            target_w = max(self.task_list.viewport().width() - 8, 280)
            for i in range(self.task_list.count()):
                li = self.task_list.item(i)
                if int(li.data(Qt.UserRole) or 0) != tid:
                    continue
                card.setFixedWidth(target_w)
                li.setSizeHint(QSize(target_w, card.sizeHint().height()))
                break
        stats = self._stats_from_items(self._items)
        total = len(self._items)
        wechat, phone, ice = self._channel_counts(self._items)
        pending = sum(
            1 for it in self._items if (it.get("status") or "") in ("pending", "in_progress")
        )
        self._update_stats(
            total=total,
            wechat=wechat,
            phone=phone,
            ice=ice,
            pending=pending,
            rate=float(stats.get("completion_rate") or 0.0),
        )
        self._update_meta_line(stats=stats)
        self._apply_filter_visibility()
        return True

    def show_error(self, message: str):
        """在 meta 行展示错误信息。"""
        self._loading = False
        self._inflight_fetch_key = None
        self._cancel_list_render()
        self._hide_list_loading_overlay()
        self.btn_refresh.setEnabled(True)
        self.meta_lbl.setText(f"⚠ 拉取任务失败：{message}")

    def show_loading(self):
        self._loading = True
        self.meta_lbl.setText("正在加载任务分配数据…")
        self.btn_refresh.setEnabled(False)
        self._show_list_loading_overlay()

    def on_page_activated(self):
        """进入任务分配模块时触发首屏拉取（懒加载）。"""
        first_enter = not self._module_entered_once
        self._module_entered_once = True
        if self.sales_combo.count() == 0:
            return
        if self._pending_initial_fetch or (first_enter and not self._items and not self._loading):
            self._pending_initial_fetch = False
            self._emit_request()
        elif self._render_deferred and self._items:
            self._render_deferred = False
            self._schedule_task_list_rebuild()

    def showEvent(self, event: QShowEvent):
        super().showEvent(event)
        if self._render_deferred and self._items:
            self._render_deferred = False
            self._schedule_task_list_rebuild()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_list_loading_overlay"):
            self._list_loading_overlay.setGeometry(self.list_area.rect())

    def _show_list_loading_overlay(self):
        if not hasattr(self, "_list_loading_overlay"):
            return
        self._list_loading_overlay.setGeometry(self.list_area.rect())
        self._list_loading_overlay.show()
        self._list_loading_overlay.raise_()
        if hasattr(self, "_list_loading_skeleton"):
            self._list_loading_skeleton.start()

    def _hide_list_loading_overlay(self):
        if not hasattr(self, "_list_loading_overlay"):
            return
        if hasattr(self, "_list_loading_skeleton"):
            self._list_loading_skeleton.stop()
        self._list_loading_overlay.hide()

    # ── 内部交互 ──
    def _current_fetch_key(self) -> tuple:
        return (
            self.current_sales_wechat_id(),
            self._period,
            int(self._page or 1),
            int(self._page_size or 0),
        )

    def _emit_request(self, *, force: bool = False):
        sw = self.current_sales_wechat_id()
        if not sw:
            return
        key = self._current_fetch_key()
        if (
            not force
            and not self._append_mode
            and self._items
            and key == self._last_fetch_key
        ):
            return
        if self._loading and not force:
            return
        self._inflight_fetch_key = key
        self.show_loading()
        self.request_overview.emit(
            sw,
            self._period,
            int(self._page or 1),
            int(self._page_size or 0),
            self._status_filter,
        )

    def _find_index_by_sw(self, sw: str) -> int:
        sw = (sw or "").strip()
        for i in range(self.sales_combo.count()):
            if str(self.sales_combo.itemData(i) or "").strip() == sw:
                return i
        return -1

    def _on_sales_changed(self, _idx: int):
        if self.sales_combo.count() == 0:
            return
        self._reset_paging()
        self._emit_request()

    def _on_period_clicked(self, key: str):
        if key not in {k for k, _ in _PERIODS}:
            return
        self._period = key
        self._set_period_active(key)
        self._reset_paging()
        self._emit_request()

    def _on_load_more_clicked(self):
        if self._loading:
            return
        if not self._can_load_more():
            return
        self._append_mode = True
        self._page = int(self._page or 1) + 1
        self._emit_request()

    def _reset_paging(self):
        self._append_mode = False
        if self._period == "monthly":
            self._page = 1
            self._page_size = 50
        else:
            self._page = 1
            self._page_size = 0
        self._status_filter = None
        self._total_items = 0

    def _can_load_more(self) -> bool:
        if self._view_mode != "month_progress":
            return False
        if self._page_size <= 0:
            return False
        if self._total_items <= 0:
            return False
        return len(self._items) < self._total_items

    def _update_load_more_button(self):
        if self._can_load_more():
            left = max(0, int(self._total_items) - len(self._items))
            self.btn_load_more.setText(f"加载更多（+50）  剩余 {left}")
            self.btn_load_more.show()
        else:
            self.btn_load_more.hide()

    def _on_filter_selection_changed(self):
        self._apply_filter_visibility()
        self._sync_stat_card_active_state()

    def _on_stat_card_clicked(self, card_key: str) -> None:
        if card_key == "total":
            self.task_filter.clear_all()
            return
        filter_key = card_key
        if filter_key not in _TASK_FILTER_META:
            return
        selected = self.task_filter.selected_keys()
        if filter_key in selected:
            self.task_filter.remove_filter(filter_key)
        else:
            self.task_filter.add_filter(filter_key)

    def _sync_stat_card_active_state(self) -> None:
        selected = self.task_filter.selected_keys()
        type_keys = selected & _TYPE_FILTER_KEYS
        status_keys = selected & _STATUS_FILTER_KEYS
        self.card_total.set_active(not selected)
        self.card_wechat.set_active(type_keys == frozenset({"wechat"}))
        self.card_phone.set_active(type_keys == frozenset({"phone"}))
        self.card_ice.set_active(type_keys == frozenset({"ice"}))
        self.card_pending.set_active(status_keys == frozenset({"pending"}))
        self.card_rate.set_active(status_keys == frozenset({"done"}))

    def _on_toggle_info_clicked(self):
        is_collapsed = self.info_container.isHidden()
        new_collapsed = not is_collapsed
        self.info_container.setHidden(new_collapsed)
        if new_collapsed:
            self.btn_toggle_info.setIcon(FluentIcon.DOWN)
            self.btn_toggle_info.setToolTip("展开上方信息栏")
        else:
            self.btn_toggle_info.setIcon(FluentIcon.UP)
            self.btn_toggle_info.setToolTip("收起上方信息栏")

    def _set_period_active(self, key: str):
        for k, btn in self._period_buttons.items():
            btn.setChecked(k == key)
            self._style_segment_button(btn, active=(k == key))

    @staticmethod
    def _channel_counts(items: list[dict]) -> tuple[int, int, int]:
        wechat = sum(
            1
            for it in items
            if (it.get("task_kind") or "") != "icebreaker"
            and (it.get("contact_channel") or "wechat") != "phone"
        )
        phone = sum(
            1
            for it in items
            if (it.get("task_kind") or "") != "icebreaker"
            and (it.get("contact_channel") or "") == "phone"
        )
        ice = sum(1 for it in items if (it.get("task_kind") or "") == "icebreaker")
        return wechat, phone, ice

    @staticmethod
    def _task_matches_filter(it: dict, *, selected: frozenset[str]) -> bool:
        if not selected:
            return True
        task_kind = (it.get("task_kind") or "contact").strip()
        channel = (it.get("contact_channel") or "wechat").strip()
        status = (it.get("status") or "pending").strip()

        type_keys = selected & _TYPE_FILTER_KEYS
        status_keys = selected & _STATUS_FILTER_KEYS

        if type_keys:
            mode = next(iter(type_keys))
            if mode == "ice" and task_kind != "icebreaker":
                return False
            if mode == "wechat" and (
                task_kind == "icebreaker" or channel == "phone"
            ):
                return False
            if mode == "phone" and (
                task_kind == "icebreaker" or channel != "phone"
            ):
                return False

        if status_keys:
            mode = next(iter(status_keys))
            if mode == "pending" and status not in (
                "pending",
                "in_progress",
                "overdue",
            ):
                return False
            if mode == "done" and status != "done":
                return False

        return True

    @staticmethod
    def _stats_from_items(items: list[dict]) -> dict:
        counts: dict[str, int] = {}
        for it in items:
            st = str(it.get("status") or "pending")
            counts[st] = counts.get(st, 0) + 1
        total = sum(counts.values())
        done = counts.get("done", 0)
        skipped = counts.get("skipped", 0)
        denom = max(1, total - skipped)
        return {
            "total": total,
            "done": done,
            "pending": counts.get("pending", 0),
            "in_progress": counts.get("in_progress", 0),
            "skipped": skipped,
            "overdue": counts.get("overdue", 0),
            "completion_rate": round(done / denom, 4),
        }

    def _cancel_list_render(self):
        self._render_gen += 1
        self._render_queue = []
        self._list_render_active = False

    def _schedule_task_list_rebuild(self):
        """分帧重建列表；页面不可见时延后，避免阻塞其他 Tab。"""
        self._cancel_list_render()
        if not self._items:
            self.task_list.clear()
            self._cards_by_id.clear()
            self._show_empty_state("暂无任务数据")
            return
        if not self.isVisible():
            self._render_deferred = True
            return
        self._render_gen += 1
        gen = self._render_gen
        self._render_queue = list(self._items)
        self._list_render_active = True
        self.task_list.setUpdatesEnabled(False)
        self.task_list.clear()
        self._cards_by_id.clear()
        self._hide_empty_state()
        self.task_list.show()
        self._render_task_batch(gen, finalize=True)

    def _append_task_list(self, page_items: list[dict]):
        """追加渲染分页数据（用于月进度「加载更多」）。"""
        page_items = list(page_items or [])
        if not page_items:
            return
        if not self.isVisible():
            self._render_deferred = True
            return
        new_items = [
            it for it in page_items
            if int(it.get("id") or 0) and int(it.get("id") or 0) not in self._cards_by_id
        ]
        if not new_items:
            return
        self._render_gen += 1
        gen = self._render_gen
        self._render_queue = list(new_items)
        self._list_render_active = True
        self.task_list.setUpdatesEnabled(False)
        self._render_task_batch(gen, finalize=True)

    def _make_task_card(self, it: dict, target_w: int) -> tuple[int, TaskCardWidget, QListWidgetItem]:
        tid = int(it.get("id") or 0)
        card = TaskCardWidget(it)
        card.action_triggered.connect(self._on_card_action)
        card.open_chat_requested.connect(self.task_open_customer_chat.emit)
        card.wechat_send_requested.connect(self.task_wechat_send_requested.emit)
        card.open_phone_requested.connect(self.task_open_customer_phone.emit)
        if tid:
            self._cards_by_id[tid] = card
        item = QListWidgetItem(self.task_list)
        item.setData(Qt.UserRole, tid)
        card.setMinimumWidth(0)
        card.setMaximumWidth(16777215)
        item.setSizeHint(QSize(target_w, card.sizeHint().height()))
        self.task_list.addItem(item)
        self.task_list.setItemWidget(item, card)
        return tid, card, item

    def _render_task_batch(self, gen: int, *, finalize: bool):
        if gen != self._render_gen:
            return
        if not self.isVisible():
            self._render_deferred = True
            self.task_list.setUpdatesEnabled(True)
            self._list_render_active = False
            return

        target_w = max(self.task_list.viewport().width(), 320)
        batch = self._render_queue[:_TASK_LIST_RENDER_BATCH]
        self._render_queue = self._render_queue[_TASK_LIST_RENDER_BATCH:]
        for it in batch:
            self._make_task_card(it, target_w)

        if self._render_queue:
            QTimer.singleShot(0, lambda g=gen: self._render_task_batch(g, finalize=finalize))
            return

        self.task_list.setUpdatesEnabled(True)
        self._list_render_active = False
        if finalize:
            self._apply_filter_visibility()
            if self.task_list.count() > 0:
                self._sync_card_widths()

    def _apply_filter_visibility(self):
        """仅切换行可见性，不销毁/重建卡片（筛选切换走此路径）。"""
        if not self._items:
            self._show_empty_state("暂无任务数据")
            return
        visible = 0
        selected = self.task_filter.selected_keys()
        for i in range(self.task_list.count()):
            li = self.task_list.item(i)
            tid = int(li.data(Qt.UserRole) or 0)
            card = self._cards_by_id.get(tid)
            if card is not None:
                row_it = card.task
            else:
                row_it = next((x for x in self._items if int(x.get("id") or 0) == tid), None)
            show = row_it is not None and self._task_matches_filter(
                row_it, selected=selected
            )
            li.setHidden(not show)
            if show:
                visible += 1
        if visible == 0:
            self._show_empty_state("当前筛选下没有任务")
        else:
            self._hide_empty_state()

    def _show_empty_state(self, text: str):
        self.empty_lbl.setText(text)
        self.empty_container.show()
        self.task_list.hide()

    def _hide_empty_state(self):
        self.empty_container.hide()
        self.task_list.show()

    def _on_card_action(self, task_id: int, op: str, payload: object = None):
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            return
        if op not in ("appeal", "restore"):
            return
        self.task_action_requested.emit(tid, op, payload)

    def _update_stats(self, *, total: int, wechat: int, phone: int, ice: int, pending: int, rate: float):
        self.card_total.set_value(str(total))
        self.card_wechat.set_value(str(wechat))
        self.card_phone.set_value(str(phone))
        self.card_ice.set_value(str(ice))
        self.card_pending.set_value(str(pending))
        pct = max(0, min(100, int(round(rate * 100))))
        self.card_rate.set_value(f"{pct}%")
        # 进度条宽度联动
        bar_w = max(1, self.progress_bar.width())
        inner_w = int(bar_w * pct / 100)
        self.progress_inner.setFixedWidth(inner_w)

    def _update_meta_line(self, stats: dict | None = None):
        stats = stats or {}
        meta = self._last_meta or {}
        period_label = next((lab for k, lab in _PERIODS if k == meta.get("period_type")), self._period)
        period_start = meta.get("period_start") or ""
        period_end = meta.get("period_end") or ""
        parts = [f"周期 <b>{period_label}</b>"]
        if period_start and period_end:
            parts.append(f"{period_start} ~ {period_end}")
        elif period_start:
            parts.append(f"自 {period_start}")
        bid = meta.get("batch_id")
        view_mode = meta.get("view_mode")
        if view_mode == "month_progress":
            parts.append("<span style='color:#576b95;'>汇总本月日/周任务（按截止日）</span>")
        elif bid:
            bstatus = (meta.get("batch_status") or "").strip().lower()
            status_label_map = {
                "published": ("已发布", "#07c160"),
                "draft": ("草稿待发布", "#fa8c16"),
                "archived": ("已归档", "#8c8c8c"),
            }
            label, color = status_label_map.get(bstatus, (bstatus or "未知", "#8c8c8c"))
            badge = f"<span style='color:{color}; font-weight:bold;'>{label}</span>"
            parts.append(f"批次 <b>#{bid}</b> {badge}")
        else:
            parts.append("<span style='color:#fa8c16;'>当前周期暂无批次，请等待自动分配或在管理后台手动生成</span>")
        done = int(stats.get("done") or 0)
        overdue = int(stats.get("overdue") or 0)
        skipped = int(stats.get("skipped") or 0)
        parts.append(f"已完成 {done} · 逾期 {overdue} · 跳过 {skipped}")
        snap = meta.get("snapshot") or {}
        if isinstance(snap, dict):
            if snap.get("main_wechat_count") is not None:
                parts.append(f"快照 微信 <b>{snap.get('main_wechat_count')}</b>")
            if snap.get("main_phone_count") is not None:
                parts.append(f"/ 电话 <b>{snap.get('main_phone_count')}</b>")
            caps = snap.get("channel_caps") or {}
            if isinstance(caps, dict) and (caps.get("wechat") is not None or caps.get("phone") is not None):
                parts.append(
                    f" · 上限 微信 <b>{caps.get('wechat', '—')}</b> / 电话 <b>{caps.get('phone', '—')}</b>"
                )
        sw = self.current_sales_wechat_id()
        sales_label = ""
        if sw:
            idx = self.sales_combo.currentIndex()
            if idx >= 0:
                sales_label = self.sales_combo.itemText(idx)
        if sales_label:
            parts.insert(0, f"销售 <b>{sales_label}</b>")
        self.meta_lbl.setText(" · ".join(parts))

    def _sync_card_widths(self):
        if self.task_list.count() == 0:
            return
        target_w = max(self.task_list.viewport().width() - 8, 280)
        for i in range(self.task_list.count()):
            item = self.task_list.item(i)
            w = self.task_list.itemWidget(item)
            if w is None:
                continue
            w.setFixedWidth(target_w)
            item.setSizeHint(QSize(target_w, w.sizeHint().height()))

    # ── 主题 ──
    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg = "#272727" if is_dark else "#f5f6f8"
        text = "#e8e8e8" if is_dark else "#1a1a1a"
        sub_text = "#999999" if is_dark else "#666666"
        toolbar_bg = "rgba(255,255,255,0.05)" if is_dark else "#ffffff"
        toolbar_border = "rgba(255,255,255,0.10)" if is_dark else "rgba(0,0,0,0.08)"
        progress_track = "rgba(255,255,255,0.12)" if is_dark else "rgba(0,0,0,0.08)"
        progress_fill = "#07c160"

        self.setStyleSheet(
            f"""
            QFrame#TaskAllocationPage {{
                background-color: {bg};
            }}
            QListWidget#TaskList {{
                background-color: transparent;
                border: none;
                outline: none;
            }}
            QListWidget#TaskList::item {{
                background-color: transparent;
                border: none;
                padding: 0px;
                margin: 0px;
            }}
            QListWidget#TaskList::item:selected,
            QListWidget#TaskList::item:hover {{
                background-color: transparent;
                border: none;
            }}
            QWidget#TaskListLoadingOverlay {{
                background-color: {"rgba(39,39,39,0.72)" if is_dark else "rgba(245,246,248,0.88)"};
            }}
            QLabel#TaskEmptyLabel {{
                color: {sub_text};
                padding: 24px 8px;
            }}
            QLabel#TaskMetaLabel {{
                color: {sub_text};
                font-size: 11px;
            }}
            QFrame#TaskProgressBar {{
                background-color: {progress_track};
                border-radius: 3px;
            }}
            QFrame#TaskProgressInner {{
                background-color: {progress_fill};
                border-radius: 3px;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 6px;
                margin: 2px 0px 2px 0px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(128,128,128,0.45);
                border-radius: 3px;
                min-height: 28px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            """
        )
        style_label(self.title_lbl, "section", color=text)
        self.meta_lbl.setStyleSheet(
            f"QLabel#TaskMetaLabel {{ {label_qss('caption', color=sub_text)} }}"
        )
        self._toolbar_frame.setStyleSheet(
            f"QFrame#TaskToolbar {{ background-color: {toolbar_bg};"
            f" border: 1px solid {toolbar_border}; border-radius: 8px; }}"
        )
        # 工具栏内固定文字 label
        for lbl in (self._lbl_sw, self._lbl_period, self._lbl_filter):
            style_label(lbl, "caption", color=sub_text)
        # 统计卡片
        for c in (
            self.card_total,
            self.card_wechat,
            self.card_phone,
            self.card_ice,
            self.card_pending,
            self.card_rate,
        ):
            c._apply_theme_style()
        # 列表内任务卡片
        for i in range(self.task_list.count()):
            item = self.task_list.item(i)
            w = self.task_list.itemWidget(item)
            if w and hasattr(w, "_apply_theme_style"):
                w._apply_theme_style()
        # 段控件（周期）+ 筛选栏
        self._set_period_active(self._period)
        if hasattr(self, "task_filter"):
            self.task_filter._apply_theme_style()

    def _style_segment_button(self, btn: PushButton, *, active: bool, accent: str | None = None):
        is_dark = isDarkTheme()
        accent = accent or "#07c160"
        if active:
            fg = "#ffffff"
            bg = accent
            border = accent
        else:
            fg = "#dddddd" if is_dark else "#444444"
            bg = "rgba(255,255,255,0.04)" if is_dark else "rgba(0,0,0,0.03)"
            border = "rgba(255,255,255,0.10)" if is_dark else "rgba(0,0,0,0.08)"
        btn.setStyleSheet(
            "QPushButton {"
            f" color: {fg};"
            f" background-color: {bg};"
            f" border: 1px solid {border};"
            " padding: 2px 12px;"
            " border-radius: 12px;"
            f" font-size: {12}px;"
            " font-weight: 500;"
            "}"
            "QPushButton:hover {"
            f" border: 1px solid {accent};"
            "}"
        )

    # ── Qt 事件 ──
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._width_sync_timer.start()
        try:
            pct_text = self.card_rate.value_lbl.text().rstrip("%").strip()
            pct = int(pct_text) if pct_text.isdigit() else 0
        except Exception:
            pct = 0
        bar_w = max(1, self.progress_bar.width())
        self.progress_inner.setFixedWidth(int(bar_w * pct / 100))

    def _clear_list_with_placeholder(self, text: str):
        self._items = []
        self._cards_by_id.clear()
        self.task_list.clear()
        self._show_empty_state(text)
        self._update_stats(total=0, wechat=0, phone=0, ice=0, pending=0, rate=0.0)
