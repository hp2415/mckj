"""微信聊天记录中的系统/群发噪音过滤（画像与增量候选共用）。"""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.sql.elements import ColumnElement

# 微信系统提示：「你通过群发助手，向他(她)发出了一条消息。」
NOISE_CHAT_TEXT_MARKERS = (
    "你通过群发助手",
)


def is_noise_chat_text(text: str | None) -> bool:
    if not text:
        return False
    s = text.strip()
    if not s:
        return False
    return any(marker in s for marker in NOISE_CHAT_TEXT_MARKERS)


def raw_chat_log_meaningful_clause(text_column) -> ColumnElement:
    """SQL：排除群发助手等系统提示，避免污染画像与夜间增量统计。"""
    noise = or_(*[text_column.like(f"%{marker}%") for marker in NOISE_CHAT_TEXT_MARKERS])
    return ~noise
