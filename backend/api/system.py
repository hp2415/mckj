from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from database import get_db
from api.auth import get_current_user
from models import User, SystemConfig
from core.tasks import fetch_and_sync_832_products
from core.wechat_friends_sync import (
    CFG_PARTNER,
    CFG_QUERY_MODE,
    CFG_TARGET_DAY,
    MSG_KEY,
    STATUS_KEY,
    SUCCESS_KEY,
    sync_wechat_friends_for_calendar_day,
)
from core.wechat_chat_sync import (
    CFG_CHAT_CURSOR_CREATE,
    CFG_CHAT_CURSOR_TIME,
    CFG_CHAT_LAST_MSG,
    CFG_CHAT_LAST_OK,
    CFG_CHAT_STATUS,
    sync_wechat_chat_increment,
)
from ai.chat_models_catalog import chat_models_for_api_payload
import os
import re
import time
import hashlib
import httpx

router = APIRouter(prefix="/api/system", tags=["System"])

@router.get("/desktop/latest")
async def get_desktop_latest_release(db: AsyncSession = Depends(get_db)):
    """
    桌面端启动更新检查（无需登录）。

    优先读取数据库 SystemConfig 配置，便于在管理后台动态修改；
    若数据库未配置，则回退读取环境变量：
    - DESKTOP_LATEST_VERSION
    - DESKTOP_INSTALLER_URL
    - DESKTOP_FORCE_UPDATE
    - DESKTOP_RELEASE_NOTES
    """
    keys = [
        "desktop_latest_version",
        "desktop_installer_url",
        "desktop_force_update",
        "desktop_release_notes",
    ]
    stmt = select(SystemConfig).where(SystemConfig.config_key.in_(keys))
    res = await db.execute(stmt)
    db_configs = {c.config_key: (c.config_value or "") for c in res.scalars().all()}

    version = (db_configs.get("desktop_latest_version") or os.getenv("DESKTOP_LATEST_VERSION") or "").strip()
    download_url = (db_configs.get("desktop_installer_url") or os.getenv("DESKTOP_INSTALLER_URL") or "").strip()
    force_str = (db_configs.get("desktop_force_update") or os.getenv("DESKTOP_FORCE_UPDATE") or "true").strip().lower()
    notes = (db_configs.get("desktop_release_notes") or os.getenv("DESKTOP_RELEASE_NOTES") or "").strip()

    if not version or not download_url:
        # 未配置更新信息时，返回 200 但不提供 data（客户端会放行，方便开发/内网）
        return {"code": 200, "data": {"version": "", "download_url": "", "force": True, "notes": ""}}

    return {
        "code": 200,
        "data": {
            "version": version,
            "download_url": download_url,
            "force": (force_str != "false"),
            "notes": notes,
        },
    }

