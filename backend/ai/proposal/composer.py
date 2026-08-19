from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import or_, select

from ai.chat_models_catalog import default_chat_model_id, resolve_chat_model_endpoint
from ai.llm_client import LLMClient
from ai.llm_usage import LLMUsageContext
from ai.prompt_models import PromptTemplate
from ai.prompt_renderer import render_system
from ai.prompt_store import get_prompt_store
from ai.proposal.policy import (
    COMPOSE_SCENARIO_KEY,
    FALLBACK_POLICY,
    ProposalPolicy,
    policy_from_params,
)
from ai.raw_profiling import _extract_first_json_object
from core.logger import logger
from models import Product, SystemConfig


# 以下为工程抽样参数，不影响报价规则本身；效果类参数见 proposal_compose.params_json
CANDIDATE_LIMIT = 180
PRICE_BANDS = 6
SHOP_CLUSTER_SHOPS = 4
SHOP_CLUSTER_PER_SHOP = 24
MAX_LLM_ROUNDS = 2
KEYWORD_QUOTA = 20
SHOP_FOCUS_SHARE = 0.75

# 提示词缺失时的兜底；正式规则在 DB 场景 proposal_compose 里维护，运营可直接改。
# 数字占位由 ProposalPolicy.as_prompt_vars 注入，避免效果参数写死在这里。
FALLBACK_COMPOSE_SYSTEM = """你是脱贫地区农副产品方案选品专家。只能使用候选清单中的商品。
「单份」指一个人/一份的组合，所有商品的「优惠单价 × 每人数量」之和必须落在人均预算 ±{{budget_tolerance_pct}}% 内，这是最重要的指标。
requirements 里的点名要求（品类、店铺、商品种类数）优先级最高；没有要求时一份 {{item_kinds_min}}-{{item_kinds_max}} 种、品类分散。
默认按成本价与毛利率 {{default_gross_margin_pct}}% 计价（优惠单价见候选 promo_price）；
无成本价的商品按平台价 {{fallback_discount_zhe}} 折兜底。对话明确要求折扣时改按平台价×折扣。
【参考修订】prior_lines 非空时把上一版当参考，由你评估保留或更换；预算变化时主动调规格/件数/换货对齐新预算，勿重复堆同款。
每条商品必须带 selling_point：面向客户的一句话卖点（约8-20字），突出品质/口感/产地/工艺等，禁止写店铺名、价格、折扣、毛利率。
只输出 JSON：{"per_capita_budget":数字,"headcount":整数,"items":[{"product_id":整数,"qty_per_person":整数,"selling_point":"一句话卖点"}],"rationale":"简短理由"}
"""


def money(value: Any) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class PricingContext:
    """方案计价：有成本价则 成本÷(1−毛利率)；无成本价则平台价×兜底折扣；对话明确折扣时全表按平台价×折扣。"""

    gross_margin: float
    fallback_discount_rate: float = 0.88
    discount_rate: float | None = None
    use_discount: bool = False

    def has_cost(self, product: Product) -> bool:
        try:
            return float(product.cost_price or 0) > 0
        except (TypeError, ValueError):
            return False

    def unit_price(self, product: Product) -> float:
        if self.use_discount and self.discount_rate is not None:
            return money(float(product.price) * float(self.discount_rate))
        if self.has_cost(product):
            margin = min(max(float(self.gross_margin), 0.0), 0.95)
            return money(float(product.cost_price) / (1.0 - margin))
        rate = min(max(float(self.fallback_discount_rate), 0.01), 1.0)
        return money(float(product.price or 0) * rate)


def _pricing_from_constraints(constraints: dict, policy: ProposalPolicy) -> PricingContext:
    margin = float(constraints.get("gross_margin") or policy.default_gross_margin)
    rate = constraints.get("discount_rate")
    use_discount = (
        str(constraints.get("discount_source") or "") == "dialog" and rate is not None
    )
    try:
        discount_rate = float(rate) if use_discount else None
    except (TypeError, ValueError):
        discount_rate = None
        use_discount = False
    fallback = float(
        constraints.get("fallback_discount_rate") or policy.fallback_discount_rate
    )
    return PricingContext(
        gross_margin=margin,
        fallback_discount_rate=fallback,
        discount_rate=discount_rate,
        use_discount=use_discount and discount_rate is not None,
    )


async def _ai_config_map(db) -> dict:
    result = await db.execute(select(SystemConfig).where(SystemConfig.config_group == "ai"))
    return {row.config_key: row.config_value for row in result.scalars().all()}


async def _config_value(db, key: str) -> str:
    result = await db.execute(select(SystemConfig).where(SystemConfig.config_key == key))
    row = result.scalars().first()
    return str(row.config_value or "") if row else ""


async def _compose_prompt(db) -> tuple[str, str | None, ProposalPolicy]:
    """返回（system 提示词，提示词版本里指定的模型，策略参数）。

    引用的提示词文档（如 proposal_playbook）一并注入；预算容差、默认毛利率、
    种类区间等从 params_json 读入并注入 {{var}}。
    """
    store = get_prompt_store()
    try:
        view = await store.get_published_version(COMPOSE_SCENARIO_KEY)
    except Exception as error:
        logger.warning("方案选品提示词读取失败，使用内置兜底: {}", error)
        view = None
    if view is None or not (view.template.system or "").strip():
        policy = FALLBACK_POLICY
        system = render_system(
            template=PromptTemplate(system=FALLBACK_COMPOSE_SYSTEM),
            ctx=policy.as_prompt_vars(),
            docs_map={},
            doc_refs=[],
        )
        return system, None, policy
    policy = policy_from_params(view.params_raw)
    doc_refs = list(view.doc_refs or [])
    docs_map: dict[str, tuple[str, int | None]] = {}
    for spec in doc_refs:
        try:
            docs_map[spec.doc_key] = await store.get_doc_text(spec.doc_key, spec.doc_version_id)
        except Exception as error:
            logger.warning("方案选品参考文档 {} 读取失败: {}", spec.doc_key, error)
    system = render_system(
        template=view.template,
        ctx=policy.as_prompt_vars(),
        docs_map=docs_map,
        doc_refs=doc_refs,
    )
    return system, (view.params.model or None), policy


