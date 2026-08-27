"""方案需求理解：口语约束解析，以小模型为主。

正则只提供无歧义数字的提示（人均300、10人份），真正的口语消歧、品类/店铺/
种类数、修订意图都交给 proposal_intake 提示词。代码只做「数字是否出现在原文」
这类薄校验，避免错误字段覆盖 known。
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select

from ai.llm_client import LLMClient
from ai.llm_usage import LLMUsageContext
from ai.prompt_store import get_prompt_store
from ai.proposal.extractor import (
    apply_constraint_defaults,
    amount_in_text,
    expand_wan_if_needed,
    extract_constraints,
    infer_plan_type,
    normalize_gross_margin,
    PLAN_CANTEEN,
    PLAN_UNION,
)
from ai.proposal.policy import get_proposal_policy
from ai.raw_profiling import _extract_first_json_object
from core.logger import logger
from models import SystemConfig


INTAKE_SCENARIO_KEY = "proposal_intake"
MAX_KEYWORDS = 16
MAX_ITEM_KINDS = 16
MAX_PRIOR_LINES = 8
SHOP_ALIAS_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("通江东晖电子商务有限公司", ("通江东晖", "通江")),
    ("云上（北川）人工智能科技有限公司", ("云上北川", "云上（北川）", "北川")),
    ("广元阡陌农业发展有限公司", ("广元阡陌", "阡陌", "苍溪县", "苍溪")),
    ("拾味山林（广元）农业有限公司", ("拾味山林", "昭化区", "昭化")),
    ("宣汉谷满田园农业有限公司", ("宣汉优品", "宣汉谷满田园", "谷满田园", "宣汉县", "宣汉", "达州市", "达州")),
)

# 提示词缺失时的兜底；正式版本在 DB 场景 proposal_intake 里维护。
FALLBACK_INTAKE_SYSTEM = """你是方案需求解析器。销售口语随意，请解析成结构化参数。
字段：plan_type（union 工会方案 / canteen 食堂方案）、per_capita_budget（人均预算元，仅工会）、
total_budget（总预算元，仅食堂）、headcount（人数/份数，仅工会；没提且 known 也没有时填 1）、
discount_rate（折扣小数，仅明确要求折扣时填）、gross_margin（毛利率小数，如 25%=0.25；仅明确改毛利率时填），
include_keywords / exclude_keywords / shop_keywords（品类与店铺短词）、item_kinds（商品种类数）。
硬规则：
1. 有人均/每人/人份/元档/工会 → plan_type=union，填 per_capita_budget，total_budget=null。
2. 只有总预算（预算3万、总预算30000）或点名食堂、且没有人均 → plan_type=canteen，填 total_budget（3万=30000），per_capita_budget=null，headcount=null。
3. 「每人N件」是件数，不是预算也不是种类数；没提人均/预算/元时 per_capita_budget 必须回传 known。
4. 没提「种/样」时 item_kinds 必须 null。
5. 「不局限某店 / 都可以」时 shop_keywords 返回 []。
6. known 与 prior_lines 是上一版已确认的值和商品；没要求改的字段原样返回。
7. regex_hint 仅供参考，与 text 冲突时以 text 语义为准。
8. 工会方案未提人数/份数时：known 有则沿用，否则 headcount=1（默认一份）。食堂方案不要填 headcount。
9. 「毛利率改为25 / 改成25%」→ gross_margin=0.25；没提毛利率时 gross_margin 必须 null。
只输出 JSON：{"plan_type":"union或canteen或null","per_capita_budget":数字或null,"total_budget":数字或null,
"headcount":整数或null,"discount_rate":数字或null,"gross_margin":数字或null,
"include_keywords":[],"exclude_keywords":[],"shop_keywords":[],"item_kinds":整数或null}
"""


async def _intake_llm(db) -> LLMClient | None:
    """复用场景路由的小模型；未配置时回退默认对话模型。"""
    result = await db.execute(select(SystemConfig).where(SystemConfig.config_group == "ai"))
    config_map = {row.config_key: row.config_value for row in result.scalars().all()}
    model = (
        (config_map.get("proposal_intake_model") or "").strip()
        or (config_map.get("llm_router_model") or "").strip()
        or (config_map.get("llm_chat_model") or "").strip()
    )
    api_url = (config_map.get("llm_router_api_url") or "").strip() or (
        config_map.get("llm_api_url") or ""
    ).strip()
    api_key = (config_map.get("llm_router_api_key") or "").strip() or (
        config_map.get("llm_api_key") or ""
    ).strip()
    if not model or not api_url or not api_key:
        from ai.chat_models_catalog import resolve_chat_model_endpoint

        api_url, api_key = resolve_chat_model_endpoint(config_map, model)
    if not model or not api_url or not api_key:
        return None
    return LLMClient(api_url=api_url, api_key=api_key, model=model)


async def _intake_prompt() -> str:
    try:
        view = await get_prompt_store().get_published_version(INTAKE_SCENARIO_KEY)
    except Exception as error:
        logger.warning("方案需求解析提示词读取失败，使用内置兜底: {}", error)
        return FALLBACK_INTAKE_SYSTEM
    if view is None or not (view.template.system or "").strip():
        return FALLBACK_INTAKE_SYSTEM
    return view.template.system


def _positive_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def keyword_list(value) -> list[str]:
    """把模型返回的品类/店铺词洗成短词列表。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    words: list[str] = []
    for item in value:
        word = str(item or "").strip().strip("、，,。")
        if word and len(word) <= 12 and word not in words:
            words.append(word)
    return words[:MAX_KEYWORDS]


