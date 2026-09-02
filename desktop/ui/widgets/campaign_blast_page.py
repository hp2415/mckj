"""桌面端「活动群发」页面。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
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
    PushButton,
    SearchLineEdit,
    SpinBox,
    StrongBodyLabel,
    SubtitleLabel,
    isDarkTheme,
)


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
        self.setWindowTitle("添加客户到外发名单")
        self.resize(640, 520)
        self._sales_wechat_id = sales_wechat_id
        self._campaign_id = campaign_id
        self._job_id = job_id
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.addWidget(
            CaptionLabel("检索当前执行微信号下的客户。单击/拖选/Shift 连选，Ctrl 点选，勾选后加入。")
        )

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

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["选", "昵称", "备注", "单位性质"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.resizeSection(0, 40)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table, 1)

        self.status_lbl = CaptionLabel("")
        layout.addWidget(self.status_lbl)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("加入选中")
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

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
                self.table.setItem(row, 1, name_item)
                self.table.setItem(row, 2, QTableWidgetItem(remark))
                self.table.setItem(row, 3, QTableWidgetItem(ut))
            shown = len(items or [])
            extra = ""
            if total > shown:
                extra = "；仅显示前 {} 条，请用搜索缩小范围".format(shown)
            self.status_lbl.setText(f"共 {total} 条可添加，当前显示 {shown} 条{extra}")
        finally:
            self._syncing = False

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
        self._job: dict | None = None
        self._sending = False
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
        title_row.addWidget(SubtitleLabel("活动群发"))
        title_row.addStretch()
        self.lbl_progress = CaptionLabel("")
        title_row.addWidget(self.lbl_progress)
        root.addLayout(title_row)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_row.addWidget(BodyLabel("执行微信号"))
        self.sales_combo = ComboBox()
        self.sales_combo.setMinimumWidth(160)
        self.sales_combo.setPlaceholderText("请选择销售微信号")
        self.sales_combo.currentIndexChanged.connect(self._on_sales_or_campaign_changed)
        filter_row.addWidget(self.sales_combo)

        filter_row.addWidget(BodyLabel("单位性质"))
        self.unit_combo = ComboBox()
        self.unit_combo.setMinimumWidth(110)
        self.unit_combo.currentTextChanged.connect(self._on_unit_changed)
        filter_row.addWidget(self.unit_combo)

        filter_row.addWidget(BodyLabel("活动"))
        self.campaign_combo = ComboBox()
        self.campaign_combo.setMinimumWidth(180)
        self.campaign_combo.currentIndexChanged.connect(self._on_sales_or_campaign_changed)
        filter_row.addWidget(self.campaign_combo, 1)
        root.addLayout(filter_row)

        gen_row = QHBoxLayout()
        gen_row.setSpacing(8)
        gen_row.addWidget(BodyLabel("数量"))
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
        tbl_hdr.addWidget(StrongBodyLabel("外发名单"))
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

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["选", "客户备注", "单位性质", "发送状态", "发送话术", "错误详情", "操作"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(_COL_CHECK, QHeaderView.Fixed)
        header.resizeSection(_COL_CHECK, 40)
        header.setSectionResizeMode(_COL_REMARK, QHeaderView.Stretch)
        header.setSectionResizeMode(_COL_UNIT, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_STATUS, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(_COL_SCRIPT, QHeaderView.Stretch)
        header.setSectionResizeMode(_COL_ERROR, QHeaderView.Stretch)
        header.setSectionResizeMode(_COL_ACTIONS, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
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

        self._apply_theme()
        self._refresh_actions()

    def _apply_theme(self):
        bg = "#2b2b2b" if isDarkTheme() else "#f5f5f5"
        self.setStyleSheet(f"QFrame#CampaignBlastPage {{ background: {bg}; }}")

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
            w.setEnabled(not busy)
        self.lbl_progress.setText(message if busy else "")

    def set_sending(self, sending: bool):
        self._sending = sending
        self.btn_cancel_send.setVisible(sending)
        self.btn_send.setEnabled(not sending)
        self.btn_gen_list.setEnabled(not sending)
        self.btn_gen_scripts.setEnabled(not sending)
        self.btn_add.setEnabled(not sending)
        self.btn_del.setEnabled(not sending)
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
                self.table.setItem(row, _COL_REMARK, QTableWidgetItem(remark))
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
                self.table.setItem(row, _COL_SCRIPT, QTableWidgetItem(script))
                self.table.setItem(
                    row, _COL_ERROR, QTableWidgetItem((rec.get("error_message") or "").strip())
                )

                action_w = QWidget()
                action_l = QHBoxLayout(action_w)
                action_l.setContentsMargins(4, 2, 4, 2)
                action_l.setSpacing(6)
                btn_edit = PushButton("修改话术")
                btn_edit.setFixedHeight(28)
                btn_edit.clicked.connect(lambda _=False, r=rid: self._on_edit_row(r))
                action_l.addWidget(btn_edit)
                can_send = st in ("pending", "failed") and script and rec.get("poster_id")
                btn_send = PushButton("重试" if st == "failed" else "发送")
                btn_send.setFixedHeight(28)
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

    def _refresh_actions(self):
        has_job = bool(self._job and self._job.get("recipients"))
        self.btn_gen_scripts.setEnabled(has_job and not self._sending)
        self.btn_add.setEnabled(bool(self.current_job_id()) and not self._sending)
        self.btn_del.setEnabled(has_job and not self._sending)

        camp_ok = False
        cid = self.current_campaign_id()
        idx = self._find_campaign_index(cid) if cid else -1
        if idx >= 0:
            camp_ok = "无海报" not in self.campaign_combo.itemText(idx)
        recipients = (self._job or {}).get("recipients") or []
        sendable = [
            r
            for r in recipients
            if (r.get("status") or "") in ("pending", "failed")
            and (r.get("script_text") or "").strip()
            and r.get("poster_id")
        ]
        self.btn_send.setEnabled(bool(sendable) and camp_ok and not self._sending)

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
        self.generate_scripts_requested.emit(jid, sel if sel else None)

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
        self._cancel_flag = True

    def is_send_cancelled(self) -> bool:
        return self._cancel_flag
