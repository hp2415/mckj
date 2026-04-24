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

from sqlalchemy import or_, update, and_

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
from schemas import normalize_purchase_months

logger = logging.getLogger(__name__)

_run_lock = asyncio.Lock()

API_HOST = "api.chatool.micheng.cn"
AUTH_TOKEN = "1031bdbd-337a-4a85-88d0-4004804e168a"

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
        + "请结合基础信息、聊天记录与订单判断符合的标签（可多个），并在 JSON 中输出 matched_profile_tag_ids（整数数组，仅使用上文列出的 id）。\n"
    )


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


async def profile_raw_customer_with_llm(
    db,
    llm: LLMClient,
    raw: RawCustomer,
    *,
    sales_wechat_id_override: str | None = None,
    rcsw_snapshot: RawCustomerSalesWechat | None = None,
) -> dict[str, Any] | None:
    logger.info(
        "画像分析 LLM model=%s raw_id=%s（配置项 llm_model，与桌面对话 chat_model 无关）",
        llm.model,
        raw.id,
    )
    chats = await get_chat_context(db, raw.id)
    # 优先使用 per-sales 快照电话，避免 raw_customers 去重快照 phone 为空导致订单拉取失败
    phone_for_orders = (getattr(rcsw_snapshot, "phone", None) or raw.phone) if rcsw_snapshot else raw.phone
    orders = await fetch_orders_with_sync(db, phone_for_orders)

    order_text = []
    for o in orders:
        products = ", ".join([g.get("product_name", "") for g in o.get("goodsInfo", [])])
        order_text.append(
            f"- {o.get('order_time')}: {o.get('status_name')}, 金额:{o.get('pay_amount')}, 产品:[{products}]"
        )

    # 基础信息优先取 per-sales 快照（同一客户在不同销售号下 remark/phone/note_des 可能不同）
    remark = (getattr(rcsw_snapshot, "remark", None) if rcsw_snapshot else None) or raw.remark
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
            logger.info("PROFILE_AUDIT_REQUEST %s", json.dumps(audit, ensure_ascii=False))
        except Exception:
            logger.exception("PROFILE_AUDIT_REQUEST log failed raw_id=%s", raw.id)

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
                    "PROFILE_AUDIT_RESPONSE %s",
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
                logger.exception("PROFILE_AUDIT_RESPONSE log failed raw_id=%s", raw.id)
        return data
    except Exception as e:
        logger.exception("LLM profile failed for raw %s: %s", raw.id, e)
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


async def run_profile_job_for_raw_ids(
    raw_ids: list[str],
    *,
    preferred_sales_wechat_by_raw_id: dict[str, str] | None = None,
) -> None:
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

                preferred_sales_wechat_by_raw_id = preferred_sales_wechat_by_raw_id or {}
                for rid in ids:
                    set_current(rid)
                    try:
                        res = await db.execute(select(RawCustomer).where(RawCustomer.id == rid))
                        raw = res.scalar_one_or_none()
                        if not raw:
                            logger.warning("画像失败：无此原始客户 raw_id=%s", rid)
                            record_fail(f"无此原始客户: {rid}")
                            continue
                        if _raw_is_deleted(raw):
                            logger.info("画像跳过已删除客户 raw_id=%s", rid)
                            record_skip()
                            continue

                        # 最优先：触发入口（per-sales 行）传入的 sales_wechat_id
                        sw = (preferred_sales_wechat_by_raw_id.get(rid) or "").strip()
                        if not sw:
                            sw = (raw.sales_wechat_id or "").strip()
                        if not sw:
                            sw_res = await db.execute(
                                select(RawCustomerSalesWechat.sales_wechat_id)
                                .where(RawCustomerSalesWechat.raw_customer_id == raw.id)
                                .order_by(RawCustomerSalesWechat.id.asc())
                                .limit(1)
                            )
                            sw = (sw_res.scalar_one_or_none() or "").strip()
                            if not sw:
                                logger.warning(
                                    "画像失败：缺少 sales_wechat_id 且无映射 raw_id=%s",
                                    rid,
                                )
                                record_fail("原始客户缺少 sales_wechat_id，且映射表无归属，已跳过")
                                continue
                        uid = user_map.get(sw)
                        if uid is None:
                            # 允许未绑定用户仍执行画像：数据会落到 per-sales 的 SalesCustomerProfile，
                            # 但 user_id 为 NULL，后续补绑定后可再回填归属。
                            logger.warning(
                                "画像继续：销售微信号未绑定用户，将以 user_id=NULL 写入 raw_id=%s sales_wechat_id=%s",
                                rid,
                                sw,
                            )

                        p = await profile_raw_customer_with_llm(
                            db,
                            llm,
                            raw,
                            sales_wechat_id_override=sw or None,
                        )
                        if not p:
                            await db.rollback()
                            logger.warning("画像失败：LLM 无有效结果 raw_id=%s", rid)
                            record_fail("LLM 无有效结果")
                            continue

                        await apply_profile_to_main(db, p, user_id=uid)
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