async def _eligible_products(
    db,
    *,
    pricing: PricingContext,
    max_unit_price: float,
) -> list[Product]:
    active_ids = [
        item.strip()
        for item in (await _config_value(db, "supplier_ids")).split(",")
        if item.strip()
    ]
    stmt = (
        select(Product)
        .where(Product.is_active.is_(True))
        .where(Product.price > 0)
        .where(Product.cover_img.is_not(None))
        .where(Product.cover_img != "")
    )
    if pricing.use_discount:
        stmt = stmt.where(
            Product.price
            <= max(1.0, max_unit_price / max(float(pricing.discount_rate or 0.01), 0.01))
        )
    else:
        # 有成本价：按毛利反推成本上限；无成本价：按兜底折扣反推平台价上限
        margin = min(max(float(pricing.gross_margin), 0.0), 0.95)
        max_cost = max(0.01, max_unit_price * (1.0 - margin))
        fallback = max(float(pricing.fallback_discount_rate or 0.01), 0.01)
        max_fallback_platform = max(1.0, max_unit_price / fallback)
        stmt = stmt.where(
            or_(
                (Product.cost_price.is_not(None))
                & (Product.cost_price > 0)
                & (Product.cost_price <= max_cost),
                (Product.cost_price.is_(None) | (Product.cost_price <= 0))
                & (Product.price <= max_fallback_platform),
            )
        )
    if active_ids:
        stmt = stmt.where(Product.supplier_id.in_(active_ids))
    result = await db.execute(stmt.order_by(Product.price.desc(), Product.id.desc()).limit(2000))
    products = list(result.scalars().all())
    # 二次过滤：按实际优惠单价卡预算（折扣模式下平台价过滤已近似）
    return [p for p in products if 0 < pricing.unit_price(p) <= max_unit_price]


async def _fetch_products_by_ids(
    db, product_ids: list[int], *, pricing: PricingContext
) -> list[Product]:
    """按主键取商品；修订时用来钉住上一版 SKU，不受本轮价格上限抽样影响。"""
    ids = [int(pid) for pid in product_ids if pid]
    if not ids:
        return []
    stmt = (
        select(Product)
        .where(Product.id.in_(ids))
        .where(Product.is_active.is_(True))
        .where(Product.price > 0)
        .where(Product.cover_img.is_not(None))
        .where(Product.cover_img != "")
    )
    result = await db.execute(stmt)
    return [p for p in result.scalars().all() if pricing.unit_price(p) > 0]


def _prior_line_ids(prior_lines: list[dict] | None) -> list[int]:
    ids: list[int] = []
    for line in prior_lines or []:
        raw = line.get("product_id") or line.get("product_db_id")
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid and pid not in ids:
            ids.append(pid)
    return ids


def _shop_clusters(products: list[Product]) -> list[Product]:
    """挑几家在预算内商品最全的店铺，各给一批价格分散的商品。

    「尽量同一店铺」这类要求只有在候选里存在成套的同店商品时才可能被满足，
    否则模型手上根本没有可选项。
    """
    by_shop: dict[str, list[Product]] = {}
    for product in products:
        key = str(product.supplier_id or product.supplier_name or "").strip()
        if key:
            by_shop.setdefault(key, []).append(product)
    picked: list[Product] = []
    for items in sorted(by_shop.values(), key=len, reverse=True)[:SHOP_CLUSTER_SHOPS]:
        ordered = sorted(items, key=lambda item: float(item.price), reverse=True)
        if len(ordered) <= SHOP_CLUSTER_PER_SHOP:
            picked.extend(ordered)
            continue
        step = len(ordered) / SHOP_CLUSTER_PER_SHOP
        picked.extend(ordered[int(index * step)] for index in range(SHOP_CLUSTER_PER_SHOP))
    return picked


def item_text(product: Product) -> str:
    """商品的可检索文本：名称 + 规格 + 类目。"""
    return " ".join(
        str(part or "")
        for part in (
            product.product_name,
            product.unit,
            product.category_name_one,
            product.category_name_two,
            product.category_name_three,
        )
    )


def shop_text(product: Product) -> str:
    """店铺/产地的可检索文本；「北川店铺」在名称、供应商、产地里都可能体现。"""
    return " ".join(
        str(part or "")
        for part in (
            product.supplier_name,
            product.origin_province,
            product.origin_city,
            product.origin_district,
            product.product_name,
        )
    )


def _hits(text: str, keywords) -> bool:
    return any(word and word in text for word in keywords or ())


def _price_spread(products: list[Product], count: int) -> list[Product]:
    """在价格上均匀取若干件，避免某个品类召回的全是一个价位。"""
    if count <= 0 or not products:
        return []
    ordered = sorted(products, key=lambda product: float(product.price))
    if len(ordered) <= count:
        return ordered
    step = len(ordered) / count
    return [ordered[int(index * step)] for index in range(count)]


