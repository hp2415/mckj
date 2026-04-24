"""
客户侧栏分组策略：与 MainWindow 解耦，后续可改为配置驱动或注入自定义 builder。

当前规则：
- 「本周建议联系」：suggested_followup_date 落在本周（周一至周日）的客户；可与销售号分组重复出现。
- 「按销售微信号」：按 relation.sales_wechat_id 分桶；分组标题使用 sales_wechat_label（主数据表 nickname）。

分页与「加载更多」由 MainWindow 按组渲染，每组首屏条数见 MainWindow.CUSTOMER_GROUP_PAGE_SIZE。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable


@dataclass(frozen=True)
class SidebarGroup:
    """单个可折叠分组（title_name 不含人数，人数在渲染时按当前列表动态拼接）。"""

    id: str
    title_name: str
    customers: list[dict[str, Any]]
    default_expanded: bool


def monday_week_bounds(today: date) -> tuple[date, date]:
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


def suggested_followup_as_date(customer: dict[str, Any]) -> date | None:
    v = customer.get("suggested_followup_date")
    if v is None:
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def _sales_wechat_key(customer: dict[str, Any]) -> str:
    return (customer.get("sales_wechat_id") or "").strip()


def _sw_bucket_title_name(sk: str, bucket: list[dict[str, Any]]) -> str:
    """销售组分组的展示名：优先备注，否则缩短的微信号。"""
    if sk == "__none__" or not bucket:
        return "未关联销售号"
    label = (bucket[0].get("sales_wechat_label") or "").strip()
    if label:
        return label
    pk = sk
    if len(pk) <= 18:
        return pk
    return pk[:16] + "…"


def build_sidebar_groups(
    customers: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> list[SidebarGroup]:
    """
    将扁平客户列表转为侧栏分组列表（顺序即展示顺序）。
    """
    today = today or date.today()
    w0, w1 = monday_week_bounds(today)

    week_customers: list[dict[str, Any]] = []
    for c in customers:
        d = suggested_followup_as_date(c)
        if d is not None and w0 <= d <= w1:
            week_customers.append(c)
    week_customers.sort(
        key=lambda c: (
            suggested_followup_as_date(c) or date.max,
            c.get("unit_name") or "",
            c.get("customer_name") or "",
        )
    )

    by_sw: dict[str, list[dict[str, Any]]] = {}
    for c in customers:
        sk = _sales_wechat_key(c) or "__none__"
        by_sw.setdefault(sk, []).append(c)

    def sw_sort_key(k: str) -> tuple:
        return (k == "__none__", k.lower())

    for sk in by_sw:
        by_sw[sk].sort(
            key=lambda c: (
                c.get("unit_name") or "",
                c.get("customer_name") or "",
                c.get("id") or 0,
            )
        )

    groups: list[SidebarGroup] = []
    if week_customers:
        groups.append(
            SidebarGroup(
                id="week",
                title_name="本周建议联系",
                customers=week_customers,
                default_expanded=True,
            )
        )

    for sk in sorted(by_sw.keys(), key=sw_sort_key):
        bucket = by_sw[sk]
        groups.append(
            SidebarGroup(
                id=f"sw:{sk}",
                title_name=_sw_bucket_title_name(sk, bucket),
                customers=bucket,
                default_expanded=False,
            )
        )

    return groups


CUSTOMER_SIDEBAR_GROUP_BUILDER: Callable[
    [list[dict[str, Any]]], list[SidebarGroup]
] = build_sidebar_groups
