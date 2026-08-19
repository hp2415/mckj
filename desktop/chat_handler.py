import asyncio
import httpx
import json
import re
from PySide6.QtWidgets import QFileDialog, QMessageBox, QPushButton
from config_loader import cfg
from logger_cfg import logger

class ChatHandler:
    """
    独立接管 AI 对话的交互信号调度、对话历史网络请求及数据库落盘同步
    """
    _PROPOSAL_REVISION_HINTS = (
        "调整", "修改", "改成", "换成", "替换", "去掉", "删除",
        "增加", "减少", "再加", "再要", "还要", "还需要", "再加上", "再配",
        "加上", "加点", "加个", "加一", "除了", "另外", "再来",
        "总价", "预算", "低一点", "高一点", "便宜", "贵一点", "折",
        "不变", "保持", "不要", "别用", "改为",
    )
    _PROPOSAL_REGENERATE_HINTS = (
        "重新生成", "重新来一份", "重新出一份", "重新做一份", "重做一份",
        "重来一份", "再生成一份", "再出一份", "再做一份",
    )
    _PROPOSAL_MARK = re.compile(r"方案\s*#(\d+)\s*v(\d+)")

    def __init__(self, app_controller, api_client):
        """
        :param app_controller: 传入主控制器提取 current_customer 上下文状态
        :param api_client: API 请求层
        """
        self.app = app_controller
        self.api = api_client
        self._current_tasks: list[asyncio.Task] = []  # 用于管理正在运行的 AI 生成任务（多模型并发）
        self._active_proposal_id: int | None = None
        self._active_proposal_status: str | None = None
        self._active_proposal_customer_id: str | None = None

    def _current_scope_key(self) -> str:
        """当前方案聊天作用域：员工自由对话 / 某个客户。"""
        mw = getattr(self.app, "main_win", None)
        mode = getattr(mw, "_chat_surface_mode", None) if mw else None
        if mode == "staff" or (
            mode not in ("staff", "customer")
            and getattr(self.app, "_chat_surface_mode", "customer") == "staff"
        ):
            return "staff:"
        return f"customer:{self._current_context_customer_id() or ''}"

    def _current_context_customer_id(self) -> str | None:
        mw = getattr(self.app, "main_win", None)
        mode = getattr(mw, "_chat_surface_mode", None) if mw else None
        if mode == "staff" or (
            mode not in ("staff", "customer")
            and getattr(self.app, "_chat_surface_mode", "customer") == "staff"
        ):
            return None
        customer = getattr(self.app, "_current_customer", None) or {}
        value = customer.get("id")
        return str(value).strip() if value is not None and str(value).strip() else None

    def _proposal_mode_enabled(self) -> bool:
        page = getattr(getattr(self.app, "main_win", None), "chat_page", None)
        btn = getattr(page, "example_btn", None) if page is not None else None
        return bool(btn is not None and btn.isChecked())

    def _proposal_scene_key(self) -> str:
        mw = getattr(self.app, "main_win", None)
        mode = getattr(mw, "_chat_surface_mode", None) if mw else None
        if mode == "staff" or (
            mode not in ("staff", "customer")
            and getattr(self.app, "_chat_surface_mode", "customer") == "staff"
        ):
            return "proposal_generate_free"
        return "proposal_generate"

    def _proposal_request_kind(self, text: str) -> str:
        """区分方案修订 / 重新生成 / 普通对话。"""
        raw = (text or "").strip()
        if not raw:
            return "chat"
        if any(token in raw for token in self._PROPOSAL_REGENERATE_HINTS):
            return "regenerate"
        if ("重新" in raw or "再" in raw) and ("生成" in raw or "来一份" in raw or "出一份" in raw):
            return "regenerate"
        if any(token in raw for token in self._PROPOSAL_REVISION_HINTS):
            return "revise"
        has_budget = ("人均" in raw) or ("每人" in raw) or ("单份" in raw)
        has_count = ("人份" in raw) or ("人数" in raw) or ("份方案" in raw)
        if has_budget and has_count:
            return "chat"
        productish = any(
            token in raw
            for token in ("米", "油", "礼盒", "茶", "菌", "坚果", "面", "糖", "酒", "方案")
        )
        if productish and len(raw) <= 80:
            return "revise"
        return "chat"

    async def handle_ai_copy(self, msg_id):
        """处理来自气泡的复制上报信号 (采纳统计)"""
        logger.info(f"监测到 AI 回复采纳行为 (复制): MsgID={msg_id}")
        await self.api.record_message_copy(msg_id)

    async def handle_ai_feedback(self, msg_id, rating):
        """处理来自气泡的评价信号"""
        logger.info(f"提交消息评价: ID={msg_id}, Rating={rating}")
        await self.api.set_message_feedback(msg_id, rating)

    async def handle_ai_regenerate(self, query: str):
        """处理针对特定问题的重新生成请求"""
        if not query:
            logger.warning("尝试重新生成，但未找到原始提问文本")
            return
            
        # 1. 界面清理：删除所有关联该提问的 AI 气泡（多模型并发会产生多条）
        chat_layout = self.app.main_win.chat_page.chat_layout
        # 倒序扫描，删除所有 user_query 匹配且为 AI 的气泡
        for i in reversed(range(chat_layout.count())):
            item = chat_layout.itemAt(i)
            w = item.widget() if item else None
            if not w:
                continue
            if getattr(w, "is_user", False):
                continue
            if getattr(w, "user_query", "") == query:
                w.deleteLater()
        
        # 2. 重新触发发送
        logger.info(f"重新生成 AI 回复，原问题: {query}")
        await self.handle_ai_chat_sent(query, is_regen=True)

    def cancel_current_task(self):
        """取消当前正在进行的 AI 对话任务"""
        alive = [t for t in (self._current_tasks or []) if t and not t.done()]
        for t in alive:
            t.cancel()
        if alive:
            logger.info(f"已手动取消当前 AI 对话任务 ({len(alive)} 个并发流)")
        self._current_tasks = []

    async def handle_ai_chat_sent(self, text, is_regen=False):
        """处理来自 UI 的 AI 发送请求与流式对话拼接"""
        # 0. 先取消可能存在的旧任务
        self.cancel_current_task()
        request_kind = self._proposal_request_kind(text)
        if (
            not is_regen
            and self._proposal_mode_enabled()
            and self._active_proposal_id
            and self._active_proposal_status in ("ready", "failed")
            and request_kind == "revise"
            and self._active_proposal_customer_id == self._current_scope_key()
        ):
            task = asyncio.create_task(
                self._do_proposal_revision(self._active_proposal_id, text)
            )
            self._current_tasks = [task]
            return
        
        # 1. 获取当前场景；点亮「方案生成」时强制进入 Excel 方案，否则交给后端分类
        scenario = "general_chat"
        if hasattr(self.app.main_win.chat_page, "get_selected_scenario_key"):
            scenario = self.app.main_win.chat_page.get_selected_scenario_key() or "general_chat"
        if self._proposal_mode_enabled():
            scenario = self._proposal_scene_key()

        # 2. 启动新任务（按模型并发）
        root = asyncio.create_task(self._do_ai_chat_multi(text, is_regen, scenario))
        self._current_tasks = [root]

    def _on_proposal_download_clicked(self, proposal_id: int, version: int):
        """模态保存框必须在同步槽里弹：在协程里弹会让 qasync 重入事件循环并抛
        "Cannot enter into task ... while another task is being executed"。"""
        path, _ = QFileDialog.getSaveFileName(
            self.app.main_win,
            "保存方案",
            f"方案_{proposal_id}_v{version}.xlsx",
            "Excel 文件 (*.xlsx)",
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        asyncio.create_task(self._download_proposal(proposal_id, version, path))

    async def _download_proposal(self, proposal_id: int, version: int, path: str):
        ok = await self.api.download_proposal(proposal_id, version, path)
        if ok:
            self.app.main_win.show_info_bar("success", "方案已下载", path)
        else:
            self.app.main_win.show_info_bar("error", "下载失败", "请稍后重试。")

    def restore_proposal_download(self, bubble, content: str):
        """历史记录里的方案预览也要能直接下载，并恢复当前可修订的方案指针。"""
        if bubble is None:
            return
        match = self._PROPOSAL_MARK.search(content or "")
        if not match:
            return
        proposal_id = int(match.group(1))
        version = int(match.group(2))
        self._attach_proposal_download(bubble, proposal_id, version)
        # 切回窗口后内存里的 active_proposal 会丢；用历史气泡里最新一份方案接着改，
        # 避免后续反馈被后端落到更早的另一份方案上。
        if (
            self._active_proposal_id is None
            or proposal_id >= int(self._active_proposal_id)
        ):
            self._active_proposal_id = proposal_id
            self._active_proposal_status = "ready"
            self._active_proposal_customer_id = self._current_scope_key()

    def _attach_proposal_download(self, bubble, proposal_id: int, version: int):
        if getattr(bubble, "_proposal_download_button", None) is not None:
            return
        button = QPushButton("下载 Excel 方案")
        button.clicked.connect(
            lambda: self._on_proposal_download_clicked(proposal_id, version)
        )
        bubble.bubble_layout.addWidget(button)
        bubble._proposal_download_button = button

    async def _poll_proposal(self, bubble, proposal_id: int):
        self._active_proposal_id = proposal_id
        self._active_proposal_status = "queued"
        self._active_proposal_customer_id = self._current_scope_key()
        for _ in range(120):
            await asyncio.sleep(1.5)
            payload = await self.api.get_proposal(proposal_id)
            if not payload:
                continue
            status = payload.get("status")
            self._active_proposal_status = status
            if status == "ready":
                spec = payload.get("spec") or {}
                totals = spec.get("totals") or {}
                meta = spec.get("meta") or {}
                lines = spec.get("lines") or []
                def _preview_line(line: dict) -> str:
                    per_person = int(line.get("qty_per_person") or 1)
                    extra = f"（每人 {per_person} 件）" if per_person > 1 else ""
                    cost = line.get("cost_price")
                    if cost is not None:
                        cost_text = f"，成本价 ¥{float(cost):.2f}"
                    elif str(line.get("priced_by") or "") == "fallback_discount":
                        zhe = float(meta.get("fallback_discount_rate") or 0.88) * 10
                        cost_text = f"，无成本价（按 {zhe:g} 折）"
                    else:
                        cost_text = ""
                    return (
                        f"- {line.get('product_name', '')} × {line.get('qty', 0)}{extra}"
                        f"，优惠单价 ¥{float(line.get('promo_unit_price') or 0):.2f}{cost_text}"
                    )

                line_text = "\n".join(_preview_line(line) for line in lines)
                budget = float(meta.get("per_capita_budget") or 0)
                has_uncosted = any(
                    str(line.get("priced_by") or "") == "fallback_discount"
                    or line.get("cost_price") is None
                    for line in lines
                )
                if str(meta.get("discount_source") or "") == "dialog" and meta.get("discount_rate") is not None:
                    pricing_note = f"\n折扣：{float(meta['discount_rate']) * 10:g} 折"
                elif not has_uncosted:
                    margin = float(meta.get("gross_margin") if meta.get("gross_margin") is not None else 0.30)
                    pricing_note = f"\n毛利率：{margin * 100:g}%"
                else:
                    pricing_note = ""
                cost_total = totals.get("cost_total")
                cost_total_text = (
                    f"\n成本合计：¥{float(cost_total):.2f}"
                    if (not has_uncosted) and cost_total is not None
                    else ""
                )
                bubble.append_text(
                    "\n\n### 方案预览"
                    f"\n{line_text}"
                    f"\n\n人均优惠价：¥{float(totals.get('per_capita_promo') or 0):.2f}"
                    + (f"（人均预算 ¥{budget:.2f}）" if budget > 0 else "")
                    + f"\n优惠总价：¥{float(totals.get('promo_total') or 0):.2f}"
                    + cost_total_text
                    + pricing_note
                    + "\n\n如需调整，可直接回复“把……换成……”或“把毛利率改为25%”。"
                )
                self._attach_proposal_download(
                    bubble, proposal_id, int(payload.get("current_version") or 1)
                )
                return
            if status == "failed":
                bubble.append_text(
                    f"\n\n方案生成失败：{payload.get('error_message') or '未知错误'}"
                )
                return
        bubble.append_text("\n\n方案仍在后台生成，可稍后重新打开对话查看。")

    async def _do_proposal_revision(self, proposal_id: int, feedback: str):
        chat_page = self.app.main_win.chat_page
        chat_page.add_message(feedback, True)
        bubble = chat_page.add_message("方案调整已进入队列，正在重新选品和生成 Excel。", False)
        payload = await self.api.revise_proposal(proposal_id, feedback)
        if not payload:
            bubble.show_error("提交方案调整失败，请稍后重试。")
            return
        await self._poll_proposal(bubble, proposal_id)
        if hasattr(bubble, "finalize_stream"):
            bubble.finalize_stream()

    async def _do_ai_chat_multi(self, text, is_regen=False, scenario="general_chat"):
        """真正的 AI 对话执行逻辑（可被取消）"""
        mw = getattr(self.app, "main_win", None)
        mw_mode = getattr(mw, "_chat_surface_mode", None) if mw else None
        if mw_mode in ("staff", "customer"):
            staff_mode = mw_mode == "staff"
        else:
            staff_mode = getattr(self.app, "_chat_surface_mode", "customer") == "staff"
        current_customer = getattr(self.app, "_current_customer", None)
        if not staff_mode and not current_customer:
            self.app.main_win.show_info_bar("warning", "未选中客户", "请先在左侧选择一个客户再进行对话。")
            return

        # 1. UI 展示用户消息 (重发时不重复展示用户消息)
        if not is_regen:
            self.app.main_win.chat_page.add_message(text, True)

        phone = None if staff_mode else (current_customer or {}).get("phone")
        if phone is not None:
            phone = str(phone).strip() or None
        raw_cid = None if staff_mode else (current_customer or {}).get("id")
        if raw_cid is not None:
            raw_cid = str(raw_cid).strip() or None
        session_sw = None if staff_mode else (current_customer or {}).get("sales_wechat_id")
        if session_sw is not None:
            session_sw = str(session_sw).strip() or None
        conv_id = None if staff_mode else (current_customer or {}).get("dify_conversation_id")
        
        # 3. 后端在线探测
        try:
            async with httpx.AsyncClient(timeout=3.0) as probe:
                probe_resp = await probe.get(
                    f"{self.api.base_url}/api/system/sync/status",
                    headers={"Authorization": f"Bearer {self.api.token}"}
                )
                if probe_resp.status_code not in (200, 403):
                    raise httpx.RequestError("Backend returned unexpected status")
        except Exception:
            # 多模型模式下此处还未创建气泡：仅提示，不要引用未定义变量
            self.app.main_win.show_info_bar("error", "云端连接失败", "服务器可能已离线。")
            return
        
        # 4. 执行后端 AI 网关流式迭代（按模型并发）
        chat_page = self.app.main_win.chat_page
        if hasattr(chat_page, "get_chat_models"):
            models = chat_page.get_chat_models() or []
        else:
            models = [chat_page.get_chat_model()] if hasattr(chat_page, "get_chat_model") else []
        models = [m for m in models if (m or "").strip()]
        if not models:
            models = [None]
        # lite：限制多模型并发，避免流式渲染与网络扇出成倍放大
        max_models = int(getattr(cfg, "max_chat_models", 0) or 0)
        if max_models > 0 and len(models) > max_models:
            logger.info(f"lite_mode：多模型并发限制为 {max_models}（原 {len(models)}）")
            models = models[:max_models]
        # 方案选品固定沿用桌面当前模型列表的第一项；即使对话并发多个模型，
        # 所有请求也传同一个 proposal_model，避免由并发先后决定方案模型。
        proposal_model = models[0] if models and models[0] else None

        async def run_one(model_id: str | None):
            mtag = ""
            if model_id and hasattr(chat_page, "get_chat_model_label"):
                mtag = chat_page.get_chat_model_label(model_id)
            elif model_id:
                mtag = model_id
            # 每个模型各自一个气泡
            ai_bubble = chat_page.add_message("", False, user_query=text, model_tag=mtag)
            full_answer = ""
            server_full = ""
            queued_proposal_id = None
            agen = None
            try:
                agen = self.api.stream_ai_chat(
                    query=text,
                    customer_phone=phone,
                    raw_customer_id=raw_cid,
                    sales_wechat_id=session_sw,
                    scenario=scenario,
                    conversation_id=conv_id,
                    chat_model=model_id,
                    proposal_model=proposal_model,
                )
                async for chunk in agen:
                    if chunk.startswith("[META_MODEL:"):
                        try:
                            raw = chunk[12:-1]
                            payload = json.loads(raw)
                            mid = payload.get("chat_model") or ""
                            scen = payload.get("scenario") or ""
                            aux = payload.get("auxiliary_scenarios") or []
                            if hasattr(chat_page, "apply_server_chat_meta"):
                                chat_page.apply_server_chat_meta(mid, scen, aux)
                            # 回写气泡的模型标签（以服务端实际模型为准）
                            if mid and hasattr(chat_page, "get_chat_model_label"):
                                ai_bubble.set_model_tag(chat_page.get_chat_model_label(mid))
                            elif mid:
                                ai_bubble.set_model_tag(mid)
                        except Exception as e:
                            logger.warning(f"解析对话 meta 失败: {e}")
                    elif chunk.startswith("[MSG_ID:"):
                        msg_id_str = chunk[8:-1]
                        try:
                            ai_bubble.msg_id = int(msg_id_str)
                            logger.info(f"AI 回复已落盘成功，返回标识: {msg_id_str}")
                        except ValueError:
                            pass
                    elif chunk.startswith("[DONE_TEXT:"):
                        try:
                            server_full = json.loads(chunk[len("[DONE_TEXT:"):])
                        except (json.JSONDecodeError, TypeError):
                            pass
                        continue
                    elif chunk.startswith("[SYSTEM_ACTION:"):
                        try:
                            changes_str = chunk[15:-1]
                            changes = json.loads(changes_str)

                            if changes.get("action") == "proposal_queued":
                                proposal_data = changes.get("proposal") or {}
                                queued_proposal_id = int(proposal_data.get("id"))
                                continue

                            # 翻译字段名为中文
                            field_map = {
                                "budget": "预算",
                                "title": "称呼",
                                "unit_name": "单位",
                                "purchase_type": "采购类型",
                                "purchase_months": "采购月份",
                                "ai_profile": "客户画像",
                                "profile_tag_ids": "动态标签",
                            }
                            modified_fields = [field_map.get(k, k) for k in changes.keys()]
                            fields_str = "、".join(modified_fields)

                            self.app.main_win.show_info_bar(
                                "success", "资料已自动更新", f"AI已帮您修改了以下资料: {fields_str}"
                            )
                            # 通知侧边栏和详情页刷新本地数据
                            self.app.main_win.ui_data_refresh_requested.emit()
                        except Exception as e:
                            logger.error(f"Failed to parse system action: {e}")
                    elif chunk.startswith("Error:"):
                        ai_bubble.show_error(chunk[6:].strip())
                        return
                    else:
                        ai_bubble.append_text(chunk)
                        full_answer += chunk
            except (asyncio.CancelledError, RuntimeError) as e:
                if isinstance(e, RuntimeError) and "cancel scope" not in str(e):
                    ai_bubble.show_error(f"系统错误: {str(e)}")
                else:
                    logger.info("AI 任务已正常中断")
                    # 中断时也做一次最终 Markdown，避免停留在轻量流式文本
                    if hasattr(ai_bubble, "finalize_stream"):
                        try:
                            ai_bubble.finalize_stream()
                        except Exception:
                            pass
                return
            except Exception as e:
                ai_bubble.show_error(f"连接异常: {str(e)}")
                return
            finally:
                # 显式关闭 async generator，避免 httpcore/anyio 在取消时输出
                # "async generator ignored GeneratorExit" / "exit cancel scope in a different task"
                try:
                    if agen is not None:
                        await agen.aclose()
                except Exception:
                    pass
            if server_full and len(server_full) > len(full_answer):
                missing = server_full[len(full_answer):]
                if missing:
                    logger.info(
                        "AI 对话流式尾包补齐: local_len={} server_len={} missing_len={}",
                        len(full_answer),
                        len(server_full),
                        len(missing),
                    )
                    full_answer = server_full
                    ai_bubble.append_text(missing)
            if queued_proposal_id:
                await self._poll_proposal(ai_bubble, queued_proposal_id)
            if not full_answer:
                ai_bubble.show_error("AI 未返回任何内容，请重试。")
            elif hasattr(ai_bubble, "finalize_stream"):
                # 流式结束：一次完整 Markdown 渲染（替代每个 chunk 全量解析）
                ai_bubble.finalize_stream()

        # 为每个模型启动并发任务（用于取消）
        tasks = [asyncio.create_task(run_one(m)) for m in models]
        self._current_tasks = tasks
        await asyncio.gather(*tasks, return_exceptions=True)