def _normalize_shop_text(value: str) -> str:
    text = (value or "").strip().lower()
    for token in (" ", "\u3000", "（", "）", "(", ")", "有限公司"):
        text = text.replace(token, "")
    return text


def _explicit_shop_keywords(text: str) -> list[str] | None:
    """只兜底明显指向单一店铺的别名；像「广元店铺」这类泛词仍交给模型。"""
    normalized = _normalize_shop_text(text)
    if not normalized:
        return None
    matched: list[str] = []
    for canonical, aliases in SHOP_ALIAS_GROUPS:
        tokens = (_normalize_shop_text(canonical),) + tuple(
            _normalize_shop_text(alias) for alias in aliases
        )
        if any(token and token in normalized for token in tokens):
            matched.append(canonical)
    unique = list(dict.fromkeys(matched))
    return unique if len(unique) == 1 else None


def number_in_text(number, text: str) -> bool:
    """薄校验：模型改写的数字必须在销售原话里出现过。"""
    try:
        value = float(number)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    token = str(int(value)) if float(value).is_integer() else str(value)
    return bool(re.search(rf"(?<!\d){re.escape(token)}(?!\d)", text or ""))


def accept_numeric_override(
    new_value,
    *,
    prior_value,
    text: str,
    prior_lines: list[dict] | None = None,
    field: str = "",
) -> float | None:
    """接受模型给出的数值；有 prior 且数值变化时，要求该数字出现在原文。

    field=budget 时额外挡住「把 prior_lines 里的每人件数误读成预算」：
    例如上一版大米每人 3 件、预算 300，模型却输出 3。
    """
    fresh = _positive_number(new_value)
    if fresh is None:
        return None
    prior = _positive_number(prior_value)
    if prior is not None and abs(fresh - prior) < 1e-9:
        return prior
    if not number_in_text(fresh, text):
        if field == "total_budget" and amount_in_text(fresh, text):
            return expand_wan_if_needed(fresh, text)
        return None
    if field == "total_budget":
        return expand_wan_if_needed(fresh, text)
    if field == "budget" and prior is not None:
        qtys = {
            int(line.get("qty_per_person") or 1)
            for line in prior_lines or []
        }
        if int(round(fresh)) in qtys and abs(fresh - prior) > 0.5:
            return None
    return fresh


def _compact_prior_lines(prior_lines: list[dict] | None) -> list[dict]:
    rows: list[dict] = []
    for line in prior_lines or []:
        name = str(line.get("name") or line.get("product_name") or "").strip()
        if not name:
            continue
        try:
            qty = int(line.get("qty") or line.get("qty_per_person") or 1)
        except (TypeError, ValueError):
            qty = 1
        rows.append({"name": name[:80], "qty_per_person": max(1, qty), "qty": max(1, qty)})
        if len(rows) >= MAX_PRIOR_LINES:
            break
    return rows


