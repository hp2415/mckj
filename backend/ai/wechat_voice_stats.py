"""语音/电话触达聚合：供任务分配使用（渠道无关 summary + 微信语音明细）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_
from sqlalchemy.future import select

from ai.phone_call_profile import phone_match_clauses_for_phones, phone_match_or_clauses, resolve_customer_phones
from models import PhoneCallRecord, RawCustomer, RawCustomerSalesWechat, RawWechatVoiceCall

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


def infer_prefers_voice(leg: dict[str, Any] | None) -> bool:
    """规则派生：单渠道是否倾向语音触达（非分配硬条件）。"""
    if not leg:
        return False
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


def _channel_habit_fragment(
    leg: dict[str, Any] | None,
    *,
    channel_label: str,
    lookback_days: int,
) -> str:
    """单渠道触达摘要片段。"""
    if not leg or not int(leg.get("call_count_90d") or 0):
        return ""
    conn = int(leg.get("connected_count_90d") or 0)
    calls = int(leg.get("call_count_90d") or 0)
    sec = int(leg.get("connected_sec_90d") or 0)
    last_conn = (leg.get("last_connected_date") or "").strip()
    if conn <= 0:
        return f"近{lookback_days}天发起{channel_label}{calls}次均未接通"
    avg = sec // conn if conn else 0
    parts = [
        f"近{lookback_days}天{channel_label}接通{conn}次",
        f"累计{_fmt_duration_sec(sec)}",
    ]
    if avg >= 60:
        parts.append(f"单次约{_fmt_duration_sec(avg)}")
    if last_conn:
        parts.append(f"末次接通{last_conn}")
    return "；".join(parts)


def voice_habit_note(
    wechat_leg: dict[str, Any] | None,
    *,
    mobile_leg: dict[str, Any] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> str:
    """分配/画像用沟通习惯摘要（规则生成，不调用 LLM）。"""
    fragments: list[str] = []
    wx = _channel_habit_fragment(wechat_leg, channel_label="微信语音", lookback_days=lookback_days)
    mob = _channel_habit_fragment(mobile_leg, channel_label="手机直拨", lookback_days=lookback_days)
    if wx:
        fragments.append(wx)
    if mob:
        fragments.append(mob)
    if not fragments:
        return f"近{lookback_days}天无微信语音与手机直拨记录。"
    return "；".join(fragments) + "。"


def _merge_connected_meta(
    wechat_leg: dict[str, Any] | None,
    mobile_leg: dict[str, Any] | None,
    *,
    ref_date: date,
) -> tuple[str, int | None, int, int]:
    """合并两渠道接通日期与计数，供 summary 顶层字段。"""
    dates: list[date] = []
    conn_total = 0
    sec_total = 0
    for leg in (wechat_leg, mobile_leg):
        if not leg:
            continue
        conn_total += int(leg.get("connected_count_90d") or 0)
        sec_total += int(leg.get("connected_sec_90d") or 0)
        d_raw = (leg.get("last_connected_date") or "").strip()
        if d_raw:
            try:
                dates.append(date.fromisoformat(d_raw))
            except ValueError:
                pass
    last_connected = max(dates).isoformat() if dates else ""
    days_since = (ref_date - max(dates)).days if dates else None
    return last_connected, days_since, conn_total, sec_total


def empty_contact_voice_summary(*, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict[str, Any]:
    return build_contact_voice_summary(None, mobile_leg=None, lookback_days=lookback_days)


def build_contact_voice_summary(
    wechat_leg: dict[str, Any] | None,
    *,
    mobile_leg: dict[str, Any] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ref_date: date | None = None,
) -> dict[str, Any]:
    """渠道无关的语音触达摘要（微信内语音 + 手机直拨）。"""
    ref_date = ref_date or date.today()
    sources: list[str] = []
    if wechat_leg and int(wechat_leg.get("call_count_90d") or 0) > 0:
        sources.append(SOURCE_WECHAT_VOICE)
    if mobile_leg and int(mobile_leg.get("call_count_90d") or 0) > 0:
        sources.append(SOURCE_MOBILE_CALL)

    last_connected, days_since_connected, connected_count, connected_sec = _merge_connected_meta(
        wechat_leg, mobile_leg, ref_date=ref_date
    )
    mobile_available = SOURCE_MOBILE_CALL in sources
    prefers = infer_prefers_voice(wechat_leg) or infer_prefers_voice(mobile_leg)
    note = voice_habit_note(wechat_leg, mobile_leg=mobile_leg, lookback_days=lookback_days)

    data_note = ""
    if SOURCE_WECHAT_VOICE in sources and not mobile_available:
        data_note = "当前仅接入微信内语音；手机直拨无记录，勿将微信语音等同于全部电话行为。"

    out: dict[str, Any] = {
        "sources_available": sources,
        "mobile_call_available": mobile_available,
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
    if mobile_leg:
        out["mobile_call"] = mobile_leg
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
        "has_transcript",
        "transcript_count",
        "last_call_gist",
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


def _phone_connected_clause():
    return and_(
        PhoneCallRecord.call_seconds.isnot(None),
        PhoneCallRecord.call_seconds > 0,
    )


async def _load_mobile_call_legs_by_customer(
    db,
    sales_wechat_id: str,
    *,
    ref_date: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, dict[str, Any]]:
    """按客户电话匹配聚合手机直拨触达（近 lookback_days 天）。"""
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return {}
    ref_date = ref_date or date.today()
    since = datetime.combine(ref_date - timedelta(days=max(1, lookback_days)), datetime.min.time())

    phone_match = phone_match_or_clauses(
        PhoneCallRecord.callee,
        RawCustomer.phone,
        RawCustomer.phone_normalized,
        RawCustomerSalesWechat.phone,
    )
    if phone_match is None:
        return {}

    connected = _phone_connected_clause()
    stmt = (
        select(
            RawCustomerSalesWechat.raw_customer_id.label("rid"),
            func.max(PhoneCallRecord.create_time).label("last_call_at"),
            func.max(case((connected, PhoneCallRecord.create_time), else_=None)).label(
                "last_connected_at"
            ),
            func.count(PhoneCallRecord.call_id).label("call_count"),
            func.sum(case((connected, PhoneCallRecord.call_seconds), else_=0)).label(
                "connected_sec"
            ),
            func.sum(case((connected, 1), else_=0)).label("connected_count"),
        )
        .select_from(PhoneCallRecord)
        .join(
            RawCustomerSalesWechat,
            PhoneCallRecord.user_wechat_account == RawCustomerSalesWechat.sales_wechat_id,
        )
        .join(RawCustomer, RawCustomer.id == RawCustomerSalesWechat.raw_customer_id)
        .where(PhoneCallRecord.user_wechat_account == sw)
        .where(PhoneCallRecord.create_time >= since)
        .where(phone_match)
        .group_by(RawCustomerSalesWechat.raw_customer_id)
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


async def _load_mobile_call_leg_for_customer(
    db,
    sales_wechat_id: str,
    raw_customer_id: str,
    *,
    ref_date: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any] | None:
    sw = (sales_wechat_id or "").strip()
    rid = (raw_customer_id or "").strip()
    if not sw or not rid:
        return None
    phones = await resolve_customer_phones(db, rid, sw)
    if not phones:
        return None

    ref_date = ref_date or date.today()
    since = datetime.combine(ref_date - timedelta(days=max(1, lookback_days)), datetime.min.time())

    match_clauses = phone_match_clauses_for_phones(PhoneCallRecord.callee, phones)
    if not match_clauses:
        return None

    connected = _phone_connected_clause()
    stmt = (
        select(
            func.max(PhoneCallRecord.create_time).label("last_call_at"),
            func.max(case((connected, PhoneCallRecord.create_time), else_=None)).label(
                "last_connected_at"
            ),
            func.count(PhoneCallRecord.call_id).label("call_count"),
            func.sum(case((connected, PhoneCallRecord.call_seconds), else_=0)).label(
                "connected_sec"
            ),
            func.sum(case((connected, 1), else_=0)).label("connected_count"),
        )
        .where(PhoneCallRecord.user_wechat_account == sw)
        .where(PhoneCallRecord.create_time >= since)
        .where(or_(*match_clauses))
    )
    row = (await db.execute(stmt)).first()
    if not row or not int(row.call_count or 0):
        return None
    return build_wechat_voice_leg(
        last_call_at=row.last_call_at,
        last_connected_at=row.last_connected_at,
        call_count=int(row.call_count or 0),
        connected_sec=int(row.connected_sec or 0),
        connected_count=int(row.connected_count or 0),
        ref_date=ref_date,
    )


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
    wechat_leg: dict[str, Any] | None = None
    if row and int(row.call_count or 0):
        wechat_leg = build_wechat_voice_leg(
            last_call_at=row.last_call_at,
            last_connected_at=row.last_connected_at,
            call_count=int(row.call_count or 0),
            connected_sec=int(row.connected_sec or 0),
            connected_count=int(row.connected_count or 0),
            ref_date=ref_date,
        )

    mobile_leg = await _load_mobile_call_leg_for_customer(
        db, sw, rid, ref_date=ref_date, lookback_days=lookback_days
    )
    if not wechat_leg and not mobile_leg:
        out = empty_contact_voice_summary(lookback_days=lookback_days)
    else:
        out = build_contact_voice_summary(
            wechat_leg,
            mobile_leg=mobile_leg,
            lookback_days=lookback_days,
            ref_date=ref_date,
        )

    from ai.voice_transcribe_queue import load_transcript_summary_for_customer

    tr = await load_transcript_summary_for_customer(db, sw, rid)
    if tr:
        out.update(tr)
    return out


async def load_contact_voice_summary_by_customer(
    db,
    sales_wechat_id: str,
    *,
    ref_date: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, dict[str, Any]]:
    """按 raw_customer_id 返回 contact_voice_summary（微信语音 + 手机直拨）。"""
    wechat_legs = await _load_wechat_voice_legs_by_customer(
        db, sales_wechat_id, ref_date=ref_date, lookback_days=lookback_days
    )
    mobile_legs = await _load_mobile_call_legs_by_customer(
        db, sales_wechat_id, ref_date=ref_date, lookback_days=lookback_days
    )
    ref_date = ref_date or date.today()
    all_rids = set(wechat_legs) | set(mobile_legs)
    out = {
        rid: build_contact_voice_summary(
            wechat_legs.get(rid),
            mobile_leg=mobile_legs.get(rid),
            lookback_days=lookback_days,
            ref_date=ref_date,
        )
        for rid in all_rids
    }
    sw = (sales_wechat_id or "").strip()
    if sw and out:
        from models import WechatVoiceTranscript
        from ai.voice_transcribe_queue import STATUS_SUCCEEDED

        talkers = list(out.keys())
        stmt = (
            select(
                WechatVoiceTranscript.talker,
                func.count(WechatVoiceTranscript.record_id),
                func.max(WechatVoiceTranscript.call_start_time),
            )
            .where(WechatVoiceTranscript.we_chat_id == sw)
            .where(WechatVoiceTranscript.talker.in_(talkers))
            .where(WechatVoiceTranscript.status == STATUS_SUCCEEDED)
            .group_by(WechatVoiceTranscript.talker)
        )
        counts = {str(r[0]): int(r[1] or 0) for r in (await db.execute(stmt)).all()}
        if counts:
            gist_stmt = (
                select(WechatVoiceTranscript)
                .where(WechatVoiceTranscript.we_chat_id == sw)
                .where(WechatVoiceTranscript.talker.in_(list(counts.keys())))
                .where(WechatVoiceTranscript.status == STATUS_SUCCEEDED)
                .order_by(WechatVoiceTranscript.talker, WechatVoiceTranscript.call_start_time.desc())
            )
            # 每客户取最近一条：在 Python 侧去重
            seen: set[str] = set()
            for row in (await db.execute(gist_stmt)).scalars().all():
                tk = str(row.talker or "")
                if not tk or tk in seen:
                    continue
                seen.add(tk)
                if tk not in out:
                    continue
                t = (row.transcript_text or "").replace("\n", " ").strip()
                gist = (t[:120] + "…") if len(t) > 120 else t
                out[tk].update(
                    {
                        "has_transcript": True,
                        "transcript_count": counts.get(tk, 0),
                        "last_call_gist": gist,
                    }
                )
    return out


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