@router.get("/sync/status")
async def get_sync_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    查询云端货源同步新鲜度。
    """
    stmt = select(SystemConfig).where(SystemConfig.config_group == "sync")
    result = await db.execute(stmt)
    configs = result.scalars().all()
    
    config_map = {c.config_key: c.config_value for c in configs}
    max_updated = max([c.updated_at for c in configs]) if configs else None
    
    failed_suppliers_str = config_map.get("sync_failed_suppliers", "")
    failed_suppliers = [s.strip() for s in failed_suppliers_str.split(",") if s.strip()]
    
    return {
        "status": config_map.get("sync_status", "idle"),
        "last_success": config_map.get("sync_last_success", "从未同步"),
        "message": config_map.get("sync_last_message", "就绪"),
        "failed_count": len(failed_suppliers),
        "failed_suppliers": failed_suppliers,
        "last_updated_at": max_updated.strftime("%Y-%m-%d %H:%M:%S") if max_updated else "无记录"
    }

@router.post("/sync/trigger")
async def trigger_sync(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user)
):
    """
    手动触发全量货源同步 (异步执行)。
    """
    if current_user.role != "admin":
        return {"code": 403, "message": "权限不足"}
        
    background_tasks.add_task(fetch_and_sync_832_products)
    return {"code": 200, "message": "全量同步任务已拉起，请稍后查看状态"}

@router.post("/sync/supplier/{supplier_id}")
async def trigger_sync_supplier(
    supplier_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user)
):
    """
    手动修复/同步单个特定供货商 (异步执行)。
    """
    if current_user.role != "admin":
        return {"code": 403, "message": "权限不足"}
        
    background_tasks.add_task(fetch_and_sync_832_products, supplier_id)
    return {"code": 200, "message": f"供货商 {supplier_id} 的同步任务已拉起"}


class WechatRawPoolSyncBody(BaseModel):
    """按自然日同步微信好友/群到原始客户池（与定时任务共用 system_configs 目标日）。"""

    calendar_day: str = Field(..., description="YYYY-MM-DD，上海时区自然日")
    partner_id: str | None = Field(
        default=None,
        description="若传该字段（含空字符串）：写入 wechat_open_partner_id；不传则不改库内覆盖项",
    )
    include_groups: bool = Field(default=True, description="是否同时同步 type=2 群")


@router.get("/sync/wechat-raw-pool/status")
async def wechat_raw_pool_sync_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        return {"code": 403, "message": "权限不足"}
    keys = [CFG_TARGET_DAY, CFG_PARTNER, CFG_QUERY_MODE, STATUS_KEY, MSG_KEY, SUCCESS_KEY]
    stmt = select(SystemConfig).where(SystemConfig.config_key.in_(keys))
    res = await db.execute(stmt)
    rows = {c.config_key: (c.config_value or "") for c in res.scalars().all()}
    return {
        "code": 200,
        "data": {
            "target_day": rows.get(CFG_TARGET_DAY, ""),
            "partner_id_override": rows.get(CFG_PARTNER, ""),
            "query_mode": rows.get(CFG_QUERY_MODE, "") or "updateTime",
            "status": rows.get(STATUS_KEY, "idle"),
            "message": rows.get(MSG_KEY, ""),
            "last_success": rows.get(SUCCESS_KEY, ""),
        },
    }


@router.post("/sync/wechat-raw-pool")
async def trigger_wechat_raw_pool_sync(
    body: WechatRawPoolSyncBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    管理员手动触发：写入目标自然日后后台异步执行（接口 5 秒限频，可能持续较久）。
    """
    if current_user.role != "admin":
        return {"code": 403, "message": "权限不足"}
    day = body.calendar_day.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return {"code": 400, "message": "calendar_day 须为 YYYY-MM-DD"}

    await db.execute(
        text(
            """
            INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
            VALUES (:k, :v, 'sync', NOW())
            ON DUPLICATE KEY UPDATE config_value=:v, updated_at=NOW()
            """
        ),
        {"k": CFG_TARGET_DAY, "v": day},
    )
    if body.partner_id is not None:
        await db.execute(
            text(
                """
                INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
                VALUES (:k, :v, 'sync', NOW())
                ON DUPLICATE KEY UPDATE config_value=:v, updated_at=NOW()
                """
            ),
            {"k": CFG_PARTNER, "v": body.partner_id.strip()},
        )
    await db.commit()

    types = (1, 2) if body.include_groups else (1,)
    run_partner = (body.partner_id.strip() or None) if body.partner_id is not None else None

    async def _job():
        await sync_wechat_friends_for_calendar_day(day, partner_id=run_partner, types=types)

    background_tasks.add_task(_job)
    return {"code": 200, "message": f"已拉起微信原始池同步任务（目标日 {day}），请稍后查看 /api/system/sync/wechat-raw-pool/status"}


class WechatChatSyncBody(BaseModel):
    start_time_ms: int | None = Field(
        default=None,
        description="起始 time(ms). 为空则从系统游标继续；必须早于当前至少40分钟更稳",
    )
    max_calls: int = Field(default=6, ge=1, le=24, description="最多请求次数（每次约1小时窗口），受5秒限频影响")
    partner_id: str | None = Field(default=None, description="可选：覆盖管理员/员工ID（空则走配置/环境变量）")
    persist_cursor: bool = Field(default=True, description="是否把 end/createTimestamp 写回系统游标")


