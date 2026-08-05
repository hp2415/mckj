"""方案选品策略参数：从提示词场景 proposal_compose 的 params_json 读取。

影响方案效果的数字（默认折扣、预算容差、种类区间、件数上限）应在管理后台
「提示词 → 方案选品编排 → 参数」里改，不要写死在代码常量里。
本模块只提供读取与校验；缺失时用 FALLBACK 兜底，避免管线挂掉。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ai.prompt_store import get_prompt_store
from core.logger import logger

COMPOSE_SCENARIO_KEY = "proposal_compose"


@dataclass(frozen=True)
class ProposalPolicy:
    # 销售没提折扣时的默认折扣率（0.88 = 八八折）
    default_discount_rate: float = 0.88
    # 人均优惠价相对预算允许的相对偏差（0.08 = ±8%）
    budget_tolerance: float = 0.08
    # 未点名种类数时的默认区间
    item_kinds_min: int = 3
    item_kinds_max: int = 6
    # 单品每人最多件数；方案最多商品行数
    max_qty_per_person: int = 6
    max_lines: int = 8

    def as_prompt_vars(self) -> dict[str, str]:
        """注入到选品 system 模板的 {{var}}。"""
        pct = int(round(self.budget_tolerance * 100))
        discount_zhe = self.default_discount_rate * 10
        return {
            "budget_tolerance": f"{self.budget_tolerance:g}",
            "budget_tolerance_pct": str(pct),
            "item_kinds_min": str(self.item_kinds_min),
            "item_kinds_max": str(self.item_kinds_max),
            "max_qty_per_person": str(self.max_qty_per_person),
            "max_lines": str(self.max_lines),
            "default_discount_rate": f"{self.default_discount_rate:g}",
            "default_discount_zhe": f"{discount_zhe:g}",
        }


FALLBACK_POLICY = ProposalPolicy()

# params_json 里这些键是业务策略；其余（temperature 等）仍归 LLM 调用参数。
POLICY_KEYS = (
    "default_discount_rate",
    "budget_tolerance",
    "item_kinds_min",
    "item_kinds_max",
    "max_qty_per_person",
    "max_lines",
)


def policy_from_params(raw: dict | None) -> ProposalPolicy:
    data = raw if isinstance(raw, dict) else {}
    # 兼容嵌套 {"policy": {...}} 与扁平写在 params 顶层两种写法
    nested = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    merged: dict = {}
    for key in POLICY_KEYS:
        if nested.get(key) is not None:
            merged[key] = nested[key]
        elif data.get(key) is not None:
            merged[key] = data[key]
    base = asdict(FALLBACK_POLICY)

    def _float(key: str, low: float, high: float) -> float:
        try:
            value = float(merged[key] if merged.get(key) is not None else base[key])
        except (TypeError, ValueError):
            return float(base[key])
        return value if low <= value <= high else float(base[key])

    def _int(key: str, low: int, high: int) -> int:
        try:
            value = int(merged[key] if merged.get(key) is not None else base[key])
        except (TypeError, ValueError):
            return int(base[key])
        return value if low <= value <= high else int(base[key])

    kinds_min = _int("item_kinds_min", 1, 12)
    kinds_max = _int("item_kinds_max", 1, 12)
    if kinds_max < kinds_min:
        kinds_min, kinds_max = kinds_max, kinds_min
    return ProposalPolicy(
        default_discount_rate=_float("default_discount_rate", 0.1, 1.0),
        budget_tolerance=_float("budget_tolerance", 0.01, 0.5),
        item_kinds_min=kinds_min,
        item_kinds_max=kinds_max,
        max_qty_per_person=_int("max_qty_per_person", 1, 20),
        max_lines=_int("max_lines", 1, 20),
    )


async def get_proposal_policy() -> ProposalPolicy:
    try:
        view = await get_prompt_store().get_published_version(COMPOSE_SCENARIO_KEY)
    except Exception as error:
        logger.warning("方案策略参数读取失败，使用内置兜底: {}", error)
        return FALLBACK_POLICY
    if view is None:
        return FALLBACK_POLICY
    return policy_from_params(getattr(view, "params_raw", None) or {})
