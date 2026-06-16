"""电话工作台：流式生成完整电话话术（scenario=phone_call_script，不落微信对话记录）。"""
from __future__ import annotations

import asyncio
import json

import httpx

from logger_cfg import logger

PHONE_CALL_SCRIPT_SCENARIO = "phone_call_script"


def _is_phone_allocation_task(task: dict | None) -> bool:
    if not isinstance(task, dict):
        return False
    return (task.get("contact_channel") or "").strip() == "phone"


def build_phone_script_query(task: dict | None = None) -> str:
    lines = ["请为该客户生成可直接口播的完整电话沟通话术。"]
    if _is_phone_allocation_task(task):
        title = (task.get("title") or "").strip()
        instr = (task.get("instruction") or "").strip()
        if title:
            lines.append(f"今日电话任务标题：{title}")
        if instr:
            lines.append(f"任务要求：{instr}")
    lines.append(
        "请严格按系统提示的五段结构输出；结合首通电话场景文档选用合适口径，"
        "优先对齐今日任务目标（如有）。"
    )
    return "\n".join(lines)


def _resolve_chat_model_id(main_win) -> str | None:
    """与中部对话一致：多模型时取第一个，未选则交后端默认。"""
    chat_page = getattr(main_win, "chat_page", None)
    if chat_page is None:
        return None
    if hasattr(chat_page, "get_chat_models"):
        models = chat_page.get_chat_models() or []
        models = [m for m in models if (m or "").strip()]
        if models:
            return models[0]
    if hasattr(chat_page, "get_chat_model"):
        mid = (chat_page.get_chat_model() or "").strip()
        if mid:
            return mid
    return None


class PhoneScriptHandler:
    def __init__(self, app, api):
        self.app = app
        self.api = api
        self._task: asyncio.Task | None = None

    def cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def generate(self):
        self.cancel()
        main_win = getattr(self.app, "main_win", None)
        if not main_win or not hasattr(main_win, "phone_workbench"):
            return
        wb = main_win.phone_workbench
        customer = getattr(self.app, "_current_customer", None)
        if not isinstance(customer, dict):
            main_win.show_info_bar("warning", "未选中客户", "请先在左侧选择客户。")
            return

        task = getattr(wb, "_task", None)
        query = build_phone_script_query(task if isinstance(task, dict) else None)
        wb.begin_script_generation()

        phone = (customer.get("phone") or "").strip() or None
        raw_cid = str(customer.get("id") or "").strip() or None
        session_sw = customer.get("sales_wechat_id")
        if session_sw is not None:
            session_sw = str(session_sw).strip() or None

        try:
            async with httpx.AsyncClient(timeout=3.0) as probe:
                probe_resp = await probe.get(
                    f"{self.api.base_url}/api/system/sync/status",
                    headers={"Authorization": f"Bearer {self.api.token}"},
                )
                if probe_resp.status_code not in (200, 403):
                    raise httpx.RequestError("Backend returned unexpected status")
        except Exception:
            wb.finish_script_generation("云端连接失败，请检查网络或稍后重试。")
            return

        chat_model = _resolve_chat_model_id(main_win)
        self._task = asyncio.create_task(
            self._stream_generate(wb, query, phone, raw_cid, session_sw, chat_model)
        )

    async def _stream_generate(self, wb, query, phone, raw_cid, session_sw, chat_model):
        full = ""
        server_full = ""
        agen = None
        try:
            agen = self.api.stream_ai_chat(
                query=query,
                customer_phone=phone,
                raw_customer_id=raw_cid,
                sales_wechat_id=session_sw,
                scenario=PHONE_CALL_SCRIPT_SCENARIO,
                conversation_id=None,
                chat_model=chat_model,
            )
            async for chunk in agen:
                if chunk.startswith("[META_MODEL:") or chunk.startswith("[MSG_ID:"):
                    continue
                if chunk.startswith("[DONE_TEXT:"):
                    try:
                        server_full = json.loads(chunk[len("[DONE_TEXT:"):])
                    except (json.JSONDecodeError, TypeError):
                        pass
                    continue
                if chunk.startswith("[SYSTEM_ACTION:"):
                    continue
                if chunk.startswith("Error:"):
                    wb.finish_script_generation(chunk[6:].strip() or "生成失败")
                    return
                full += chunk
                wb.append_script_stream(chunk)
        except asyncio.CancelledError:
            logger.info("电话话术生成已取消")
            wb.finish_script_generation(None)
            return
        except Exception as e:
            logger.exception(f"电话话术生成异常: {e}")
            wb.finish_script_generation(f"生成异常: {e}")
            return
        finally:
            try:
                if agen is not None:
                    await agen.aclose()
            except Exception:
                pass
            self._task = None

        if server_full and len(server_full) > len(full):
            missing = server_full[len(full):]
            if missing:
                logger.info(
                    "电话话术流式尾包补齐: local_len={} server_len={} missing_len={}",
                    len(full),
                    len(server_full),
                    len(missing),
                )
                full = server_full
                wb.append_script_stream(missing)

        if not full.strip():
            wb.finish_script_generation("AI 未返回内容，请重试。")
        else:
            wb.finish_script_generation(None)
