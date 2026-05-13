"""场景路由器受控关键词词表：管理后台多选 + 运行时展开为 keywords。"""
from __future__ import annotations

from typing import Iterable

# (ref_id, 展示名, 匹配子串列表)
ROUTER_KEYWORD_CATALOG: list[tuple[str, str, tuple[str, ...]]] = [
    ("greeting", "问候/打招呼", ("问候", "打招呼", "开场", "寒暄", "早安", "晚安")),
    ("opening", "破冰/首次沟通", ("破冰", "首次", "新好友", "加微信")),
    ("quote", "报价/推品", ("报价", "推品", "推荐", "多少钱", "价格", "型号", "礼盒", "预算", "方案", "出个方案", "出方案")),
    ("objection", "异议处理", ("异议", "太贵", "考虑一下", "再看看", "犹豫")),
    ("close", "逼单/促成交", ("逼单", "促成交", "定下来", "就这款", "名额")),
    ("order_guide", "下单引导/协助下单", ("下单", "操作", "链接", "账号", "平台流程", "协助下单")),
    ("followup", "跟进/回访", ("跟进", "回访", "联系一下", "进度")),
    ("wake", "唤醒/沉默客户", ("唤醒", "好久没联系", "沉睡", "30天", "90天")),
    ("referral", "转介绍", ("转介绍", "推荐朋友", "介绍客户")),
    ("holiday", "节日关怀", ("节日", "中秋", "春节", "教师节", "三八", "建党节", "生日")),
    ("after_sale", "售后/投诉", ("售后", "退货", "投诉", "质量问题")),
    ("policy", "规则/政策/内部问答", ("规则", "政策", "流程", "话术", "策略", "怎么写")),
    ("profile", "客户资料/画像", ("画像", "备注", "标签", "预算", "采购月份")),
]


# 未在后台勾选 keyword_refs 时，仍按场景补全的默认词表引用
SCENARIO_DEFAULT_KEYWORD_REFS: dict[str, tuple[str, ...]] = {
    "product_recommend": ("quote",),
}


def scenario_default_keyword_refs(scenario_key: str) -> tuple[str, ...]:
    return SCENARIO_DEFAULT_KEYWORD_REFS.get(str(scenario_key or "").strip(), ())


def router_keyword_choices() -> list[tuple[str, str]]:
    return [(ref, label) for ref, label, _ in ROUTER_KEYWORD_CATALOG]


def expand_keyword_refs(refs: Iterable[str]) -> list[str]:
    ref_set = {str(r).strip() for r in (refs or []) if str(r).strip()}
    out: list[str] = []
    seen: set[str] = set()
    for ref, _label, needles in ROUTER_KEYWORD_CATALOG:
        if ref not in ref_set:
            continue
        for needle in needles:
            n = needle.strip()
            if not n or n in seen:
                continue
            seen.add(n)
            out.append(n)
    return out


def merge_router_keywords(
    refs: Iterable[str],
    extra_lines: Iterable[str],
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for kw in expand_keyword_refs(refs):
        if kw not in seen:
            seen.add(kw)
            merged.append(kw)
    for raw in extra_lines or []:
        kw = str(raw).strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        merged.append(kw)
    return merged
