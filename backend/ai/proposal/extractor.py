"""方案约束的轻量工具：折扣解析、约束裁剪，以及无歧义数字的正则提示。

口语消歧、品类/店铺/种类数一律交给 proposal_intake 小模型；
这里的预算/人数正则只给模型作 regex_hint，或在无可用模型时兜底。
"""
from __future__ import annotations

import re
from decimal import Decimal


# 只保留几乎无歧义的写法；「每人3件」之类不在此匹配，交给模型。
_BUDGET_PATTERNS = (
    re.compile(r"人均\s*(?:预算|价格|价)?\s*[¥￥]?\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(?:每人|单份)\s*(?:预算)?\s*[¥￥]\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(?:每人|单份)\s*(?:预算\s*)?(\d+(?:\.\d+)?)\s*元"),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:元)?\s*(?:档|/人)"),
)
# 总预算：预算3万 / 总预算30000。不与「人均300」抢数字。
_CN_WAN_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_CN_WAN_AMOUNT = re.compile(r"([一二两三四五六七八九十])\s*万")
_WAN_AMOUNT = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*[万wW]\s*(\d(?:\.\d+)?)?"
)
_TOTAL_BUDGET_PATTERNS = (
    re.compile(r"(?:总)?预算\s*[是为:]?\s*[¥￥]?\s*(\d+(?:\.\d+)?)\s*[万wW]"),
    re.compile(r"(\d+(?:\.\d+)?)\s*[万wW]\s*(?:的)?(?:预算|方案|食堂)"),
    re.compile(r"(?:总)?预算\s*[是为:]?\s*[¥￥]?\s*(\d{3,}(?:\.\d+)?)"),
)
_HEADCOUNT_PATTERNS = (
    re.compile(r"(\d+)\s*(?:人份|人|份)(?!\s*(?:钱|价|件))"),
    re.compile(r"(?:人数|份数)\s*[:：]?\s*(\d+)"),
)
UNION_CUES = ("人均", "每人", "单份", "元档", "人份", "工会")
CANTEEN_CUES = ("食堂",)
PLAN_UNION = "union"
PLAN_CANTEEN = "canteen"
_ARABIC_DISCOUNT = re.compile(r"(?<!\d)(\d{1,2}(?:\.\d+)?)\s*折")
_DECIMAL_DISCOUNT = re.compile(r"(?:折扣|优惠比例)\s*[:：]?\s*(0(?:\.\d+))")
_CHINESE_DISCOUNT = re.compile(r"([一二三四五六七八九])([一二三四五六七八九])折")
_CHINESE_SINGLE_DISCOUNT = re.compile(r"([一二三四五六七八九])折")
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
# 「毛利率改为25」「改成25%毛利」「毛利率 0.25」
_GROSS_MARGIN_PATTERNS = (
    re.compile(
        r"毛利率?\s*(?:改?为|调到|调成|改成|设为|设置?为|是|:|：)?\s*"
        r"(0?\.\d+|\d+(?:\.\d+)?)\s*%?"
    ),
    re.compile(
        r"(?:改?为|调到|调成|改成|设为)\s*(0?\.\d+|\d+(?:\.\d+)?)\s*%?\s*的?\s*毛利率?"
    ),
)


def _first_float(patterns: tuple[re.Pattern, ...], text: str) -> float | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except (TypeError, ValueError):
                continue
    return None


def parse_discount_rate(text: str, default: float | None = None) -> tuple[float | None, str]:
    """解析对话中的折扣；未提折扣时返回 (default, \"default\")，默认定价走毛利率。"""
    raw = text or ""
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

    return default, "default"


