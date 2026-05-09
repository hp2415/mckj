"""
原始客户(RawCustomer) LLM 画像：拉取聊天与订单上下文，写回主库 Customer / UserCustomerRelation；
成功时更新 SalesCustomerProfile 与 raw_customers.profile_status（实体级，与 per-sales SCP 并存）。
"""
from __future__ import annotations

import asyncio
import json
import http.client
import os
import re
import traceback
from datetime import datetime
from urllib.parse import urlparse
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, update, and_
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from sqlalchemy.future import select

import crud as crud_ops

from models import (
    RawCustomer,
    RawCustomerSalesWechat,
    RawChatLog,
    RawOrder,
    RawOrderItem,
    SalesCustomerProfile,
    SalesWechatAccount,
    User,
    UserSalesWechat,
    SystemConfig,
    ProfileTagDefinition,
)
from ai.llm_client import LLMClient
from ai.prompt_seed import CUSTOMER_PROFILE_SYSTEM, CUSTOMER_PROFILE_USER
from core.logger import logger
from schemas import normalize_purchase_months

API_HOST = "api.chatool.micheng.cn"
AUTH_TOKEN_DEFAULT = "1031bdbd-337a-4a85-88d0-4004804e168a"

async def _profile_audit_enabled(db) -> bool:
    """
    轻量画像审计开关：读 system_configs.profile_audit_log（1/true/on 开启）。
    默认关闭，避免日志膨胀与泄露敏感上下文。
    """
    try:
        stmt = select(SystemConfig).where(SystemConfig.config_key == "profile_audit_log")
        res = await db.execute(stmt)
        cfg = res.scalars().first()
        if not cfg:
            return False
        v = str(cfg.config_value or "").strip()
        return v not in ("0", "false", "False", "off", "OFF", "")
    except Exception:
        return False


def _rcsw_relation_inactive(rcsw: RawCustomerSalesWechat | None) -> bool:
    """无 per-sales 行或该行标记已删好友时，不参与画像等业务。"""
    if rcsw is None:
        return True
    v = getattr(rcsw, "is_deleted", None)
    if v is None or v is False:
        return False
    if v is True:
        return True
    try:
        return int(v) == 1
    except (TypeError, ValueError):
        return bool(v)


async def load_profile_tags_catalog_text(db) -> str:
    """管理平台启用的动态标签，格式化后注入画像提示词。"""
    stmt = (
        select(ProfileTagDefinition)
        .where(ProfileTagDefinition.is_active.is_(True))
        .order_by(ProfileTagDefinition.sort_order, ProfileTagDefinition.id)
    )
    res = await db.execute(stmt)
    rows = res.scalars().all()
    if not rows:
        return "（尚未配置任何动态标签；请输出 matched_profile_tag_ids 为空数组 []。）"
    lines: list[str] = []
    for r in rows:
        feat = (r.feature_note or "").strip().replace("\n", " ")
        strat = (r.strategy_note or "").strip().replace("\n", " ")
        lines.append(f"- id={r.id} 名称「{r.name}」")
        if feat:
            lines.append(f"  特征：{feat}")
        if strat:
            lines.append(f"  策略：{strat}")
    return "\n".join(lines)


async def _use_db_prompts(db) -> bool:
    """与 PromptService 一致：读 system_configs.use_db_prompts，未配置则默认启用。"""
    try:
        stmt = select(SystemConfig).where(SystemConfig.config_key == "use_db_prompts")
        res = await db.execute(stmt)
        cfg = res.scalars().first()
        if not cfg:
            return True
        return str(cfg.config_value).strip() not in ("0", "false", "False", "off", "OFF", "")
    except Exception as e:
        logger.warning("画像提示词: 读取 use_db_prompts 失败，默认走 DB: {}", e)
        return True


