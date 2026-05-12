"""AI 气泡「发微信 / 编辑发送」编排：本机声明校验 → 后端审计 → RPA 发送 → 结果回写。"""

from __future__ import annotations

import asyncio
import threading
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from logger_cfg import logger
from ui.local_wechat_claim_dialog import LocalWechatClaimDialog
from ui.rpa_progress_dialog import RpaProgressDialog
from ui.wechat_send_dialog import WechatSendEditDialog
import wechat_rpa_adapter


async def _run_rpa_with_cancel(
    receiver: str,
    text: str,
    cancel_event: threading.Event,
    *,
    grace_after_cancel_s: float = 3.0,
    poll_interval_s: float = 0.15,
) -> bool:
    """在 daemon 线程里执行 RPA 发送，允许用户在卡住时强行返回。

    与 ``asyncio.to_thread`` 的关键差异：
    - 工作线程标记为 daemon，进程退出时不会等待它结束，避免微信 UIA 调用
      卡在 COM 里时把整个 Python 进程拖死。
    - 异步侧每 ``poll_interval_s`` 检查一次 cancel_event；用户点击「中断」或
      关闭弹窗后，最多再等 ``grace_after_cancel_s`` 秒让线程自然退出，
      超过则放弃等待并返回，让 UI 立刻恢复响应。
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[bool] = loop.create_future()

    def _resolve(value: bool) -> None:
        if not fut.done():
            fut.set_result(value)

    def _reject(exc: BaseException) -> None:
        if not fut.done():
            fut.set_exception(exc)

    def _worker() -> None:
        try:
            result = wechat_rpa_adapter.send_text_to_contact(receiver, text, cancel_event)
            loop.call_soon_threadsafe(_resolve, bool(result))
        except BaseException as e:  # noqa: BLE001 — 必须把所有异常带回主线程
            loop.call_soon_threadsafe(_reject, e)

    threading.Thread(target=_worker, name="wechat-rpa-send", daemon=True).start()

    cancel_seen_at: float | None = None
    while True:
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=poll_interval_s)
        except asyncio.TimeoutError:
            if cancel_event.is_set():
                if cancel_seen_at is None:
                    cancel_seen_at = 0.0
                cancel_seen_at += poll_interval_s
                if cancel_seen_at >= grace_after_cancel_s:
                    logger.warning(
                        f"RPA 工作线程在用户中断后 {grace_after_cancel_s}s 未能自然退出，"
                        f"放弃等待并恢复 UI（线程将作为 daemon 在后台自行收尾）。"
                    )
                    return False
            continue


ACTIVE_LOCAL_KEY = "active_local_sales_wechat"


async def _exec_dialog_async(dlg: QDialog) -> int:
    """非阻塞地显示模态对话框并等待用户关闭。

    避免在 qasync 协程中调用 QDialog.exec() —— 那会启动嵌套 Qt 事件循环，
    触发 qasync 重入其它待执行任务，抛出 "Cannot enter into task" RuntimeError。
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[int] = loop.create_future()

    def _on_finished(result: int) -> None:
        if not fut.done():
            fut.set_result(int(result))

    dlg.finished.connect(_on_finished)
    dlg.setModal(True)
    dlg.setAttribute(Qt.WA_DeleteOnClose, False)
    dlg.open()
    try:
        return await fut
    finally:
        try:
            dlg.finished.disconnect(_on_finished)
        except (TypeError, RuntimeError):
            pass