def _seed_from_prior(prior: dict, *, default_margin: float) -> dict:
    constraints: dict = {
        "gross_margin": float(prior.get("gross_margin") or default_margin),
        "margin_source": prior.get("margin_source") or "default",
        "discount_source": prior.get("discount_source") or "default",
        "shipping": prior.get("shipping") or "包邮",
    }
    if prior.get("discount_source") == "dialog" and prior.get("discount_rate") is not None:
        constraints["discount_rate"] = float(prior["discount_rate"])
        constraints["discount_source"] = "dialog"
    for key in (
        "plan_type",
        "per_capita_budget",
        "total_budget",
        "headcount",
        "include_keywords",
        "exclude_keywords",
        "shop_keywords",
        "item_kinds",
        "chat_model",
        "feedback_history",
    ):
        if prior.get(key) is not None:
            constraints[key] = prior[key]
    return constraints


def _apply_dialog_margin(constraints: dict, *, margin: float | None, source: str) -> None:
    """对话明确改毛利率时写入，并退出折扣覆盖，改回成本价×毛利计价。"""
    if margin is None or source != "dialog":
        return
    constraints["gross_margin"] = margin
    constraints["margin_source"] = "dialog"
    constraints["discount_source"] = "default"
    constraints.pop("discount_rate", None)