def _targeted_pool(
    products: list[Product],
    *,
    include=(),
    shop=(),
) -> list[Product]:
    """按销售点名的品类/店铺定向召回。

    分层抽样只看价格和类目，「北川店铺的米油」这种要求根本进不了候选，
    模型手上没有对应商品就只能拿别家的货凑，方案自然不对。
    """
    picked: dict[int, Product] = {}
    for keyword in include or ():
        matched = [product for product in products if keyword in item_text(product)]
        if not matched:
            continue
        if shop:
            in_shop = [product for product in matched if _hits(shop_text(product), shop)]
            outside = [product for product in matched if not _hits(shop_text(product), shop)]
            chosen = _price_spread(in_shop, KEYWORD_QUOTA) + _price_spread(
                outside, max(3, KEYWORD_QUOTA // 3)
            )
        else:
            chosen = _price_spread(matched, KEYWORD_QUOTA)
        for product in chosen:
            picked.setdefault(product.id, product)
    return list(picked.values())


def _band_sample(
    products: list[Product],
    *,
    max_platform_price: float,
    target: int,
    selected: dict[int, Product],
) -> None:
    """按价格档位 + 类目交错，把候选补到 target 件。

    只取「最贵的 N 件」会让候选全部贴着人均预算，模型无论怎么选都会超预算；
    必须让低价档也进入候选，模型才有凑组合的空间。
    """
    if len(selected) >= target:
        return
    width = max(max_platform_price, 1.0) / PRICE_BANDS
    buckets: list[list[Product]] = [[] for _ in range(PRICE_BANDS)]
    for product in products:
        index = min(PRICE_BANDS - 1, int(float(product.price) / width))
        buckets[index].append(product)

    quota = max(1, (target - len(selected)) // PRICE_BANDS)
    leftovers: list[Product] = []
    for bucket in buckets:
        by_category: dict[str, list[Product]] = {}
        for product in bucket:
            by_category.setdefault(_category_key(product), []).append(product)
        interleaved: list[Product] = []
        while any(by_category.values()):
            for items in by_category.values():
                if items:
                    interleaved.append(items.pop(0))
        taken = 0
        for product in interleaved:
            if len(selected) >= target:
                return
            if taken >= quota:
                leftovers.append(product)
            elif product.id not in selected:
                selected[product.id] = product
                taken += 1
    for product in leftovers:
        if len(selected) >= target:
            break
        selected.setdefault(product.id, product)


def _sample_candidates(
    products: list[Product],
    *,
    max_platform_price: float,
    limit: int = CANDIDATE_LIMIT,
    include=(),
    shop=(),
    exclude=(),
    pinned: list[Product] | None = None,
) -> list[Product]:
    """组装候选清单：可选钉入上一版 SKU（仅保证可选），再满足点名品类与店铺，最后按档位补齐。"""
    pool = (
        [product for product in products if not _hits(item_text(product), exclude)]
        if exclude
        else list(products)
    )
    if not pool:
        pool = list(products)

    selected: dict[int, Product] = {}
    # 上一版商品进入候选，方便模型选择保留；不表示必须选用
    for product in pinned or ():
        if exclude and _hits(item_text(product), exclude):
            continue
        selected[product.id] = product

    if len(pool) + len(selected) <= limit and not pinned:
        return pool

    for product in _targeted_pool(pool, include=include, shop=shop):
        if len(selected) >= limit * 2 // 3:
            break
        selected[product.id] = product

    in_shop = [product for product in pool if _hits(shop_text(product), shop)] if shop else []
    if in_shop:
        _band_sample(
            in_shop,
            max_platform_price=max_platform_price,
            target=int(limit * SHOP_FOCUS_SHARE),
            selected=selected,
        )
    else:
        for product in _shop_clusters(pool):
            if len(selected) >= limit // 2:
                break
            selected.setdefault(product.id, product)
    _band_sample(
        pool, max_platform_price=max_platform_price, target=limit, selected=selected
    )
    # pinned 始终保留在结果里，即使超过 limit 也不挤掉
    ordered = list(selected.values())
    if pinned:
        pinned_ids = {p.id for p in pinned if not (exclude and _hits(item_text(p), exclude))}
        head = [p for p in ordered if p.id in pinned_ids]
        tail = [p for p in ordered if p.id not in pinned_ids]
        return head + tail[: max(0, limit - len(head))]
    return ordered[:limit]


async def _proposal_llm(db, model_override: str | None = None) -> LLMClient | None:
    config_map = await _ai_config_map(db)
    model = (
        (model_override or "").strip()
        or default_chat_model_id(config_map)
    )
    api_url, api_key = resolve_chat_model_endpoint(config_map, model)
    if not model or not api_url or not api_key:
        return None
    logger.info("方案选品使用模型 model={}", model)
    return LLMClient(api_url=api_url, api_key=api_key, model=model)


def _origin_text(product: Product) -> str:
    return "".join(
        part
        for part in (
            product.origin_province,
            product.origin_city,
            product.origin_district,
        )
        if part
    )


def _fallback_selling_point(product: Product) -> str:
    """模型未给卖点或程序换货后的兜底：从品名/产地抽短卖点，绝不回填店铺名。"""
    name = str(product.product_name or "").strip()
    cues = [
        token
        for token in (
            "非转基因",
            "有机",
            "富硒",
            "一级",
            "特级",
            "野生",
            "礼盒",
            "长粒",
            "香米",
            "新鲜",
        )
        if token in name
    ]
    origin = _origin_text(product)
    parts: list[str] = []
    if origin:
        parts.append(f"{origin}特产" if len(origin) <= 10 else origin[:10])
    if cues:
        parts.append("、".join(cues[:2]))
    elif product.category_name_two:
        parts.append(str(product.category_name_two).strip())
    text = "，".join(part for part in parts if part) or (name[:16] if name else "")
    return text[:28]


def _clean_selling_point(raw: Any) -> str:
    text = str(raw or "").strip()
    text = re.sub(r"\s+", "", text)
    return text[:28]


_SPEC_PACK_UNITS = "桶|瓶|袋|盒|罐|件|箱"
_SPEC_AMOUNT_RE = (
    r"(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<measure>kg|KG|g|G|千克|公斤|L|l|ml|ML|毫升|升|斤|两|枚)"
    r"(?:\s*[x×*]\s*(?P<pack>\d+)\s*(?P<pack_unit>" + _SPEC_PACK_UNITS + r")?)?"
)


def _normalize_spec_measure(measure: str) -> str:
    raw = (measure or "").strip()
    key = raw.lower()
    mapping = {
        "kg": "kg",
        "千克": "kg",
        "公斤": "kg",
        "g": "g",
        "l": "L",
        "升": "L",
        "ml": "ml",
        "毫升": "ml",
        "斤": "斤",
        "两": "两",
        "枚": "枚",
    }
    return mapping.get(key, raw)


def _compose_spec(match: re.Match, unit: str) -> str:
    """把正则命中的分量与包装单位拼成「5kg/袋」「5L×4/箱」。"""
    amount = match.group("amount")
    measure = _normalize_spec_measure(match.group("measure"))
    pack = match.group("pack")
    pack_unit = match.group("pack_unit") or ""
    size = f"{amount}{measure}"
    if pack:
        # 多件装：包装词与最终单位相同时不重复（5L×4桶 + 桶 → 5L×4/桶）
        if pack_unit and unit and pack_unit == unit:
            size = f"{size}×{pack}"
        else:
            size = f"{size}×{pack}{pack_unit}"
    if not unit:
        return size
    if size.endswith(f"/{unit}"):
        return size
    if size.endswith(unit) and "×" in size:
        return size
    return f"{size}/{unit}"


def _format_product_spec(product: Product) -> str:
    """规格写成「5kg/袋」：分量从商品名抽取，包装单位用 product.unit。"""
    name = str(product.product_name or "")
    unit = str(product.unit or "").strip()
    if unit:
        matched = re.search(_SPEC_AMOUNT_RE + r"\s*/\s*" + re.escape(unit), name, re.I)
        if matched:
            return _compose_spec(matched, unit)
    matched = re.search(
        _SPEC_AMOUNT_RE + r"\s*/\s*(?P<slash_unit>" + _SPEC_PACK_UNITS + r")",
        name,
        re.I,
    )
    if matched:
        return _compose_spec(matched, matched.group("slash_unit"))
    matched = re.search(_SPEC_AMOUNT_RE, name, re.I)
    if matched:
        return _compose_spec(matched, unit)
    return unit


def _candidate_payload(products: list[Product], pricing: PricingContext) -> list[dict]:
    # 按店铺分组、店内优惠价升序：模型会顺着看到的顺序锚定，先给便宜规格能少烧一轮重选，
    # 同时让「尽量同店铺」这类要求在清单里一眼可见。
    ordered = sorted(
        products,
        key=lambda product: (
            str(product.supplier_name or product.supplier_id or ""),
            pricing.unit_price(product),
        ),
    )
    return [
        {
            "id": product.id,
            "name": product.product_name,
            "unit": product.unit or "",
            "spec": _format_product_spec(product),
            "platform_price": money(product.price),
            "cost_price": money(product.cost_price) if product.cost_price is not None else None,
            "promo_price": pricing.unit_price(product),
            "category": "/".join(
                part
                for part in (product.category_name_one, product.category_name_two)
                if part
            ),
            "origin": _origin_text(product),
            "shop": product.supplier_name or "",
            "shop_id": product.supplier_id or "",
        }
        for product in ordered
    ]


def _requirements(constraints: dict, *, max_lines: int) -> dict:
    """销售点名的选品要求（品类、店铺、种类数），由需求理解环节解析得到。"""
    out: dict = {}
    for key in ("include_keywords", "exclude_keywords", "shop_keywords"):
        words = [str(word).strip() for word in (constraints.get(key) or []) if str(word).strip()]
        if words:
            out[key] = words
    kinds = constraints.get("item_kinds")
    try:
        kinds = int(kinds) if kinds else 0
    except (TypeError, ValueError):
        kinds = 0
    if 1 <= kinds <= max_lines:
        out["item_kinds"] = kinds
    return out


def _requirement_notes(requirements: dict) -> list[str]:
    """把结构化要求翻成给模型看的硬约束，压过提示词里的默认偏好。"""
    notes: list[str] = []
    if requirements.get("include_keywords"):
        notes.append(
            "方案里必须出现这些品类：" + "、".join(requirements["include_keywords"])
            + "；销售没提到的品类不要自行加进来凑预算，先靠规格和每人件数凑，"
            "确实凑不满再补并在 rationale 里说明"
        )
    if requirements.get("exclude_keywords"):
        notes.append("不要出现这些品类：" + "、".join(requirements["exclude_keywords"]))
    if requirements.get("shop_keywords"):
        notes.append(
            "只用这些店铺/产地的商品：" + "、".join(requirements["shop_keywords"])
            + "；候选里确实凑不齐时才可外扩，并在 rationale 里说明原因"
        )
    if requirements.get("item_kinds"):
        notes.append(
            f"商品种类数必须正好 {requirements['item_kinds']} 种，"
            "这条优先于提示词里的默认种类区间；靠调整每人件数来对齐人均预算"
        )
    return notes


def _revision_notes(prior_lines: list[dict] | None, requirements: dict) -> list[str]:
    """修订模式说明：上一版仅作参考，是否换货由模型评估。"""
    if not prior_lines:
        return []
    summary = []
    exclude = requirements.get("exclude_keywords") or ()
    for line in prior_lines:
        name = str(line.get("name") or line.get("product_name") or "").strip()
        if not name:
            continue
        if any(word and word in name for word in exclude):
            continue
        qty = line.get("qty_per_person") or 1
        price = line.get("platform_price")
        piece = f"{name}×每人{qty}件"
        if price is not None:
            piece += f"（平台价约¥{price}）"
        summary.append(piece)
    notes = [
        "prior_lines 是上一版方案，仅供参考：结合本轮预算与反馈，由你评估哪些保留、"
        "哪些换成更大/更合适规格，或换成同店其他商品；不必强行沿用全部 product_id。",
        "预算上调时优先：加大规格、提高合适品类的每人件数，或换成更优商品把人均凑近新预算；"
        "禁止为凑预算重复堆叠同款/同品类（例如两桶几乎一样的油）。",
        "预算下调或点名剔除时再删换；主题与店铺约束仍以 requirements 为准。",
    ]
    if summary:
        notes.append("上一版摘要：" + "；".join(summary[:6]))
    if exclude:
        notes.append("必须去掉名称含这些词的商品：" + "、".join(exclude))
    return notes


def _missing_includes(items: list[dict], lookup: dict[int, Product], include) -> list[str]:
    texts = [item_text(lookup[item["product_id"]]) for item in items if item["product_id"] in lookup]
    return [word for word in include or () if not any(word in text for text in texts)]


def _outside_shop(items: list[dict], lookup: dict[int, Product], shop) -> list[str]:
    if not shop:
        return []
    return [
        lookup[item["product_id"]].product_name
        for item in items
        if item["product_id"] in lookup
        and not _hits(shop_text(lookup[item["product_id"]]), shop)
    ]


def _include_keep_ids(items: list[dict], lookup: dict[int, Product], include) -> set[int]:
    """点名品类的商品在预算修正时不能被直接删掉，每个品类保住最便宜的一件。"""
    keep: set[int] = set()
    for word in include or ():
        matched = [
            item["product_id"]
            for item in items
            if item["product_id"] in lookup and word in item_text(lookup[item["product_id"]])
        ]
        if matched:
            keep.add(min(matched, key=lambda pid: float(lookup[pid].price)))
    return keep


def _normalize_items(
    data: dict,
    lookup: dict[int, Product],
    *,
    max_lines: int,
    max_qty_per_person: int,
) -> list[dict]:
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raw_items = [{"product_id": pid} for pid in (data.get("product_ids") or [])]
    items: list[dict] = []
    seen: set[int] = set()
    for entry in raw_items:
        selling_point = ""
        if isinstance(entry, dict):
            raw_id = entry.get("product_id") or entry.get("id")
            raw_qty = entry.get("qty_per_person") or entry.get("qty") or 1
            selling_point = _clean_selling_point(
                entry.get("selling_point") or entry.get("remark")
            )
        else:
            raw_id, raw_qty = entry, 1
        try:
            product_id = int(raw_id)
            qty = int(raw_qty)
        except (TypeError, ValueError):
            continue
        if product_id not in lookup or product_id in seen:
            continue
        seen.add(product_id)
        row = {
            "product_id": product_id,
            "qty_per_person": max(1, min(max_qty_per_person, qty)),
        }
        if selling_point:
            row["selling_point"] = selling_point
        items.append(row)
        if len(items) >= max(1, max_lines):
            break
    return items


def _cheaper_alternatives(
    items: list[dict],
    lookup: dict[int, Product],
    pool: list[Product],
    pricing: PricingContext,
    *,
    per_item: int = 3,
) -> str:
    """超预算重选时，把同类目里更便宜的候选摆给模型看（同店优先）。

    模型常见的卡点不是不会算，而是不知道同一品类还有小规格可换。
    """
    fragments: list[str] = []
    for item in items:
        current = lookup.get(item["product_id"])
        if current is None:
            continue
        same_shop = str(current.supplier_id or current.supplier_name or "")
        current_promo = pricing.unit_price(current)
        options = [
            product
            for product in pool
            if product.id != current.id
            and _category_key(product) == _category_key(current)
            and pricing.unit_price(product) < current_promo
        ]
        options.sort(
            key=lambda product: (
                str(product.supplier_id or product.supplier_name or "") != same_shop,
                pricing.unit_price(product),
            )
        )
        if not options:
            continue
        listed = "、".join(
            f"{product.product_name}(id={product.id},"
            f"¥{pricing.unit_price(product):.2f})"
            for product in options[:per_item]
        )
        fragments.append(f"{current.product_name} → {listed}")
    return "；".join(fragments[:4])


def _per_capita(items: list[dict], lookup: dict[int, Product], pricing: PricingContext) -> float:
    return money(
        sum(
            pricing.unit_price(lookup[item["product_id"]]) * item["qty_per_person"]
            for item in items
        )
    )


def _accepted_override(value: Any, text: str) -> float | None:
    """模型改写人均/人数时，要求该数字确实出现在销售原话里，避免凭空改预算。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    trimmed = int(number) if float(number).is_integer() else number
    return number if re.search(rf"(?<!\d){trimmed}(?!\d)", text) else None


def _category_key(product: Product) -> str:
    return product.category_name_two or product.category_name_one or ""


def _swap_to_budget(
    items: list[dict],
    lookup: dict[int, Product],
    pool: list[Product],
    *,
    budget: float,
    pricing: PricingContext,
    tolerance: float,
    keep_ids: set[int] | frozenset[int] = frozenset(),
) -> list[dict]:
    """超预算时先在同类目里换更便宜的商品，保住模型选定（也是销售点名）的品类结构。"""
    by_category: dict[str, list[Product]] = {}
    for product in pool:
        by_category.setdefault(_category_key(product), []).append(product)
    selection = [dict(item) for item in items]
    high = budget * (1 + tolerance)

    for _ in range(len(selection) * 3 or 1):
        current_total = _per_capita(selection, lookup, pricing)
        if current_total <= high:
            break
        best: tuple[float, list[dict]] | None = None
        for index, item in enumerate(selection):
            # 点名品类对应的 keep_ids：优先靠减件数/换其他行，不直接换掉该 SKU
            if item["product_id"] in keep_ids:
                continue
            current = lookup[item["product_id"]]
            current_promo = pricing.unit_price(current)
            for candidate in by_category.get(_category_key(current), []):
                if candidate.id == current.id or pricing.unit_price(candidate) >= current_promo:
                    continue
                trial = [dict(entry) for entry in selection]
                # 换货后旧卖点不再适用，交给最终回填重新生成
                trial[index] = {
                    "product_id": candidate.id,
                    "qty_per_person": trial[index]["qty_per_person"],
                }
                value = _per_capita(trial, lookup, pricing)
                # 一步换不到位也要换，逐步往预算走，好过直接把某个品类删掉
                if value >= current_total:
                    continue
                score = abs(value - budget)
                if best is None or score < best[0]:
                    best = (score, trial)
        if best is None:
            break
        selection = best[1]
    return selection


def _fit_to_budget(
    items: list[dict],
    lookup: dict[int, Product],
    *,
    budget: float,
    pricing: PricingContext,
    tolerance: float,
    keep_ids: set[int] | frozenset[int] = frozenset(),
    min_lines: int = 1,
    max_qty_per_person: int = 6,
) -> list[dict]:
    """模型多轮仍不达标时的算术兜底：先削超支，再补欠缺。

    keep_ids / min_lines 用来护住销售点名的品类和种类数——把「米」删掉换来的
    预算达标，对销售来说仍然是一版废方案。
    """
    high = budget * (1 + tolerance)
    low = budget * (1 - tolerance)
    selection = [dict(item) for item in items]

    while selection and _per_capita(selection, lookup, pricing) > high:
        best: tuple[float, list[dict]] | None = None
        for index in range(len(selection)):
            trial = [dict(item) for item in selection]
            if trial[index]["qty_per_person"] > 1:
                trial[index]["qty_per_person"] -= 1
            elif trial[index]["product_id"] in keep_ids or len(trial) <= min_lines:
                continue
            else:
                trial.pop(index)
            if not trial:
                continue
            score = abs(_per_capita(trial, lookup, pricing) - budget)
            if best is None or score < best[0]:
                best = (score, trial)
        if best is None:
            break
        selection = best[1]

    for _ in range(20):
        if not selection or _per_capita(selection, lookup, pricing) >= low:
            break
        best = None
        for index in range(len(selection)):
            trial = [dict(item) for item in selection]
            if trial[index]["qty_per_person"] >= max_qty_per_person:
                continue
            trial[index]["qty_per_person"] += 1
            value = _per_capita(trial, lookup, pricing)
            if value <= high and (best is None or abs(value - budget) < best[0]):
                best = (abs(value - budget), trial)
        if best is None:
            break
        selection = best[1]
    return selection


def _fallback_pick(
    products: list[Product],
    target: float,
    pricing: PricingContext,
    *,
    tolerance: float = 0.08,
    max_kinds: int = 4,
) -> list[dict]:
    """无可用模型时的兜底：动态规划挑最接近人均预算的一组商品。"""
    cap = max(1, int(round(target * 100)))
    high = int(cap * (1 + tolerance))
    dp: dict[int, list[int]] = {0: []}
    for product in products[:80]:
        promo_cents = max(1, int(round(pricing.unit_price(product) * 100)))
        for total, ids in sorted(list(dp.items()), reverse=True):
            if len(ids) >= max_kinds:
                continue
            new_total = total + promo_cents
            if new_total <= high and new_total not in dp:
                dp[new_total] = ids + [product.id]
    eligible = [(abs(total - cap), -total, ids) for total, ids in dp.items() if ids]
    chosen = min(eligible)[2] if eligible else ([products[-1].id] if products else [])
    return [{"product_id": pid, "qty_per_person": 1} for pid in chosen]


async def _select_with_llm(
    db,
    *,
    products: list[Product],
    constraints: dict,
    context_summary: str,
    prior_lines: list[dict] | None,
    user_id: int,
    policy: ProposalPolicy,
    system_prompt: str,
) -> tuple[list[dict], dict, str]:
    """返回（选品明细，模型理解到的约束覆盖，理由）。"""
    tolerance = policy.budget_tolerance
    # 桌面传入当前勾选列表的第一项；非桌面调用则回退可选对话模型列表第一项。
    llm = await _proposal_llm(db, (constraints.get("chat_model") or "").strip())
    if llm is None:
        logger.warning("方案选品未配置可用模型，回退到程序组合")
        return [], {}, ""

    pricing = _pricing_from_constraints(constraints, policy)
    lookup = {product.id: product for product in products}
    requirements = _requirements(constraints, max_lines=policy.max_lines)
    constraint_block: dict[str, Any] = {
        "per_capita_budget": constraints.get("per_capita_budget"),
        "headcount": constraints.get("headcount"),
        "gross_margin": pricing.gross_margin,
        "shipping": constraints.get("shipping") or "包邮",
    }
    if pricing.use_discount and pricing.discount_rate is not None:
        constraint_block["discount_rate"] = pricing.discount_rate
    payload = {
        "request": constraints.get("request_text"),
        "feedback_history": constraints.get("feedback_history") or [],
        "requirements": requirements,
        "policy": {
            "budget_tolerance": policy.budget_tolerance,
            "item_kinds_min": policy.item_kinds_min,
            "item_kinds_max": policy.item_kinds_max,
            "max_qty_per_person": policy.max_qty_per_person,
            "default_gross_margin": policy.default_gross_margin,
            "fallback_discount_rate": policy.fallback_discount_rate,
        },
        "constraints": constraint_block,
        "prior_lines": prior_lines or [],
        "customer_context": (context_summary or "")[:2500],
        "candidates": _candidate_payload(products, pricing),
    }
    notes = _requirement_notes(requirements) + _revision_notes(prior_lines, requirements)
    user_content = json.dumps(payload, ensure_ascii=False)
    if notes:
        label = (
            "销售点名的要求与修订说明（优先级最高，与默认偏好冲突时以此为准）"
            if prior_lines
            else "销售点名的要求（优先级最高，与默认偏好冲突时以此为准）"
        )
        user_content += f"\n\n{label}：\n" + "\n".join(f"- {note}" for note in notes)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    spoken = " ".join(
        [str(constraints.get("request_text") or "")]
        + [str(item) for item in (constraints.get("feedback_history") or [])]
    )
    budget = float(constraints["per_capita_budget"])
    overrides: dict = {}
    items: list[dict] = []
    rationale = ""

    for round_index in range(1, MAX_LLM_ROUNDS + 1):
        response = await llm.chat(
            messages,
            temperature=0.2,
            max_tokens=1200,
            usage=LLMUsageContext(scenario_key=COMPOSE_SCENARIO_KEY, user_id=user_id),
        )
        content = (response.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        data = _extract_first_json_object(content) or {}
        if round_index == 1:
            new_budget = _accepted_override(data.get("per_capita_budget"), spoken)
            new_headcount = _accepted_override(data.get("headcount"), spoken)
            if new_budget and new_budget != budget:
                overrides["per_capita_budget"] = round(new_budget, 2)
                budget = new_budget
            if new_headcount:
                overrides["headcount"] = int(new_headcount)
        parsed = _normalize_items(
            data,
            lookup,
            max_lines=requirements.get("item_kinds") or policy.max_lines,
            max_qty_per_person=policy.max_qty_per_person,
        )
        if parsed:
            items = parsed
            rationale = str(data.get("rationale") or "").strip() or rationale
        if not items:
            break
        per_capita = _per_capita(items, lookup, pricing)
        issues: list[str] = []
        if abs(per_capita - budget) > budget * tolerance:
            issues.append(
                f"优惠后人均 ¥{per_capita:.2f}，不在预算 ¥{budget:.2f} 的 ±{tolerance:.0%} 区间内，"
                f"请调到 ¥{budget * (1 - tolerance):.2f}~¥{budget * (1 + tolerance):.2f}："
                "优先把偏贵的商品换成同类目的小规格，或增减每人件数"
            )
        missing = _missing_includes(items, lookup, requirements.get("include_keywords"))
        if missing:
            issues.append("销售点名的品类没有出现：" + "、".join(missing) + "，必须补上")
        kinds = requirements.get("item_kinds")
        if kinds and len(items) != kinds:
            issues.append(f"商品种类数是 {len(items)} 种，销售要求正好 {kinds} 种")
        outside = _outside_shop(items, lookup, requirements.get("shop_keywords"))
        if outside:
            issues.append(
                "这些商品不属于销售点名的店铺/产地：" + "、".join(outside[:4])
                + "，候选里有同店商品就换过来"
            )
        if not issues:
            return items, overrides, rationale
        if round_index == MAX_LLM_ROUNDS:
            break
        logger.info("方案选品第 {} 轮不达标，要求模型重选：{}", round_index, "；".join(issues))
        breakdown = "；".join(
            f"{lookup[item['product_id']].product_name}"
            f" 优惠单价¥{pricing.unit_price(lookup[item['product_id']]):.2f}"
            f"×{item['qty_per_person']}"
            for item in items
        )
        alternatives = _cheaper_alternatives(items, lookup, products, pricing)
        messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"这一版不符合要求。明细：{breakdown}。\n"
                    + "\n".join(f"- {issue}" for issue in issues)
                    + (f"\n同类更便宜的候选：{alternatives}。" if alternatives else "")
                    + "\n请重新给出一版，仍然只输出同样格式的 JSON。"
                ),
            }
        )
    return items, overrides, rationale


async def compose_spec(
    db,
    *,
    constraints: dict,
    context_summary: str,
    user_id: int,
    customer_id: str | None,
    prior_lines: list[dict] | None = None,
) -> dict:
    policy_bundle = await _compose_prompt(db)
    system_prompt, _, policy = policy_bundle
    budget = float(constraints.get("per_capita_budget") or 0)
    try:
        headcount = int(constraints.get("headcount") or 0)
    except (TypeError, ValueError):
        headcount = 0
    pricing = _pricing_from_constraints(constraints, policy)
    if budget <= 0:
        raise ValueError("缺少人均预算，无法生成方案")
    if headcount <= 0:
        headcount = 1

    # 单品优惠价上限按人均预算放宽，避免只筛出极小规格
    max_unit_price = max(1.0, budget)
    eligible = await _eligible_products(db, pricing=pricing, max_unit_price=max_unit_price)
    if not eligible:
        raise ValueError("商品库中没有同时具备有效售价和封面图的可用商品")
    requirements = _requirements(constraints, max_lines=policy.max_lines)
    exclude = requirements.get("exclude_keywords") or ()
    # 上一版 SKU 仅保证出现在候选里供模型选用，不强制保留
    prior_refs = await _fetch_products_by_ids(
        db, _prior_line_ids(prior_lines), pricing=pricing
    )
    products = _sample_candidates(
        eligible,
        max_platform_price=max_unit_price,
        include=requirements.get("include_keywords") or (),
        shop=requirements.get("shop_keywords") or (),
        exclude=exclude,
        pinned=prior_refs,
    )

    tolerance = policy.budget_tolerance
    items, overrides, rationale = await _select_with_llm(
        db,
        products=products,
        constraints=constraints,
        context_summary=context_summary,
        prior_lines=prior_lines,
        user_id=user_id,
        policy=policy,
        system_prompt=system_prompt,
    )
    if overrides.get("per_capita_budget"):
        budget = float(overrides["per_capita_budget"])
    if overrides.get("headcount"):
        headcount = int(overrides["headcount"])

    lookup = {product.id: product for product in products}
    if not items:
        items = _fallback_pick(
            products,
            budget,
            pricing,
            tolerance=tolerance,
            max_kinds=policy.item_kinds_max,
        )
        rationale = rationale or "按人均预算从商品库自动组合。"
    elif abs(_per_capita(items, lookup, pricing) - budget) > budget * tolerance:
        before = _per_capita(items, lookup, pricing)
        keep_ids = _include_keep_ids(items, lookup, requirements.get("include_keywords"))
        items = _swap_to_budget(
            items,
            lookup,
            products,
            budget=budget,
            pricing=pricing,
            tolerance=tolerance,
            keep_ids=keep_ids,
        )
        min_lines = min(int(requirements.get("item_kinds") or 1), len(items))
        if abs(_per_capita(items, lookup, pricing) - budget) > budget * tolerance:
            items = _fit_to_budget(
                items,
                lookup,
                budget=budget,
                pricing=pricing,
                tolerance=tolerance,
                keep_ids=keep_ids,
                min_lines=min_lines,
                max_qty_per_person=policy.max_qty_per_person,
            )
        if _per_capita(items, lookup, pricing) > budget * (1 + tolerance):
            # 护品类仍然超支时，预算优先：宁可少一个品类，也不能把报价做飞
            items = _fit_to_budget(
                items,
                lookup,
                budget=budget,
                pricing=pricing,
                tolerance=tolerance,
                max_qty_per_person=policy.max_qty_per_person,
            )
        logger.warning(
            "方案选品仍偏离预算，已按预算修正 人均 ¥{} → ¥{}（预算 ¥{}）",
            before,
            _per_capita(items, lookup, pricing),
            budget,
        )

    lines: list[dict] = []
    for seq, item in enumerate(items, 1):
        product = lookup.get(item["product_id"])
        if product is None:
            continue
        qty_per_person = int(item["qty_per_person"])
        qty = qty_per_person * headcount
        platform_price = money(product.price)
        promo_price = pricing.unit_price(product)
        if promo_price <= 0:
            continue
        cost_price = money(product.cost_price) if pricing.has_cost(product) else None
        selling_point = _clean_selling_point(item.get("selling_point")) or _fallback_selling_point(
            product
        )
        if pricing.use_discount:
            priced_by = "discount"
        elif cost_price is not None:
            priced_by = "margin"
        else:
            priced_by = "fallback_discount"
        lines.append(
            {
                "seq": seq,
                "product_db_id": product.id,
                "product_id": product.product_id,
                "product_name": product.product_name,
                "spec": _format_product_spec(product),
                "platform_price": platform_price,
                "cost_price": cost_price,
                "promo_unit_price": promo_price,
                "qty_per_person": qty_per_person,
                "qty": qty,
                "platform_subtotal": money(platform_price * qty),
                "cost_subtotal": money(cost_price * qty) if cost_price is not None else None,
                "promo_subtotal": money(promo_price * qty),
                "image_url": product.cover_img,
                "remark": selling_point,
                "priced_by": priced_by,
                "supplier_name": (product.supplier_name or "").strip(),
                "supplier_id": product.supplier_id or "",
            }
        )
    if not lines:
        raise ValueError("未能从商品库组出满足人均预算的方案")

    platform_total = money(sum(line["platform_subtotal"] for line in lines))
    promo_total = money(sum(line["promo_subtotal"] for line in lines))
    cost_values = [
        float(line["cost_subtotal"])
        for line in lines
        if line.get("cost_subtotal") is not None
    ]
    cost_total = money(sum(cost_values)) if cost_values else None
    per_capita = money(promo_total / headcount)
    shop_counts: dict[str, int] = {}
    for line in lines:
        shop = str(line.get("supplier_name") or "").strip()
        if shop:
            shop_counts[shop] = shop_counts.get(shop, 0) + 1
    primary_shop = max(shop_counts, key=shop_counts.get) if shop_counts else ""
    if not primary_shop:
        keywords = [
            str(item).strip()
            for item in (constraints.get("shop_keywords") or [])
            if str(item).strip()
        ]
        primary_shop = keywords[0] if keywords else ""
    meta = {
        **constraints,
        "per_capita_budget": round(budget, 2),
        "headcount": headcount,
        "gross_margin": round(pricing.gross_margin, 4),
        "margin_source": constraints.get("margin_source") or "default",
        "fallback_discount_rate": round(pricing.fallback_discount_rate, 4),
        "customer_id": customer_id,
        "primary_shop": primary_shop,
    }
    if pricing.use_discount and pricing.discount_rate is not None:
        meta["discount_rate"] = pricing.discount_rate
        meta["discount_source"] = "dialog"
    else:
        meta.pop("discount_rate", None)
        meta["discount_source"] = "default"
    return {
        "title": "脱贫地区农副产品网络销售平台 产品供应表（包邮）",
        "meta": meta,
        "context_note": (context_summary or "")[:500],
        "lines": lines,
        "totals": {
            "per_capita_promo": per_capita,
            "platform_total": platform_total,
            "promo_total": promo_total,
            # 仅供对话预览；xlsx 渲染不得使用
            "cost_total": cost_total,
        },
        "rationale": rationale,
    }