class WechatSendHandler:
    def __init__(self, app_controller, api_client):
        self.app = app_controller
        self.api = api_client

    def _load_active_local(self) -> str | None:
        st = self.api.storage.load_json(ACTIVE_LOCAL_KEY) if self.api.storage else None
        if isinstance(st, dict):
            s = (st.get("sales_wechat_id") or "").strip()
            return s or None
        return None

    def _save_active_local(self, sales_wechat_id: str) -> None:
        if not self.api.storage:
            return
        self.api.storage.save_json(ACTIVE_LOCAL_KEY, {"sales_wechat_id": (sales_wechat_id or "").strip()})

    async def _fetch_bindings(self) -> list:
        rows = await self.api.list_sales_wechats()
        return rows or []

    async def _open_claim_dialog(self, rows: list, preferred: str | None = None) -> str | None:
        dlg = LocalWechatClaimDialog(self.app.main_win, rows=rows, preferred_sales_wechat_id=preferred)
        try:
            result = await _exec_dialog_async(dlg)
            if result != QDialog.Accepted:
                return None
            sw = dlg.selected_sales_wechat_id()
            if sw:
                self._save_active_local(sw)
            return sw
        finally:
            dlg.deleteLater()

    async def _ensure_active_matches_session(self, session_sw: str) -> str | None:
        """返回与 session 一致的已声明 sales_wechat_id；必要时弹窗。"""
        session_sw = (session_sw or "").strip()
        if not session_sw:
            self.app.main_win.show_info_bar("warning", "无法发送", "当前客户行缺少销售微信号。")
            return None

        active = self._load_active_local()
        if active == session_sw:
            return active

        rows = await self._fetch_bindings()
        if not rows:
            self.app.main_win.show_info_bar(
                "warning", "未绑定销售微信",
                "请先在设置页绑定销售微信号。",
            )
            return None

        self.app.main_win.show_info_bar(
            "info",
            "请声明本机微信",
            "请选择与本客户会话一致的销售微信号。",
        )
        picked = await self._open_claim_dialog(rows, preferred=session_sw)
        if not picked:
            return None
        if picked != session_sw:
            self.app.main_win.show_info_bar(
                "warning", "仍不一致",
                f"本机需选择当前客户对应的销售微信：{session_sw[:16]}…",
            )
            return None
        return picked

    async def handle_send(self, msg_id, text: str):
        await self._do_send(msg_id, text, edit_mode=False, original_text=text)

    async def handle_edit_send(self, msg_id, text: str):
        if getattr(self.app, "_chat_surface_mode", "customer") == "staff":
            self.app.main_win.show_info_bar("warning", "不可用", "自由对话模式下不可发送到微信。")
            return
        cust = getattr(self.app, "_current_customer", None) or {}
        rcid = str(cust.get("id") or "").strip()
        ssw = str(cust.get("sales_wechat_id") or "").strip()
        name_hint = (cust.get("wechat_remark") or cust.get("customer_name") or "") or ""
        phone_hint = str(cust.get("phone") or "")

        dlg = WechatSendEditDialog(
            self.app.main_win,
            original_text=text or "",
            summary_lines=[
                f"客户：{name_hint or rcid}  {phone_hint}".strip(),
                "编辑完成后确认，将通过本机微信 RPA 发送。",
            ],
        )
        try:
            result = await _exec_dialog_async(dlg)
            if result != QDialog.Accepted:
                return
            edited = dlg.edited_text()
        finally:
            dlg.deleteLater()
        if not edited:
            self.app.main_win.show_info_bar("warning", "内容为空", "请输入要发送的文本。")
            return
        await self._do_send(msg_id, edited, edit_mode=True, original_text=text or "")

    async def _do_send(self, msg_id, text: str, *, edit_mode: bool, original_text: str):
        if getattr(self.app, "_chat_surface_mode", "customer") == "staff":
            self.app.main_win.show_info_bar("warning", "不可用", "自由对话模式下不可发送到微信。")
            return

        cust = getattr(self.app, "_current_customer", None)
        if not cust:
            self.app.main_win.show_info_bar("warning", "未选客户", "请先选择客户。")
            return

        raw_cid = str(cust.get("id") or "").strip()
        session_sw = str(cust.get("sales_wechat_id") or "").strip()
        if not raw_cid or not session_sw:
            self.app.main_win.show_info_bar("warning", "无法发送", "缺少客户 ID 或销售微信号。")
            return

        active = await self._ensure_active_matches_session(session_sw)
        if not active:
            return

        action_type = "edit_send" if edit_mode else "send"
        sid = None
        if msg_id is not None:
            try:
                sid = int(msg_id)
            except (TypeError, ValueError):
                sid = None
        body = {
            "raw_customer_id": raw_cid,
            "sales_wechat_id": session_sw,
            "claimed_local_sales_wechat_id": active,
            "action_type": action_type,
            "edited_text": (text or "").strip(),
            "original_text": (original_text if edit_mode else text) or "",
            "source_chat_message_id": sid,
        }

        resp = await self.api.create_wechat_outbound_action(body)
        if not resp or resp.get("code") != 200:
            msg = (resp or {}).get("message") or "创建审计失败"
            self.app.main_win.show_info_bar("error", "发送被拒", str(msg))
            self.app.main_win.append_wechat_send_log(f"[blocked] create_failed: {msg}")
            logger.warning(f"outbound create failed: {resp}")
            return

        data = (resp or {}).get("data") or {}
        action_id = data.get("id")
        receiver = (data.get("receiver") or "").strip()
        rsrc = (data.get("receiver_source") or "").strip() or "unknown"

        if not action_id:
            self.app.main_win.show_info_bar("error", "错误", "服务器未返回动作 ID。")
            self.app.main_win.append_wechat_send_log("[failed] no_action_id")
            return

        progress = RpaProgressDialog(
            self.app.main_win,
            title="正在发送到微信",
            detail=f"接收方：{receiver}",
        )
        progress.show()
        ok = False
        rpa_exc: Exception | None = None
        try:
            ok = await _run_rpa_with_cancel(
                receiver,
                text or "",
                progress.cancel_event,
            )
        except Exception as e:
            rpa_exc = e
            if not isinstance(e, RuntimeError):
                logger.exception(f"RPA 等待异常: {e}")
        finally:
            # 在关弹窗前先快照 cancel 状态，避免 closeEvent / mark_completed
            # 之间任何边角时序把成功发送误标记为「用户中断」。
            user_cancelled = progress.cancel_event.is_set() and not ok
            try:
                progress.mark_completed()
            except Exception:
                pass
            try:
                progress.close()
            except Exception:
                pass

        # 1) RPA 抛异常：直接报失败
        if rpa_exc is not None:
            err = str(rpa_exc)
            title = "RPA 失败" if isinstance(rpa_exc, RuntimeError) else "RPA 异常"
            tag = "rpa_error" if isinstance(rpa_exc, RuntimeError) else "rpa_exception"
            await self.api.report_wechat_outbound_result(
                action_id,
                {"status": "failed", "error": err},
            )
            self.app.main_win.show_info_bar("error", title, err)
            self.app.main_win.append_wechat_send_log(f"[failed] {tag}: {err[:120]}")
            return

        # 2) 发送成功 —— 即便 cancel_event 在最后一刻被置上，也优先按成功处理
        if ok:
            await self.api.report_wechat_outbound_result(
                action_id,
                {"status": "sent", "error": None},
            )
            self.app.main_win.show_info_bar("success", "已发起发送", f"接收方搜索：{receiver}")
            self.app.main_win.append_wechat_send_log(
                f"[sent] via {rsrc}: {receiver}  ({(text or '')[:18]}...)"
            )
            return

        # 3) 失败 + 用户取消（cancel 是失败的因）
        if user_cancelled:
            await self.api.report_wechat_outbound_result(
                action_id,
                {"status": "failed", "error": "用户中断 RPA"},
            )
            self.app.main_win.show_info_bar("warning", "已中断", "已取消本次微信自动化发送。")
            self.app.main_win.append_wechat_send_log(f"[cancelled] via {rsrc}: {receiver}")
            return

        # 4) 失败 + 非用户取消（微信端原因）
        err_msg = "微信发送未确认成功，请检查微信窗口与联系人"
        await self.api.report_wechat_outbound_result(
            action_id,
            {"status": "failed", "error": err_msg},
        )
        self.app.main_win.show_info_bar("warning", "发送可能失败", err_msg)
        self.app.main_win.append_wechat_send_log(
            f"[failed] via {rsrc}: {receiver}  ({(text or '')[:18]}...)"
        )

    async def open_claim_dialog_manual(self):
        """设置页「声明本机微信」：写入 SecureStorage，供发微信串号校验。"""
        pref = None
        cust = getattr(self.app, "_current_customer", None)
        if cust:
            pref = str(cust.get("sales_wechat_id") or "").strip() or None
        rows = await self._fetch_bindings()
        if not rows:
            self.app.main_win.show_info_bar("warning", "无绑定", "请先在上方绑定销售微信号。")
            return
        picked = await self._open_claim_dialog(rows, preferred=pref)
        if picked:
            self.app.main_win.show_info_bar(
                "success",
                "已声明本机微信",
                "发送时将校验与客户会话的销售微信号一致。",
            )
