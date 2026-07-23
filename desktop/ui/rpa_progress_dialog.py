"""RPA 发送进度提示弹窗；提供「中断」以协作取消后台 RPA 线程，并展示各步骤。"""

import ctypes
import threading
from ctypes import wintypes

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QCloseEvent, QGuiApplication, QShowEvent
from PySide6.QtWidgets import QDialog, QHBoxLayout, QListWidget, QListWidgetItem, QVBoxLayout

from qfluentwidgets import BodyLabel, CaptionLabel, IndeterminateProgressRing, PushButton


def _query_wechat_window_rect() -> QRect | None:
    """轻量查找微信主窗口矩形，供外发进度窗避让点击区。"""
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return None

    # (class, title)；title=None 表示不限定标题
    probes = (
        ("mmui::MainWindow", None),
        ("WeChatMainWndForPC", None),
        ("Qt51514QWindowIcon", "微信"),
        ("Qt6QWindowIcon", "微信"),
        ("Chrome_WidgetWin_0", "微信"),
        ("WeWorkWindow", None),
        (None, "微信"),
    )
    hwnd = 0
    for cls, title in probes:
        hwnd = user32.FindWindowW(cls, title)
        if hwnd:
            break
    if not hwnd or not user32.IsWindowVisible(hwnd):
        return None

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return QRect(int(rect.left), int(rect.top), int(rect.right - rect.left), int(rect.bottom - rect.top))


