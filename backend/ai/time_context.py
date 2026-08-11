"""话术时间上下文：只提供季节锚点，禁止模型自判时段问候与节气。"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

SHANGHAI_TZ = timezone(timedelta(hours=8))

# 禁止出现在客户话术中的时段问候
FORBIDDEN_PERIOD_GREETINGS: tuple[str, ...] = (
    "早上好",
    "上午好",
    "中午好",
    "下午好",
    "晚上好",
    "早安",
    "晚安",
)

# 二十四节气名：话术中一律不提具体节气
SOLAR_TERM_NAMES: tuple[str, ...] = (
    "小寒",
    "大寒",
    "立春",
    "雨水",
    "惊蛰",
    "春分",
    "清明",
    "谷雨",
    "立夏",
    "小满",
    "芒种",
    "夏至",
    "小暑",
    "大暑",
    "立秋",
    "处暑",
    "白露",
    "秋分",
    "寒露",
    "霜降",
    "立冬",
    "小雪",
    "大雪",
    "冬至",
)

_SEASON_HINTS: dict[str, str] = {
    "春": "春暖花开、春日安好",
    "夏": "夏日炎炎、暑热留意",
    "秋": "秋高气爽、金秋时节",
    "冬": "冬日寒冷、注意保暖",
}

_WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def format_cn_datetime(dt: datetime) -> str:
    """中文日期时间，含星期（便于「下周一」等相对说法换算）。"""
    local = dt
    if local.tzinfo is not None:
        local = local.astimezone(SHANGHAI_TZ)
    return (
        f"{local.strftime('%Y年%m月%d日')} "
        f"{_WEEKDAY_CN[local.weekday()]} "
        f"{local.strftime('%H:%M:%S')}"
    )


def _month_to_season(month: int) -> str:
    if month in (3, 4, 5):
        return "春"
    if month in (6, 7, 8):
        return "夏"
    if month in (9, 10, 11):
        return "秋"
    return "冬"


def _parse_ref_date(text: str | None) -> date | None:
    raw = (text or "").strip()
    if not raw:
        return None
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", raw)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def resolve_now(*, now: datetime | None = None, ref_date_text: str | None = None) -> datetime:
    """优先用显式 now；否则从 ref_date_text 取日期（无钟点则取当日正午）；再否则取上海当前时间。"""
    if now is not None:
        if now.tzinfo is None:
            return now.replace(tzinfo=SHANGHAI_TZ)
        return now.astimezone(SHANGHAI_TZ)
    parsed = _parse_ref_date(ref_date_text)
    if parsed is not None:
        # 仅有日期时不推断时段，钟点固定正午仅供季节判定
        return datetime(parsed.year, parsed.month, parsed.day, 12, 0, 0, tzinfo=SHANGHAI_TZ)
    return datetime.now(SHANGHAI_TZ)


def build_time_anchor(
    *,
    now: datetime | None = None,
    ref_date_text: str | None = None,
) -> dict[str, Any]:
    dt = resolve_now(now=now, ref_date_text=ref_date_text)
    season = _month_to_season(dt.month)
    return {
        "current_date": format_cn_datetime(dt),
        "season_label": season,
        "season_hint": _SEASON_HINTS[season],
        "forbidden_period_greetings": "、".join(FORBIDDEN_PERIOD_GREETINGS),
        "forbidden_solar_terms": "、".join(SOLAR_TERM_NAMES),
    }


def format_script_time_rules(anchor: dict[str, Any] | None = None) -> str:
    """渲染进 system 末尾的硬性规则块（激活/对话话术共用）。"""
    a = anchor or build_time_anchor()
    return (
        "## 话术时间与打招呼硬性规则（必须遵守）\n"
        f"- 当前季节：**{a['season_label']}**（可参考：{a['season_hint']}）。\n"
        "- **打招呼统一用「称呼 + 好」**，例如「王老师好」「李主任好」；"
        "有明确称呼用称呼，没有则用「您好」。\n"
        f"- **禁止**使用时段问候：{a['forbidden_period_greetings']}。\n"
        "- **禁止**提及具体二十四节气名（如大暑、立秋、处暑等），"
        "也勿写「入秋/交节」等节气口吻。\n"
        "- 寒暄可跟季节写一句（如「夏日炎炎，注意防暑」），不要叠时段问候，也不要堆砌多种季节说法。\n"
    )


# 单位性质软分段（与 route_context 学校关键词对齐；供任务分配快照）
_SCHOOL_NEEDLES: tuple[str, ...] = ("学校", "幼儿园", "小学", "中学", "高校", "职高", "教育")


def resolve_unit_segment(
    unit_type: str | None = None,
    unit_name: str | None = None,
) -> str:
    """根据单位性质/名称推断分段；学校优先。"""
    ut = (unit_type or "").strip()
    un = (unit_name or "").strip()
    text = f"{ut}{un}"
    if not text:
        return "unknown"
    if ut == "学校" or any(n in text for n in _SCHOOL_NEEDLES):
        return "school"
    mapping = (
        ("fire", ("消防",)),
        ("tax", ("税务",)),
        ("justice", ("公安", "检察", "法院", "监狱", "派出所", "公检法")),
        ("health", ("医院", "卫生", "疾控", "卫健委", "医疗")),
        ("bank", ("银行",)),
        ("government", ("人民政府", "街道办", "政府")),
    )
    for key, needles in mapping:
        if any(n in text for n in needles):
            return key
    return "other"


def school_season_window(d: date) -> str:
    """
    公历近似学校业务窗口（各地放假略有差异）。
    fall_open / spring_open：开学前约 1–2 周及开学当周；
    deep_summer / deep_winter：深寒暑假；in_term：学期中。
    """
    md = (d.month, d.day)
    if (8, 15) <= md <= (9, 7):
        return "fall_open"
    if (7, 1) <= md <= (8, 14):
        return "deep_summer"
    if (2, 10) <= md <= (3, 5):
        return "spring_open"
    if (1, 10) <= md <= (2, 9):
        return "deep_winter"
    return "in_term"


def is_school_unit(
    *,
    unit_type: str | None = None,
    unit_name: str | None = None,
    unit_segment: str | None = None,
) -> bool:
    if (unit_segment or "").strip() == "school":
        return True
    return resolve_unit_segment(unit_type, unit_name) == "school"


def is_school_defer_window(d: date) -> bool:
    """深寒暑假：学校客户默认不排主线/激活任务。"""
    return school_season_window(d) in ("deep_summer", "deep_winter")


def format_unit_season_context(anchor: dict[str, Any] | None = None) -> str:
    """
    注入当前业务窗口 + 对本窗口的硬性排期要点；细则仍可参考 unit_followup_playbook。
    与客户动态标签无关，也非话术寒暄规则。
    """
    a = anchor or build_time_anchor()
    raw = str(a.get("current_date") or "")
    parsed = _parse_ref_date(raw)
    if parsed is None:
        dt = resolve_now(ref_date_text=raw)
        parsed = dt.date()
    window = school_season_window(parsed)
    window_label = {
        "deep_summer": "深暑假（约 7 月～8 月上旬）",
        "fall_open": "秋季开学窗口（约 8 月中下旬～9 月初，开学前 1–2 周及开学当周）",
        "deep_winter": "深寒假（约 1 月中下旬～2 月初）",
        "spring_open": "春季开学窗口（约 2 月中下旬～3 月初，开学前 1–2 周及开学当周）",
        "in_term": "学期中（非寒暑假深休、非开学冲刺窗）",
    }.get(window, window)

    if window in ("deep_summer", "deep_winter"):
        school_rule = (
            "- **【硬性】学校客户（`unit_type=学校` 或 `unit_segment=school`）本批默认不入选**；"
            "激活与主线均少排/不排。\n"
            "- 例外仅限：ABC=A 且业务紧急、客户已约定今日/近几日回访、"
            "`pending`/`overdue` 未完成、标签强制日触达、画像写明近期要采购。"
            " **勿为凑满 task_cap / icebreaker_cap 用学校客户充数。**\n"
        )
    elif window in ("fall_open", "spring_open"):
        school_rule = (
            "- **【硬性】学校客户在 cap 内提高入选优先级**，可与 ABC/高档标签并列；"
            "动作侧重开学备货、食堂/工会采购节奏。\n"
        )
    else:
        school_rule = (
            "- 学校客户按标签/ABC/`purchase_months`/`suggested_followup_date` 正常排，"
            "勿机械天天联系。\n"
        )

    return (
        "## 当前单位业务窗口（系统日历，非标签；必须遵守）\n"
        f"- 参考日：{parsed.isoformat()}；当前学校业务窗口：**{window_label}**"
        f"（窗口码 `{window}`）。\n"
        f"{school_rule}"
        "- 更细说明见注入文档 **「单位性质跟进策略手册」**（`unit_followup_playbook`）；"
        "手册 **不是** 客户动态标签，勿与 `profile_tags` 混用。\n"
        "- 各地放假略有差异时，以客户画像约定与 `purchase_months` 优先。\n"
    )


def time_context_vars(
    *,
    now: datetime | None = None,
    ref_date_text: str | None = None,
) -> dict[str, str]:
    a = build_time_anchor(now=now, ref_date_text=ref_date_text)
    return {
        "current_date": str(a["current_date"]),
        "season_label": str(a["season_label"]),
        "season_hint": str(a["season_hint"]),
        "time_context": format_script_time_rules(a),
        "unit_season_context": format_unit_season_context(a),
        "forbidden_period_greetings": str(a["forbidden_period_greetings"]),
        "forbidden_solar_terms": str(a["forbidden_solar_terms"]),
    }
