"""本机微信 RPA 适配：使用项目内嵌 wxrpa 子模块发送文本。"""
from __future__ import annotations

import os
import sys

from logger_cfg import logger


def _load_controller():
    try:
        # vendored module under desktop/rpa/wxrpa
        from rpa.wxrpa.wechat_controller import wechat  # type: ignore
        return wechat
    except Exception as e:
        logger.error(f"无法加载内置 wxrpa: {e}")
        raise RuntimeError("无法加载微信自动化模块（内置 wxrpa）。") from e


def send_text_to_contact(receiver: str, message: str) -> bool:
    """
    调用 RPA 向微信联系人发送文本。阻塞直到返回（RPA 内部可能较慢）。
    """
    wechat = _load_controller()
    recv = (receiver or "").strip()
    msg = message or ""
    if not recv or not msg.strip():
        return False
    try:
        ok = bool(wechat.send_message(recv, msg))
        return ok
    except Exception as e:
        logger.exception(f"微信 RPA 发送异常: {e}")
        return False

