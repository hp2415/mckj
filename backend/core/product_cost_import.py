"""从 XLSX 批量更新商品成本价。

表头兼容：id + 成本价 / cost_price；其余列（商品名、售价等）忽略。
按本地主键 id 更新，不覆盖未给出成本价的行。
"""
from __future__ import annotations

import io
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import select

from models import Product

_ID_HEADERS = frozenset({"id", "商品id", "本地id", "主键"})
_COST_HEADERS = frozenset({"成本价", "cost_price", "成本", "成本价(元)", "成本价（元）"})


def _header_key(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("（", "(").replace("）", ")")


def _parse_cost(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
    else:
        text = str(value).strip().replace("￥", "").replace("¥", "").replace(",", "")
        if not text:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
    if number < 0:
        return None
    return number.quantize(Decimal("0.01"))


def _find_columns(headers: list[Any]) -> tuple[int | None, int | None]:
    id_headers = {_header_key(h) for h in _ID_HEADERS}
    cost_headers = {_header_key(h) for h in _COST_HEADERS}
    id_col = cost_col = None
    for index, raw in enumerate(headers):
        key = _header_key(raw)
        if id_col is None and key in id_headers:
            id_col = index
        if cost_col is None and key in cost_headers:
            cost_col = index
    return id_col, cost_col


async def import_cost_prices_from_xlsx(db, content: bytes) -> dict[str, Any]:
    """解析并写入成本价。返回统计信息。"""
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        try:
            headers = list(next(rows))
        except StopIteration:
            return {
                "updated": 0,
                "skipped": 0,
                "not_found": 0,
                "invalid": 0,
                "errors": ["文件为空或没有表头"],
            }
        id_col, cost_col = _find_columns(headers)
        if id_col is None or cost_col is None:
            return {
                "updated": 0,
                "skipped": 0,
                "not_found": 0,
                "invalid": 0,
                "errors": [
                    "未识别表头：需要同时包含「id」与「成本价」（或 cost_price）列。"
                    f" 当前表头：{[str(h or '') for h in headers]}"
                ],
            }

        updates: list[tuple[int, Decimal]] = []
        skipped = invalid = 0
        errors: list[str] = []
        for row_index, row in enumerate(rows, start=2):
            if not row or all(cell is None or str(cell).strip() == "" for cell in row):
                continue
            raw_id = row[id_col] if id_col < len(row) else None
            raw_cost = row[cost_col] if cost_col < len(row) else None
            try:
                product_id = int(raw_id)
            except (TypeError, ValueError):
                invalid += 1
                if len(errors) < 20:
                    errors.append(f"第 {row_index} 行：id 无效（{raw_id!r}）")
                continue
            cost = _parse_cost(raw_cost)
            if cost is None:
                skipped += 1
                continue
            updates.append((product_id, cost))
    finally:
        workbook.close()

    if not updates:
        return {
            "updated": 0,
            "skipped": skipped,
            "not_found": 0,
            "invalid": invalid,
            "errors": errors or ["没有可导入的成本价行"],
        }

    ids = [item[0] for item in updates]
    result = await db.execute(select(Product).where(Product.id.in_(ids)))
    by_id = {row.id: row for row in result.scalars().all()}
    updated = not_found = 0
    for product_id, cost in updates:
        product = by_id.get(product_id)
        if product is None:
            not_found += 1
            if len(errors) < 20:
                errors.append(f"id={product_id} 在商品库中不存在")
            continue
        product.cost_price = cost
        updated += 1
    await db.commit()
    return {
        "updated": updated,
        "skipped": skipped,
        "not_found": not_found,
        "invalid": invalid,
        "errors": errors,
    }
