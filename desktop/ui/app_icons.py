from enum import Enum
from pathlib import Path

from qfluentwidgets import FluentIconBase, Theme, isDarkTheme


_ICON_DIR = Path(__file__).resolve().parents[1] / "assets"


class AppIcon(FluentIconBase, Enum):
    """Project SVG icons that follow qfluentwidgets light/dark themes."""

    PROFILE = "个人信息"
    SEND_WECHAT = "微信外发"
    HEART = "心"
    HEART_BROKEN = "心碎"
    # 侧边栏等固定样式场景：单文件 SVG，不随深浅主题切换
    TASK_LIST = "任务清单"


    def path(self, theme=Theme.AUTO):
        if self is AppIcon.TASK_LIST:
            return str(_ICON_DIR / f"{self.value}.svg")
        is_dark = isDarkTheme() if theme == Theme.AUTO else theme == Theme.DARK
        suffix = "dark" if is_dark else "light"
        return str(_ICON_DIR / f"{self.value}_{suffix}.svg")
