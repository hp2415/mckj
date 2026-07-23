"""桌面端统一排版：字体族、字号阶梯、语义色与样式助手。"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QWidget

from qfluentwidgets import isDarkTheme
from qfluentwidgets.common.config import qconfig
from qfluentwidgets.common.font import setFontFamilies
from qfluentwidgets.common.font import setFont as apply_widget_font

_FONT_FAMILIES_WIN = [
    "Microsoft YaHei UI",
    "Segoe UI Variable",
    "Segoe UI",
    "PingFang SC",
    "sans-serif",
]
_FONT_FAMILIES_DEFAULT = [
    "PingFang SC",
    "Microsoft YaHei UI",
    "Segoe UI",
    "sans-serif",
]

# 字号阶梯（px）— 小字不低于 11，侧栏副文案 12，避免糊成一团
SIZE_XS = 11
SIZE_SM = 12
SIZE_BASE = 12
SIZE_MD = 13
SIZE_LG = 15
SIZE_XL = 18

WEIGHT_NORMAL = 400
WEIGHT_MEDIUM = 500
WEIGHT_SEMIBOLD = 600
WEIGHT_BOLD = 700

# 小于等于该字号时附加轻微字距，提升小字可读性
_TRACKING_MAX_SIZE = 12
_TRACKING_CSS = "letter-spacing: 0.25px;"


@dataclass(frozen=True)
class TextPalette:
    primary: str
    secondary: str
    tertiary: str
    muted: str
    accent: str


def text_palette() -> TextPalette:
    """深浅主题语义色：在柔和与可读之间略抬高对比，减少「发灰发黏」感。"""
    if isDarkTheme():
        return TextPalette(
            primary="#ebebeb",
            secondary="#b3b3b3",
            tertiary="#949494",
            muted="#787878",
            accent="#95e06e",
        )
    return TextPalette(
        primary="#141414",
        secondary="#3d3d3d",
        tertiary="#5c5c5c",
        muted="#757575",
        accent="#06ae56",
    )


# role -> (size_px, weight, palette_field)
_LABEL_ROLES: dict[str, tuple[int, int, str]] = {
    "page_title": (SIZE_XL, WEIGHT_SEMIBOLD, "primary"),
    "section": (SIZE_LG, WEIGHT_MEDIUM, "primary"),
    "body": (SIZE_MD, WEIGHT_NORMAL, "primary"),
    "body_emphasis": (SIZE_MD, WEIGHT_MEDIUM, "primary"),
    "sidebar_primary": (SIZE_BASE, WEIGHT_MEDIUM, "primary"),
    "sidebar_secondary": (SIZE_SM, WEIGHT_NORMAL, "secondary"),
    "sidebar_group": (SIZE_BASE, WEIGHT_MEDIUM, "primary"),
    "sidebar_group_sub": (SIZE_SM, WEIGHT_NORMAL, "secondary"),
    "caption": (SIZE_SM, WEIGHT_NORMAL, "secondary"),
    "caption_emphasis": (SIZE_SM, WEIGHT_MEDIUM, "secondary"),
    "micro": (SIZE_XS, WEIGHT_NORMAL, "tertiary"),
    "stat_value": (SIZE_XL, WEIGHT_SEMIBOLD, "primary"),
    "stat_label": (SIZE_SM, WEIGHT_NORMAL, "secondary"),
    "price": (13, WEIGHT_SEMIBOLD, "primary"),
    "product_title": (SIZE_MD, WEIGHT_MEDIUM, "primary"),
    "empty": (SIZE_MD, WEIGHT_NORMAL, "muted"),
    "chat_bubble": (SIZE_MD, WEIGHT_NORMAL, "primary"),
    "chat_meta": (SIZE_SM, WEIGHT_NORMAL, "secondary"),
    "badge": (SIZE_SM, WEIGHT_MEDIUM, "primary"),
    "link": (SIZE_SM, WEIGHT_MEDIUM, "accent"),
}


def _qt_weight(weight: int) -> QFont.Weight:
    return {
        400: QFont.Weight.Normal,
        500: QFont.Weight.Medium,
        600: QFont.Weight.DemiBold,
        700: QFont.Weight.Bold,
    }.get(weight, QFont.Weight.Normal)


def make_ui_font(size_px: int, weight: int = WEIGHT_NORMAL) -> QFont:
    """创建 UI 字体。

    设计阶梯 SIZE_* 以像素计；这里按屏幕 DPI 换算为 point 再 setPointSize，
    既接近原先 setPixelSize 的视觉大小，又保证 pointSize()>0，避免原生菜单告警。
    """
    font = QFont()
    try:
        families = qconfig.get(qconfig.fontFamilies)
        if families:
            font.setFamilies(list(families))
    except Exception:
        pass
    dpi = 96.0
    app = QApplication.instance()
    if app is not None:
        try:
            screen = app.primaryScreen()
            if screen is not None:
                dpi = float(screen.logicalDotsPerInch()) or 96.0
        except Exception:
            dpi = 96.0
    pt = max(1.0, float(size_px or 1) * 72.0 / dpi)
    font.setPointSizeF(pt)
    font.setWeight(_qt_weight(weight))
    return font


def _tracking_for_size(size_px: int) -> str:
    if size_px <= _TRACKING_MAX_SIZE:
        return _TRACKING_CSS
    return ""


def label_qss(role: str, *, color: str | None = None, extra: str = "") -> str:
    """生成标签用 QSS 片段。"""
    size, weight, color_key = _LABEL_ROLES[role]
    pal = text_palette()
    col = color if color is not None else getattr(pal, color_key)
    qss = (
        f"color: {col}; font-size: {size}px; font-weight: {weight};"
        f" background: transparent; {_tracking_for_size(size)}"
    )
    if extra:
        qss = f"{qss} {extra}"
    return qss


def style_label(widget: QWidget, role: str, *, color: str | None = None, extra: str = "") -> None:
    """同时设置 QFont 与 QSS，保证 Fluent 控件与原生 QLabel 一致。"""
    size, weight, _ = _LABEL_ROLES[role]
    font = make_ui_font(size, weight)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality)
    try:
        font.setHintingPreference(QFont.HintingPreference.PreferDefaultHinting)
    except Exception:
        pass
    widget.setFont(font)
    widget.setStyleSheet(label_qss(role, color=color, extra=extra))


def badge_qss(fg: str, bg: str, *, border: str | None = None, padding: str = "1px 8px") -> str:
    size, weight, _ = _LABEL_ROLES["badge"]
    b = border if border is not None else f"{fg}66"
    return (
        f"color: {fg}; background-color: {bg}; border: 1px solid {b};"
        f" padding: {padding}; border-radius: 8px;"
        f" font-size: {size}px; font-weight: {weight}; {_TRACKING_CSS}"
    )


def compact_button_qss(*, fg: str, bg: str, border: str, hover_bg: str, hover_border: str) -> str:
    size, weight, _ = _LABEL_ROLES["badge"]
    return (
        f"QPushButton {{ color: {fg}; background-color: {bg};"
        f" border: 1px solid {border}; border-radius: 5px;"
        f" padding: 1px 10px; font-size: {size}px; font-weight: {weight}; {_TRACKING_CSS} }}"
        f"QPushButton:hover {{ background-color: {hover_bg}; border-color: {hover_border}; }}"
        f"QPushButton:pressed {{ background-color: rgba(7,193,96,0.18);"
        f" border-color: {hover_border}; color: {hover_border}; }}"
    )


def apply_app_typography(app: QApplication | None = None) -> None:
    """在 QApplication 创建后调用一次。"""
    families = _FONT_FAMILIES_WIN if sys.platform == "win32" else _FONT_FAMILIES_DEFAULT
    setFontFamilies(families, save=False)

    app = app or QApplication.instance()
    if app is None:
        return

    base = make_ui_font(SIZE_MD, WEIGHT_NORMAL)
    base.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality)
    try:
        # 小字号中文在 LCD 上默认 hinting 通常比完全关闭更清晰
        base.setHintingPreference(QFont.HintingPreference.PreferDefaultHinting)
    except Exception:
        pass
    app.setFont(base)
