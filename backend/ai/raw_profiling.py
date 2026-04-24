"""
原始客户(RawCustomer) LLM 画像：拉取聊天与订单上下文，写回主库 Customer / UserCustomerRelation，并更新 raw_customers.profile_status。
"""
from __future__ import annotations

import asyncio
import json
import http.client
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, update
from sqlalchemy.future import select

from models import (
    RawCustomer,
    RawChatLog,
    RawOrder,
    RawOrderItem,
    Customer,
    UserCustomerRelation,
    User,
    SystemConfig,
)
from ai.llm_client import LLMClient
from ai.prompt_seed import CUSTOMER_PROFILE_SYSTEM, CUSTOMER_PROFILE_USER
from schemas import normalize_purchase_months

logger = logging.getLogger(__name__)

_run_lock = asyncio.Lock()

API_HOST = "api.chatool.micheng.cn"
AUTH_TOKEN = "1031bdbd-337a-4a85-88d0-4004804e168a"


def _raw_is_deleted(raw: RawCustomer) -> bool:
    """客户池 raw_customers.is_deleted：库内多为布尔或 0/1，为真则视为已删除。"""
    v = getattr(raw, "is_deleted", None)
    if v is None or v is False:
        return False
    if v is True:
        return True
    try:
        return int(v) == 1
    except (TypeError, ValueError):
        return bool(v)


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
) -> list[dict]:
    """组装画像 LLM 消息：优先使用管理平台场景 customer_profile（published）。"""
    from ai.prompt_models import PromptTemplate
    from ai.prompt_renderer import render_system
    from ai.prompt_store import get_prompt_store

    ctx = {
        "basic_info": basic_info,
        "chat_context": chat_context,
        "order_context": order_context,
    }
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
            return [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ]

    user_text = render_system(
        PromptTemplate(system=CUSTOMER_PROFILE_USER.strip()),
        ctx,
        {},
        (),
    )
    return [
        {"role": "system", "content": CUSTOMER_PROFILE_SYSTEM},
        {"role": "user", "content": user_text},
    ]


