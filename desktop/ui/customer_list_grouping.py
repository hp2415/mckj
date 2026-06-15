"""
客户侧栏分组策略：与 MainWindow 解耦，后续可改为配置驱动或注入自定义 builder。

当前规则：
- 「今日建议联系」：命中今日任务（来自任务系统 /api/tasks/overview）的客户；
  由 today_task_order=list[(raw_customer_id, sales_wechat_id)] 注入，顺序与任务列表一致（priority_rank）。
  传 None 时跳过该分组（任务数据尚未就绪，客户列表照常秒出）。
- 「按销售微信号」：按 relation.sales_wechat_id 分桶；分组标题使用 sales_wechat_label（主数据表 nickname）。

分页与「加载更多」由 MainWindow 按组渲染，每组首屏条数见 MainWindow.CUSTOMER_GROUP_PAGE_SIZE。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable


CustomerKey = tuple[str, str]


def customer_task_key(customer: dict[str, Any]) -> CustomerKey:
    """与任务系统对齐的客户主键：(raw_customer_id, sales_wechat_id)。"""
    return (
        str(customer.get("id") or "").strip(),
        str(customer.get("sales_wechat_id") or "").strip(),
    )


@dataclass(frozen=True)
class SidebarGroup:
    """单个可折叠分组（title_name 不含人数，人数在渲染时按当前列表动态拼接）。"""

    id: str
    title_name: str
    customers: list[dict[str, Any]]
    default_expanded: bool
    children: list["SidebarGroup"] | None = None


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
    """销售组分组的展示名：优先备注，否则原始微信号（不截断，由 UI 负责展示）。"""
    if sk == "__none__" or not bucket:
        return "未关联销售号"
    label = (bucket[0].get("sales_wechat_label") or "").strip()
    if label:
        return label
    return sk


def _is_customer_classified(customer: dict[str, Any]) -> bool:
    """
    “已分析/未分析”判定：有任一画像产物即视为已分析。

    当前以三个信号判断：
    - has_ai_profile：列表瘦身后后端下发的画像存在标记
    - ai_profile：后端/桌面端保存的私域画像文本（旧版后端兼容/详情已加载时）
    - profile_tags：动态标签（list[dict]）；为空或缺失视为未分析
    """
    if customer.get("has_ai_profile"):
        return True

    ai_profile = (customer.get("ai_profile") or "").strip()
    if ai_profile:
        return True

    tags = customer.get("profile_tags")
    return isinstance(tags, list) and len(tags) > 0


def build_sidebar_groups(
    customers: list[dict[str, Any]],
    *,
    today: date | None = None,
    today_task_order: list[CustomerKey] | None = None,
) -> list[SidebarGroup]:
    """
    将扁平客户列表转为侧栏分组列表（顺序即展示顺序）。

    today_task_order：今日任务客户键的有序列表（与任务列表 priority_rank 一致）。
    - None：任务数据尚未加载，跳过「今日建议联系」分组（客户列表先行渲染）。
    - 非空列表：按该顺序置顶展示今日客户。
    """
    today = today or date.today()

    today_customers: list[dict[str, Any]] = []
    manual_customers: list[dict[str, Any]] = []

    order_keys: set[CustomerKey] = set()
    if today_task_order:
        order_keys = set(today_task_order)
        by_key: dict[CustomerKey, dict[str, Any]] = {}
        for c in customers:
            k = customer_task_key(c)
            if k in order_keys:
                by_key[k] = c
        for k in today_task_order:
            c = by_key.get(k)
            if c is not None:
                today_customers.append(c)

    for c in customers:
        # 手动导入跟进
        tags = c.get("profile_tags") or []
        if any(t.get("name") == "📌 手动导入跟进" for t in tags if isinstance(t, dict)):
            manual_customers.append(c)

    manual_customers.sort(
        key=lambda c: (
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
                str(c.get("id") or ""),
            )
        )

    groups: list[SidebarGroup] = []
    if today_customers:
        groups.append(
            SidebarGroup(
                id="today",
                title_name="今日建议联系",
                customers=today_customers,
                default_expanded=True,
            )
        )

    if manual_customers:
        groups.append(
            SidebarGroup(
                id="manual_import",
                title_name="📌 手动导入跟进",
                customers=manual_customers,
                default_expanded=True,
            )
        )

    for sk in sorted(by_sw.keys(), key=sw_sort_key):
        bucket = by_sw[sk]
        analyzed = [c for c in bucket if _is_customer_classified(c)]
        unanalyzed = [c for c in bucket if not _is_customer_classified(c)]

        # 逻辑层级：销售号 →（未分析/已分析）子组
        # 展示层级缩进由 UI 控制（可设置 indentation=0 实现“有层级但不缩进”）
        children: list[SidebarGroup] = []
        if unanalyzed:
            children.append(
                SidebarGroup(
                    id=f"sw:{sk}:unanalyzed",
                    title_name="未分析",
                    customers=unanalyzed,
                    default_expanded=False,
                )
            )
        if analyzed:
            children.append(
                SidebarGroup(
                    id=f"sw:{sk}:analyzed",
                    title_name="已分析",
                    customers=analyzed,
                    default_expanded=False,
                )
            )

        groups.append(
            SidebarGroup(
                id=f"sw:{sk}",
                title_name=_sw_bucket_title_name(sk, bucket),
                customers=[],
                default_expanded=False,
                children=children,
            )
        )

    return groups


CUSTOMER_SIDEBAR_GROUP_BUILDER: Callable[
    [list[dict[str, Any]]], list[SidebarGroup]
] = build_sidebar_groups