@router.get("/sync/wechat-chat/status")
async def wechat_chat_sync_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        return {"code": 403, "message": "权限不足"}
    keys = [
        CFG_CHAT_CURSOR_TIME,
        CFG_CHAT_CURSOR_CREATE,
        CFG_CHAT_STATUS,
        CFG_CHAT_LAST_MSG,
        CFG_CHAT_LAST_OK,
    ]
    stmt = select(SystemConfig).where(SystemConfig.config_key.in_(keys))
    res = await db.execute(stmt)
    rows = {c.config_key: (c.config_value or "") for c in res.scalars().all()}
    return {
        "code": 200,
        "data": {
            "cursor_time_ms": rows.get(CFG_CHAT_CURSOR_TIME, ""),
            "cursor_create_ts_ms": rows.get(CFG_CHAT_CURSOR_CREATE, ""),
            "status": rows.get(CFG_CHAT_STATUS, "idle"),
            "message": rows.get(CFG_CHAT_LAST_MSG, ""),
            "last_success": rows.get(CFG_CHAT_LAST_OK, ""),
        },
    }


@router.post("/sync/wechat-chat")
async def trigger_wechat_chat_sync(
    body: WechatChatSyncBody,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        return {"code": 403, "message": "权限不足"}

    async def _job():
        await sync_wechat_chat_increment(
            start_time_ms=body.start_time_ms,
            max_calls=body.max_calls,
            partner_id=body.partner_id,
            persist_cursor=body.persist_cursor,
        )

    background_tasks.add_task(_job)
    return {"code": 200, "message": "已拉起微信聊天增量同步任务，请稍后查看 /api/system/sync/wechat-chat/status"}


@router.get("/configs_dict")
async def get_configs_dict(db: AsyncSession = Depends(get_db)):
    """
    拉取系统级的配置字典选项列表，用于给客户端渲染多级菜单。
    """
    keys = [
        "unit_type_choices",
        "admin_division_choices",
        "purchase_type_choices",
        "llm_chat_models_list",
        "desktop_default_chat_models",
    ]
    stmt = select(SystemConfig).where(SystemConfig.config_key.in_(keys))
    res = await db.execute(stmt)
    configs = res.scalars().all()

    raw_map = {c.config_key: c.config_value for c in configs}
    config_map = {
        k: [x.strip() for x in raw_map[k].split(",") if x.strip()]
        for k in ("unit_type_choices", "admin_division_choices", "purchase_type_choices")
        if k in raw_map
    }

    llm_chat_models = chat_models_for_api_payload(raw_map)
    desktop_default_raw = (raw_map.get("desktop_default_chat_models") or "").strip()
    desktop_default = [x.strip() for x in desktop_default_raw.split(",") if x.strip()] if desktop_default_raw else []

    # 填充一些默认的 fallback 配置以防数据库没来及配置
    return {
        "code": 200,
        "data": {
            "unit_type_choices": config_map.get("unit_type_choices", ["学校", "医院", "消防", "街道办", "银行", "税务局", "其他"]),
            "admin_division_choices": config_map.get("admin_division_choices", ["越秀区", "天河区", "海珠区", "荔湾区", "其他"]),
            "purchase_type_choices": config_map.get("purchase_type_choices", ["食堂采购", "工会采购", "食堂+工会采购", "其他"]),
            "llm_chat_models": llm_chat_models,
            "desktop_default_chat_models": desktop_default,
        }
    }


def _md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


@router.post("/debug/wechat/getAllFriendsIncrement")
async def debug_wechat_get_all_friends_increment(
    type: int = Query(..., description="类型 1:好友 2:群"),
    getFirstData: bool = Query(True, description="true 返回最早一条；false 增量最多 2000+ 条"),
    queryMode: str = Query("createTime", description="createTime 或 updateTime"),
    startTime: str | None = Query(None, description="yyyy-MM-dd HH:mm:ss；getFirstData=false 时必填"),
    partnerId: str | None = Query(None, description="可选：覆盖默认管理员/员工ID（用于单独同步）"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    【开发调试专用】调用开放平台接口 `/open/wechat/getAllFriendsIncrement` 并原样返回结果，用于确认返参结构。

    认证要素从环境变量读取（建议写入 backend/.env 或系统环境变量）：
    - WECHAT_OPEN_BASE_URL: 开放平台接口根地址（例如 https://open.xxx.com）
    - WECHAT_OPEN_COMPANY: 企业码
    - WECHAT_OPEN_ADMIN_PARTNER_ID: 默认管理员ID（不传 partnerId 时使用）
    - WECHAT_OPEN_KEY: 接口签名 key
    """
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="权限不足")

    base_url = (os.getenv("WECHAT_OPEN_BASE_URL") or "").strip().rstrip("/")
    company = (os.getenv("WECHAT_OPEN_COMPANY") or "").strip()
    default_partner = (os.getenv("WECHAT_OPEN_ADMIN_PARTNER_ID") or "").strip()
    key = (os.getenv("WECHAT_OPEN_KEY") or "").strip()

    use_partner = (partnerId or default_partner).strip()

    if not base_url:
        raise HTTPException(status_code=500, detail="缺少环境变量 WECHAT_OPEN_BASE_URL")
    if not company:
        raise HTTPException(status_code=500, detail="缺少环境变量 WECHAT_OPEN_COMPANY")
    if not use_partner:
        raise HTTPException(status_code=500, detail="缺少管理员/员工ID：WECHAT_OPEN_ADMIN_PARTNER_ID 或 partnerId")
    if not key:
        raise HTTPException(status_code=500, detail="缺少环境变量 WECHAT_OPEN_KEY")

    if not getFirstData and not (startTime or "").strip():
        raise HTTPException(status_code=422, detail="getFirstData=false 时 startTime 必填")

    ts_ms = str(int(time.time() * 1000))
    sign = _md5_upper(key + company + use_partner + ts_ms)

    url = f"{base_url}/open/wechat/getAllFriendsIncrement"
    headers = {
        "company": company,
        "partnerId": use_partner,
        "timestamp": ts_ms,
        "key": key,
        "sign": sign,
        "content-type": "application/json",
    }
    payload = {
        "type": type,
        "getFirstData": getFirstData,
        "queryMode": queryMode,
        "startTime": (startTime or ""),
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            # 有些平台失败也会返回 200+success=false，这里先把 HTTP 错误显式抛出
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        # 兼容把第三方返回体带回来，方便定位签名/限频等问题
        body = e.response.text if e.response is not None else ""
        raise HTTPException(status_code=502, detail=f"开放平台 HTTP 错误: {e}. body={body[:2000]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/debug/wechat/allRecords")
async def debug_wechat_all_records(
    timestamp: int = Query(..., description="翻页时间戳：消息保存时间 time（13位ms），首次自行指定，后续用返回 end"),
    createTimestamp: int = Query(0, description="补充条件：消息发送时间 timestamp（13位ms），缺省0"),
    partnerId: str | None = Query(None, description="可选：覆盖管理员/员工ID"),
    current_user: User = Depends(get_current_user),
):
    """
    【开发调试专用】调用开放平台接口 `/open/wechat/allRecords` 并原样返回结果，用于确认返参与分页字段。
    """
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="权限不足")

    base_url = (os.getenv("WECHAT_OPEN_BASE_URL") or "").strip().rstrip("/")
    company = (os.getenv("WECHAT_OPEN_COMPANY") or "").strip()
    default_partner = (os.getenv("WECHAT_OPEN_ADMIN_PARTNER_ID") or "").strip()
    key = (os.getenv("WECHAT_OPEN_KEY") or "").strip()

    use_partner = (partnerId or default_partner).strip()
    if not base_url or not company or not key or not use_partner:
        raise HTTPException(
            status_code=500,
            detail="缺少开放平台环境变量：WECHAT_OPEN_BASE_URL / WECHAT_OPEN_COMPANY / WECHAT_OPEN_ADMIN_PARTNER_ID / WECHAT_OPEN_KEY",
        )

    ts_ms = str(int(time.time() * 1000))
    sign = _md5_upper(key + company + use_partner + ts_ms)
    url = f"{base_url}/open/wechat/allRecords"
    headers = {
        "company": company,
        "partnerId": use_partner,
        "timestamp": ts_ms,
        "key": key,
        "sign": sign,
        "content-type": "application/json",
    }
    payload = {"timestamp": int(timestamp), "createTimestamp": int(createTimestamp)}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        body = e.response.text if e.response is not None else ""
        raise HTTPException(status_code=502, detail=f"开放平台 HTTP 错误: {e}. body={body[:2000]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
