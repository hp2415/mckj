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
    candidates: list[dict],
    text: str,
    cancel_event: threading.Event,
    progress: RpaProgressDialog | None = None,
    *,
    grace_after_cancel_s: float = 3.0,
    poll_interval_s: float = 0.15,
) -> wechat_rpa_adapter.RpaSendOutcome:
    """在 daemon 线程里执行 RPA 发送，允许用户在卡住时强行返回。"""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[wechat_rpa_adapter.RpaSendOutcome] = loop.create_future()

    def _resolve(value: wechat_rpa_adapter.RpaSendOutcome) -> None:
        if not fut.done():
            fut.set_result(value)

    def _reject(exc: BaseException) -> None:
        if not fut.done():
            fut.set_exception(exc)

    def _on_step(_step_id: str, message: str) -> None:
        if progress is not None:
            loop.call_soon_threadsafe(progress.append_step, message)

    def _on_confirm(message: str) -> bool:
        if progress is None:
            return False
        done = threading.Event()
        answer: list[bool] = [False]

        def _ask() -> None:
            progress.prepare_user_confirm(message, done, answer)

        loop.call_soon_threadsafe(_ask)
        done.wait(timeout=180)
        return bool(answer[0])

    def _worker() -> None:
        try:
            result = wechat_rpa_adapter.send_text_with_candidates(
                candidates,
                text,
                cancel_event,
                on_step=_on_step,
                on_confirm=_on_confirm,
            )
            loop.call_soon_threadsafe(_resolve, result)
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
                    return wechat_rpa_adapter.RpaSendOutcome(False, error="用户中断 RPA")
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

    async def handle_send(
        self,
        msg_id,
        text: str,
        *,
        customer: dict | None = None,
        contact_task: dict | None = None,
    ):
        await self._do_send(
            msg_id,
            text,
            edit_mode=False,
            original_text=text,
            customer=customer,
            contact_task=contact_task,
        )

    async def handle_edit_send(
        self,
        msg_id,
        text: str,
        *,
        customer: dict | None = None,
        contact_task: dict | None = None,
    ):
        if customer is None and getattr(self.app, "_chat_surface_mode", "customer") == "staff":
            self.app.main_win.show_info_bar("warning", "不可用", "自由对话模式下不可发送到微信。")
            return
        cust = customer or getattr(self.app, "_current_customer", None) or {}
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
        await self._do_send(
            msg_id,
            edited,
            edit_mode=True,
            original_text=text or "",
            customer=customer,
            contact_task=contact_task,
        )

    async def _do_send(
        self,
        msg_id,
        text: str,
        *,
        edit_mode: bool,
        original_text: str,
        customer: dict | None = None,
        contact_task: dict | None = None,
    ):
        if customer is None and getattr(self.app, "_chat_surface_mode", "customer") == "staff":
            self.app.main_win.show_info_bar("warning", "不可用", "自由对话模式下不可发送到微信。")
            return

        cust = customer or getattr(self.app, "_current_customer", None)
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
        candidates = data.get("receiver_candidates") or []
        if not candidates and receiver:
            candidates = [{"keyword": receiver, "source": rsrc}]

        if not action_id:
            self.app.main_win.show_info_bar("error", "错误", "服务器未返回动作 ID。")
            self.app.main_win.append_wechat_send_log("[failed] no_action_id")
            return

        cand_hint = " → ".join(
            (c.get("keyword") or "").strip() for c in candidates if (c.get("keyword") or "").strip()
        )
        progress = RpaProgressDialog(
            self.app.main_win,
            title="正在发送到微信",
            detail=f"搜索词顺序：{cand_hint or receiver}",
        )
        progress.show()
        progress.append_step("正在启动微信 RPA…")
        outcome: wechat_rpa_adapter.RpaSendOutcome | None = None
        rpa_exc: Exception | None = None
        try:
            outcome = await _run_rpa_with_cancel(
                candidates,
                text or "",
                progress.cancel_event,
                progress,
            )
        except Exception as e:
            rpa_exc = e
            if not isinstance(e, RuntimeError):
                logger.exception(f"RPA 等待异常: {e}")
        finally:
            user_cancelled = (
                progress.cancel_event.is_set()
                and (outcome is None or not outcome.ok)
            )
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

        outcome = outcome or wechat_rpa_adapter.RpaSendOutcome(False, error="未知错误")
        used_kw = (outcome.receiver_used or receiver).strip()
        used_src = (outcome.receiver_source or rsrc).strip() or "unknown"

        # 2) 发送成功
        if outcome.ok:
            await self.api.report_wechat_outbound_result(
                action_id,
                {"status": "sent", "error": None},
            )
            self.app.main_win.show_info_bar(
                "success",
                "发送成功",
                f"已通过 {used_src}「{used_kw}」确认送达",
            )
            self.app.main_win.append_wechat_send_log(
                f"[sent] via {used_src}: {used_kw}  ({(text or '')[:18]}...)"
            )
            task_for_complete = contact_task
            if not isinstance(task_for_complete, dict):
                task_for_complete = self.app.main_win.pending_wechat_task()
            await self.app._complete_wechat_task_after_send(task_for_complete)
            return

        err_msg = (outcome.error or "").strip() or "微信发送失败"

        # 3) 失败 + 用户取消
        if user_cancelled or err_msg == "用户中断 RPA":
            await self.api.report_wechat_outbound_result(
                action_id,
                {"status": "failed", "error": "用户中断 RPA"},
            )
            self.app.main_win.show_info_bar("warning", "已中断", "已取消本次微信自动化发送。")
            self.app.main_win.append_wechat_send_log(f"[cancelled] via {rsrc}: {receiver}")
            return

        # 4) 失败 + 具体原因
        await self.api.report_wechat_outbound_result(
            action_id,
            {"status": "failed", "error": err_msg},
        )
        self.app.main_win.show_info_bar("error", "发送失败", err_msg)
        self.app.main_win.append_wechat_send_log(
            f"[failed] via {rsrc}: {receiver} — {err_msg[:80]}"
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
