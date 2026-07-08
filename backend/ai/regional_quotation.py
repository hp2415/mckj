"""
区域现采/外部采买报价表检索。

从 regional_quotation 文档（markdown 表格或 TSV）解析报价行，
供 lookup_regional_quotation 工具做确定性匹配，避免模型在长 system 中漏读。
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class QuotationRow:
    seq: str
    scope: str
    district: str
    product: str
    spec: str
    cost: str

    @property
    def is_national_fallback(self) -> bool:
        scope = self.scope or ""
        return "全国" in scope or (self.district or "").strip() in ("全部", "—", "-", "")


_MD_SEP_RE = re.compile(r"^\|[\s:|\-]+\|$")


def _strip_admin(s: str) -> str:
    return (s or "").strip().strip("*")


def _norm_place_key(s: str) -> str:
    """地名归一：去掉常见行政后缀便于模糊匹配。"""
    t = (s or "").strip()
    for suffix in ("黎族苗族自治县", "彝族自治州", "特别行政区", "自治区", "自治州", "地区", "市", "区", "县", "镇"):
        if t.endswith(suffix) and len(t) > len(suffix):
            t = t[: -len(suffix)]
            break
    return t


def _district_match(query: str, row: QuotationRow) -> bool:
    q = (query or "").strip()
    if not q:
        return True
    district = (row.district or "").strip()
    scope = (row.scope or "").strip()
    candidates = [district, scope]
    if district.endswith("（广东）") or district.endswith("（汕尾）"):
        candidates.append(district.split("（")[0])
    qn = _norm_place_key(q)
    for cand in candidates:
        if not cand:
            continue
        if q in cand or cand in q:
            return True
        cn = _norm_place_key(cand)
        if qn and cn and (qn in cn or cn in qn):
            return True
    return False


def _product_match(query: str, product_name: str) -> bool:
    q = (query or "").strip().lower()
    name = (product_name or "").strip().lower()
    if not q:
        return True
    if not name:
        return False
    if q in name or name in q:
        return True
    # 品类模糊：珍珠米 ↔ 东北珍珠米；长粒香族；大米类
    if "珍珠米" in q and "珍珠米" in name:
        return True
    if "长粒香" in q and "长粒香" in name:
        return True
    if q in ("大米", "米") and "米" in name and "油" not in name:
        return True
    if "菜籽油" in q and "菜籽油" in name:
        return True
    if "玉米油" in q and "玉米油" in name:
        return True
    if "花生油" in q and "花生油" in name:
        return True
    if "调和油" in q and "调和油" in name:
        return True
    return False


def parse_quotation_content(content: str) -> list[QuotationRow]:
    """解析 markdown 表格行；兼容 6 列明细表与 5 列区县速查表。"""
    rows: list[QuotationRow] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("|"):
            if _MD_SEP_RE.match(line.replace(" ", "")):
                continue
            cells = [_strip_admin(c) for c in line.strip("|").split("|")]
            if not cells or cells[0] in ("序号", "区县") or "大致范围" in cells[0]:
                continue
            if len(cells) == 6:
                row = QuotationRow(
                    seq=cells[0], scope=cells[1], district=cells[2],
                    product=cells[3], spec=cells[4], cost=cells[5],
                )
            elif len(cells) == 5:
                row = QuotationRow(
                    seq=cells[4], scope="", district=cells[0],
                    product=cells[1], spec=cells[2], cost=cells[3],
                )
            else:
                continue
        elif "\t" in line:
            cells = [c.strip() for c in line.split("\t")]
            if not cells or cells[0] in ("序号",) or not cells[0].isdigit():
                continue
            if len(cells) < 6:
                continue
            row = QuotationRow(
                seq=cells[0], scope=cells[1], district=cells[2],
                product=cells[3], spec=cells[4], cost=cells[5],
            )
        else:
            continue

        key = (row.seq, row.district, row.product, row.spec, row.cost)
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)

    return rows


def lookup_rows(
    rows: list[QuotationRow],
    *,
    district: str | None = None,
    product: str | None = None,
) -> list[QuotationRow]:
    district = (district or "").strip()
    product = (product or "").strip()

    exclusive: list[QuotationRow] = []
    national: list[QuotationRow] = []

    for row in rows:
        if not _product_match(product, row.product):
            continue
        if district and not _district_match(district, row):
            continue
        if row.is_national_fallback:
            national.append(row)
        else:
            exclusive.append(row)

    if exclusive:
        return exclusive
    if national:
        return national
    # 仅有品类、无区县：返回该品类全部专属行
    if product and not district:
        return [r for r in rows if _product_match(product, r.product) and not r.is_national_fallback]
    return []


def format_lookup_result(
    matches: list[QuotationRow],
    *,
    district: str | None = None,
    product: str | None = None,
) -> str:
    if not matches:
        return (
            f"未在区域报价表中找到匹配项（区县={district or '未指定'}, 商品={product or '未指定'}）。"
            "请核对地名与品类，或联系采购补充报价。"
        )
    lines = [f"区域报价表命中 {len(matches)} 条："]
    for r in matches:
        place = r.district or r.scope or "—"
        lines.append(
            f"- 序号{r.seq} | {place} | {r.product} | 规格 {r.spec} | 成本 {r.cost}"
            + ("（全国一二线城市兜底区间）" if r.is_national_fallback else "")
        )
    return "\n".join(lines)


def lookup_regional_quotation(
    content: str,
    *,
    district: str | None = None,
    product: str | None = None,
) -> str:
    rows = parse_quotation_content(content)
    if not rows:
        return "报价表已加载但未能解析出有效数据行，请检查后台文档格式（需 markdown 表格或 TSV）。"
    matches = lookup_rows(rows, district=district, product=product)
    return format_lookup_result(matches, district=district, product=product)