async def get_llm_client(db) -> LLMClient:
    """画像分析专用：仅使用 llm_model（与桌面对话的 chat_model / llm_chat_model 无关）。"""
    stmt = select(SystemConfig).where(SystemConfig.config_group == "ai")
    res = await db.execute(stmt)
    configs = {c.config_key: c.config_value for c in res.scalars().all()}
    api_url = configs.get("llm_api_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    api_key = configs.get("llm_api_key", "")
    model = configs.get("llm_model", "qwen-max")
    return LLMClient(api_url=api_url, api_key=api_key, model=model)


async def fetch_orders_with_sync(db, phone: str | None) -> list[dict[str, Any]]:
    if not phone:
        return []
    phone = "".join(filter(str.isdigit, str(phone)))
    if len(phone) < 7:
        return []

    try:
        conn = http.client.HTTPSConnection(API_HOST)
        payload = json.dumps({"phone": phone, "page": 1, "page_size": 50})
        headers = {"Authorization": AUTH_TOKEN, "Content-Type": "application/json"}
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


async def get_chat_context(db, customer_id: str) -> str:
    stmt = (
        select(RawChatLog)
        .where((RawChatLog.talker == customer_id) | (RawChatLog.wechat_id == customer_id))
        .order_by(RawChatLog.timestamp.desc())
        .limit(50)
    )
    res = await db.execute(stmt)
    logs = res.scalars().all()
    context_lines = []
    for l in reversed(logs):
        sender = "客户" if l.is_send == 0 else "工作人员"
        context_lines.append(f"{sender}: {l.text}")
    return "\n".join(context_lines)


async def profile_raw_customer_with_llm(db, llm: LLMClient, raw: RawCustomer) -> dict[str, Any] | None:
    logger.info(
        "画像分析 LLM model=%s raw_id=%s（配置项 llm_model，与桌面对话 chat_model 无关）",
        llm.model,
        raw.id,
    )
    chats = await get_chat_context(db, raw.id)
    orders = await fetch_orders_with_sync(db, raw.phone)

    order_text = []
    for o in orders:
        products = ", ".join([g.get("product_name", "") for g in o.get("goodsInfo", [])])
        order_text.append(
            f"- {o.get('order_time')}: {o.get('status_name')}, 金额:{o.get('pay_amount')}, 产品:[{products}]"
        )

    add_time_str = raw.add_time.strftime("%Y-%m-%d") if raw.add_time else "未知"
    basic_info = (
        f"原始ID: {raw.id}, 微信备注/名: {raw.remark}/{raw.name}, "
        f"预存电话: {raw.phone}, 详细描述: {raw.note_des}, 标签: {raw.label}, 地区: {raw.region}, "
        f"微信加好友时间(建联日期): {add_time_str}, 当前日期: {datetime.now().strftime('%Y-%m-%d')}"
    )

    chat_block = chats if chats else "暂无最近聊天记录"
    order_block = "\n".join(order_text) if order_text else "暂无历史订单记录"
    messages = await build_profile_chat_messages(db, basic_info, chat_block, order_block)

    try:
        full_content = ""
        async for chunk in llm.stream_chat(messages):
            if not chunk.startswith("__TOOL_CALL__"):
                full_content += chunk

        start = full_content.find("{")
        end = full_content.rfind("}")
        if start == -1 or end == -1:
            return None
        data = json.loads(full_content[start : end + 1])
        data["raw_id"] = raw.id
        data["sales_wechat_id"] = raw.sales_wechat_id
        return data
    except Exception as e:
        logger.exception("LLM profile failed for raw %s: %s", raw.id, e)
        return None


async def get_user_id_map(db) -> dict[str, int]:
    stmt = select(User).where(User.wechat_id.is_not(None))
    res = await db.execute(stmt)
    users = res.scalars().all()
    return {u.wechat_id: u.id for u in users if u.wechat_id}


async def apply_profile_to_main(
    db,
    p: dict[str, Any],
    *,
    user_map: dict[str, int],
    default_user_id: int = 1,
) -> None:
    """将单条画像结果写入 Customer / UserCustomerRelation（不 commit）。"""
    raw_id = p.get("raw_id")
    sales_wx_id = p.get("sales_wechat_id")
    user_id = user_map.get(sales_wx_id, default_user_id)

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
    wechat_remark = rc.remark if rc else None

    contact_date_val = rc.add_time.date() if rc and rc.add_time else None

    followup_str = p.get("suggested_followup_date")
    followup_date_val = None
    if followup_str:
        try:
            followup_date_val = datetime.strptime(str(followup_str).strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass

    stmt = select(Customer).where(Customer.external_id == raw_id)
    res = await db.execute(stmt)
    customer = res.scalar_one_or_none()

    if not customer:
        if phone:
            stmt_p = select(Customer).where(Customer.phone == phone)
            res_p = await db.execute(stmt_p)
            if res_p.scalar_one_or_none():
                phone = f"{raw_id}_surrogate"

        customer = Customer(
            external_id=raw_id,
            phone=phone,
            customer_name=p.get("contact_name") or "未命名",
            unit_name=p.get("entity_name") or "未知单位",
            unit_type=p.get("entity_type"),
            admin_division=p.get("region_info"),
            purchase_months=purchase_months,
            profile_status=1,
        )
        db.add(customer)
        await db.flush()
    else:
            if p.get("contact_name"):
                customer.customer_name = p.get("contact_name") or customer.customer_name
            if p.get("entity_name"):
                customer.unit_name = p.get("entity_name") or customer.unit_name
            customer.unit_type = p.get("entity_type") or customer.unit_type
            customer.admin_division = p.get("region_info") or customer.admin_division
            customer.purchase_months = purchase_months or customer.purchase_months
            customer.profile_status = 1
            if p.get("contact_tel"):
                customer.phone = p.get("contact_tel")

    stmt_rel = select(UserCustomerRelation).where(
        (UserCustomerRelation.customer_id == customer.id) & (UserCustomerRelation.user_id == user_id)
    )
    res_rel = await db.execute(stmt_rel)
    rel = res_rel.scalar_one_or_none()

    budget_val = p.get("budget")
    budget_num = None
    if budget_val is not None and str(budget_val).replace(".", "", 1).isdigit():
        budget_num = float(budget_val)

    if not rel:
        rel = UserCustomerRelation(
            user_id=user_id,
            customer_id=customer.id,
            ai_profile=p.get("ai_profile"),
            title=p.get("contact_title"),
            budget_amount=Decimal(str(budget_num)) if budget_num is not None else Decimal("0.00"),
            purchase_type=str(p.get("purchase_type")) if p.get("purchase_type") else None,
            relation_type="active",
            wechat_remark=wechat_remark,
            contact_date=contact_date_val or datetime.now().date(),
            suggested_followup_date=followup_date_val,
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


async def run_profile_job_for_raw_ids(raw_ids: list[str]) -> None:
    """后台任务：按 raw_customers.id（微信侧 ID）逐个画像并同步主库。"""
    from ai.profiling_progress import (
        complete,
        fail_job,
        record_fail,
        record_skip,
        record_success,
        reset_for_start,
        set_current,
    )
    from database import AsyncSessionLocal

    async with _run_lock:
        ids = [(r or "").strip() for r in raw_ids if (r or "").strip()]
        reset_for_start(len(ids))
        if not ids:
            complete()
            return

        try:
            async with AsyncSessionLocal() as db:
                user_map = await get_user_id_map(db)
                llm = await get_llm_client(db)

                for rid in ids:
                    set_current(rid)
                    try:
                        res = await db.execute(select(RawCustomer).where(RawCustomer.id == rid))
                        raw = res.scalar_one_or_none()
                        if not raw:
                            record_fail(f"无此原始客户: {rid}")
                            continue
                        if _raw_is_deleted(raw):
                            logger.info("画像跳过已删除客户 raw_id=%s", rid)
                            record_skip()
                            continue

                        p = await profile_raw_customer_with_llm(db, llm, raw)
                        if not p:
                            await db.rollback()
                            record_fail("LLM 无有效结果")
                            continue

                        await apply_profile_to_main(db, p, user_map=user_map)
                        await db.execute(
                            update(RawCustomer).where(RawCustomer.id == rid).values(profile_status=1)
                        )
                        await db.commit()
                        record_success()
                    except Exception:
                        logger.exception("Profile job failed for raw_id=%s", rid)
                        await db.rollback()
                        record_fail("单条处理异常")
        except Exception as e:
            logger.exception("Profile batch aborted: %s", e)
            fail_job(str(e))
        else:
            complete()


def schedule_profile_raw_customers(raw_ids: list[str]) -> None:
    """在事件循环中投递后台画像任务（避免管理后台 HTTP 超时）。"""
    asyncio.create_task(run_profile_job_for_raw_ids(raw_ids))


async def run_profile_all_unprofiled() -> None:
    """找出所有未画像的原始客户并开始画像。"""
    from database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        stmt = select(RawCustomer.id).where(
            RawCustomer.profile_status == 0,
            or_(RawCustomer.is_deleted.is_(False), RawCustomer.is_deleted.is_(None)),
        )
        res = await db.execute(stmt)
        ids = res.scalars().all()
        if ids:
            await run_profile_job_for_raw_ids(list(ids))


def schedule_profile_all_unprofiled() -> None:
    """投递全量未画像客户分析任务。"""
    asyncio.create_task(run_profile_all_unprofiled())
