"""米城主系统 MiBuddy API 客户端（客资认领/收藏等联动）。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import httpx

from core.logger import logger

MIBUDDY_SUCCESS_CODE = 10000

_COLOR_LABELS = {
    "gray": "灰色",
    "grey": "灰色",
    "red": "红色",
    "blue": "蓝色",
    "orange": "橙色",
    "green": "绿色",
}

_BUYER_TYPE_LABELS = {
    1: "食堂",
    2: "工会",
    3: "食堂+工会",
    4: "其他",
}

_TAG_LABELS = {
    "20": "20不反感可跟进",
    "30": "30本月内采购",
    "40": "40本周内采购",
    "60": "60选定商品待下单",
    "80": "80已下单待发货",
    "e1": "停机",
    "e2": "暂停服务",
    "e3": "负责人更换",
    "e4": "拒绝",
    "e5": "未接通",
}


class MibuddyConfigError(Exception):
    """MiBuddy 服务未配置或配置不完整。"""


class MibuddyApiError(Exception):
    """MiBuddy 接口返回业务错误。"""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


def _credentials() -> tuple[str, str]:
    base = (os.getenv("MIBUDDY_BASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("MIBUDDY_API_KEY") or "").strip()
    return base, key


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }


def _is_success_body(body: dict) -> bool:
    code = body.get("code")
    msg = str(body.get("message") or "").strip().lower()
    if code in (0, MIBUDDY_SUCCESS_CODE):
        return True
    if msg == "success":
        return True
    return False


async def _request_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    base, key = _credentials()
    if not base or not key:
        raise MibuddyConfigError("MiBuddy API 未配置，请在 .env 中设置 MIBUDDY_BASE_URL 与 MIBUDDY_API_KEY")

    url = f"{base}{path}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=_headers(key))
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPStatusError as e:
        logger.warning("MiBuddy HTTP 错误 %s %s: %s", path, e.response.status_code, e.response.text[:500])
        raise MibuddyApiError(f"MiBuddy 请求失败: HTTP {e.response.status_code}") from e
    except httpx.RequestError as e:
        logger.warning("MiBuddy 网络错误 %s: %s", path, e)
        raise MibuddyApiError(f"无法连接 MiBuddy 服务: {e}") from e
    except ValueError as e:
        raise MibuddyApiError("MiBuddy 返回非 JSON 响应") from e

    if not isinstance(body, dict):
        raise MibuddyApiError("MiBuddy 响应格式异常")

    if not _is_success_body(body):
        msg = str(body.get("message") or "未知错误")
        code = body.get("code")
        raise MibuddyApiError(msg, code=int(code) if code is not None else None)
    return body


async def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = await _request_json(path, payload)
    data = body.get("data")
    if not isinstance(data, dict):
        raise MibuddyApiError("MiBuddy 响应缺少 data 字段")
    return data


async def _post_command(path: str, payload: dict[str, Any]) -> None:
    await _request_json(path, payload)


_BUYER_TYPE_REVERSE = {
    "食堂": 1,
    "工会": 2,
    "工会+食堂": 3,
    "其他": 4,
}

_COLOR_LABEL_TO_API: dict[str, str | None] = {
    "灰色": None,
    "红色": "red",
    "蓝色": "blue",
    "橙色": "orange",
    "绿色": "green",
}

_TAG_LABEL_TO_CODE = {label: code for code, label in _TAG_LABELS.items()}


def _parse_budget(value: Any) -> int | None:
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("，", "").replace("元", "").strip()
    if not s or s == "待设置":
        return None
    try:
        num = int(float(s))
        return max(0, min(num, 10_000_000))
    except (TypeError, ValueError):
        return None


def _parse_buy_month(value: Any) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "待设置":
        return None
    s = s.replace("，", ",").split(",")[0].strip().replace("月", "").strip()
    try:
        month = int(s)
        if 1 <= month <= 12:
            return month
    except (TypeError, ValueError):
        pass
    return None


def _tag_display_to_api(value: Any) -> str | None:
    s = str(value or "").strip()
    if not s or s == "待设置":
        return None
    if s in _TAG_LABEL_TO_CODE:
        return _TAG_LABEL_TO_CODE[s]
    for code, label in _TAG_LABELS.items():
        if s == label or s.startswith(code):
            return code
    return s


def _color_display_to_api(value: Any) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    if s in _COLOR_LABEL_TO_API:
        return _COLOR_LABEL_TO_API[s]
    key = s.lower()
    if key in _COLOR_LABELS:
        return key if key in {"red", "blue", "orange", "green"} else None
    return s


def _buyer_type_display_to_api(value: Any) -> int | None:
    s = str(value or "").strip()
    if not s or s == "待设置":
        return None
    if s in _BUYER_TYPE_REVERSE:
        return _BUYER_TYPE_REVERSE[s]
    try:
        n = int(s)
        if 1 <= n <= 4:
            return n
    except (TypeError, ValueError):
        pass
    return None


def _followup_display_to_api(value: Any) -> str | None:
    s = str(value or "").strip()
    if not s or s == "待设置":
        return None
    return s[:19] if len(s) >= 19 else s


def build_update_info_from_form(form: dict[str, Any]) -> dict[str, Any]:
    """将桌面详情表单字段转换为主系统 update_my_lead_info.info 结构。"""
    info: dict[str, Any] = {}

    budget = _parse_budget(form.get("budget"))
    if budget is not None:
        info["budget"] = budget

    buy_month = _parse_buy_month(form.get("purchase_month"))
    if buy_month is not None:
        info["buy_month"] = buy_month

    buyer_type = _buyer_type_display_to_api(form.get("purchase_type"))
    if buyer_type is not None:
        info["buyer_type"] = buyer_type

    if "is_favorite" in form:
        info["collected"] = 1 if form.get("is_favorite") else None

    if "color" in form:
        info["color"] = _color_display_to_api(form.get("color"))

    tag = _tag_display_to_api(form.get("tags"))
    if tag is not None:
        info["tag"] = tag

    if "followup_time" in form:
        info["recall_time"] = _followup_display_to_api(form.get("followup_time"))

    wechat = str(form.get("wechat_id") or "").strip()
    if wechat and wechat != "待设置":
        info["wechat"] = wechat
    elif "wechat_id" in form:
        info["wechat"] = wechat or None

    return info


def _calc_recycle_days(recycle_time: str | None) -> str:
    if not recycle_time:
        return "-"
    try:
        dt = datetime.strptime(str(recycle_time)[:19], "%Y-%m-%d %H:%M:%S")
        days = (dt - datetime.now()).days
        return f"{max(days, 0)}天"
    except Exception:
        return "-"


def _format_budget(value: Any) -> str:
    if value is None or value == "":
        return "待设置"
    try:
        num = int(float(value))
        return f"{num:,} 元"
    except (TypeError, ValueError):
        return str(value)


def _format_buy_month(value: Any) -> str:
    if value is None or value == "":
        return "待设置"
    try:
        month = int(value)
        if 1 <= month <= 12:
            return f"{month}月"
    except (TypeError, ValueError):
        pass
    return str(value)


def _format_color(value: Any) -> str:
    if value is None or value == "":
        return "灰色"
    key = str(value).strip().lower()
    return _COLOR_LABELS.get(key, str(value))


def _format_tag(value: Any) -> str:
    if value is None or value == "":
        return "待设置"
    key = str(value).strip()
    return _TAG_LABELS.get(key, key)


def _format_buyer_type(value: Any) -> str:
    if value is None or value == "":
        return "待设置"
    try:
        n = int(value)
        return _BUYER_TYPE_LABELS.get(n, str(value))
    except (TypeError, ValueError):
        s = str(value).strip()
        return s or "待设置"


def _map_lead_core(row: dict[str, Any]) -> dict[str, Any]:
    province = str(row.get("province") or "").strip()
    city = str(row.get("city") or "").strip()
    county = str(row.get("county") or "").strip()
    region_parts = [p for p in (province, city, county) if p]
    region = " / ".join(region_parts)
    collected = str(row.get("collected") or "").strip()
    log_text = row.get("log")
    followup_records = []
    if log_text:
        followup_records.append(
            {"time": row.get("operate_time") or collected or "-", "content": str(log_text)}
        )
    return {
        "id": row.get("id"),
        "unit_name": str(row.get("client_name") or "").strip() or "未知单位",
        "customer_name": str(row.get("contacts") or "").strip() or "未知",
        "phone": str(row.get("tel") or "").strip(),
        "region": region,
        "tags": _format_tag(row.get("tag")),
        "color": _format_color(row.get("color")),
        "budget": _format_budget(row.get("budget")),
        "followup_time": str(row.get("recall_time") or "").strip() or "待设置",
        "wechat_id": str(row.get("wechat") or "").strip() or "待设置",
        "purchase_month": _format_buy_month(row.get("buy_month")),
        "purchase_type": _format_buyer_type(row.get("buyer_type")),
        "last_call_time": "-",
        "followup_records": followup_records,
        "remarks": str(row.get("remarks") or "").strip(),
    }


def map_lead_item_for_desktop(row: dict[str, Any]) -> dict[str, Any]:
    """将主系统 my_leads 单条记录映射为桌面客资卡片字段。"""
    item = _map_lead_core(row)
    collected = str(row.get("collected") or "").strip()
    item.update(
        {
            "allocation_time": str(row.get("assign_time") or "").strip() or "-",
            "recycle_days": _calc_recycle_days(row.get("recycle_time")),
            "is_favorite": bool(collected),
            "favorite_time": collected or "-",
        }
    )
    return item


def map_album_lead_item_for_desktop(row: dict[str, Any]) -> dict[str, Any]:
    """将主系统 my_leads_album 单条记录映射为桌面收藏客资卡片字段。"""
    item = _map_lead_core(row)
    collected = str(row.get("collected") or "").strip()
    item.update(
        {
            "allocation_time": "-",
            "recycle_days": "-",
            "is_favorite": True,
            "favorite_time": collected or "-",
        }
    )
    return item


def parse_changhu_phones(data: dict[str, Any]) -> list[str]:
    """从主系统用户信息解析畅呼号码（兼容 changhu 列表与 changhu_tel JSON 字符串）。"""
    if not isinstance(data, dict):
        return []
    raw = data.get("changhu")
    if isinstance(raw, list):
        return [str(p).strip() for p in raw if str(p).strip()]
    tel_field = data.get("changhu_tel")
    if tel_field is None:
        return []
    if isinstance(tel_field, list):
        return [str(p).strip() for p in tel_field if str(p).strip()]
    text = str(tel_field).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(p).strip() for p in parsed if str(p).strip()]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return [text]


async def fetch_uuid_user_info(user_uuid: str) -> dict[str, Any]:
    """通过用户 UUID 查询主系统基本信息（绑定前校验 / 展示用）。"""
    uuid = (user_uuid or "").strip()
    if not uuid:
        raise MibuddyApiError("UUID 不能为空")
    return await _post("/uuid_user_info", {"uuid": uuid})


async def fetch_my_leads(
    user_uuid: str,
    *,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """查询用户认领客资列表（分页）。"""
    uuid = (user_uuid or "").strip()
    if not uuid:
        raise MibuddyApiError("UUID 不能为空")
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 50)))
    return await _post(
        "/my_leads",
        {"uuid": uuid, "page": page, "page_size": page_size},
    )


async def fetch_my_leads_album(
    user_uuid: str,
    *,
    page: int = 1,
    page_size: int = 50,
    client_name: str | None = None,
) -> dict[str, Any]:
    """查询用户收藏客资列表（分页）；client_name 为单位名称关键词。"""
    uuid = (user_uuid or "").strip()
    if not uuid:
        raise MibuddyApiError("UUID 不能为空")
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 50)))
    payload: dict[str, Any] = {"uuid": uuid, "page": page, "page_size": page_size}
    keyword = (client_name or "").strip()
    if keyword:
        payload["client_name"] = keyword
    return await _post("/my_leads_album", payload)


async def update_my_lead_info(
    user_uuid: str,
    lead_id: int,
    info: dict[str, Any],
) -> None:
    """更新用户认领/收藏客资的可编辑字段。"""
    uuid = (user_uuid or "").strip()
    if not uuid:
        raise MibuddyApiError("UUID 不能为空")
    try:
        lid = int(lead_id)
    except (TypeError, ValueError) as e:
        raise MibuddyApiError("lead_id 无效") from e
    if lid <= 0:
        raise MibuddyApiError("lead_id 无效")
    if not isinstance(info, dict):
        raise MibuddyApiError("info 不能为空")
    await _post_command(
        "/update_my_lead_info",
        {"uuid": uuid, "lead_id": lid, "info": info},
    )


def map_remark_item_for_desktop(row: dict[str, Any]) -> dict[str, Any]:
    """将主系统备注列表单条记录映射为桌面跟进记录字段。"""
    text = row.get("remark")
    if text is None or str(text).strip() == "":
        text = row.get("remarks")
    if text is None or str(text).strip() == "":
        text = row.get("content")
    if text is None or str(text).strip() == "":
        text = row.get("log")
    return {
        "id": row.get("id"),
        "remark": str(text or "").strip(),
        "create_time": str(row.get("create_time") or row.get("time") or "").strip(),
    }


async def fetch_my_leads_remarks(
    user_uuid: str,
    lead_id: int,
    *,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """查询用户对某客资的历史跟进备注（分页）。"""
    uuid = (user_uuid or "").strip()
    if not uuid:
        raise MibuddyApiError("UUID 不能为空")
    try:
        lid = int(lead_id)
    except (TypeError, ValueError) as e:
        raise MibuddyApiError("lead_id 无效") from e
    if lid <= 0:
        raise MibuddyApiError("lead_id 无效")
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    return await _post(
        "/my_leads_remarks",
        {"uuid": uuid, "lead_id": lid, "page": page, "page_size": page_size},
    )


async def add_remark_to_leads(
    user_uuid: str,
    lead_id: int,
    remark: str,
) -> dict[str, Any]:
    """向客资添加跟进备注。"""
    uuid = (user_uuid or "").strip()
    if not uuid:
        raise MibuddyApiError("UUID 不能为空")
    try:
        lid = int(lead_id)
    except (TypeError, ValueError) as e:
        raise MibuddyApiError("lead_id 无效") from e
    if lid <= 0:
        raise MibuddyApiError("lead_id 无效")
    text = (remark or "").strip()
    if not text:
        raise MibuddyApiError("备注内容不能为空")
    if len(text) > 500:
        raise MibuddyApiError("备注内容不能超过 500 字")
    return await _post(
        "/add_remark_to_leads",
        {"uuid": uuid, "lead_id": lid, "remark": text},
    )


async def call_changhu(
    user_uuid: str,
    *,
    tel: str | None = None,
    lead_id: int | None = None,
    changhu_tel: str | None = None,
    user_wechat_account: str | None = None,
) -> dict[str, Any]:
    """用畅呼发起外呼；tel 与 lead_id 至少提供一个。"""
    uuid = (user_uuid or "").strip()
    if not uuid:
        raise MibuddyApiError("UUID 不能为空")
    phone = (tel or "").strip()
    lid: int | None = None
    if lead_id is not None:
        try:
            lid = int(lead_id)
        except (TypeError, ValueError) as e:
            raise MibuddyApiError("lead_id 无效") from e
        if lid <= 0:
            raise MibuddyApiError("lead_id 无效")
    if not phone and lid is None:
        raise MibuddyApiError("请提供被叫号码或客资 ID")
    caller = (changhu_tel or "").strip()
    if not caller:
        raise MibuddyApiError("请选择畅呼主叫号码")
    payload: dict[str, Any] = {"uuid": uuid, "changhu_tel": caller}
    if phone:
        payload["tel"] = phone
    if lid is not None:
        payload["lead_id"] = lid
    account = (user_wechat_account or "").strip()
    if account:
        payload["user_wechat_account"] = account
    return await _post("/call_changhu", payload)


async def call_yunke(
    user_uuid: str,
    *,
    tel: str | None = None,
    lead_id: int | None = None,
    user_wechat_account: str | None = None,
) -> dict[str, Any]:
    """用云客发起外呼；tel 与 lead_id 至少提供一个。"""
    uuid = (user_uuid or "").strip()
    if not uuid:
        raise MibuddyApiError("UUID 不能为空")
    phone = (tel or "").strip()
    lid: int | None = None
    if lead_id is not None:
        try:
            lid = int(lead_id)
        except (TypeError, ValueError) as e:
            raise MibuddyApiError("lead_id 无效") from e
        if lid <= 0:
            raise MibuddyApiError("lead_id 无效")
    if not phone and lid is None:
        raise MibuddyApiError("请提供被叫号码或客资 ID")
    payload: dict[str, Any] = {"uuid": uuid}
    if phone:
        payload["tel"] = phone
    if lid is not None:
        payload["lead_id"] = lid
    account = (user_wechat_account or "").strip()
    if account:
        payload["user_wechat_account"] = account
    return await _post("/call_yunke", payload)


async def approve_tel(user_uuid: str, lead_id: int) -> None:
    """发起查看客资完整电话的审批申请。"""
    uuid = (user_uuid or "").strip()
    if not uuid:
        raise MibuddyApiError("UUID 不能为空")
    try:
        lid = int(lead_id)
    except (TypeError, ValueError) as e:
        raise MibuddyApiError("lead_id 无效") from e
    if lid <= 0:
        raise MibuddyApiError("lead_id 无效")
    await _post_command("/approval_tel", {"uuid": uuid, "lead_id": lid})


async def ignore_my_lead(user_uuid: str, lead_id: int) -> None:
    """用户移除(忽略)待拨打的客资。"""
    uuid = (user_uuid or "").strip()
    if not uuid:
        raise MibuddyApiError("UUID 不能为空")
    try:
        lid = int(lead_id)
    except (TypeError, ValueError) as e:
        raise MibuddyApiError("lead_id 无效") from e
    if lid <= 0:
        raise MibuddyApiError("lead_id 无效")
    await _post_command("/ignore_my_lead", {"uuid": uuid, "lead_id": lid})
