# ui/widgets 通用可复用组件包

from PySide6.QtWidgets import QListWidget


def resolve_list_content_width(list_widget: QListWidget) -> int:
    """解析列表可用内容宽度。

    ``QStackedWidget`` 中非当前页在首次展示前 ``viewport().width()`` 常为 0，
    此时回退到控件自身宽度或父级已布局宽度，避免后台渲染的卡片拿不到约束。
    """
    vp = list_widget.viewport().width()
    if vp > 0:
        return vp
    w = list_widget.width()
    if w > 0:
        frame = list_widget.frameWidth() * 2
        return max(0, w - frame)
    parent = list_widget.parentWidget()
    while parent is not None:
        pw = parent.width()
        if pw > 0:
            return pw
        parent = parent.parentWidget()
    return 0


def safe_card_width(
    list_widget: QListWidget,
    *,
    min_width: int = 120,
    scrollbar_reserve: int = 4,
    extra_reserve: int = 0,
    viewport_width: int | None = None,
) -> int:
    """根据列表视口计算卡片安全宽度。

    列表卡片若直接用 ``viewport().width()`` 作为固定宽度，会忽略两侧的
    ``spacing`` 边距与悬浮式竖向滚动条所覆盖的区域，导致卡片被强行撑得过宽、
    右侧靠右排列的按钮被挤出可视区或被滚动条遮挡。此处统一扣除：

    - ``spacing * 2``：列表项左右两侧的间距（``QListWidget.setSpacing`` 设置）；
    - ``scrollbar_reserve``：qfluentwidgets 悬浮竖向滚动条宽度（~6px）+ 容错；
    - ``extra_reserve``：调用方按需追加的额外余量。

    ``viewport_width`` 可由 ``resolve_list_content_width`` 预解析后传入。
    返回 0 表示视口尚未布局完成（宽度不可用），调用方应跳过本次同步。
    """
    vp = viewport_width if viewport_width is not None else resolve_list_content_width(list_widget)
    if vp <= 0:
        return 0
    spacing = max(0, list_widget.spacing())
    reserve = spacing * 2 + scrollbar_reserve + extra_reserve
    return max(min_width, vp - reserve)