async def run_profile_job_for_pairs(pairs: list[tuple[str, str]]) -> None:
    """
    后台任务：按 (raw_customer_id, sales_wechat_id) 逐个画像并同步主库。

    与 run_profile_job_for_raw_ids 的区别：
    - 同一个 raw_id 在多个销售号下会被处理多次（分别写入不同 sales_wechat_id 的 SCP）。
    - sales_wechat_id 由 pair 显式指定，避免落错销售号。
    """
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
        cleaned: list[tuple[str, str]] = []
        for rid, sw in pairs or []:
            rid = (rid or "").strip()
            sw = (sw or "").strip()
            if rid and sw:
                cleaned.append((rid, sw))

        reset_for_start(len(cleaned))
        if not cleaned:
            complete()
            return

        try:
            async with AsyncSessionLocal() as db:
                user_map = await get_user_id_map(db)
                llm = await get_llm_client(db)

                for rid, sw in cleaned:
                    set_current(f"{rid}|{sw}")
                    try:
                        res = await db.execute(select(RawCustomer).where(RawCustomer.id == rid))
                        raw = res.scalar_one_or_none()
                        if not raw:
                            logger.warning("画像失败：无此原始客户 raw_id=%s", rid)
                            record_fail(f"无此原始客户: {rid}")
                            continue
                        if _raw_is_deleted(raw):
                            logger.info("画像跳过已删除客户 raw_id=%s sales_wechat_id=%s", rid, sw)
                            record_skip()
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

                        uid = user_map.get(sw)
                        if uid is None:
                            logger.warning(
                                "画像继续：销售微信号未绑定用户，将以 user_id=NULL 写入 raw_id=%s sales_wechat_id=%s",
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
                            logger.warning("画像失败：LLM 无有效结果 raw_id=%s sales_wechat_id=%s", rid, sw)
                            record_fail("LLM 无有效结果")
                            continue

                        await apply_profile_to_main(db, p, user_id=uid)
                        await db.execute(
                            update(RawCustomer).where(RawCustomer.id == rid).values(profile_status=1)
                        )
                        await db.commit()
                        record_success()
                    except Exception:
                        logger.exception("Profile job failed for raw_id=%s sales_wechat_id=%s", rid, sw)
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


def schedule_profile_raw_customer_sales_pairs(pairs: list[tuple[str, str]]) -> None:
    """
    后台画像任务（最推荐入口）：显式指定 (raw_customer_id, sales_wechat_id)。
    这样画像一定会落到正确销售号下，桌面端列表 join 不会不稳定。
    """
    asyncio.create_task(run_profile_job_for_pairs(pairs or []))


async def run_profile_all_unprofiled(sales_wechat_ids: list[str] | None = None) -> None:
    """找出所有未画像的原始客户并开始画像。可选仅处理给定销售微信号列表。"""
    from database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        # 注意：RawCustomer.profile_status 是“客户实体”层面的旧字段；这里仍做粗筛以减少扫描量。
        # per-sales 的“未画像”判定在 sales_wechat_ids 分支中改为基于 SCP.profile_status。
        stmt = select(RawCustomer.id).where(
            RawCustomer.profile_status == 0,
            or_(RawCustomer.is_deleted.is_(False), RawCustomer.is_deleted.is_(None)),
        )
        if sales_wechat_ids:
            cleaned = [(s or "").strip() for s in sales_wechat_ids if (s or "").strip()]
            if cleaned:
                # 关键修复：按映射表筛选，避免 raw_customers.sales_wechat_id 快照导致漏数
                pair_stmt = (
                    select(
                        RawCustomerSalesWechat.raw_customer_id,
                        RawCustomerSalesWechat.sales_wechat_id,
                    )
                    .join(RawCustomer, RawCustomer.id == RawCustomerSalesWechat.raw_customer_id)
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
                        or_(RawCustomer.is_deleted.is_(False), RawCustomer.is_deleted.is_(None)),
                        RawCustomerSalesWechat.sales_wechat_id.in_(cleaned),
                        # per-sales 未画像：无 SCP 或 SCP.profile_status=0
                        or_(
                            SalesCustomerProfile.id.is_(None),
                            SalesCustomerProfile.profile_status == 0,
                        ),
                    )
                    .distinct()
                )
                res = await db.execute(pair_stmt)
                pairs = [(r[0], r[1]) for r in res.all() if r and r[0] and r[1]]
                if pairs:
                    await run_profile_job_for_pairs(pairs)
                return

        res = await db.execute(stmt)
        ids = res.scalars().all()
        if ids:
            await run_profile_job_for_raw_ids(list(ids))


def schedule_profile_all_unprofiled(sales_wechat_ids: list[str] | None = None) -> None:
    """投递全量未画像客户分析任务；sales_wechat_ids 非空时仅处理这些销售号下的未画像客户。"""
    asyncio.create_task(run_profile_all_unprofiled(sales_wechat_ids=sales_wechat_ids))
