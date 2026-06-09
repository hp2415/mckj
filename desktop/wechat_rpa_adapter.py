"""本机微信 RPA 适配：使用项目内嵌 wxrpa 子模块发送文本。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from logger_cfg import logger


@dataclass
class RpaSendOutcome:
    ok: bool
    error: str | None = None
    receiver_used: str | None = None
    receiver_source: str | None = None


StepCallback = Callable[[str, str], None]
ConfirmCallback = Callable[[str], bool]


def _load_controller():
    try:
        from rpa.wxrpa.wechat_controller import wechat  # type: ignore

        return wechat
    except Exception as e:
        logger.error(f"无法加载内置 wxrpa: {e}")
        raise RuntimeError("无法加载微信自动化模块（内置 wxrpa）。") from e


def prepare_wechat_for_rpa() -> bool:
    """UI 主线程在外发 RPA 启动前调用：解除前台锁并预激活微信。"""
    try:
        from rpa.wxrpa.wechat_controller import prepare_wechat_for_rpa as _prep

        return bool(_prep())
    except Exception as e:
        logger.warning(f"预激活微信失败: {e}")
        return False


def send_text_to_contact(
    receiver: str,
    message: str,
    cancel_event: threading.Event | None = None,
    on_step: StepCallback | None = None,
) -> bool:
    """
    调用 RPA 向微信联系人发送文本（单关键词，兼容旧调用）。
    若提供 cancel_event 且在执行中被 set，则尽快中止并返回 False。
    """
    outcome = send_text_with_candidates(
        [{"keyword": receiver, "source": "unknown"}],
        message,
        cancel_event=cancel_event,
        on_step=on_step,
    )
    return outcome.ok


def send_text_with_candidates(
    candidates: list[dict],
    message: str,
    cancel_event: threading.Event | None = None,
    on_step: StepCallback | None = None,
    on_confirm: ConfirmCallback | None = None,
) -> RpaSendOutcome:
    """
    按候选关键词依次切换对话、校验窗口、发送并确认送达。
    返回结构化结果，含失败原因。
    """
    wechat = _load_controller()
    msg = message or ""
    if not msg.strip():
        return RpaSendOutcome(False, error="消息内容为空")
    if not candidates:
        return RpaSendOutcome(False, error="缺少联系人搜索关键词")

    try:
        result = wechat.send_message_with_candidates(
            candidates,
            msg,
            cancel_event=cancel_event,
            on_step=on_step,
            on_confirm=on_confirm,
        )
        if result.ok:
            return RpaSendOutcome(
                True,
                receiver_used=result.receiver_used,
                receiver_source=result.receiver_source,
            )
        if cancel_event is not None and cancel_event.is_set():
            return RpaSendOutcome(False, error="用户中断 RPA")
        return RpaSendOutcome(False, error=result.error or "微信发送失败")
    except Exception as e:
        logger.exception(f"微信 RPA 发送异常: {e}")
        if cancel_event is not None and cancel_event.is_set():
            return RpaSendOutcome(False, error="用户中断 RPA")
        return RpaSendOutcome(False, error=f"RPA 异常: {e}")
