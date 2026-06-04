"""语音/电话触达聚合：供任务分配使用（渠道无关 summary + 微信语音明细）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.future import select

from models import RawWechatVoiceCall

DEFAULT_LOOKBACK_DAYS = 90
SOURCE_WECHAT_VOICE = "wechat_voice"
SOURCE_MOBILE_CALL = "mobile_call"


def _fmt_duration_sec(sec: int) -> str:
    s = max(0, int(sec or 0))
    if s < 60:
        return f"{s}秒"
    m, r = divmod(s, 60)
    if m < 60:
        return f"{m}分{r}秒" if r else f"{m}分钟"
    h, m = divmod(m, 60)
    return f"{h}小时{m}分" if m else f"{h}小时"


def build_wechat_voice_leg(
    *,
    last_call_at: datetime | None,
    last_connected_at: datetime | None,
    call_count: int,
    connected_sec: int,
    connected_count: int,
    ref_date: date,
) -> dict[str, Any]:
    last_d = last_call_at.date() if isinstance(last_call_at, datetime) else None
    last_conn_d = last_connected_at.date() if isinstance(last_connected_at, datetime) else None
    days_since = (ref_date - last_d).days if last_d else None
    days_since_conn = (ref_date - last_conn_d).days if last_conn_d else None
    conn_cnt = int(connected_count or 0)
    sec = int(connected_sec or 0)
    return {
        "last_call_date": last_d.isoformat() if last_d else "",
        "last_connected_date": last_conn_d.isoformat() if last_conn_d else "",
        "days_since_call": days_since,
        "days_since_connected": days_since_conn,
        "call_count_90d": int(call_count or 0),
        "connected_count_90d": conn_cnt,
        "connected_sec_90d": sec,
        "has_connected": conn_cnt > 0,
    }


def infer_prefers_voice(leg: dict[str, Any]) -> bool:
    """规则派生：是否倾向语音触达（非分配硬条件）。"""
    conn = int(leg.get("connected_count_90d") or 0)
    sec = int(leg.get("connected_sec_90d") or 0)
    days_conn = leg.get("days_since_connected")
    if conn >= 2:
        return True
    if conn >= 1 and sec >= 120:
        return True
    if conn >= 1 and isinstance(days_conn, int) and days_conn <= 21:
        return True
    return False


def voice_habit_note(leg: dict[str, Any] | None, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> str:
    """分配前一句沟通习惯摘要（规则生成，不调用 LLM）。"""
    if not leg or not int(leg.get("call_count_90d") or 0):
        return f"近{lookback_days}天无微信语音记录；手机直拨数据未接入，不能推断是否曾手机通话。"
    conn = int(leg.get("connected_count_90d") or 0)
    calls = int(leg.get("call_count_90d") or 0)
    sec = int(leg.get("connected_sec_90d") or 0)
    last_conn = (leg.get("last_connected_date") or "").strip()
    if conn <= 0:
        return (
            f"近{lookback_days}天发起微信语音{calls}次均未接通；"
            "更适合微信文字轻触达，不宜强推电话。"
        )
    avg = sec // conn if conn else 0
    parts = [
        f"近{lookback_days}天微信语音接通{conn}次",
        f"累计{_fmt_duration_sec(sec)}",
    ]
    if avg >= 60:
        parts.append(f"单次约{_fmt_duration_sec(avg)}")
    if last_conn:
        parts.append(f"末次接通{last_conn}")
    parts.append("手机直拨数据未接入")
    return "；".join(parts) + "。"


def empty_contact_voice_summary(*, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict[str, Any]:
    return build_contact_voice_summary(None, lookback_days=lookback_days)


def build_contact_voice_summary(
    wechat_leg: dict[str, Any] | None,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """
    渠道无关的语音触达摘要。当前仅 wechat_voice 有数据；mobile_call 预留。
    """
    sources: list[str] = []
    if wechat_leg and int(wechat_leg.get("call_count_90d") or 0) > 0:
        sources.append(SOURCE_WECHAT_VOICE)

    last_connected = ""
    days_since_connected = None
    connected_count = 0
    connected_sec = 0
    if wechat_leg:
        last_connected = (wechat_leg.get("last_connected_date") or "").strip()
        days_since_connected = wechat_leg.get("days_since_connected")
        connected_count = int(wechat_leg.get("connected_count_90d") or 0)
        connected_sec = int(wechat_leg.get("connected_sec_90d") or 0)

    prefers = infer_prefers_voice(wechat_leg or {})
    note = voice_habit_note(wechat_leg, lookback_days=lookback_days)

    if SOURCE_MOBILE_CALL not in sources:
        data_note = "当前仅接入微信内语音；手机直拨通话尚未接入，勿将微信语音等同于全部电话行为。"
    else:
        data_note = ""

    out: dict[str, Any] = {
        "sources_available": sources,
        "mobile_call_available": False,
        "lookback_days": lookback_days,
        "last_connected_date": last_connected,
        "days_since_connected": days_since_connected,
        "connected_count_90d": connected_count,
        "connected_sec_90d": connected_sec,
        "prefers_voice": prefers,
        "habit_note": note,
        "data_note": data_note,
    }
    if wechat_leg:
        out["wechat_voice"] = wechat_leg
    return out


def compact_contact_voice_for_feature(summary: dict[str, Any] | None) -> dict[str, Any]:
    """CustomerFeature.recency 用紧凑字段。"""
    if not summary:
        return {}
    keys = (
        "sources_available",
        "last_connected_date",
        "days_since_connected",
        "connected_count_90d",
        "connected_sec_90d",
        "prefers_voice",
        "habit_note",
    )
    compact = {
        k: summary[k]
        for k in keys
        if summary.get(k) not in (None, "", [], 0, False)
    }
    # prefers_voice=False 仍有信息价值，保留
    if "prefers_voice" in summary:
        compact["prefers_voice"] = bool(summary["prefers_voice"])
    return compact


async def _load_wechat_voice_legs_by_customer(
    db,
    sales_wechat_id: str,
    *,
    ref_date: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, dict[str, Any]]:
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return {}
    ref_date = ref_date or date.today()
    since = datetime.combine(ref_date - timedelta(days=max(1, lookback_days)), datetime.min.time())

    stmt = (
        select(
            RawWechatVoiceCall.talker.label("rid"),
            func.max(RawWechatVoiceCall.start_time).label("last_call_at"),
            func.max(
                case(
                    (RawWechatVoiceCall.call_status == 1, RawWechatVoiceCall.start_time),
                    else_=None,
                )
            ).label("last_connected_at"),
            func.count(RawWechatVoiceCall.record_id).label("call_count"),
            func.sum(
                case(
                    (RawWechatVoiceCall.call_status == 1, RawWechatVoiceCall.duration_file),
                    else_=0,
                )
            ).label("connected_sec"),
            func.sum(case((RawWechatVoiceCall.call_status == 1, 1), else_=0)).label("connected_count"),
        )
        .where(RawWechatVoiceCall.we_chat_id == sw)
        .where(RawWechatVoiceCall.is_room == 0)
        .where(RawWechatVoiceCall.call_type == 1)
        .where(RawWechatVoiceCall.start_time >= since)
        .group_by(RawWechatVoiceCall.talker)
    )
    rows = (await db.execute(stmt)).all()
    out: dict[str, dict[str, Any]] = {}
    for rid, last_at, last_conn, cnt, sec, conn_cnt in rows:
        rid_s = (rid or "").strip()
        if not rid_s:
            continue
        out[rid_s] = build_wechat_voice_leg(
            last_call_at=last_at,
            last_connected_at=last_conn,
            call_count=int(cnt or 0),
            connected_sec=int(sec or 0),
            connected_count=int(conn_cnt or 0),
            ref_date=ref_date,
        )
    return out


async def load_contact_voice_summary_for_customer(
    db,
    sales_wechat_id: str,
    raw_customer_id: str,
    *,
    ref_date: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """单客户 contact_voice_summary（画像/分配共用）。"""
    sw = (sales_wechat_id or "").strip()
    rid = (raw_customer_id or "").strip()
    if not sw or not rid:
        return empty_contact_voice_summary(lookback_days=lookback_days)
    ref_date = ref_date or date.today()
    since = datetime.combine(ref_date - timedelta(days=max(1, lookback_days)), datetime.min.time())

    stmt = (
        select(
            func.max(RawWechatVoiceCall.start_time).label("last_call_at"),
            func.max(
                case(
                    (RawWechatVoiceCall.call_status == 1, RawWechatVoiceCall.start_time),
                    else_=None,
                )
            ).label("last_connected_at"),
            func.count(RawWechatVoiceCall.record_id).label("call_count"),
            func.sum(
                case(
                    (RawWechatVoiceCall.call_status == 1, RawWechatVoiceCall.duration_file),
                    else_=0,
                )
            ).label("connected_sec"),
            func.sum(case((RawWechatVoiceCall.call_status == 1, 1), else_=0)).label("connected_count"),
        )
        .where(RawWechatVoiceCall.we_chat_id == sw)
        .where(RawWechatVoiceCall.talker == rid)
        .where(RawWechatVoiceCall.is_room == 0)
        .where(RawWechatVoiceCall.call_type == 1)
        .where(RawWechatVoiceCall.start_time >= since)
    )
    row = (await db.execute(stmt)).first()
    if not row or not int(row.call_count or 0):
        return empty_contact_voice_summary(lookback_days=lookback_days)
    leg = build_wechat_voice_leg(
        last_call_at=row.last_call_at,
        last_connected_at=row.last_connected_at,
        call_count=int(row.call_count or 0),
        connected_sec=int(row.connected_sec or 0),
        connected_count=int(row.connected_count or 0),
        ref_date=ref_date,
    )
    return build_contact_voice_summary(leg, lookback_days=lookback_days)


async def load_contact_voice_summary_by_customer(
    db,
    sales_wechat_id: str,
    *,
    ref_date: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, dict[str, Any]]:
    """按 raw_customer_id 返回 contact_voice_summary（仅有微信语音数据的客户）。"""
    legs = await _load_wechat_voice_legs_by_customer(
        db, sales_wechat_id, ref_date=ref_date, lookback_days=lookback_days
    )
    return {
        rid: build_contact_voice_summary(leg, lookback_days=lookback_days)
        for rid, leg in legs.items()
    }


async def load_wechat_voice_stats_by_customer(
    db,
    sales_wechat_id: str,
    *,
    ref_date: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, dict[str, Any]]:
    """
    兼容旧字段名（wechat_voice_*）。新代码请用 load_contact_voice_summary_by_customer。
    """
    legs = await _load_wechat_voice_legs_by_customer(
        db, sales_wechat_id, ref_date=ref_date, lookback_days=lookback_days
    )
    out: dict[str, dict[str, Any]] = {}
    for rid, leg in legs.items():
        out[rid] = {
            "last_wechat_voice_call_date": leg.get("last_call_date") or "",
            "last_wechat_voice_connected_date": leg.get("last_connected_date") or "",
            "days_since_wechat_voice_call": leg.get("days_since_call"),
            "days_since_wechat_voice_connected": leg.get("days_since_connected"),
            "wechat_voice_calls_90d": leg.get("call_count_90d"),
            "wechat_voice_connected_90d": leg.get("connected_count_90d"),
            "wechat_voice_connected_sec_90d": leg.get("connected_sec_90d"),
            "has_wechat_voice_contact": bool(leg.get("has_connected")),
        }
    return out
