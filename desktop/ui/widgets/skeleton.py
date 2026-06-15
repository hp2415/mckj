"""骨架屏组件：基于 SkeletonBase 实现 Fluent 风格加载占位（无需 Pro 版）。"""
from __future__ import annotations

from typing import Literal

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget
from qfluentwidgets import isDarkTheme

CardSkeletonStyle = Literal["task", "lead", "compact"]


class SkeletonBase(QWidget):
    """骨架屏基类：提供闪烁动画，子类在 paint_skeleton 中绘制占位形状。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._on_tick)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def start(self):
        self._timer.start()
        self.show()
        self.update()

    def stop(self):
        self._timer.stop()
        self.hide()

    def is_active(self) -> bool:
        return self._timer.isActive()

    def _on_tick(self):
        self._phase = (self._phase + 0.04) % 1.0
        self.update()

    def _base_color(self) -> QColor:
        return QColor(72, 72, 76) if isDarkTheme() else QColor(228, 228, 230)

    def _highlight_color(self) -> QColor:
        return QColor(96, 96, 100) if isDarkTheme() else QColor(245, 245, 247)

    def _shimmer_brush(self, rect: QRectF):
        base = self._base_color()
        hi = self._highlight_color()
        grad = QLinearGradient(rect.left() - rect.width(), rect.top(), rect.right() + rect.width(), rect.top())
        shift = self._phase
        grad.setColorAt(max(0.0, shift - 0.35), base)
        grad.setColorAt(shift, hi)
        grad.setColorAt(min(1.0, shift + 0.35), base)
        return grad

    def _fill_round_rect(self, painter: QPainter, rect: QRectF, radius: float = 4.0):
        if rect.width() <= 0 or rect.height() <= 0:
            return
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, self._shimmer_brush(rect))

    def _card_colors(self) -> tuple[QColor, QColor, QColor]:
        if isDarkTheme():
            return QColor(46, 46, 46), QColor(255, 255, 255, 30), QColor(24, 144, 255)
        return QColor(255, 255, 255), QColor(0, 0, 0, 23), QColor(24, 144, 255)

    def _paint_card_shell(self, painter: QPainter, rect: QRectF, *, accent: bool = True):
        card_bg, border_col, accent_col = self._card_colors()
        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        painter.fillPath(path, card_bg)
        painter.setPen(QPen(border_col, 1))
        painter.drawPath(path)
        if accent:
            strip_h = max(0.0, rect.height() - 34)
            strip = QRectF(rect.left() + 1, rect.top() + 1, 4, strip_h)
            strip_path = QPainterPath()
            strip_path.addRoundedRect(strip, 2, 2)
            painter.fillPath(strip_path, accent_col)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self.paint_skeleton(painter)

    def paint_skeleton(self, painter: QPainter):
        raise NotImplementedError


class ListSkeletonPanel(SkeletonBase):
    """窄侧栏客户列表骨架（紧凑行，非卡片）。"""

    def __init__(
        self,
        *,
        row_count: int = 6,
        row_height: int = 52,
        row_spacing: int = 8,
        margins: tuple[int, int, int, int] = (10, 12, 10, 12),
        compact: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._row_count = max(1, int(row_count))
        self._row_height = max(28, int(row_height))
        self._row_spacing = max(4, int(row_spacing))
        self._margins = margins
        self._compact = compact
        min_h = margins[1] + margins[3] + self._row_count * (self._row_height + self._row_spacing)
        self.setMinimumHeight(min_h)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def paint_skeleton(self, painter: QPainter):
        left, top, right, bottom = self._margins
        w = max(20.0, float(self.width() - left - right))
        y = float(top)
        for _ in range(self._row_count):
            if self._compact:
                self._fill_round_rect(painter, QRectF(left, y + 6, w * 0.72, 10), 3)
                self._fill_round_rect(painter, QRectF(left, y + 22, w * 0.45, 8), 3)
            else:
                self._fill_round_rect(painter, QRectF(left, y + 8, w * 0.55, 12), 4)
                self._fill_round_rect(painter, QRectF(left, y + 28, w * 0.38, 10), 3)
            y += self._row_height + self._row_spacing
            if y > self.height() - bottom:
                break


class CardListSkeletonPanel(SkeletonBase):
    """卡片列表骨架：任务卡 / 客资卡风格，与真实卡片布局相近。"""

    def __init__(
        self,
        *,
        card_style: CardSkeletonStyle = "task",
        row_count: int = 5,
        row_spacing: int = 8,
        margins: tuple[int, int, int, int] = (8, 16, 8, 8),
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._card_style = card_style
        self._row_count = max(1, int(row_count))
        self._row_spacing = max(4, int(row_spacing))
        self._margins = margins
        card_h = 108 if card_style == "lead" else 100
        min_h = margins[1] + margins[3] + self._row_count * (card_h + self._row_spacing)
        self.setMinimumHeight(min_h)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def paint_skeleton(self, painter: QPainter):
        left, top, right, bottom = self._margins
        w = max(40.0, float(self.width() - left - right))
        card_h = 108.0 if self._card_style == "lead" else 100.0
        y = float(top)
        for _ in range(self._row_count):
            card = QRectF(left, y, w, card_h)
            self._paint_card_shell(painter, card, accent=(self._card_style == "task"))
            inner_l = card.left() + 14
            inner_w = card.width() - 24
            if self._card_style == "task":
                self._paint_task_card_content(painter, inner_l, card.top(), inner_w, card_h)
            elif self._card_style == "lead":
                self._paint_lead_card_content(painter, inner_l, card.top(), inner_w, card_h)
            y += card_h + self._row_spacing
            if y > self.height() - bottom:
                break

    def _paint_task_card_content(self, painter: QPainter, x: float, y: float, w: float, h: float):
        row_y = y + 10
        self._fill_round_rect(painter, QRectF(x, row_y, 28, 14), 7)
        self._fill_round_rect(painter, QRectF(x + 34, row_y, 42, 14), 7)
        self._fill_round_rect(painter, QRectF(x + 82, row_y, 36, 14), 7)
        self._fill_round_rect(painter, QRectF(x + w - 52, row_y, 48, 14), 7)
        self._fill_round_rect(painter, QRectF(x, row_y + 22, w * 0.62, 12), 4)
        self._fill_round_rect(painter, QRectF(x, row_y + 40, w * 0.48, 10), 3)
        self._fill_round_rect(painter, QRectF(x, row_y + 56, w * 0.72, 10), 3)
        foot_y = y + h - 32
        self._fill_round_rect(painter, QRectF(x, foot_y, 72, 10), 3)
        btn_x = x + w - 148
        self._fill_round_rect(painter, QRectF(btn_x, foot_y - 2, 52, 22), 6)
        self._fill_round_rect(painter, QRectF(btn_x + 58, foot_y - 2, 44, 22), 6)

    def _paint_lead_card_content(self, painter: QPainter, x: float, y: float, w: float, h: float):
        row_y = y + 10
        self._fill_round_rect(painter, QRectF(x, row_y, w * 0.78, 14), 4)
        self._fill_round_rect(painter, QRectF(x, row_y + 22, w * 0.92, 10), 3)
        self._fill_round_rect(painter, QRectF(x, row_y + 38, w * 0.55, 10), 3)
        self._fill_round_rect(painter, QRectF(x, row_y + 54, w * 0.68, 10), 3)
        foot_y = y + h - 32
        btn_w = 58.0
        gap = 8.0
        btn_x = x + w - (btn_w * 3 + gap * 2)
        for i in range(3):
            self._fill_round_rect(painter, QRectF(btn_x + i * (btn_w + gap), foot_y - 2, btn_w, 22), 6)


class ProductListSkeletonPanel(SkeletonBase):
    """商品卡片骨架：左侧方图 + 右侧多行文字。"""

    def __init__(self, *, row_count: int = 4, parent: QWidget | None = None):
        super().__init__(parent)
        self._row_count = max(1, int(row_count))
        self.setMinimumHeight(140 * self._row_count)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def paint_skeleton(self, painter: QPainter):
        margin_x, margin_y = 12, 10
        row_h = 128.0
        gap = 10.0
        w = max(40.0, float(self.width() - margin_x * 2))
        y = float(margin_y)
        img_w = 88.0
        text_left = margin_x + img_w + 14.0
        text_w = max(40.0, w - img_w - 14.0)
        for _ in range(self._row_count):
            card = QRectF(margin_x, y, w, row_h)
            self._paint_card_shell(painter, card, accent=False)
            self._fill_round_rect(painter, QRectF(margin_x + 10, y + 12, img_w - 8, 104), 8)
            self._fill_round_rect(painter, QRectF(text_left, y + 16, text_w * 0.85, 12), 4)
            self._fill_round_rect(painter, QRectF(text_left, y + 38, text_w * 0.35, 12), 4)
            self._fill_round_rect(painter, QRectF(text_left, y + 62, text_w * 0.55, 10), 3)
            self._fill_round_rect(painter, QRectF(text_left, y + 82, text_w * 0.42, 10), 3)
            y += row_h + gap
            if y > self.height() - margin_y:
                break