class RpaProgressDialog(QDialog):
    step_appended = Signal(str)

    def __init__(self, parent=None, *, title: str = "正在发送", detail: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(400)
        self.setMinimumHeight(260)
        self.setWindowFlag(Qt.WindowCloseButtonHint, True)
        # 显示进度时不抢键盘焦点，避免首次外发时 RPA 无法把微信切到前台
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self._cancel_event = threading.Event()
        self._completed = False
        self._confirm_waiter: tuple[threading.Event, list[bool]] | None = None
        self._placed_away_from_wechat = False

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        lab_title = BodyLabel(title)
        lab_title.setWordWrap(True)
        layout.addWidget(lab_title)

        if detail:
            self._lab_detail = CaptionLabel(detail)
            self._lab_detail.setWordWrap(True)
            layout.addWidget(self._lab_detail)
        else:
            self._lab_detail = None

        self._lab_hint = CaptionLabel(
            "外发自动化进行中请勿操作键鼠或切换窗口；需要停止请点下方「中断」。"
        )
        self._lab_hint.setWordWrap(True)
        layout.addWidget(self._lab_hint)

        ring = IndeterminateProgressRing(self)
        ring.setFixedSize(22, 22)
        ring.setStrokeWidth(2)
        ring.start()
        layout.addWidget(ring)

        self._step_list = QListWidget(self)
        self._step_list.setMinimumHeight(100)
        self._step_list.setFocusPolicy(Qt.NoFocus)
        layout.addWidget(self._step_list)

        self._lab_confirm = CaptionLabel("")
        self._lab_confirm.setWordWrap(True)
        self._lab_confirm.hide()
        layout.addWidget(self._lab_confirm)

        confirm_row = QHBoxLayout()
        self._btn_confirm_yes = PushButton("已跳转，继续发送")
        self._btn_confirm_yes.setToolTip("确认微信已切换到正确客户对话后继续发送")
        self._btn_confirm_yes.clicked.connect(self._on_confirm_yes)
        self._btn_confirm_yes.hide()
        confirm_row.addWidget(self._btn_confirm_yes)

        self._btn_confirm_no = PushButton("取消发送")
        self._btn_confirm_no.clicked.connect(self._on_confirm_no)
        self._btn_confirm_no.hide()
        confirm_row.addWidget(self._btn_confirm_no)
        layout.addLayout(confirm_row)

        self._btn_cancel = PushButton("中断")
        self._btn_cancel.setToolTip("停止本次微信自动化，避免误操作到错误联系人")
        self._btn_cancel.clicked.connect(self._on_cancel_clicked)
        layout.addWidget(self._btn_cancel)

        self.step_appended.connect(self._on_step_appended)

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    def append_step(self, text: str) -> None:
        """线程安全：可从 RPA 工作线程调用。"""
        msg = (text or "").strip()
        if not msg:
            return
        self.step_appended.emit(msg)

    def _on_step_appended(self, text: str) -> None:
        item = QListWidgetItem(text)
        self._step_list.addItem(item)
        self._step_list.scrollToBottom()

    def prepare_user_confirm(
        self,
        message: str,
        done_event: threading.Event,
        answer: list[bool],
    ) -> None:
        """在主线程调用：展示确认按钮；由工作线程等待 done_event。"""
        self.setAttribute(Qt.WA_ShowWithoutActivating, False)
        self.activateWindow()
        self.raise_()
        self._confirm_waiter = (done_event, answer)
        self._lab_confirm.setText((message or "").strip())
        self._lab_confirm.show()
        self._btn_confirm_yes.show()
        self._btn_confirm_no.show()
        self._btn_cancel.setEnabled(False)

    def _hide_confirm_ui(self) -> None:
        self._lab_confirm.hide()
        self._btn_confirm_yes.hide()
        self._btn_confirm_no.hide()
        if not self._cancel_event.is_set():
            self._btn_cancel.setEnabled(True)

    def _resolve_confirm(self, accepted: bool) -> None:
        waiter = self._confirm_waiter
        if waiter is None:
            return
        event, answer = waiter
        answer[0] = accepted
        self._confirm_waiter = None
        self._hide_confirm_ui()
        event.set()

    def _on_confirm_yes(self):
        self.append_step("用户确认：已跳转，继续发送")
        self._resolve_confirm(True)

    def _on_confirm_no(self):
        self.append_step("用户取消：未确认跳转")
        self._resolve_confirm(False)

    def mark_completed(self) -> None:
        self._completed = True
        waiter = self._confirm_waiter
        if waiter is not None:
            self._resolve_confirm(False)

    def _on_cancel_clicked(self):
        self._cancel_event.set()
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.setText("正在中断…")
        self.append_step("用户请求中断…")
        self._resolve_confirm(False)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # 置顶进度窗若居中叠在微信上，RPA 点击对话/输入框会误点到「中断」
        if not self._placed_away_from_wechat:
            self._placed_away_from_wechat = True
            self._position_away_from_wechat()

    def _position_away_from_wechat(self) -> None:
        """将弹窗放到尽量不与微信窗口重叠的位置（优先父窗右下角 / 屏幕角落）。"""
        self.adjustSize()
        w = max(self.width(), self.minimumWidth())
        h = max(self.height(), self.minimumHeight())
        margin = 12

        parent = self.parentWidget()
        screen = None
        if parent is not None:
            screen = parent.screen()
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()

        wechat = _query_wechat_window_rect()
        # RPA 常点微信右下输入区 / 消息列表底部，重叠惩罚更高
        danger = QRect()
        if wechat is not None and wechat.isValid():
            danger = QRect(
                wechat.left() + int(wechat.width() * 0.45),
                wechat.top() + int(wechat.height() * 0.55),
                max(1, int(wechat.width() * 0.55)),
                max(1, int(wechat.height() * 0.45)),
            )

        candidates: list[tuple[int, int]] = []
        if parent is not None:
            pg = parent.frameGeometry()
            candidates.extend(
                [
                    (pg.right() - w - margin, pg.bottom() - h - margin),
                    (pg.right() - w - margin, pg.top() + margin),
                    (pg.left() + margin, pg.bottom() - h - margin),
                    (pg.left() + margin, pg.top() + margin),
                ]
            )
        if wechat is not None and wechat.isValid():
            candidates[0:0] = [
                (wechat.right() + margin, wechat.top() + margin),
                (wechat.right() + margin, wechat.bottom() - h - margin),
                (wechat.left() - w - margin, wechat.top() + margin),
                (wechat.left() - w - margin, wechat.bottom() - h - margin),
            ]
        candidates.extend(
            [
                (avail.right() - w - margin, avail.bottom() - h - margin),
                (avail.right() - w - margin, avail.top() + margin),
                (avail.left() + margin, avail.bottom() - h - margin),
                (avail.left() + margin, avail.top() + margin),
            ]
        )

        def _clamp(x: int, y: int) -> tuple[int, int]:
            x = max(avail.left(), min(x, avail.right() - w))
            y = max(avail.top(), min(y, avail.bottom() - h))
            return x, y

        def _overlap_area(a: QRect, b: QRect) -> int:
            if not a.isValid() or not b.isValid():
                return 0
            inter = a.intersected(b)
            if inter.isEmpty():
                return 0
            return int(inter.width() * inter.height())

        best: tuple[int, int] | None = None
        best_score: int | None = None
        for cx, cy in candidates:
            x, y = _clamp(cx, cy)
            dlg_rect = QRect(x, y, w, h)
            score = _overlap_area(dlg_rect, wechat) if wechat is not None else 0
            score += _overlap_area(dlg_rect, danger) * 8
            if best_score is None or score < best_score:
                best = (x, y)
                best_score = score
                if score == 0:
                    break

        if best is not None:
            self.move(best[0], best[1])

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._completed:
            self._cancel_event.set()
        self._resolve_confirm(False)
        super().closeEvent(event)