def normalize_gross_margin(value: float | int | str | None) -> float | None:
    """把 25 / 25% / 0.25 统一成 0~0.9 的小数毛利率。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        text = str(value).strip().replace("%", "")
        try:
            number = float(text)
        except (TypeError, ValueError):
            return None
    if number > 1:
        # 25 → 0.25；超过 90 视为无效，避免把预算数字误当毛利
        if number > 90:
            return None
        number = number / 100.0
    if 0.01 <= number <= 0.9:
        return round(number, 4)
    return None


def parse_gross_margin(text: str, default: float | None = None) -> tuple[float | None, str]:
    """解析对话中的毛利率；未提时返回 (default, \"default\")。"""
    raw = text or ""
    for pattern in _GROSS_MARGIN_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        margin = normalize_gross_margin(match.group(1))
        if margin is not None:
            return margin, "dialog"
    return default, "default"


def parse_wan_amount(text: str) -> float | None:
    """把 3万 / 3.5万 / 3万5 / 3w / 两万 读成元。"""
    raw = text or ""
    match = _WAN_AMOUNT.search(raw)
    if match:
        try:
            major = float(match.group(1))
        except (TypeError, ValueError):
            major = 0
        if major > 0:
            value = major * 10000
            minor = match.group(2)
            if minor:
                try:
                    extra = float(minor)
                except (TypeError, ValueError):
                    extra = 0
                # 3万5 → 35000；3万50 少见，按字面加
                value += extra * 1000 if extra < 10 else extra
            return round(value, 2)
    cn = _CN_WAN_AMOUNT.search(raw)
    if cn:
        major = _CN_WAN_DIGITS.get(cn.group(1))
        if major:
            return float(major * 10000)
    return None


def expand_wan_if_needed(value: float, text: str) -> float:
    """模型把「3万」写成 3 时补成 30000。"""
    if value >= 1000:
        return value
    token = str(int(value)) if float(value).is_integer() else str(value)
    if re.search(rf"(?<!\d){re.escape(token)}\s*[万wW]", text or ""):
        return round(value * 10000, 2)
    return value


def amount_in_text(number, text: str) -> bool:
    """数字是否在原话里出现过，含 3万=30000 这种写法。"""
    try:
        value = float(number)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    raw = text or ""
    token = str(int(value)) if float(value).is_integer() else str(value)
    if re.search(rf"(?<!\d){re.escape(token)}(?!\d)", raw):
        return True
    if value >= 1000:
        wan = value / 10000.0
        wan_token = (
            str(int(wan)) if abs(wan - round(wan)) < 1e-9 else f"{wan:.2f}".rstrip("0").rstrip(".")
        )
        if re.search(rf"(?<!\d){re.escape(wan_token)}\s*[万wW]", raw):
            return True
        for word, digit in _CN_WAN_DIGITS.items():
            if abs(value - digit * 10000) < 1e-6 and f"{word}万" in raw:
                return True
    return False


def has_union_cue(text: str) -> bool:
    return any(cue in (text or "") for cue in UNION_CUES)


def has_canteen_cue(text: str) -> bool:
    return any(cue in (text or "") for cue in CANTEEN_CUES)


def infer_plan_type(
    text: str,
    *,
    per_capita_budget: float | None = None,
    total_budget: float | None = None,
    prior_type: str | None = None,
) -> str | None:
    """有人均/人份/工会 → 工会方案；只有总预算或点名食堂 → 食堂方案。"""
    unionish = has_union_cue(text)
    canteenish = has_canteen_cue(text)
    if canteenish and not unionish:
        return PLAN_CANTEEN
    if unionish and not canteenish:
        return PLAN_UNION
    if per_capita_budget and not total_budget:
        return PLAN_UNION
    if total_budget and not per_capita_budget:
        return PLAN_CANTEEN
    if per_capita_budget and total_budget:
        return PLAN_UNION if unionish else PLAN_CANTEEN
    if prior_type in (PLAN_UNION, PLAN_CANTEEN):
        return prior_type
    if per_capita_budget:
        return PLAN_UNION
    if total_budget:
        return PLAN_CANTEEN
    return None


def is_canteen_plan(constraints: dict | None) -> bool:
    data = constraints or {}
    return str(data.get("plan_type") or "") == PLAN_CANTEEN


def target_budget(constraints: dict | None) -> float:
    data = constraints or {}
    if is_canteen_plan(data):
        try:
            return float(data.get("total_budget") or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(data.get("per_capita_budget") or 0)
    except (TypeError, ValueError):
        return 0.0


def extract_constraints(query: str) -> dict:
    """无歧义数字提示；不负责口语消歧。未提折扣/毛利率时不覆盖默认策略。"""
    text = (query or "").strip()
    budget = _first_float(_BUDGET_PATTERNS, text)
    headcount = _first_float(_HEADCOUNT_PATTERNS, text)
    discount_rate, discount_source = parse_discount_rate(text, default=None)
    gross_margin, margin_source = parse_gross_margin(text, default=None)

    total_budget = None
    # 有人均/元档等工会口径时，不要把「预算」误读成食堂总预算
    if not has_union_cue(text) or has_canteen_cue(text):
        wan = parse_wan_amount(text)
        if wan:
            total_budget = wan
        else:
            raw_total = _first_float(_TOTAL_BUDGET_PATTERNS, text)
            if raw_total and raw_total > 0:
                total_budget = expand_wan_if_needed(raw_total, text)

    per_capita = round(budget, 2) if budget and budget > 0 else None
    if has_canteen_cue(text) and not has_union_cue(text):
        per_capita = None
        if total_budget is None and budget and budget > 0:
            total_budget = round(budget, 2)

    plan_type = infer_plan_type(
        text,
        per_capita_budget=per_capita,
        total_budget=total_budget,
    )

    out: dict = {
        "per_capita_budget": per_capita if plan_type != PLAN_CANTEEN else None,
        "total_budget": round(float(total_budget), 2) if total_budget else None,
        "headcount": int(headcount) if headcount and headcount > 0 else None,
        "plan_type": plan_type,
        "discount_source": discount_source,
        "shipping": "包邮",
        "request_text": text,
    }
    if discount_rate is not None and discount_source == "dialog":
        out["discount_rate"] = round(discount_rate, 4)
    if gross_margin is not None and margin_source == "dialog":
        out["gross_margin"] = gross_margin
        out["margin_source"] = "dialog"
    return out


# 只有这些键会在多轮修订之间传递；其余（画像摘要、上一版明细等）每轮重新装配，
# 否则 constraint_json 会把上一版整体嵌套进 request_text，越改越大也越慢。
CONSTRAINT_KEYS = (
    "plan_type",
    "per_capita_budget",
    "total_budget",
    "headcount",
    "gross_margin",
    "margin_source",
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
MAX_KEYWORDS = 16


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
    """补齐可缺省字段。工会方案未提人数时按 1 份；食堂方案不对人数做默认。"""
    out = dict(constraints or {})
    plan_type = infer_plan_type(
        str(out.get("request_text") or ""),
        per_capita_budget=out.get("per_capita_budget"),
        total_budget=out.get("total_budget"),
        prior_type=out.get("plan_type"),
    )
    if plan_type:
        out["plan_type"] = plan_type
    if out.get("plan_type") == PLAN_CANTEEN:
        out.pop("per_capita_budget", None)
        out.pop("headcount", None)
        return out
    if out.get("plan_type") == PLAN_UNION:
        out.pop("total_budget", None)
    try:
        headcount = int(out.get("headcount") or 0)
    except (TypeError, ValueError):
        headcount = 0
    if headcount <= 0:
        out["headcount"] = DEFAULT_HEADCOUNT
    return out


def missing_required_constraints(constraints: dict) -> list[str]:
    """工会方案必填人均预算；食堂方案必填总预算；二者都没有时提示补预算。"""
    missing: list[str] = []
    if is_canteen_plan(constraints) or (
        not constraints.get("per_capita_budget") and constraints.get("total_budget")
    ):
        if not constraints.get("total_budget"):
            missing.append("总预算")
        return missing
    if not constraints.get("per_capita_budget"):
        missing.append("人均预算或总预算")
    return missing
