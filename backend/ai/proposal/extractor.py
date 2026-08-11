"""方案约束的轻量工具：折扣解析、约束裁剪，以及无歧义数字的正则提示。

口语消歧、品类/店铺/种类数一律交给 proposal_intake 小模型；
这里的预算/人数正则只给模型作 regex_hint，或在无可用模型时兜底。
"""
from __future__ import annotations

import re
from decimal import Decimal

from ai.proposal.policy import FALLBACK_POLICY


# 只保留几乎无歧义的写法；「每人3件」之类不在此匹配，交给模型。
_BUDGET_PATTERNS = (
    re.compile(r"人均\s*(?:预算|价格|价)?\s*[¥￥]?\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(?:每人|单份)\s*(?:预算)?\s*[¥￥]\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(?:每人|单份)\s*(?:预算\s*)?(\d+(?:\.\d+)?)\s*元"),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:元)?\s*(?:档|/人)"),
)
_HEADCOUNT_PATTERNS = (
    re.compile(r"(\d+)\s*(?:人份|人|份)(?!\s*(?:钱|价|件))"),
    re.compile(r"(?:人数|份数)\s*[:：]?\s*(\d+)"),
)
_ARABIC_DISCOUNT = re.compile(r"(?<!\d)(\d{1,2}(?:\.\d+)?)\s*折")
_DECIMAL_DISCOUNT = re.compile(r"(?:折扣|优惠比例)\s*[:：]?\s*(0(?:\.\d+))")
_CHINESE_DISCOUNT = re.compile(r"([一二三四五六七八九])([一二三四五六七八九])折")
_CHINESE_SINGLE_DISCOUNT = re.compile(r"([一二三四五六七八九])折")
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _first_float(patterns: tuple[re.Pattern, ...], text: str) -> float | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except (TypeError, ValueError):
                continue
    return None


def parse_discount_rate(text: str, default: float | None = None) -> tuple[float, str]:
    raw = text or ""
    fallback = (
        float(default)
        if default is not None
        else FALLBACK_POLICY.default_discount_rate
    )
    match = _DECIMAL_DISCOUNT.search(raw)
    if match:
        value = float(match.group(1))
        if 0 < value <= 1:
            return value, "dialog"

    match = _ARABIC_DISCOUNT.search(raw)
    if match:
        number = Decimal(match.group(1))
        value = float(number / (Decimal(100) if number > 10 else Decimal(10)))
        if 0 < value <= 1:
            return value, "dialog"

    match = _CHINESE_DISCOUNT.search(raw)
    if match:
        value = (_CN_DIGITS[match.group(1)] * 10 + _CN_DIGITS[match.group(2)]) / 100
        return value, "dialog"

    match = _CHINESE_SINGLE_DISCOUNT.search(raw)
    if match:
        return _CN_DIGITS[match.group(1)] / 10, "dialog"

    return fallback, "default"


def extract_constraints(query: str, *, default_discount_rate: float | None = None) -> dict:
    """无歧义数字提示；不负责口语消歧。"""
    text = (query or "").strip()
    budget = _first_float(_BUDGET_PATTERNS, text)
    headcount = _first_float(_HEADCOUNT_PATTERNS, text)
    discount_rate, discount_source = parse_discount_rate(
        text, default=default_discount_rate
    )

    return {
        "per_capita_budget": round(budget, 2) if budget and budget > 0 else None,
        "headcount": int(headcount) if headcount and headcount > 0 else None,
        "discount_rate": round(discount_rate, 4),
        "discount_source": discount_source,
        "shipping": "包邮",
        "request_text": text,
    }


# 只有这些键会在多轮修订之间传递；其余（画像摘要、上一版明细等）每轮重新装配，
# 否则 constraint_json 会把上一版整体嵌套进 request_text，越改越大也越慢。
CONSTRAINT_KEYS = (
    "per_capita_budget",
    "headcount",
    "discount_rate",
    "discount_source",
    "shipping",
    "request_text",
    "feedback_history",
    "source",
    "intake_source",
    "chat_model",
    "include_keywords",
    "exclude_keywords",
    "shop_keywords",
    "item_kinds",
)
KEYWORD_KEYS = ("include_keywords", "exclude_keywords", "shop_keywords")
MAX_FEEDBACK_HISTORY = 5
MAX_KEYWORDS = 6


def sanitize_constraints(data: dict | None) -> dict:
    """裁剪出可跨轮携带的小体积约束。"""
    raw = dict(data or {})
    out: dict = {}
    for key in CONSTRAINT_KEYS:
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        if key == "request_text":
            out[key] = str(value)[:500]
        elif key == "feedback_history":
            items = value if isinstance(value, list) else [value]
            out[key] = [str(item)[:200] for item in items if str(item or "").strip()][
                -MAX_FEEDBACK_HISTORY:
            ]
        elif key in KEYWORD_KEYS:
            items = value if isinstance(value, list) else [value]
            words = [str(item).strip()[:12] for item in items if str(item or "").strip()]
            if words:
                out[key] = words[:MAX_KEYWORDS]
            elif key == "shop_keywords" and isinstance(value, list):
                # 显式空列表表示「取消店铺限制」，必须保留，否则下一轮又锁回旧店
                out[key] = []
        else:
            out[key] = value
    return out


DEFAULT_HEADCOUNT = 1


def apply_constraint_defaults(constraints: dict) -> dict:
    """补齐可缺省字段：未提人数/份数时按 1 份生成。"""
    out = dict(constraints or {})
    try:
        headcount = int(out.get("headcount") or 0)
    except (TypeError, ValueError):
        headcount = 0
    if headcount <= 0:
        out["headcount"] = DEFAULT_HEADCOUNT
    return out


def missing_required_constraints(constraints: dict) -> list[str]:
    """仅人均预算必填；人数/份数缺省由 apply_constraint_defaults 补 1。"""
    missing: list[str] = []
    if not constraints.get("per_capita_budget"):
        missing.append("人均预算")
    return missing
