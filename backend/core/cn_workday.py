"""中国大陆工作日判断（法定节假日、周末休息；含调休上班日）。"""
from __future__ import annotations

from datetime import date


def is_cn_workday(d: date) -> bool:
    """
    是否为工作日。周六日休息、法定节假日休息；国务院调休的周末上班日记为工作日。
    依赖 chinesecalendar；未安装或年份超出库数据时回退为周一至周五。
    """
    try:
        from chinese_calendar import is_workday

        return bool(is_workday(d))
    except Exception:
        return d.weekday() < 5