async def build_profile_chat_messages(
    db,
    basic_info: str,
    chat_context: str,
    order_context: str,
    extra_ctx: dict[str, Any] | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """组装画像 LLM 消息：优先使用管理平台场景 customer_profile（published）。"""
    from ai.prompt_models import PromptTemplate
    from ai.prompt_renderer import render_system
    from ai.prompt_store import get_prompt_store

    ctx: dict[str, Any] = {
        "basic_info": basic_info,
        "chat_context": chat_context,
        "order_context": order_context,
    }
    if extra_ctx:
        ctx.update(extra_ctx)
    if not str(ctx.get("profile_tags_catalog") or "").strip():
        ctx["profile_tags_catalog"] = (
            "（尚未配置任何动态标签；请输出 matched_profile_tag_ids 为空数组 []。）"
        )
    if await _use_db_prompts(db):
        store = get_prompt_store()
        version = await store.get_published_version("customer_profile")
        if version:
            docs_map: dict[str, tuple[str, int | None]] = {}
            for spec in version.doc_refs or []:
                c, vid = await store.get_doc_text(spec.doc_key, spec.doc_version_id)
                docs_map[spec.doc_key] = (c, vid)
            system_text = render_system(version.template, ctx, docs_map, version.doc_refs or [])
            user_src = (version.template.user or "").strip() or CUSTOMER_PROFILE_USER.strip()
            user_text = render_system(PromptTemplate(system=user_src), ctx, {}, ())
            user_text = _ensure_profile_tags_user_block(user_text, str(ctx.get("profile_tags_catalog") or ""))
            messages = [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ]
            meta = {
                "prompt_source": "db",
                "scenario_key": "customer_profile",
                "prompt_version_id": getattr(version, "id", None),
                "prompt_version": getattr(version, "version", None),
            }
            return messages, meta

    user_text = render_system(
        PromptTemplate(system=CUSTOMER_PROFILE_USER.strip()),
        ctx,
        {},
        (),
    )
    user_text = _ensure_profile_tags_user_block(user_text, str(ctx.get("profile_tags_catalog") or ""))
    messages = [
        {"role": "system", "content": CUSTOMER_PROFILE_SYSTEM},
        {"role": "user", "content": user_text},
    ]
    meta = {
        "prompt_source": "local",
        "scenario_key": "customer_profile",
        "prompt_version_id": None,
        "prompt_version": None,
    }
    return messages, meta


def _ensure_profile_tags_user_block(user_text: str, catalog: str) -> str:
    """已发布 DB 模板若未含动态标签段，则追加，避免升级库后旧模板漏注入。"""
    marker = "【可匹配的客户动态标签】"
    if marker in (user_text or ""):
        return user_text
    cat = (catalog or "").strip()
    if not cat:
        cat = "（尚未配置任何动态标签；请输出 matched_profile_tag_ids 为空数组 []。）"
    return (
        (user_text or "").rstrip()
        + f"\n\n{marker}\n{cat}\n"
        + "请结合基础信息、聊天记录与订单判断符合的标签。**注意：如果客户同时满足多个标签特征，请务必将它们全部放入 matched_profile_tag_ids 数组中，强烈建议尽可能多选，不要遗漏！**（整数数组，仅使用上文列出的 id）。\n"
    )


async def _fetch_ai_system_configs(db) -> dict[str, str]:
    stmt = select(SystemConfig).where(SystemConfig.config_group == "ai")
    res = await db.execute(stmt)
    return {c.config_key: (c.config_value or "") for c in res.scalars().all()}


async def get_profile_llm_display_for_progress(db) -> dict[str, str]:
    """管理端画像进度页展示用：当前生效的模型与 API 主机（不含密钥）。"""
    configs = await _fetch_ai_system_configs(db)
    model = (configs.get("profile_llm_model") or configs.get("llm_model") or "qwen-max").strip() or "qwen-max"
    api_url = (
        configs.get("profile_llm_api_url")
        or configs.get("llm_api_url")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ).strip()
    try:
        host = (urlparse(api_url).netloc or api_url)[:120]
    except Exception:
        host = "—"
    return {"model": model, "api_host": host}


async def get_llm_client(db) -> LLMClient:
    """
    画像分析专用 LLM：

    - 优先读取独立配置：
      - profile_llm_api_url
      - profile_llm_api_key
      - profile_llm_model
    - 若未配置，则回退到历史字段（兼容老环境）：
      - llm_api_url / llm_api_key / llm_model

    说明：画像分析与桌面端对话模型（chat_model / llm_chat_model）完全隔离。
    """
    configs = await _fetch_ai_system_configs(db)
    api_url = (
        configs.get("profile_llm_api_url")
        or configs.get("llm_api_url")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    api_key = (configs.get("profile_llm_api_key") or configs.get("llm_api_key") or "")
    model = (configs.get("profile_llm_model") or configs.get("llm_model") or "qwen-max")
    return LLMClient(api_url=api_url, api_key=api_key, model=model)


_REMARK_MOBILE_RE = re.compile(
    r"(?<![0-9])(?:\+?86[\s\-\u00a0]*)?(1[3-9]\d{9})(?![0-9])"
)


def _digits_phone(p: str | None) -> str:
    return "".join(filter(str.isdigit, str(p or "")))


def _extract_mobile_candidates_from_remark(text: str | None, *, limit: int = 3) -> list[str]:
    """从备注等文本中提取大陆手机号（1[3-9]…），按出现顺序去重，最多 limit 个。"""
    if not (text or "").strip():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _REMARK_MOBILE_RE.finditer(str(text)):
        num = m.group(1)
        if num not in seen:
            seen.add(num)
            out.append(num)
            if len(out) >= limit:
                break
    return out


def _profile_order_merge_key(o: dict[str, Any]) -> tuple:
    return (str(o.get("dddh") or ""), str(o.get("order_time") or ""))


async def fetch_orders_for_profile_context(
    db,
    phone_primary: str | None,
    remark: str | None,
) -> list[dict[str, Any]]:
    """
    画像前拉订单：优先预存/快照电话，再从 remark 中解析号码依次尝试，合并去重。
    避免「电话只在备注里」时首轮画像订单上下文为空。
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(raw: str | None) -> None:
        if not raw:
            return
        d = _digits_phone(raw)
        if len(d) < 7:
            return
        if d not in seen:
            seen.add(d)
            candidates.append(d)

    add_candidate(phone_primary)
    for extra in _extract_mobile_candidates_from_remark(remark):
        add_candidate(extra)

    if not candidates:
        return []

    merged: list[dict[str, Any]] = []
    seen_orders: set[tuple] = set()
    for cand in candidates:
        chunk = await fetch_orders_with_sync(db, cand)
        for o in chunk:
            k = _profile_order_merge_key(o)
            if k not in seen_orders:
                seen_orders.add(k)
                merged.append(o)
    merged.sort(key=lambda x: str(x.get("order_time") or ""), reverse=True)
    return merged


async def fetch_orders_with_sync(db, phone: str | None) -> list[dict[str, Any]]:
    if not phone:
        return []
    phone = "".join(filter(str.isdigit, str(phone)))
    if len(phone) < 7:
        return []

    try:
        stmt_cfg = select(SystemConfig).where(SystemConfig.config_key == "order_api_token")
        res_cfg = await db.execute(stmt_cfg)
        cfg_obj = res_cfg.scalars().first()
        token = (cfg_obj.config_value or "").strip() if cfg_obj else AUTH_TOKEN_DEFAULT

        conn = http.client.HTTPSConnection(API_HOST)
        payload = json.dumps({"phone": phone, "page": 1, "page_size": 50})
        headers = {"Authorization": token, "Content-Type": "application/json"}
        conn.request("POST", "/api/order-fupin", payload, headers)
        res = conn.getresponse()
        resp_data = json.loads(res.read().decode("utf-8"))

        if resp_data.get("code") == 200:
            api_list = resp_data.get("data", {}).get("list", [])

            for o in api_list:
                stmt_find = select(RawOrder).where(RawOrder.order_id == str(o.get("order_id") or o.get("id")))
                res_find = await db.execute(stmt_find)
                existing = res_find.scalar_one_or_none()

                if not existing:
                    ot_str = o.get("order_time")
                    ot_dt = None
                    if ot_str:
                        try:
                            ot_dt = datetime.strptime(ot_str, "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            pass

                    new_order = RawOrder(
                        order_id=str(o.get("order_id") or o.get("id")),
                        dddh=o.get("dddh"),
                        store=o.get("store"),
                        pay_type_name=o.get("pay_type_name"),
                        pay_amount=o.get("pay_amount"),
                        freight=o.get("freight"),
                        status_name=o.get("status_name"),
                        order_time=ot_dt,
                        remark=o.get("remark"),
                        consignee=o.get("consignee"),
                        consignee_phone=o.get("consignee_phone"),
                        consignee_address=o.get("consignee_address"),
                        buyer_name=o.get("buyer_name"),
                        buyer_phone=o.get("buyer_phone"),
                        purchase_type=o.get("purchase_type") if o.get("purchase_type") is not None else 0,
                        search_phone=phone,
                        raw_json=json.dumps(o, ensure_ascii=False),
                    )
                    db.add(new_order)
                    await db.flush()

                    for gi in o.get("goodsInfo", []):
                        item = RawOrderItem(
                            raw_order_id=new_order.id,
                            uuid=gi.get("uuid"),
                            product_name=gi.get("product_name"),
                        )
                        db.add(item)

            await db.commit()
    except Exception as e:
        logger.warning("API Fetch/Sync Error for {}: {}", phone, e)

    stmt = select(RawOrder).where(RawOrder.search_phone == phone).order_by(RawOrder.order_time.desc())
    res = await db.execute(stmt)
    all_local = res.scalars().all()

    results = []
    for lo in all_local:
        stmt_items = select(RawOrderItem).where(RawOrderItem.raw_order_id == lo.id)
        res_items = await db.execute(stmt_items)
        items = res_items.scalars().all()

        results.append(
            {
                "dddh": lo.dddh,
                "status_name": lo.status_name,
                "pay_amount": float(lo.pay_amount) if lo.pay_amount else 0,
                "order_time": lo.order_time.strftime("%Y-%m-%d %H:%M:%S") if lo.order_time else "",
                "goodsInfo": [{"product_name": item.product_name} for item in items],
            }
        )
    return results


async def get_chat_context(
    db,
    customer_id: str,
    *,
    sales_wechat_id: str | None = None,
) -> str:
    """严格按「业务微信 × 客户」拉取 raw_chat_logs（与 ContextAssembler._build_chat_summary 一致）。

    性能要点：
    - 避免 OR 条件导致索引失效：改为两段查询再合并
    - 优先使用 time_ms（同步模块写入字段），其次回退 timestamp（历史字段）
    """
    cid = (customer_id or "").strip()
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return "暂无微信聊天记录。（未解析到当前业务微信，无法按会话加载。）"
    # 两段查询：会话双方互为 wechat_id/talker
    stmt_a = (
        select(RawChatLog)
        .where(and_(RawChatLog.wechat_id == sw, RawChatLog.talker == cid))
        # MySQL 不支持 "NULLS LAST"：用 COALESCE 做兼容排序
        .order_by(func.coalesce(RawChatLog.time_ms, RawChatLog.timestamp, 0).desc())
        .limit(50)
    )
    stmt_b = (
        select(RawChatLog)
        .where(and_(RawChatLog.wechat_id == cid, RawChatLog.talker == sw))
        .order_by(func.coalesce(RawChatLog.time_ms, RawChatLog.timestamp, 0).desc())
        .limit(50)
    )
    res_a = await db.execute(stmt_a)
    res_b = await db.execute(stmt_b)
    logs = list(res_a.scalars().all()) + list(res_b.scalars().all())
    # 合并后按时间取最新 50
    def _ts(v) -> int:
        try:
            if v is None:
                return 0
            return int(v)
        except Exception:
            return 0

    logs.sort(key=lambda x: (_ts(getattr(x, "time_ms", None)) or _ts(getattr(x, "timestamp", None))), reverse=True)
    logs = logs[:50]
    if not logs:
        return "暂无微信聊天记录。"
    context_lines = []
    for l in reversed(logs):
        time_str = ""
        try:
            ms = getattr(l, "time_ms", None)
            if ms is None:
                ms = getattr(l, "timestamp", None)
            if ms is not None:
                ts = int(ms) / 1000
                time_str = datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M")
        except Exception:
            time_str = ""
        sender = "客户" if l.is_send == 0 else "工作人员"
        prefix = f"[{time_str}] " if time_str else ""
        context_lines.append(f"{prefix}{sender}: {l.text}")
    return "\n".join(context_lines)


def _extract_first_json_object(text: str) -> dict | None:
    """从 LLM 文本响应中稳健地提取首个 JSON 对象。

    处理 LLM 常见返回格式：
    - 仅返回单个 JSON
    - JSON 前后混有思考/解释文本（其中可能也包含 `{` `}`）
    - 用 ```json ... ``` 围栏包裹
    - JSON 后追加补充说明（导致 rfind('}') 越界产生 "Extra data" 错误）

    返回 None 表示未能解析出有效 dict。
    """
    if not text:
        return None

    # 1) 优先尝试 ```json ... ``` / ``` ... ``` 围栏
    fence = re.search(r"```(?:json|JSON)?\s*(\{[\s\S]*?\})\s*```", text)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # 2) 从每个 '{' 处尝试 raw_decode，找到第一个能完整解析为 dict 的对象。
    #    raw_decode 只解析单个 JSON 值并返回结束位置，能容忍后续多余文本。
    decoder = json.JSONDecoder()
    n = len(text)
    i = 0
    while i < n:
        i = text.find("{", i)
        if i == -1:
            return None
        try:
            obj, _ = decoder.raw_decode(text, i)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        i += 1
    return None


async def profile_raw_customer_with_llm(
    db,
    llm: LLMClient,
    raw: RawCustomer,
    *,
    sales_wechat_id_override: str | None = None,
    rcsw_snapshot: RawCustomerSalesWechat | None = None,
) -> dict[str, Any] | None:
    logger.info(
        "画像分析 LLM model={} raw_id={}（配置项 llm_model，与桌面对话 chat_model 无关）",
        llm.model,
        raw.id,
    )
    sw_for_chat = (sales_wechat_id_override or "").strip()
    if not sw_for_chat:
        sw_for_chat = (raw.sales_wechat_id or "").strip()
    if not sw_for_chat:
        sw_res = await db.execute(
            select(RawCustomerSalesWechat.sales_wechat_id)
            .where(RawCustomerSalesWechat.raw_customer_id == raw.id)
            .order_by(RawCustomerSalesWechat.id.asc())
            .limit(1)
        )
        sw_for_chat = (sw_res.scalar_one_or_none() or "").strip()
    chats = await get_chat_context(db, raw.id, sales_wechat_id=(sw_for_chat or None))
    # 优先使用 per-sales 快照电话，避免 raw_customers 去重快照 phone 为空导致订单拉取失败
    phone_for_orders = (getattr(rcsw_snapshot, "phone", None) or raw.phone) if rcsw_snapshot else raw.phone
    remark_for_orders = (
        (getattr(rcsw_snapshot, "remark", None) if rcsw_snapshot else None) or raw.remark
    )
    orders = await fetch_orders_for_profile_context(db, phone_for_orders, remark_for_orders)

    order_text = []
    for o in orders:
        products = ", ".join([g.get("product_name", "") for g in o.get("goodsInfo", [])])
        order_text.append(
            f"- {o.get('order_time')}: {o.get('status_name')}, 金额:{o.get('pay_amount')}, 产品:[{products}]"
        )

    # 基础信息优先取 per-sales 快照（同一客户在不同销售号下 remark/phone/note_des 可能不同）
    remark = remark_for_orders
    nick = (getattr(rcsw_snapshot, "name", None) if rcsw_snapshot else None) or raw.name
    note_des = (getattr(rcsw_snapshot, "note_des", None) if rcsw_snapshot else None) or raw.note_des
    label = (getattr(rcsw_snapshot, "label", None) if rcsw_snapshot else None) or raw.label
    region = (getattr(rcsw_snapshot, "region", None) if rcsw_snapshot else None) or raw.region
    add_time = (getattr(rcsw_snapshot, "add_time", None) if rcsw_snapshot else None) or raw.add_time
    add_time_str = add_time.strftime("%Y-%m-%d") if add_time else "未知"
    basic_info = (
        f"原始ID: {raw.id}, 客户通讯录备注/微信昵称: {remark}/{nick}, "
        f"预存电话: {phone_for_orders}, 详细描述: {note_des}, 标签: {label}, 地区: {region}, "
        f"微信加好友时间(建联日期): {add_time_str}, 当前日期: {datetime.now().strftime('%Y-%m-%d')}"
        f"（上列为客户侧信息；当前业务微信号及其昵称/别名由系统库维护，勿写入 ai_profile。）"
    )

    chat_block = chats if chats else "暂无最近聊天记录"
    order_block = "\n".join(order_text) if order_text else "暂无历史订单记录"
    catalog = await load_profile_tags_catalog_text(db)
    messages, meta = await build_profile_chat_messages(
        db,
        basic_info,
        chat_block,
        order_block,
        extra_ctx={"profile_tags_catalog": catalog},
    )
    # 临时调试：打印最终发送给模型的 prompt（默认关闭）
    # 用法：在 backend/.env 或运行环境加入 PROFILE_DEBUG_PROMPT=1，然后重启后端
    # 注意：包含聊天内容与电话等敏感信息，请仅在本地/受控环境短期开启。
    try:
        dbg = str(os.getenv("PROFILE_DEBUG_PROMPT") or "").strip()
        if dbg not in ("", "0", "false", "False", "off", "OFF"):
            sys_text = ""
            user_text = ""
            for m in messages or []:
                if m.get("role") == "system" and not sys_text:
                    sys_text = str(m.get("content") or "")
                if m.get("role") == "user" and not user_text:
                    user_text = str(m.get("content") or "")
            logger.info(
                "PROFILE_DEBUG_PROMPT raw_id={} sales_wechat_id={} meta={}\n---SYSTEM---\n{}\n---USER---\n{}\n---END---",
                raw.id,
                sales_wechat_id_override or sw_for_chat,
                json.dumps(meta or {}, ensure_ascii=False),
                (sys_text[:4000] + ("...<truncated>" if len(sys_text) > 4000 else "")),
                (user_text[:6000] + ("...<truncated>" if len(user_text) > 6000 else "")),
            )
    except Exception:
        logger.exception("PROFILE_DEBUG_PROMPT log failed raw_id={}", raw.id)
    # 轻量审计（默认关闭）：把画像请求消息落到日志，便于复盘上下文
    if await _profile_audit_enabled(db):
        try:
            audit = {
                "raw_id": raw.id,
                "sales_wechat_id_override": sales_wechat_id_override,
                "llm_model": llm.model,
                **(meta or {}),
                "messages": messages,
            }
            logger.info("PROFILE_AUDIT_REQUEST {}", json.dumps(audit, ensure_ascii=False))
        except Exception:
            logger.exception("PROFILE_AUDIT_REQUEST log failed raw_id={}", raw.id)

    try:
        full_content = ""
        async for chunk in llm.stream_chat(messages):
            # 思维链/工具调用是带前缀的“伪 chunk”，必须在拼接 JSON 内容前过滤掉，
            # 否则 reasoning_content 里出现的 '{' '}' 会污染后续 JSON 抽取（实测见到
            # JSONDecodeError: Extra data 与 reasoning 文本被当作 JSON 起点的两类故障）。
            if chunk.startswith("__TOOL_CALL__:") or chunk.startswith("__REASONING_CONTENT__:"):
                continue
            full_content += chunk

        data = _extract_first_json_object(full_content)
        if not data:
            logger.warning(
                "画像 LLM 响应未抽取到有效 JSON raw_id={} preview={}",
                raw.id,
                (full_content[:300] + ("..." if len(full_content) > 300 else "")),
            )
            return None
        data["raw_id"] = raw.id
        # 关键修复：当入口显式传入 sales_wechat_id 时，不允许再“默默回退”到快照/映射表，
        # 否则会掩盖上游没有按 (raw_id, sales_wechat_id) 传参的问题，造成画像落到错误销售号。
        sw_override = (sales_wechat_id_override or "").strip()
        if sw_override:
            data["sales_wechat_id"] = sw_override
            return data

        # 无 override 时再回退：raw 快照 → 映射表首行
        sw = (raw.sales_wechat_id or "").strip()
        if not sw:
            sw_res = await db.execute(
                select(RawCustomerSalesWechat.sales_wechat_id)
                .where(RawCustomerSalesWechat.raw_customer_id == raw.id)
                .order_by(RawCustomerSalesWechat.id.asc())
                .limit(1)
            )
            sw = (sw_res.scalar_one_or_none() or "").strip()
        data["sales_wechat_id"] = sw or None
        if await _profile_audit_enabled(db):
            try:
                logger.info(
                    "PROFILE_AUDIT_RESPONSE {}",
                    json.dumps(
                        {
                            "raw_id": raw.id,
                            "sales_wechat_id": data.get("sales_wechat_id"),
                            "parsed": data,
                        },
                        ensure_ascii=False,
                    ),
                )
            except Exception:
                logger.exception("PROFILE_AUDIT_RESPONSE log failed raw_id={}", raw.id)
        return data
    except Exception as e:
        logger.exception("LLM profile failed for raw {}: {}", raw.id, e)
        return None


async def get_user_id_map(db) -> dict[str, int]:
    """
    销售微信号（sales_wechat_id / wxid_...）→ 登录用户 id。

    优先级：
    1) user_sales_wechats 显式绑定（推荐做法）
    2) users.wechat_id 直接等于 sales_wechat_id（历史数据兼容）
    3) sales_wechat_accounts.account_code ↔ users.username（从云客主数据推断）
    """
    mapping: dict[str, int] = {}

    # 1) 显式绑定表
    res = await db.execute(select(UserSalesWechat))
    for r in res.scalars().all():
        sid = (r.sales_wechat_id or "").strip()
        if sid and r.user_id:
            mapping[sid] = int(r.user_id)

    # 2) users.wechat_id 直接映射
    u_res = await db.execute(select(User.id, User.wechat_id).where(User.wechat_id.isnot(None)))
    for uid, wid in u_res.all():
        sid = (wid or "").strip()
        if sid and uid and sid not in mapping:
            mapping[sid] = int(uid)

    # 3) sales_wechat_accounts.account_code ↔ users.username 推断
    #    如果某个 sales_wechat_id 还没映射，且 account_code 能对应到 username，则补上
    from sqlalchemy import and_
    from sqlalchemy.orm import aliased

    U = aliased(User)
    a_res = await db.execute(
        select(SalesWechatAccount.sales_wechat_id, U.id)
        .join(U, U.username == SalesWechatAccount.account_code)
        .where(
            and_(
                SalesWechatAccount.account_code.isnot(None),
                SalesWechatAccount.account_code != "",
            )
        )
    )
    for sid, uid in a_res.all():
        sid = (sid or "").strip()
        if sid and uid and sid not in mapping:
            mapping[sid] = int(uid)

    return mapping


async def ensure_sales_wechat_account_row(db, sales_wechat_id: str | None) -> None:
    """
    sales_customer_profiles 外键要求 sales_wechat_id 存在于 sales_wechat_accounts。
    好友池里可能出现尚未做主数据同步的 wxid，画像落库前插入占位行（可被后续开放平台/xlsx 同步覆盖）。
    """
    sw = (sales_wechat_id or "").strip()
    if not sw:
        return
    r = await db.execute(
        select(SalesWechatAccount.sales_wechat_id).where(SalesWechatAccount.sales_wechat_id == sw)
    )
    if r.first():
        return
    try:
        async with db.begin_nested():
            db.add(SalesWechatAccount(sales_wechat_id=sw, source="raw_pool"))
            await db.flush()
    except IntegrityError:
        pass


async def apply_profile_to_main(
    db,
    p: dict[str, Any],
    *,
    user_id: int | None,
) -> None:
    """将单条画像结果写入 RawCustomer / SalesCustomerProfile（不 commit）。

    说明：
    - 正常情况下 user_id 来自销售微信号绑定（UserSalesWechat）。
    - 若 sales_wechat_id 未绑定任何登录用户，允许 user_id=None：画像仍会落到 per-sales 关系上，
      但 SalesCustomerProfile.user_id 为空，后续补绑定/迁移关系时可再回填。
    """
    raw_id = p.get("raw_id")
    sales_wx_id = p.get("sales_wechat_id")

    phone = p.get("contact_tel") or None

    purchase_months = p.get("purchase_months")
    if isinstance(purchase_months, list):
        purchase_months = ", ".join([str(m) for m in purchase_months])
    elif not purchase_months:
        purchase_months = ""
    else:
        purchase_months = str(purchase_months)
    purchase_months = normalize_purchase_months(purchase_months)

    stmt_rc = select(RawCustomer).where(RawCustomer.id == raw_id)
    res_rc = await db.execute(stmt_rc)
    rc = res_rc.scalar_one_or_none()
    if not rc:
        return

    # per-sales 好友快照备注优先
    wechat_remark = None
    if sales_wx_id:
        sw_res = await db.execute(
            select(RawCustomerSalesWechat.remark)
            .where(
                RawCustomerSalesWechat.raw_customer_id == raw_id,
                RawCustomerSalesWechat.sales_wechat_id == sales_wx_id,
            )
            .limit(1)
        )
        wechat_remark = sw_res.scalar_one_or_none()
    wechat_remark = (wechat_remark or rc.remark or None)
    # 兜底：若 LLM 未提取姓名且实体姓名为空，可从“微信备注”中提取简单姓氏（如“金主任/张总”）
    # 仅在极明确的称谓模式下生效，避免误伤。
    extracted_surname: str | None = None
    if wechat_remark:
        import re
        m = re.search(r"([\\u4e00-\\u9fff]{1,3})(主任|局|总|老板|经理|老师|哥|姐)", str(wechat_remark))
        if m:
            extracted_surname = (m.group(1) or "").strip() or None

    contact_date_val = rc.add_time.date() if rc and rc.add_time else None

    followup_str = p.get("suggested_followup_date")
    followup_date_val = None
    if followup_str:
        try:
            followup_date_val = datetime.strptime(str(followup_str).strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass

    # 写回 raw_customers 归一化字段
    if phone:
        rc.phone = str(phone).strip()
        rc.phone_normalized = str(phone).strip()
    if p.get("contact_name"):
        rc.customer_name = str(p.get("contact_name") or "").strip() or rc.customer_name
    elif extracted_surname and not (rc.customer_name or "").strip():
        rc.customer_name = extracted_surname
    if p.get("entity_name"):
        rc.unit_name = str(p.get("entity_name") or "").strip() or rc.unit_name
    rc.unit_type = p.get("entity_type") or rc.unit_type
    rc.admin_division = p.get("region_info") or rc.admin_division
    rc.purchase_months = [m.strip() for m in purchase_months.split(",") if m.strip()] if purchase_months else []
    rc.profile_status = 1
    rc.profile_updated_at = datetime.now()

    rel_sales = sales_wx_id if sales_wx_id else None
    if rel_sales:
        await ensure_sales_wechat_account_row(db, rel_sales)
    stmt_rel = select(SalesCustomerProfile).where(
        SalesCustomerProfile.raw_customer_id == rc.id,
        SalesCustomerProfile.sales_wechat_id == rel_sales,
    )
    res_rel = await db.execute(stmt_rel)
    rel = res_rel.scalar_one_or_none()
    if user_id is not None and rel is not None and rel.user_id != user_id:
        rel.user_id = user_id

    budget_val = p.get("budget")
    budget_num = None
    if budget_val is not None and str(budget_val).replace(".", "", 1).isdigit():
        budget_num = float(budget_val)

    if not rel:
        rel = SalesCustomerProfile(
            user_id=user_id,
            raw_customer_id=rc.id,
            sales_wechat_id=rel_sales,
            ai_profile=p.get("ai_profile"),
            title=p.get("contact_title"),
            budget_amount=Decimal(str(budget_num)) if budget_num is not None else Decimal("0.00"),
            purchase_type=str(p.get("purchase_type")) if p.get("purchase_type") else None,
            relation_type="active",
            wechat_remark=wechat_remark,
            contact_date=contact_date_val or datetime.now().date(),
            suggested_followup_date=followup_date_val,
            profile_status=1,
            profiled_at=datetime.now(),
        )
        db.add(rel)
    else:
        rel.ai_profile = p.get("ai_profile")
        rel.title = p.get("contact_title") or rel.title
        if budget_num is not None:
            rel.budget_amount = Decimal(str(budget_num))
        if p.get("purchase_type"):
            rel.purchase_type = str(p.get("purchase_type"))
        if wechat_remark:
            rel.wechat_remark = wechat_remark
        if contact_date_val:
            rel.contact_date = contact_date_val
        if followup_date_val:
            rel.suggested_followup_date = followup_date_val
        rel.profile_status = 1
        rel.profiled_at = rel.profiled_at or datetime.now()

    await db.flush()
    await crud_ops.replace_ucr_profile_tags(
        db, rel, p.get("matched_profile_tag_ids"), require_active=True
    )


async def execute_profile_batch(batch: dict[str, Any]) -> None:
    """队列消费者调用的单批执行入口。"""
    kind = (batch.get("kind") or "").strip()
    meta = batch.get("meta") or {}
    if kind == "pairs":
        await _run_profile_job_for_pairs(batch.get("pairs") or [], batch_meta=meta)
    elif kind == "raw_ids":
        await _run_profile_job_for_raw_ids(
            batch.get("raw_ids") or [],
            preferred_sales_wechat_by_raw_id=batch.get("preferred_sales_wechat_by_raw_id"),
            batch_meta=meta,
        )
    else:
        from ai.profiling_progress import fail_job

        fail_job(f"未知画像批次类型: {kind or '(空)'}")


async def _run_profile_job_for_raw_ids(
    raw_ids: list[str],
    *,
    preferred_sales_wechat_by_raw_id: dict[str, str] | None = None,
    batch_meta: dict[str, Any] | None = None,
) -> None:
    """按 raw_customers.id 逐个画像并同步主库（由队列串行调度）。"""
    from ai.profiling_progress import (
        complete,
        fail_job,
        is_cancel_requested,
        record_fail,
        record_skip,
        record_success,
        reset_for_start,
        set_current,
    )
    from database import AsyncSessionLocal

    ids = [(r or "").strip() for r in raw_ids if (r or "").strip()]
    reset_for_start(len(ids), batch_meta)
    if not ids:
        complete()
        return

    cancelled = False
    try:
        async with AsyncSessionLocal() as db:
            user_map = await get_user_id_map(db)
            llm = await get_llm_client(db)

            preferred_sales_wechat_by_raw_id = preferred_sales_wechat_by_raw_id or {}
            for rid in ids:
                if is_cancel_requested():
                    cancelled = True
                    break
                set_current(rid)
                try:
                    res = await db.execute(select(RawCustomer).where(RawCustomer.id == rid))
                    raw = res.scalar_one_or_none()
                    if not raw:
                        logger.warning("画像失败：无此原始客户 raw_id={}", rid)
                        record_fail("无此原始客户", target=rid)
                        continue

                    sw = (preferred_sales_wechat_by_raw_id.get(rid) or "").strip()
                    if not sw:
                        sw = (raw.sales_wechat_id or "").strip()
                    if not sw:
                        sw_res = await db.execute(
                            select(RawCustomerSalesWechat.sales_wechat_id)
                            .where(
                                RawCustomerSalesWechat.raw_customer_id == raw.id,
                                or_(
                                    RawCustomerSalesWechat.is_deleted.is_(False),
                                    RawCustomerSalesWechat.is_deleted.is_(None),
                                ),
                            )
                            .order_by(RawCustomerSalesWechat.id.asc())
                            .limit(1)
                        )
                        sw = (sw_res.scalar_one_or_none() or "").strip()
                        if not sw:
                            logger.warning(
                                "画像失败：缺少 sales_wechat_id 且无映射 raw_id={}",
                                rid,
                            )
                            record_fail(
                                "原始客户缺少 sales_wechat_id，且映射表无归属，已跳过",
                                target=rid,
                            )
                            continue
                    snap_res = await db.execute(
                        select(RawCustomerSalesWechat)
                        .where(
                            RawCustomerSalesWechat.raw_customer_id == rid,
                            RawCustomerSalesWechat.sales_wechat_id == sw,
                        )
                        .limit(1)
                    )
                    snap = snap_res.scalars().first()
                    if _rcsw_relation_inactive(snap):
                        logger.info(
                            "画像跳过：该销售好友关系已删除 raw_id={} sales_wechat_id={}",
                            rid,
                            sw,
                        )
                        record_skip()
                        continue
                    uid = user_map.get(sw)
                    if uid is None:
                        logger.warning(
                            "画像继续：销售微信号未绑定用户，将以 user_id=NULL 写入 raw_id={} sales_wechat_id={}",
                            rid,
                            sw,
                        )

                    p = await profile_raw_customer_with_llm(
                        db,
                        llm,
                        raw,
                        sales_wechat_id_override=sw or None,
                        rcsw_snapshot=snap,
                    )
                    if not p:
                        await db.rollback()
                        logger.warning("画像失败：LLM 无有效结果 raw_id={}", rid)
                        record_fail("LLM 无有效结果（解析失败或无 JSON）", target=rid)
                        continue

                    await apply_profile_to_main(db, p, user_id=uid)
                    await db.execute(
                        update(RawCustomer).where(RawCustomer.id == rid).values(profile_status=1)
                    )
                    await db.commit()
                    record_success()
                except Exception:
                    logger.exception("Profile job failed for raw_id={}", rid)
                    await db.rollback()
                    record_fail(
                        "单条处理异常",
                        target=rid,
                        detail=traceback.format_exc(),
                    )
    except Exception as e:
        logger.exception("Profile batch aborted: {}", e)
        fail_job(f"{type(e).__name__}: {e}\n{traceback.format_exc()[:600]}")
        return

    complete(cancelled=cancelled)


async def _run_profile_job_for_pairs(
    pairs: list[tuple[str, str]],
    *,
    batch_meta: dict[str, Any] | None = None,
) -> None:
    """
    按 (raw_customer_id, sales_wechat_id) 逐个画像并同步主库。

    与 raw_ids 批次的区别：同一 raw_id 在多个销售号下会分别写入不同 SCP；sales_wechat_id 由 pair 显式指定。
    """
    from ai.profiling_progress import (
        complete,
        fail_job,
        is_cancel_requested,
        record_fail,
        record_skip,
        record_success,
        reset_for_start,
        set_current,
    )
    from database import AsyncSessionLocal

    cleaned: list[tuple[str, str]] = []
    for rid, sw in pairs or []:
        rid = (rid or "").strip()
        sw = (sw or "").strip()
        if rid and sw:
            cleaned.append((rid, sw))

    reset_for_start(len(cleaned), batch_meta)
    if not cleaned:
        complete()
        return

    cancelled = False
    try:
        async with AsyncSessionLocal() as db:
            user_map = await get_user_id_map(db)
            llm = await get_llm_client(db)

            for rid, sw in cleaned:
                if is_cancel_requested():
                    cancelled = True
                    break
                set_current(f"{rid}|{sw}")
                try:
                    res = await db.execute(select(RawCustomer).where(RawCustomer.id == rid))
                    raw = res.scalar_one_or_none()
                    if not raw:
                        logger.warning("画像失败：无此原始客户 raw_id={}", rid)
                        record_fail("无此原始客户", target=f"{rid}|{sw}")
                        continue

                    snap_res = await db.execute(
                        select(RawCustomerSalesWechat)
                        .where(
                            RawCustomerSalesWechat.raw_customer_id == rid,
                            RawCustomerSalesWechat.sales_wechat_id == sw,
                        )
                        .limit(1)
                    )
                    snap = snap_res.scalars().first()
                    if _rcsw_relation_inactive(snap):
                        logger.info(
                            "画像跳过：该销售好友关系已删除 raw_id={} sales_wechat_id={}",
                            rid,
                            sw,
                        )
                        record_skip()
                        continue

                    uid = user_map.get(sw)
                    if uid is None:
                        logger.warning(
                            "画像继续：销售微信号未绑定用户，将以 user_id=NULL 写入 raw_id={} sales_wechat_id={}",
                            rid,
                            sw,
                        )

                    p = await profile_raw_customer_with_llm(
                        db,
                        llm,
                        raw,
                        sales_wechat_id_override=sw,
                        rcsw_snapshot=snap,
                    )
                    if not p:
                        await db.rollback()
                        logger.warning("画像失败：LLM 无有效结果 raw_id={} sales_wechat_id={}", rid, sw)
                        record_fail(
                            "LLM 无有效结果（解析失败或无 JSON）",
                            target=f"{rid}|{sw}",
                        )
                        continue

                    await apply_profile_to_main(db, p, user_id=uid)
                    await db.execute(
                        update(RawCustomer).where(RawCustomer.id == rid).values(profile_status=1)
                    )
                    await db.commit()
                    record_success()
                except Exception:
                    logger.exception("Profile job failed for raw_id={} sales_wechat_id={}", rid, sw)
                    await db.rollback()
                    record_fail(
                        "单条处理异常",
                        target=f"{rid}|{sw}",
                        detail=traceback.format_exc(),
                    )
    except Exception as e:
        logger.exception("Profile batch aborted: {}", e)
        fail_job(f"{type(e).__name__}: {e}\n{traceback.format_exc()[:600]}")
        return

    complete(cancelled=cancelled)


def schedule_profile_raw_customers(raw_ids: list[str]) -> None:
    """在事件循环中投递后台画像任务（避免管理后台 HTTP 超时）。"""
    asyncio.create_task(_enqueue_raw_ids_batch(raw_ids))


async def enqueue_profile_sales_pairs(
    pairs: list[tuple[str, str]],
    label: str = "per-sales 画像",
) -> None:
    """将 (raw_id, sales_wechat_id) 批次放入画像队列（DB 持久化，支持多 worker 并行）。"""
    from ai.profiling_progress import new_batch_meta
    from ai.profile_queue import enqueue_pairs

    cleaned: list[tuple[str, str]] = []
    for rid, sw in pairs or []:
        rid = (rid or "").strip()
        sw = (sw or "").strip()
        if rid and sw:
            cleaned.append((rid, sw))
    if not cleaned:
        return

    meta = new_batch_meta("pairs", len(cleaned), label)
    await enqueue_pairs(cleaned, batch_id=str(meta.get("batch_id") or ""), batch_label=str(meta.get("label") or ""))


def schedule_profile_raw_customer_sales_pairs(
    pairs: list[tuple[str, str]],
    label: str = "per-sales 画像",
) -> None:
    """
    后台画像任务：显式指定 (raw_customer_id, sales_wechat_id)。
    无运行中事件循环时请改用 enqueue_profile_sales_pairs 的 await 调用。
    """
    asyncio.create_task(enqueue_profile_sales_pairs(pairs or [], label))


async def _enqueue_pairs_batch(pairs: list[tuple[str, str]], label: str) -> None:
    # legacy: in-memory queue is deprecated; keep as wrapper for backward compatibility
    await enqueue_profile_sales_pairs(pairs or [], label=label)


async def _enqueue_raw_ids_batch(raw_ids: list[str]) -> None:
    # 兼容入口：raw_ids 会被展开为 pairs（需要解析 sales_wechat_id），再入库队列
    from ai.profiling_progress import new_batch_meta
    from database import AsyncSessionLocal
    from sqlalchemy.future import select
    from sqlalchemy import or_
    from models import RawCustomer, RawCustomerSalesWechat
    from ai.profile_queue import enqueue_pairs

    ids = [(r or "").strip() for r in raw_ids if (r or "").strip()]
    if not ids:
        return

    pairs: list[tuple[str, str]] = []
    async with AsyncSessionLocal() as db:
        for rid in ids:
            res = await db.execute(select(RawCustomer).where(RawCustomer.id == rid))
            raw = res.scalar_one_or_none()
            if not raw:
                continue
            sw = (raw.sales_wechat_id or "").strip()
            if not sw:
                sw_res = await db.execute(
                    select(RawCustomerSalesWechat.sales_wechat_id)
                    .where(
                        RawCustomerSalesWechat.raw_customer_id == raw.id,
                        or_(
                            RawCustomerSalesWechat.is_deleted.is_(False),
                            RawCustomerSalesWechat.is_deleted.is_(None),
                        ),
                    )
                    .order_by(RawCustomerSalesWechat.id.asc())
                    .limit(1)
                )
                sw = (sw_res.scalar_one_or_none() or "").strip()
            if sw:
                pairs.append((rid, sw))

    if not pairs:
        return
    meta = new_batch_meta("pairs", len(pairs), "raw_id 批次")
    await enqueue_pairs(
        pairs,
        batch_id=str(meta.get("batch_id") or ""),
        batch_label=str(meta.get("label") or ""),
    )


async def collect_unprofiled_work(
    sales_wechat_ids: list[str] | None,
) -> list[tuple[str, str]]:
    """
    收集待画像的 (raw_customer_id, sales_wechat_id)。

    「未画像」一律按 per-sales 的 SalesCustomerProfile 判定：无 SCP 或 profile_status=0；
    且仅包含 raw_customer_sales_wechats 中未删好友关系的行。
    全库批任务与指定销售号批任务使用同一套逻辑，不再依赖 raw_customers.profile_status，
    避免去重客户实体导致「多销售只跑一条」的数据丢失。
    """
    from database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        pair_stmt = (
            select(
                RawCustomerSalesWechat.raw_customer_id,
                RawCustomerSalesWechat.sales_wechat_id,
            )
            .outerjoin(
                SalesCustomerProfile,
                and_(
                    SalesCustomerProfile.raw_customer_id
                    == RawCustomerSalesWechat.raw_customer_id,
                    SalesCustomerProfile.sales_wechat_id
                    == RawCustomerSalesWechat.sales_wechat_id,
                ),
            )
            .where(
                or_(
                    RawCustomerSalesWechat.is_deleted.is_(False),
                    RawCustomerSalesWechat.is_deleted.is_(None),
                ),
                or_(
                    SalesCustomerProfile.id.is_(None),
                    SalesCustomerProfile.profile_status == 0,
                ),
            )
        )
        if sales_wechat_ids:
            cleaned = [(s or "").strip() for s in sales_wechat_ids if (s or "").strip()]
            if not cleaned:
                return []
            pair_stmt = pair_stmt.where(RawCustomerSalesWechat.sales_wechat_id.in_(cleaned))
        pair_stmt = pair_stmt.distinct()
        res = await db.execute(pair_stmt)
        return [(r[0], r[1]) for r in res.all() if r and r[0] and r[1]]


async def run_profile_all_unprofiled(sales_wechat_ids: list[str] | None = None) -> None:
    """找出未画像 (客户, 销售号) 对并入队；实际执行由队列 worker 顺序处理。"""
    pairs = await collect_unprofiled_work(sales_wechat_ids)
    if pairs:
        await _enqueue_pairs_batch(
            pairs,
            "未画像 · 指定销售号" if sales_wechat_ids else "未画像 · 全库 per-sales",
        )


def schedule_profile_all_unprofiled(sales_wechat_ids: list[str] | None = None) -> None:
    """投递未画像分析：按 (raw_id, sales_wechat_id) 入队；sales_wechat_ids 非空时限定销售号。"""
    asyncio.create_task(run_profile_all_unprofiled(sales_wechat_ids=sales_wechat_ids))
