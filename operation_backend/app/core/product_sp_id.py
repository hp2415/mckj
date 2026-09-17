"""主系统商品规格 ID（mibuddy_sp_id）清洗与校验。"""
from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any

# 主系统规格 ID 为纯数字，长短不一（如 2869、202510301724209358280）
_SP_ID_RE = re.compile(r"^\d{1,64}$")
_MAX_SAFE_ABS = 10**15
_MISSING_TOKENS = frozenset(
    {
        "",
        "-",
        "--",
        "/",
        "／",
        "n/a",
        "na",
        "none",
        "null",
        "#n/a",
        "#n/a!",
        "#value!",
        "#value",
        "#ref!",
        "#name?",
        "#div/0!",
    }
)


def parse_mibuddy_sp_id(value: Any) -> tuple[str | None, str | None]:
    """解析规格 ID。

    返回 ``(sp_id, error)``：
    - 缺失/空/#N/A：``(None, None)``
    - 合法：``("2025…", None)``
    - 非法（科学计数法、精度丢失、格式不符）：``(None, 原因)``
    """
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "规格 ID 无效"
    if isinstance(value, int):
        return _from_digit_text(str(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            return None, "规格 ID 无效"
        if abs(value) >= _MAX_SAFE_ABS:
            return None, "规格 ID 疑似被 Excel 转成数字导致精度丢失"
        if not value.is_integer():
            return None, "规格 ID 无效"
        return _from_digit_text(str(int(value)))
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None, "规格 ID 无效"
        if abs(value) >= _MAX_SAFE_ABS:
            return None, "规格 ID 疑似被 Excel 转成数字导致精度丢失"
        if value != value.to_integral_value():
            return None, "规格 ID 无效"
        return _from_digit_text(format(int(value), "d"))

    text = str(value).strip().replace("\u3000", " ").strip()
    if text.startswith("'"):
        text = text[1:].strip()
    collapsed = text.replace(",", "").replace(" ", "")
    key = collapsed.lower()
    if key in _MISSING_TOKENS:
        return None, None
    if "e+" in key or "e-" in key:
        return None, "规格 ID 为科学计数法，已丢失精度"
    return _from_digit_text(collapsed, raw_display=text)


def _from_digit_text(text: str, *, raw_display: str | None = None) -> tuple[str | None, str | None]:
    if text == "0":
        return None, "规格 ID 无效（0）"
    if _SP_ID_RE.fullmatch(text):
        return text, None
    shown = raw_display if raw_display is not None else text
    return None, f"规格 ID 无效（{shown}）"
