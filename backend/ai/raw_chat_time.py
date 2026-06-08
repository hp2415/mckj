"""raw_chat_logs 时间字段：云客发送时间 vs 平台保存时间。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func
from sqlalchemy.sql.elements import ColumnElement

from ai.chat_log_filter import raw_chat_log_meaningful_clause
from models import RawChatLog

SHANGHAI_TZ = timezone(timedelta(hours=8))


def raw_chat_event_time_ms_expr():
    """
    业务上的「消息发生时间」：优先 send_timestamp_ms（云客 timestamp，发送时间），
    其次 time_ms（云客 time，保存/入库时间），最后历史 timestamp。
    """
    return func.coalesce(
        RawChatLog.send_timestamp_ms,
        RawChatLog.time_ms,
        RawChatLog.timestamp,
    )


def raw_chat_in_event_window_clause(
    since_ms: int,
    until_ms: int,
    *,
    text_column=RawChatLog.text,
) -> ColumnElement:
    """窗口 [since_ms, until_ms) 内、且非群发噪音的有效聊天。"""
    event_ms = raw_chat_event_time_ms_expr()
    return and_(
        event_ms >= since_ms,
        event_ms < until_ms,
        raw_chat_log_meaningful_clause(text_column),
    )


def calendar_day_window_ms(day: datetime | None = None) -> tuple[int, int]:
    """返回 [day 00:00, next-day 00:00) 在 Asia/Shanghai 下的毫秒区间。"""
    base = (day or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    start = base.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def profiled_at_to_ms(profiled_at: datetime | None) -> int:
    """将 SCP.profiled_at 转为毫秒，与 raw_chat_event_time_ms_expr 对齐比较。"""
    if profiled_at is None:
        return 0
    pat = profiled_at
    if pat.tzinfo is None:
        pat = pat.replace(tzinfo=SHANGHAI_TZ)
    else:
        pat = pat.astimezone(SHANGHAI_TZ)
    return int(pat.timestamp() * 1000)


def scp_profiled_at_ms_expr(profiled_at_column):
    """SQL：画像完成时间 → 毫秒（MySQL unix_timestamp）。"""
    return func.unix_timestamp(profiled_at_column) * 1000