async def understand_constraints(
    db,
    query: str,
    *,
    prior: dict | None = None,
    prior_lines: list[dict] | None = None,
    user_id: int | None = None,
) -> dict:
    """以小模型解析为主；正则只作提示与无模型时的兜底。"""
    prior = prior or {}
    policy = await get_proposal_policy()
    text = (query or "").strip()
    constraints = _seed_from_prior(prior, default_margin=policy.default_gross_margin)
    constraints["request_text"] = text
    constraints.setdefault("gross_margin", policy.default_gross_margin)

    regex_hint = extract_constraints(text)
    llm = await _intake_llm(db)
    if llm is None:
        logger.warning("方案需求解析未配置可用小模型，仅使用正则结果")
        for key in ("plan_type", "per_capita_budget", "total_budget", "headcount"):
            if constraints.get(key) is None and regex_hint.get(key):
                constraints[key] = regex_hint[key]
        if regex_hint.get("discount_source") == "dialog":
            constraints["discount_rate"] = regex_hint["discount_rate"]
            constraints["discount_source"] = "dialog"
        _apply_dialog_margin(
            constraints,
            margin=regex_hint.get("gross_margin"),
            source=str(regex_hint.get("margin_source") or ""),
        )
        explicit_shop = _explicit_shop_keywords(text)
        if explicit_shop:
            constraints["shop_keywords"] = explicit_shop
        constraints["intake_source"] = "regex"
        return apply_constraint_defaults(constraints)

    compact_lines = _compact_prior_lines(prior_lines)
    payload = {
        "text": text,
        "known": {
            "plan_type": constraints.get("plan_type"),
            "per_capita_budget": constraints.get("per_capita_budget"),
            "total_budget": constraints.get("total_budget"),
            "headcount": constraints.get("headcount"),
            "discount_rate": (
                constraints.get("discount_rate")
                if constraints.get("discount_source") == "dialog"
                else None
            ),
            "gross_margin": (
                constraints.get("gross_margin")
                if constraints.get("margin_source") == "dialog"
                else None
            ),
            "include_keywords": constraints.get("include_keywords") or [],
            "exclude_keywords": constraints.get("exclude_keywords") or [],
            "shop_keywords": constraints.get("shop_keywords") or [],
            "item_kinds": constraints.get("item_kinds"),
        },
        "prior_lines": compact_lines,
        "regex_hint": {
            "plan_type": regex_hint.get("plan_type"),
            "per_capita_budget": regex_hint.get("per_capita_budget"),
            "total_budget": regex_hint.get("total_budget"),
            "headcount": regex_hint.get("headcount"),
            "gross_margin": regex_hint.get("gross_margin"),
        },
    }
    try:
        response = await llm.chat(
            [
                {"role": "system", "content": await _intake_prompt()},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.0,
            max_tokens=320,
            usage=LLMUsageContext(scenario_key=INTAKE_SCENARIO_KEY, user_id=user_id),
        )
    except Exception as error:
        logger.warning("方案需求解析小模型调用失败: {}", error)
        for key in ("plan_type", "per_capita_budget", "total_budget", "headcount"):
            if constraints.get(key) is None and regex_hint.get(key):
                constraints[key] = regex_hint[key]
        if regex_hint.get("discount_source") == "dialog":
            constraints["discount_rate"] = regex_hint["discount_rate"]
            constraints["discount_source"] = "dialog"
        _apply_dialog_margin(
            constraints,
            margin=regex_hint.get("gross_margin"),
            source=str(regex_hint.get("margin_source") or ""),
        )
        explicit_shop = _explicit_shop_keywords(text)
        if explicit_shop:
            constraints["shop_keywords"] = explicit_shop
        constraints["intake_source"] = "regex_fallback"
        return apply_constraint_defaults(constraints)

    content = (response.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    data = _extract_first_json_object(content) or {}

    budget = accept_numeric_override(
        data.get("per_capita_budget"),
        prior_value=constraints.get("per_capita_budget"),
        text=text,
        prior_lines=compact_lines,
        field="budget",
    )
    if budget is not None:
        constraints["per_capita_budget"] = round(budget, 2)
    elif constraints.get("per_capita_budget") is None and regex_hint.get("per_capita_budget"):
        constraints["per_capita_budget"] = regex_hint["per_capita_budget"]

    total = accept_numeric_override(
        data.get("total_budget"),
        prior_value=constraints.get("total_budget"),
        text=text,
        prior_lines=compact_lines,
        field="total_budget",
    )
    if total is not None:
        constraints["total_budget"] = round(float(total), 2)
    elif constraints.get("total_budget") is None and regex_hint.get("total_budget"):
        constraints["total_budget"] = regex_hint["total_budget"]

    raw_type = str(data.get("plan_type") or "").strip().lower()
    if raw_type in (PLAN_UNION, PLAN_CANTEEN):
        constraints["plan_type"] = raw_type
    inferred = infer_plan_type(
        text,
        per_capita_budget=constraints.get("per_capita_budget"),
        total_budget=constraints.get("total_budget"),
        prior_type=constraints.get("plan_type") or regex_hint.get("plan_type"),
    )
    if inferred:
        constraints["plan_type"] = inferred

    headcount = accept_numeric_override(
        data.get("headcount"),
        prior_value=constraints.get("headcount"),
        text=text,
    )
    if headcount is not None:
        constraints["headcount"] = int(headcount)
    elif constraints.get("headcount") is None and regex_hint.get("headcount"):
        constraints["headcount"] = regex_hint["headcount"]

    rate = _positive_number(data.get("discount_rate"))
    if rate and 0.1 < rate <= 1 and (
        regex_hint.get("discount_source") == "dialog" or "折" in text or "折扣" in text
    ):
        constraints["discount_rate"] = round(rate, 4)
        constraints["discount_source"] = "dialog"
    elif regex_hint.get("discount_source") == "dialog":
        constraints["discount_rate"] = regex_hint["discount_rate"]
        constraints["discount_source"] = "dialog"

    # 毛利率：正则优先（口语「改为25」最稳）；模型仅在原文明确提毛利时采用
    if regex_hint.get("margin_source") == "dialog" and regex_hint.get("gross_margin") is not None:
        _apply_dialog_margin(
            constraints,
            margin=float(regex_hint["gross_margin"]),
            source="dialog",
        )
    else:
        model_margin = normalize_gross_margin(data.get("gross_margin"))
        if model_margin is not None and ("毛利" in text):
            _apply_dialog_margin(constraints, margin=model_margin, source="dialog")

    # 品类/店铺：模型返回了该键就整表替换（含空数组=取消限制）
    for key in ("include_keywords", "exclude_keywords", "shop_keywords"):
        if key not in data:
            continue
        constraints[key] = keyword_list(data.get(key))

    explicit_shop = _explicit_shop_keywords(text)
    if explicit_shop:
        constraints["shop_keywords"] = explicit_shop

    kinds = _positive_number(data.get("item_kinds"))
    # 种类数完全信任模型（提示词已规定没提种/样必须 null）；只做范围裁剪
    if kinds is not None and 1 <= int(kinds) <= MAX_ITEM_KINDS:
        constraints["item_kinds"] = int(kinds)

    constraints["intake_source"] = "llm"
    constraints = apply_constraint_defaults(constraints)
    logger.info(
        "方案需求解析 model={} text={!r} → 类型={} 人均={} 总预算={} 人数={} 毛利={} 品类={} 店铺={} 种类={}",
        llm.model,
        text[:60],
        constraints.get("plan_type"),
        constraints.get("per_capita_budget"),
        constraints.get("total_budget"),
        constraints.get("headcount"),
        constraints.get("gross_margin"),
        constraints.get("include_keywords"),
        constraints.get("shop_keywords"),
        constraints.get("item_kinds"),
    )
    return constraints
