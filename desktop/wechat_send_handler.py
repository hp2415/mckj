"""AI 气泡「发微信 / 编辑发送」编排：本机声明校验 → 后端审计 → RPA 发送 → 结果回写。"""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import tempfile
import threading
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QToolTip

from logger_cfg import logger
from ui.campaign_poster_dialog import CampaignPosterPreviewDialog
from ui.local_wechat_claim_dialog import LocalWechatClaimDialog
from ui.rpa_progress_dialog import RpaProgressDialog
from ui.wechat_send_dialog import WechatSendEditDialog
from utils import mask_phone
import wechat_rpa_adapter


async def _run_rpa_with_cancel(
    candidates: list[dict],
    text: str,
    cancel_event: threading.Event,
    progress: RpaProgressDialog | None = None,
    *,
    image_paths: list[str] | None = None,
    grace_after_cancel_s: float = 3.0,
    poll_interval_s: float = 0.15,
    on_thread_started=None,
    allow_user_confirm: bool = True,
) -> wechat_rpa_adapter.RpaSendOutcome:
    """在 daemon 线程里执行 RPA 发送，允许用户在卡住时强行返回。

    allow_user_confirm=False 时（群发）：窗口未正确跳转直接失败返回，
    不弹出「手动确认跳转」，便于批量继续下一条。
    """
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

    confirm_cb = _on_confirm if allow_user_confirm else None

    def _worker() -> None:
        try:
            result = wechat_rpa_adapter.send_text_with_candidates(
                candidates,
                text,
                cancel_event,
                on_step=_on_step,
                on_confirm=confirm_cb,
                image_paths=image_paths,
            )
            loop.call_soon_threadsafe(_resolve, result)
        except BaseException as e:  # noqa: BLE001 — 必须把所有异常带回主线程
            loop.call_soon_threadsafe(_reject, e)

    thread = threading.Thread(target=_worker, name="wechat-rpa-send", daemon=True)
    if on_thread_started is not None:
        try:
            on_thread_started(thread)
        except Exception:
            pass
    thread.start()

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
                        f"放弃等待并恢复 UI（线程将作为 daemon 在后台自行收尾；"
                        f"完成前将阻止新的外发）。"
                    )
                    return wechat_rpa_adapter.RpaSendOutcome(False, error="用户中断 RPA")
            continue


ACTIVE_LOCAL_KEY = "active_local_sales_wechat"


def _is_staff_surface(app) -> bool:
    """是否处于「自由对话」界面。以主窗口可见态为准，避免与 app 状态短暂不同步。"""
    mw = getattr(app, "main_win", None)
    if mw is not None:
        mode = getattr(mw, "_chat_surface_mode", None)
        if mode in ("staff", "customer"):
            return mode == "staff"
    return getattr(app, "_chat_surface_mode", "customer") == "staff"


async def _exec_dialog_async(dlg: QDialog) -> int:
    """非阻塞地显示模态对话框并等待用户关闭。

    避免在 qasync 协程中调用 QDialog.exec() —— 那会启动嵌套 Qt 事件循环，
    触发 qasync 重入其它待执行任务，抛出 "Cannot enter into task" RuntimeError。
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[int] = loop.create_future()

    def _on_finished(result: int = 0) -> None:
        if not fut.done():
            fut.set_result(int(result))

    def _on_destroyed(_obj=None) -> None:
        # 防止弹窗异常销毁时 future 永不结束，拖住进程退出
        if not fut.done():
            fut.set_result(int(QDialog.Rejected))

    dlg.finished.connect(_on_finished)
    dlg.destroyed.connect(_on_destroyed)
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
        try:
            dlg.destroyed.disconnect(_on_destroyed)
        except (TypeError, RuntimeError):
            pass


class WechatSendHandler:
    def __init__(self, app_controller, api_client):
        self.app = app_controller
        self.api = api_client
        # 防连点/双开：协程占用标记 + 未退出的 RPA 工作线程
        self._send_busy = False
        self._rpa_thread: threading.Thread | None = None

    def _is_send_in_flight(self) -> bool:
        if self._send_busy:
            return True
        t = self._rpa_thread
        return t is not None and t.is_alive()

    def _warn_send_busy(self) -> None:
        self.app.main_win.show_info_bar(
            "warning",
            "正在发送",
            "当前已有微信外发进行中，请等待完成或中断后再试。",
        )

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

    def _attach_cached_poster(self, item: dict) -> dict:
        """浅拷贝活动项，并尽量附上本地缓存路径（同步、不打网络）。"""
        row = dict(item or {})
        poster = row.get("next_poster") or {}
        image_path = str(poster.get("image_path") or "").strip()
        if not image_path:
            return row
        cached = self.api.get_cached_media_path(image_path)
        if cached:
            row["_local_image"] = cached
        return row

    async def _ensure_campaign_local_image(self, camp: dict) -> str | None:
        """确保活动项具备可用的本地海报路径（写回 camp['_local_image']）。"""
        if not camp:
            return None
        existing = str(camp.get("_local_image") or "").strip()
        if existing and os.path.isfile(existing) and os.path.getsize(existing) > 0:
            return existing
        poster = camp.get("next_poster") or {}
        image_path = str(poster.get("image_path") or "").strip()
        if not image_path:
            return None
        try:
            local = await self.api.ensure_media_local(image_path)
        except Exception as e:
            logger.warning(f"准备活动海报失败: {e}")
            return None
        if not local:
            # 无用户缓存目录时退回临时文件
            try:
                suffix = Path(image_path).suffix or ".jpg"
                fd, tmp = tempfile.mkstemp(prefix="camp_poster_", suffix=suffix)
                os.close(fd)
                ok = await self.api.download_media(image_path, tmp)
                if not ok:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    return None
                camp["_local_image"] = tmp
                camp["_local_image_is_tmp"] = True
                return tmp
            except Exception as e:
                logger.warning(f"准备活动海报临时文件失败: {e}")
                return None
        camp["_local_image"] = local
        camp.pop("_local_image_is_tmp", None)
        return local

    async def _materialize_campaign_posters(
        self, with_poster: list[dict]
    ) -> tuple[list[dict], list[str]]:
        """并行物化海报到本地；优先磁盘缓存，缓存文件不进入待清理列表。"""
        tmp_files: list[str] = []
        rows = [self._attach_cached_poster(x) for x in (with_poster or [])]

        async def _one(row: dict) -> dict | None:
            local = await self._ensure_campaign_local_image(row)
            if not local:
                return None
            if row.get("_local_image_is_tmp"):
                tmp_files.append(local)
            return row

        results = await asyncio.gather(
            *[_one(r) for r in rows],
            return_exceptions=True,
        )
        ready: list[dict] = []
        for res in results:
            if isinstance(res, Exception):
                logger.warning(f"准备活动海报失败: {res}")
                continue
            if res:
                ready.append(res)
        return ready, tmp_files

    async def _prefetch_missing_posters(self, campaigns: list[dict]) -> None:
        """弹窗已打开后后台补齐未缓存海报（就地写回共享 dict）。"""
        missing = [
            c
            for c in (campaigns or [])
            if c
            and not (
                str(c.get("_local_image") or "").strip()
                and os.path.isfile(str(c.get("_local_image")))
            )
        ]
        if not missing:
            return
        await asyncio.gather(
            *[self._ensure_campaign_local_image(c) for c in missing],
            return_exceptions=True,
        )

    async def _load_campaign_metadata(
        self, rcid: str, ssw: str
    ) -> list[dict]:
        """只拉活动元数据并附上缓存命中，不阻塞下载。"""
        try:
            resp = await self.api.list_active_campaigns(rcid, sales_wechat_id=ssw)
        except Exception as e:
            logger.warning(f"拉取匹配活动失败: {e}")
            return []
        if not resp or resp.get("code") != 200:
            return []
        items = list(((resp.get("data") or {}).get("items")) or [])
        with_poster = [x for x in items if (x or {}).get("next_poster")]
        return [self._attach_cached_poster(x) for x in with_poster]

    def _cleanup_tmp_files(self, paths: list[str], *, warn: str = "") -> None:
        still = self._rpa_thread is not None and self._rpa_thread.is_alive()
        if still:
            logger.warning(warn or "临时文件暂不删除：RPA 线程仍在收尾")
            return
        for path in paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    async def handle_edit_send(
        self,
        msg_id,
        text: str,
        *,
        customer: dict | None = None,
        contact_task: dict | None = None,
    ):
        if self._is_send_in_flight():
            self._warn_send_busy()
            return
        if customer is None and _is_staff_surface(self.app):
            self.app.main_win.show_info_bar("warning", "不可用", "自由对话模式下不可发送到微信。")
            return
        cust = customer or getattr(self.app, "_current_customer", None) or {}
        rcid = str(cust.get("id") or "").strip()
        ssw = str(cust.get("sales_wechat_id") or "").strip()
        name_hint = (cust.get("wechat_remark") or cust.get("customer_name") or "") or ""
        phone_hint = mask_phone(str(cust.get("phone") or ""))

        history_items: list[dict] = []
        history_scope = ""
        ready: list[dict] = []
        tmp_files: list[str] = []
        prefetch_task: asyncio.Task | None = None
        try:
            if rcid:
                coros: list = [
                    self.api.list_wechat_outbound_actions(
                        raw_customer_id=rcid,
                        limit=20,
                    )
                ]
                if ssw:
                    # 只拉元数据 + 本地缓存命中，避免下载阻塞弹窗打开
                    coros.append(self._load_campaign_metadata(rcid, ssw))
                results = await asyncio.gather(*coros, return_exceptions=True)
                hist_resp = results[0]
                if isinstance(hist_resp, Exception):
                    logger.warning(f"拉取外发历史失败（不影响编辑发送）: {hist_resp}")
                elif hist_resp and hist_resp.get("code") == 200:
                    data = hist_resp.get("data") or {}
                    history_items = list(data.get("list") or [])
                    history_scope = str(data.get("scope") or "customer")
                if ssw and len(results) > 1:
                    camp_res = results[1]
                    if isinstance(camp_res, Exception):
                        logger.warning(f"拉取匹配活动失败（不影响编辑发送）: {camp_res}")
                    else:
                        ready = list(camp_res or [])

            # 弹窗期间后台预取未缓存海报（与对话框共享 dict 引用）
            if ready:
                prefetch_task = asyncio.create_task(self._prefetch_missing_posters(ready))

            dlg = WechatSendEditDialog(
                self.app.main_win,
                original_text=text or "",
                summary_lines=[
                    f"客户：{name_hint or rcid}  {phone_hint}".strip(),
                    "编辑完成后确认，将通过本机微信 RPA 发送。",
                ],
                history_items=history_items,
                history_scope=history_scope,
                campaigns=ready,
            )
            try:
                result = await _exec_dialog_async(dlg)
                if result != QDialog.Accepted:
                    return
                edited = dlg.edited_text()
                selected = dlg.selected_campaign() if dlg.attach_poster() else None
            finally:
                dlg.deleteLater()
            if not edited:
                self.app.main_win.show_info_bar("warning", "内容为空", "请输入要发送的文本。")
                return
            # 编辑弹窗期间可能另起了直发；确认后再拦一次
            if self._is_send_in_flight():
                self._warn_send_busy()
                return

            campaign_id = None
            poster_id = None
            image_paths = None
            if selected:
                # 与 ready 中共享项对齐，便于写回预取结果
                selected_id = selected.get("id")
                for row in ready:
                    if row.get("id") == selected_id:
                        selected = row
                        break
                if prefetch_task is not None and not prefetch_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(prefetch_task), timeout=25.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                local_image = await self._ensure_campaign_local_image(selected)
                if selected.get("_local_image_is_tmp") and local_image:
                    tmp_files.append(local_image)
                poster = selected.get("next_poster") or {}
                try:
                    campaign_id = int(selected.get("id"))
                    poster_id = int(poster.get("id"))
                except (TypeError, ValueError):
                    self.app.main_win.show_info_bar("error", "无法发送", "活动或海报信息不完整。")
                    return
                if not local_image or not os.path.exists(local_image):
                    self.app.main_win.show_info_bar(
                        "error", "无法发送", "海报仍在加载或已失效，请稍后重试。"
                    )
                    return
                suffix = Path(local_image).suffix or ".jpg"
                fd, rpa_image = tempfile.mkstemp(prefix="camp_poster_rpa_", suffix=suffix)
                os.close(fd)
                shutil.copyfile(local_image, rpa_image)
                tmp_files.append(rpa_image)
                image_paths = [rpa_image]

            await self._do_send(
                msg_id,
                edited,
                edit_mode=True,
                original_text=text or "",
                customer=customer,
                contact_task=contact_task,
                campaign_id=campaign_id,
                poster_id=poster_id,
                image_paths=image_paths,
            )
        finally:
            if prefetch_task is not None and not prefetch_task.done():
                prefetch_task.cancel()
                try:
                    await prefetch_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._cleanup_tmp_files(tmp_files, warn="海报临时文件暂不删除：RPA 线程仍在收尾")

    async def handle_poster_send(
        self,
        msg_id,
        text: str,
        *,
        customer: dict | None = None,
        contact_task: dict | None = None,
    ):
        if self._is_send_in_flight():
            self._warn_send_busy()
            return
        if customer is None and _is_staff_surface(self.app):
            self.app.main_win.show_info_bar("warning", "不可用", "自由对话模式下不可发送到微信。")
            return
        cust = customer or getattr(self.app, "_current_customer", None) or {}
        rcid = str(cust.get("id") or "").strip()
        ssw = str(cust.get("sales_wechat_id") or "").strip()
        if not rcid or not ssw:
            self.app.main_win.show_info_bar("warning", "无法发送", "缺少客户 ID 或销售微信号。")
            return

        try:
            resp = await self.api.list_active_campaigns(rcid, sales_wechat_id=ssw)
        except Exception as e:
            logger.warning(f"拉取匹配活动失败: {e}")
            resp = None
        if not resp or resp.get("code") != 200:
            msg = (resp or {}).get("message") or "无法读取当前活动"
            self.app.main_win.show_info_bar("error", "无法外发海报", str(msg))
            return
        items = list(((resp.get("data") or {}).get("items")) or [])
        with_poster = [x for x in items if (x or {}).get("next_poster")]
        if not items:
            self.app.main_win.show_info_bar(
                "warning", "暂无活动", "当前客户没有匹配的进行中活动。"
            )
            return
        if not with_poster:
            self.app.main_win.show_info_bar(
                "warning", "暂无海报", "匹配的活动还没有可外发的海报。"
            )
            return

        tmp_files: list[str] = []
        prefetch_task: asyncio.Task | None = None
        try:
            # 优先本地缓存秒开；未命中的在预览弹窗打开后后台补齐
            ready = [self._attach_cached_poster(x) for x in with_poster]
            if not ready:
                self.app.main_win.show_info_bar(
                    "error", "海报加载失败", "无法准备活动海报，请稍后重试。"
                )
                return
            prefetch_task = asyncio.create_task(self._prefetch_missing_posters(ready))

            name_hint = (cust.get("wechat_remark") or cust.get("customer_name") or "") or ""
            phone_hint = mask_phone(str(cust.get("phone") or ""))
            QToolTip.hideText()
            dlg = CampaignPosterPreviewDialog(
                self.app.main_win,
                campaigns=ready,
                customer_hint=(
                    f"客户：{name_hint or rcid}  {phone_hint}".strip()
                    + "\n确认后将通过本机微信发送海报，不附带文字。"
                ),
            )
            try:
                result = await _exec_dialog_async(dlg)
                if result != QDialog.Accepted:
                    return
                selected = dlg.selected_campaign()
            finally:
                dlg.deleteLater()

            if self._is_send_in_flight():
                self._warn_send_busy()
                return
            selected_id = selected.get("id")
            for row in ready:
                if row.get("id") == selected_id:
                    selected = row
                    break
            if prefetch_task is not None and not prefetch_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(prefetch_task), timeout=25.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            local_image = await self._ensure_campaign_local_image(selected)
            if selected.get("_local_image_is_tmp") and local_image:
                tmp_files.append(local_image)
            poster = selected.get("next_poster") or {}
            try:
                campaign_id = int(selected.get("id"))
                poster_id = int(poster.get("id"))
            except (TypeError, ValueError):
                self.app.main_win.show_info_bar("error", "无法发送", "活动或海报信息不完整。")
                return
            if not local_image or not os.path.exists(local_image):
                self.app.main_win.show_info_bar(
                    "error", "无法发送", "海报仍在加载或已失效，请稍后重试。"
                )
                return
            suffix = Path(local_image).suffix or ".jpg"
            fd, rpa_image = tempfile.mkstemp(prefix="camp_poster_rpa_", suffix=suffix)
            os.close(fd)
            shutil.copyfile(local_image, rpa_image)
            tmp_files.append(rpa_image)
            await self._do_send(
                msg_id,
                "",
                edit_mode=False,
                original_text="",
                customer=customer,
                contact_task=contact_task,
                action_type="poster_send",
                campaign_id=campaign_id,
                poster_id=poster_id,
                image_paths=[rpa_image],
            )
        finally:
            if prefetch_task is not None and not prefetch_task.done():
                prefetch_task.cancel()
                try:
                    await prefetch_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._cleanup_tmp_files(tmp_files, warn="海报临时文件暂不删除：RPA 线程仍在收尾")

    async def _do_send(
        self,
        msg_id,
        text: str,
        *,
        edit_mode: bool,
        original_text: str,
        customer: dict | None = None,
        contact_task: dict | None = None,
        action_type: str | None = None,
        campaign_id: int | None = None,
        poster_id: int | None = None,
        image_paths: list[str] | None = None,
    ):
        # 协程调度窗口内的二次进入（双击 create_task）在此拦截
        if self._is_send_in_flight():
            self._warn_send_busy()
            return
        self._send_busy = True
        try:
            await self._do_send_locked(
                msg_id,
                text,
                edit_mode=edit_mode,
                original_text=original_text,
                customer=customer,
                contact_task=contact_task,
                action_type=action_type,
                campaign_id=campaign_id,
                poster_id=poster_id,
                image_paths=image_paths,
            )
        finally:
            self._send_busy = False

    async def _do_send_locked(
        self,
        msg_id,
        text: str,
        *,
        edit_mode: bool,
        original_text: str,
        customer: dict | None = None,
        contact_task: dict | None = None,
        action_type: str | None = None,
        campaign_id: int | None = None,
        poster_id: int | None = None,
        image_paths: list[str] | None = None,
    ):
        if customer is None and _is_staff_surface(self.app):
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

        resolved_type = (action_type or "").strip() or ("edit_send" if edit_mode else "send")
        is_poster = resolved_type == "poster_send"
        has_images = bool(image_paths)
        rpa_text = "" if is_poster else (text or "")
        audit_text = "（活动海报）" if is_poster else (text or "").strip()
        sid = None
        if msg_id is not None:
            try:
                sid = int(msg_id)
            except (TypeError, ValueError):
                sid = None
        task_id = None
        if isinstance(contact_task, dict) and contact_task.get("id") is not None:
            try:
                task_id = int(contact_task.get("id"))
            except (TypeError, ValueError):
                task_id = None
        body = {
            "raw_customer_id": raw_cid,
            "sales_wechat_id": session_sw,
            "claimed_local_sales_wechat_id": active,
            "action_type": resolved_type,
            "edited_text": audit_text,
            "original_text": (
                original_text if (edit_mode or is_poster) else text
            ) or "",
            "source_chat_message_id": sid,
            "source_contact_task_id": task_id,
        }
        if campaign_id is not None:
            body["campaign_id"] = int(campaign_id)
        if poster_id is not None:
            body["poster_id"] = int(poster_id)

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
        if is_poster:
            progress_title = "正在发送活动海报"
        elif has_images:
            progress_title = "正在发送文字与活动图片"
        else:
            progress_title = "正在发送到微信"
        progress = RpaProgressDialog(
            self.app.main_win,
            title=progress_title,
            detail=f"搜索词顺序：{cand_hint or receiver}",
        )
        progress.show()
        progress.append_step("正在启动微信 RPA…")
        progress.append_step("关键操作期间将短暂屏蔽键鼠，请勿切换窗口")
        # RPA 期间暂停窗口吸附，避免 250ms FindWindow 与 UIA 抢主线程
        main_win = self.app.main_win
        if main_win is not None and hasattr(main_win, "pause_snap_for_rpa"):
            try:
                main_win.pause_snap_for_rpa()
            except Exception:
                pass
        outcome: wechat_rpa_adapter.RpaSendOutcome | None = None
        rpa_exc: Exception | None = None
        try:
            outcome = await _run_rpa_with_cancel(
                candidates,
                rpa_text,
                progress.cancel_event,
                progress,
                image_paths=image_paths,
                on_thread_started=lambda t: setattr(self, "_rpa_thread", t),
            )
        except Exception as e:
            rpa_exc = e
            if not isinstance(e, RuntimeError):
                logger.exception(f"RPA 等待异常: {e}")
        finally:
            if main_win is not None and hasattr(main_win, "resume_snap_after_rpa"):
                try:
                    main_win.resume_snap_after_rpa()
                except Exception:
                    pass
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
            if is_poster:
                ok_extra = "发送活动海报"
                log_extra = "  poster"
            elif has_images:
                ok_extra = "确认送达（含活动图片）"
                log_extra = f"  text+poster ({(text or '')[:18]}...)"
            else:
                ok_extra = "确认送达"
                log_extra = f"  ({(text or '')[:18]}...)"
            self.app.main_win.show_info_bar(
                "success",
                "发送成功",
                f"已通过 {used_src}「{used_kw}」{ok_extra}",
            )
            self.app.main_win.append_wechat_send_log(
                f"[sent] via {used_src}: {used_kw}{log_extra}"
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
            still = self._rpa_thread is not None and self._rpa_thread.is_alive()
            tip = (
                "已取消本次微信自动化发送；后台收尾未完成前请勿再次外发。"
                if still
                else "已取消本次微信自动化发送。"
            )
            self.app.main_win.show_info_bar("warning", "已中断", tip)
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

    async def handle_campaign_blast_send(self, job: dict):
        """活动群发：逐个发送个性化话术 + 活动海报，失败跳过，可取消。"""
        if self._is_send_in_flight():
            self._warn_send_busy()
            return
        if not isinstance(job, dict):
            return

        page = getattr(self.app.main_win, "campaign_blast_page", None)
        job_id = int(job.get("id") or 0)
        campaign_id = int(job.get("campaign_id") or 0)
        sales_sw = str(job.get("sales_wechat_id") or "").strip()
        if not job_id or not campaign_id or not sales_sw:
            self.app.main_win.show_info_bar("warning", "无法发送", "群发任务信息不完整。")
            return

        active = await self._ensure_active_matches_session(sales_sw)
        if not active:
            return

        only_ids = job.get("_recipient_ids")
        id_set = {int(x) for x in (only_ids or []) if int(x) > 0} if only_ids else None

        start_resp = await self.api.start_campaign_blast_sending(job_id)
        if not start_resp or start_resp.get("code") != 200:
            msg = (start_resp or {}).get("message") or "无法锁定海报"
            self.app.main_win.show_info_bar("error", "发送被拒", str(msg))
            return
        job = (start_resp.get("data") or {}) if isinstance(start_resp.get("data"), dict) else job

        queue = [
            r
            for r in (job.get("recipients") or [])
            if (r.get("status") or "") in ("pending", "failed")
            and (r.get("script_text") or "").strip()
            and r.get("poster_id")
            and (id_set is None or int(r.get("id") or 0) in id_set)
        ]
        if not queue:
            self.app.main_win.show_info_bar("warning", "无可发送", "请先生成话术并确保活动有海报。")
            return

        self._send_busy = True
        if page is not None:
            page.set_sending(True)
            page._cancel_flag = False

        total = len(queue)
        success_n = 0
        failed_n = 0
        poster_cache: dict[str, str] = {}
        progress: RpaProgressDialog | None = None
        cancel_event = threading.Event()
        main_win = self.app.main_win
        try:
            progress = RpaProgressDialog(
                self.app.main_win,
                title="正在群发到微信",
                detail=f"共 {total} 人待发送",
            )
            progress.show()
            progress.append_step("正在启动微信 RPA…")
            progress.append_step("关键操作期间将短暂屏蔽键鼠，请勿切换窗口")
            progress.set_batch_progress(0, total, success=0, failed=0)
            cancel_event = progress.cancel_event
            if main_win is not None and hasattr(main_win, "pause_snap_for_rpa"):
                try:
                    main_win.pause_snap_for_rpa()
                except Exception:
                    pass

            for idx, rec in enumerate(queue, start=1):
                if page is not None and page.is_send_cancelled():
                    cancel_event.set()
                if cancel_event.is_set():
                    if page is not None:
                        page._cancel_flag = True
                    progress.append_step("用户请求中断，停止后续发送")
                    break

                rid = str(rec.get("raw_customer_id") or "").strip()
                recipient_id = int(rec.get("id") or 0)
                text = (rec.get("script_text") or "").strip()
                poster_id = int(rec.get("poster_id") or 0)
                poster_path = (rec.get("poster_image_path") or "").strip()
                display_name = str(rec.get("display_name") or rec.get("remark") or rid).strip()
                if not rid or not recipient_id or not text or not poster_id:
                    failed_n += 1
                    progress.append_step(f"{display_name or rid or recipient_id}：缺少话术或海报，已跳过")
                    progress.set_batch_progress(
                        idx, total, success=success_n, failed=failed_n, current_name=display_name
                    )
                    await self.api.ack_campaign_blast_recipient(
                        job_id,
                        recipient_id,
                        success=False,
                        error_message="缺少话术或海报",
                    )
                    continue

                local_image = poster_cache.get(poster_path)
                if not local_image and poster_path:
                    progress.append_step(f"正在准备海报：{display_name}")
                    local_image = await self.api.ensure_media_local(poster_path)
                    if local_image:
                        poster_cache[poster_path] = local_image
                image_paths = [local_image] if local_image else None
                if not image_paths:
                    failed_n += 1
                    progress.append_step(f"{display_name}：海报下载失败")
                    progress.set_batch_progress(
                        idx, total, success=success_n, failed=failed_n, current_name=display_name
                    )
                    await self.api.ack_campaign_blast_recipient(
                        job_id,
                        recipient_id,
                        success=False,
                        error_message="海报下载失败",
                    )
                    continue

                if page is not None:
                    page.set_send_progress(
                        current=idx,
                        total=total,
                        success=success_n,
                        failed=failed_n,
                        message=f"正在发送 {display_name or rid} ({idx}/{total})",
                    )
                progress.set_batch_progress(
                    idx, total, success=success_n, failed=failed_n, current_name=display_name
                )
                progress.append_step(f"开始发送 {display_name}（{idx}/{total}）")

                body = {
                    "raw_customer_id": rid,
                    "sales_wechat_id": sales_sw,
                    "claimed_local_sales_wechat_id": active,
                    "action_type": "edit_send",
                    "edited_text": text,
                    "original_text": text,
                    "campaign_id": campaign_id,
                    "poster_id": poster_id,
                }
                resp = await self.api.create_wechat_outbound_action(body)
                if not resp or resp.get("code") != 200:
                    failed_n += 1
                    err = (resp or {}).get("message") or "创建审计失败"
                    progress.append_step(f"{display_name}：{err}")
                    progress.set_batch_progress(
                        idx, total, success=success_n, failed=failed_n, current_name=display_name
                    )
                    await self.api.ack_campaign_blast_recipient(
                        job_id,
                        recipient_id,
                        success=False,
                        error_message=str(err)[:500],
                    )
                    continue

                data = (resp or {}).get("data") or {}
                action_id = data.get("id")
                candidates = data.get("receiver_candidates") or []
                receiver = (data.get("receiver") or "").strip()
                if not candidates and receiver:
                    candidates = [{"keyword": receiver, "source": data.get("receiver_source") or "unknown"}]
                if not action_id:
                    failed_n += 1
                    progress.append_step(f"{display_name}：服务器未返回动作 ID")
                    progress.set_batch_progress(
                        idx, total, success=success_n, failed=failed_n, current_name=display_name
                    )
                    await self.api.ack_campaign_blast_recipient(
                        job_id,
                        recipient_id,
                        success=False,
                        error_message="服务器未返回动作 ID",
                    )
                    continue

                cand_hint = " → ".join(
                    (c.get("keyword") or "").strip()
                    for c in candidates
                    if (c.get("keyword") or "").strip()
                )
                if cand_hint:
                    progress.append_step(f"搜索词顺序：{cand_hint}")

                try:
                    outcome = await _run_rpa_with_cancel(
                        candidates,
                        text,
                        cancel_event,
                        progress,
                        image_paths=image_paths,
                        on_thread_started=lambda t: setattr(self, "_rpa_thread", t),
                        # 群发不弹「手动跳转确认」，失败则跳过本条继续下一条
                        allow_user_confirm=False,
                    )
                except Exception as e:
                    outcome = wechat_rpa_adapter.RpaSendOutcome(False, error=str(e))

                if outcome.ok:
                    await self.api.report_wechat_outbound_result(
                        int(action_id),
                        {"status": "sent", "error": None},
                    )
                    ack_resp = await self.api.ack_campaign_blast_recipient(
                        job_id,
                        recipient_id,
                        success=True,
                        outbound_action_id=int(action_id),
                    )
                    if ack_resp and ack_resp.get("code") == 200:
                        success_n += 1
                        progress.append_step(f"{display_name}：发送成功")
                    else:
                        failed_n += 1
                        err = (ack_resp or {}).get("message") or "回执写入失败"
                        progress.append_step(f"{display_name}：{err}")
                        await self.api.ack_campaign_blast_recipient(
                            job_id,
                            recipient_id,
                            success=False,
                            error_message=str(err)[:500],
                        )
                else:
                    err_msg = (outcome.error or "").strip() or "微信发送失败"
                    await self.api.report_wechat_outbound_result(
                        int(action_id),
                        {"status": "failed", "error": err_msg},
                    )
                    await self.api.ack_campaign_blast_recipient(
                        job_id,
                        recipient_id,
                        success=False,
                        error_message=err_msg[:500],
                    )
                    failed_n += 1
                    progress.append_step(f"{display_name}：失败已跳过 — {err_msg}")

                progress.set_batch_progress(
                    idx, total, success=success_n, failed=failed_n, current_name=display_name
                )
                if page is not None:
                    page.set_send_progress(
                        current=idx,
                        total=total,
                        success=success_n,
                        failed=failed_n,
                    )

                if cancel_event.is_set():
                    if page is not None:
                        page._cancel_flag = True
                    progress.append_step("用户请求中断，停止后续发送")
                    break

                if idx < total and not cancel_event.is_set():
                    progress.append_step("发送间隔等待…")
                    delay = random.uniform(3.0, 6.0)
                    waited = 0.0
                    while waited < delay:
                        if cancel_event.is_set() or (
                            page is not None and page.is_send_cancelled()
                        ):
                            cancel_event.set()
                            break
                        step = min(0.15, delay - waited)
                        await asyncio.sleep(step)
                        waited += step
        finally:
            if main_win is not None and hasattr(main_win, "resume_snap_after_rpa"):
                try:
                    main_win.resume_snap_after_rpa()
                except Exception:
                    pass
            if progress is not None:
                try:
                    progress.mark_completed()
                except Exception:
                    pass
                try:
                    progress.close()
                except Exception:
                    pass
            self._send_busy = False
            self._rpa_thread = None
            final_resp = await self.api.get_campaign_blast_job(job_id)
            final_job = (final_resp or {}).get("data") if final_resp and final_resp.get("code") == 200 else job
            if page is not None:
                page.mark_send_finished(final_job if isinstance(final_job, dict) else job)
            tip = f"群发完成：成功 {success_n}，失败 {failed_n}"
            if cancel_event.is_set() or (page is not None and page.is_send_cancelled()):
                tip = f"群发已中断：成功 {success_n}，失败 {failed_n}"
            self.app.main_win.show_info_bar(
                "success" if failed_n == 0 else "warning",
                "活动群发",
                tip,
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
