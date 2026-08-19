import asyncio
import httpx
import os
from datetime import datetime, timedelta
from sqlalchemy import text, update
from sqlalchemy.future import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from database import AsyncSessionLocal
from models import Product, SystemConfig, SyncFailure
from core.logger import logger
from core.system_config_store import upsert_system_config_row


def _write_bytes_to_file(abs_path: str, content: bytes) -> None:
    with open(abs_path, "wb") as f:
        f.write(content)


async def _resolve_product_cover_image(
    client: httpx.AsyncClient,
    *,
    raw_img: str,
    sku: str,
    pid,
) -> str:
    """下载商品封面并持久化到 media/products；文件 IO 走线程池避免阻塞事件循环。"""
    if not raw_img:
        return ""
    filename = f"{sku}.jpg" if sku else f"{pid}.jpg"
    rel_path = f"/media/products/{filename}"
    abs_dir = os.path.join(os.getcwd(), "media", "products")
    abs_path = os.path.join(abs_dir, filename)

    exists = await asyncio.to_thread(os.path.exists, abs_path)
    if not exists:
        await asyncio.to_thread(os.makedirs, abs_dir, exist_ok=True)
        try:
            img_resp = await client.get(
                raw_img,
                timeout=10.0,
                headers={"Referer": "https://www.fupin832.com/"},
            )
            if img_resp.status_code == 200:
                await asyncio.to_thread(_write_bytes_to_file, abs_path, img_resp.content)
                logger.debug(f"成功保存商品图片: {filename}")
            else:
                logger.warning(f"下载图片失败 ({img_resp.status_code}): {raw_img}")
        except Exception as e:
            logger.error(f"下载图片异常: {e}")
    return rel_path


def _apply_product_fields(existing: Product, p: dict, *, price: float, img: str, supplier_id: str) -> None:
    existing.price = price
    existing.cover_img = img
    existing.supplier_id = supplier_id
    existing.is_active = True  # 接口再次返回 = 重新上架，保留 cost_price
    existing.category_name_one = p.get("categoryNameOne")
    existing.category_name_two = p.get("categoryNameTwo")
    existing.category_name_three = p.get("categoryNameThree")
    existing.origin_province = p.get("deliveryProvinceName")
    existing.origin_city = p.get("deliveryCityName")
    existing.origin_district = p.get("deliveryDistrictName")


async def fetch_and_sync_832_products(single_supplier_id: str = None):
    """
    后台任务核心逻辑：自动化拉取 832 供应商所有页码的商品。
    支持自动重试机制与失败详情记录。
    """
    mode = "单点同步" if single_supplier_id else "全量同步"
    logger.info(f"[APScheduler] 开始执行 832 平台商品{mode}...")
    url = "https://ys.fupin832.com/frontweb/search/searchProduct"
    headers = {
        "origin": "https://ys.fupin832.com",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    async with AsyncSessionLocal() as db:
        # ======= 1. 标记同步开始 =======
        await upsert_system_config_row(
            db,
            config_key="sync_status",
            config_value="running",
            config_group="sync",
        )
        await db.commit()

        # ======= 2. 动态读取目标供货商 =======
        if single_supplier_id:
            target_suppliers = [single_supplier_id]
        else:
            config_res = await db.execute(select(SystemConfig).where(SystemConfig.config_key == "supplier_ids"))
            config_obj = config_res.scalars().first()
            if config_obj and config_obj.config_value.strip():
                target_suppliers = [s.strip() for s in config_obj.config_value.split(",") if s.strip()]
            else:
                target_suppliers = ["1090698369754404144"]
            
        final_errors = {} # Mapping supplier_id -> error_msg
        total_all_fetched = 0
        
        async def sync_supplier(client, supplier_id):
            # ===== [测试模式] 强制触发异常 =====
            if supplier_id == "DEBUG_FAIL":
                raise Exception("人工注入：模拟 832 平台网关超时 (504 Gateway Timeout)")
            # ===============================

            nonlocal total_all_fetched
            logger.info(f"正在抓取供货商: {supplier_id}...")
            page = 1
            total_fetched = 0
            seen_pids: set[str] = set()
            while True:
                payload = {"nowPage": page, "pageShow": 100, "sortType": "DESC", "supplierId": supplier_id}
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                products = data.get("retData", {}).get("results", [])
                if not products: break

                page_pids = [p.get("productId") for p in products if p.get("productId")]
                existing_map: dict = {}
                if page_pids:
                    res = await db.execute(select(Product).where(Product.product_id.in_(page_pids)))
                    existing_map = {row.product_id: row for row in res.scalars().all()}

                for p in products:
                    pid = p.get("productId")
                    if not pid:
                        continue
                    pid = str(pid)
                    seen_pids.add(pid)
                    pname = p.get("productFullName", "未知商品")
                    price = float(p.get("basePrice", 0.0))
                    raw_img = p.get("coverImg", "")
                    sku = p.get("skuCode", "")

                    img = await _resolve_product_cover_image(
                        client, raw_img=raw_img, sku=sku, pid=pid,
                    )

                    existing = existing_map.get(pid)
                    if existing:
                        _apply_product_fields(
                            existing, p, price=price, img=img, supplier_id=supplier_id,
                        )
                    else:
                        db.add(Product(
                            uuid=p.get("uuid", ""), product_id=pid, product_name=pname,
                            price=price, cover_img=img, product_url=f"https://ys.fupin832.com/pages/detail/{sku}",
                            unit=p.get("packingUnit", "件"), supplier_name=p.get("supplierName", supplier_id),
                            supplier_id=supplier_id,
                            is_active=True,
                            category_name_one=p.get("categoryNameOne"),
                            category_name_two=p.get("categoryNameTwo"),
                            category_name_three=p.get("categoryNameThree"),
                            origin_province=p.get("deliveryProvinceName"),
                            origin_city=p.get("deliveryCityName"),
                            origin_district=p.get("deliveryDistrictName")
                        ))
                await db.commit()
                total_fetched += len(products)
                total_all_fetched += len(products)
                total_pages = int(data.get("retData", {}).get("totalPage", 0))
                if page >= total_pages: break
                page += 1

            # 软下架：832 本次全量未返回的商品保留行（含成本价），仅标记 is_active=False。
            # 若整店一条都没拉到，多半是接口异常，跳过以免误伤全部上架商品。
            if not seen_pids:
                logger.warning(
                    f"供货商 {supplier_id} 本次未拉到任何商品，跳过软下架以免误伤"
                )
            else:
                stale_res = await db.execute(
                    update(Product)
                    .where(Product.supplier_id == supplier_id)
                    .where(Product.is_active.is_(True))
                    .where(Product.product_id.notin_(list(seen_pids)))
                    .values(is_active=False)
                )
                deactivated = stale_res.rowcount or 0
                if deactivated:
                    logger.info(
                        f"供货商 {supplier_id} 软下架商品 {deactivated} 条（保留成本价）"
                    )
            
            # 成功后，如果原本在失败表里，则清理掉
            await db.execute(text("DELETE FROM sync_failures WHERE supplier_id = :sid"), {"sid": supplier_id})
            await db.commit()
            return True

        async with httpx.AsyncClient(timeout=30.0) as client:
            failed_queue = {}
            # 第一轮抓取
            for sid in target_suppliers:
                try:
                    await sync_supplier(client, sid)
                except Exception as e:
                    logger.warning(f"供货商 {sid} 首次同步失败: {e}，加入重试队列")
                    failed_queue[sid] = str(e)
            
            # 自动重试逻辑 (1次)
            if failed_queue:
                logger.info(f"开始重试失败的供货商 (共 {len(failed_queue)} 个)...")
                for sid, old_err in failed_queue.items():
                    try:
                        await sync_supplier(client, sid)
                    except Exception as e:
                        logger.error(f"供货商 {sid} 重试后依然失败: {e}")
                        final_errors[sid] = str(e)
                        # 记录到持久化异常表
                        await db.execute(text("INSERT INTO sync_failures (supplier_id, last_error, updated_at) VALUES (:sid, :err, NOW()) ON DUPLICATE KEY UPDATE last_error=:err, updated_at=NOW()"), {"sid": sid, "err": str(e)})
                        await db.commit()
            
        # ======= 3. 结果持久化 (主要用于桌面端快速显示) =======
        # 读取当前所有失败的 ID
        fail_res = await db.execute(select(SyncFailure.supplier_id))
        all_failed_ids = fail_res.scalars().all()
        
        status = "success" if not all_failed_ids else "error"
        failed_ids_str = ",".join(all_failed_ids)

        await upsert_system_config_row(
            db,
            config_key="sync_status",
            config_value=status,
            config_group="sync",
        )
        await upsert_system_config_row(
            db,
            config_key="sync_failed_suppliers",
            config_value=failed_ids_str,
            config_group="sync",
        )

        # 优化：区分全量与单点消息
        if single_supplier_id:
            msg = f"单点修复成功 (本次核对 {total_all_fetched} 条商品)" if not final_errors else f"单点修复失败 (供货商 {single_supplier_id})"
        else:
            msg = f"全量核对完成 (共 {total_all_fetched} 条商品)"
            if all_failed_ids:
                msg += f" - 仍有 {len(all_failed_ids)} 个供货商待修复"

        await upsert_system_config_row(
            db,
            config_key="sync_last_message",
            config_value=msg,
            config_group="sync",
        )

        if status == "success" and not single_supplier_id:
            # 只有全量同步成功才更新“最后一次全量成功时间”
            await upsert_system_config_row(
                db,
                config_key="sync_last_success",
                config_value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                config_group="sync",
            )

        await db.commit()
        logger.info(f"[APScheduler] {mode}任务结束 [状态: {status}]")


async def scheduled_sales_wechat_accounts_open_sync():
    """
    每日全量同步销售微信主数据：开放平台 /open/wechat/companyAccounts。
    数据量不大时按最大 pageSize 分页拉全库并 upsert；与 4:20 好友池任务同窗口（略晚数十秒），降低并发打开放平台。
    """
    from sync.company_accounts_open import sync_from_open_api

    try:
        # 长驻进程必须走 sync_from_open_api，禁止 dispose 全局 engine：
        # 4:20 好友池任务可能仍占用连接，dispose 会把池里已断开的 socket
        # 再 close 一次，aiomysql 就会打出 ConnectionResetError: Connection lost。
        st = await sync_from_open_api(
            page_size=400,
            sleep_between_pages=1.0,
        )
        logger.info(
            "[APScheduler] 销售微信主数据(开放平台)同步完成 "
            "upserted={} flattened={} pages={} total_count_api={} partner={}",
            st.get("upserted"),
            st.get("flattened_rows"),
            st.get("pages_fetched"),
            st.get("total_count_api"),
            st.get("partner_id"),
        )
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.exception("[APScheduler] 销售微信主数据(开放平台)同步失败: {}", e)


# 初始化全局异步调度器
scheduler = AsyncIOScheduler(
    timezone='Asia/Shanghai',
    job_defaults={
        'misfire_grace_time': 3600,  # 允许最多 1 小时的执行延迟（例如电脑休眠唤醒），不会被直接丢弃
        'coalesce': True,            # 多次漏掉只补跑一次
        'max_instances': 1,          # 同一任务禁止叠跑
    }
)


def _interval_next_run(*, offset_seconds: int) -> datetime:
    """相对启动时刻错峰：避免多个 interval 任务同秒开火打满 MySQL。"""
    return datetime.now(scheduler.timezone) + timedelta(seconds=max(0, int(offset_seconds)))


def start_scheduler():
    """
    配置任务并拉起调度引擎

    周期任务错峰约定（相对进程启动）：
    - 聊天增量写入 raw_chat_logs：+10s，之后每 15min
    - 语音增量：+45s
    - 电话增量：+5min，之后每 30min
    - 夜间画像预览预热（重扫 raw_chat_logs）：+3min，等聊天 upsert 收尾
    - 看板增量快照（同样走候选扫描）：+8min，与预热错开
    - 订单增量：+10min，之后每 60min
    - 逾期标记：+12min，之后每 60min
    - 事件画像冷静期扫尾：+20s，之后每 2min（轻量，可靠近聊天任务）
    """
    # 1. 挂在一个长驻巡检任务（每天凌晨 03:00 自动巡查洗数）
    scheduler.add_job(
        fetch_and_sync_832_products, 
        CronTrigger(hour=3, minute=0),
        id="daily_sync_832",
        replace_existing=True
    )

    # 2. 微信好友/群 → 原始客户池：04:20 补同步「昨天」；当天由 15 分钟聊天任务前置同步「今天」
    from core.wechat_friends_sync import scheduled_wechat_friends_sync_yesterday

    scheduler.add_job(
        scheduled_wechat_friends_sync_yesterday,
        CronTrigger(hour=4, minute=20),
        id="daily_wechat_friends_raw_pool",
        replace_existing=True,
    )

    # 2b. 销售微信主数据：开放平台 companyAccounts 全量同步（与上项同一天 4:20，错开 30 秒避免并发打满）
    scheduler.add_job(
        scheduled_sales_wechat_accounts_open_sync,
        CronTrigger(hour=4, minute=20, second=30),
        id="daily_sales_wechat_accounts_open_api",
        replace_existing=True,
    )

    # 3. 微信聊天 allRecords：周期性追赶（受“必须早于当前30分钟”约束）
    from core.wechat_chat_sync import scheduled_wechat_chat_increment

    scheduler.add_job(
        scheduled_wechat_chat_increment,
        IntervalTrigger(minutes=15, timezone=scheduler.timezone),
        id="interval_wechat_chat_increment",
        replace_existing=True,
        next_run_time=_interval_next_run(offset_seconds=10),
    )

    from core.wechat_voice_sync import scheduled_wechat_voice_increment

    scheduler.add_job(
        scheduled_wechat_voice_increment,
        IntervalTrigger(minutes=15, timezone=scheduler.timezone),
        id="interval_wechat_voice_increment",
        replace_existing=True,
        next_run_time=_interval_next_run(offset_seconds=45),
    )

    from core.phone_call_sync import (
        scheduled_phone_call_increment,
        scheduled_phone_call_sync_yesterday,
    )

    scheduler.add_job(
        scheduled_phone_call_increment,
        IntervalTrigger(minutes=30, timezone=scheduler.timezone),
        id="interval_phone_call_increment",
        replace_existing=True,
        next_run_time=_interval_next_run(offset_seconds=5 * 60),
    )
    # 0 点补昨日，早于 01:30 夜间画像，避免与画像争抢 DB/API
    scheduler.add_job(
        scheduled_phone_call_sync_yesterday,
        CronTrigger(hour=0, minute=0),
        id="daily_phone_call_sync_yesterday",
        replace_existing=True,
    )

    from core.order_fupin_sync import scheduled_order_fupin_increment

    scheduler.add_job(
        scheduled_order_fupin_increment,
        IntervalTrigger(minutes=60, timezone=scheduler.timezone),
        id="interval_order_fupin_increment",
        replace_existing=True,
        next_run_time=_interval_next_run(offset_seconds=10 * 60),
    )
    
    # 4. 夜间增量画像：每天 01:30 跑前一日有聊天且销售号已绑定的客户对（含未画像）
    from ai.profile_nightly import scheduled_nightly_profile_refresh

    scheduler.add_job(
        scheduled_nightly_profile_refresh,
        CronTrigger(hour=1, minute=30),
        id="daily_profile_refresh_nightly",
        replace_existing=True,
    )
    
    # 5. 联系任务分配：工作日 06:00 日任务；周任务改由画像跟进日期汇总（保留空 cron 防旧 id 报警）
    from ai.task_allocation import (
        scheduled_daily_task_allocation,
        scheduled_weekly_task_allocation,
        scheduled_mark_overdue_tasks,
    )

    scheduler.add_job(
        scheduled_daily_task_allocation,
        CronTrigger(hour=6, minute=0),
        id="daily_contact_task_allocation",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_weekly_task_allocation,
        CronTrigger(day_of_week="mon", hour=6, minute=30),
        id="weekly_contact_task_allocation",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_mark_overdue_tasks,
        IntervalTrigger(hours=1, timezone=scheduler.timezone),
        id="hourly_mark_overdue_contact_tasks",
        replace_existing=True,
        next_run_time=_interval_next_run(offset_seconds=12 * 60),
    )

    # 5b. 优化器 P0.5：参数轨提案（周一 08:00）+ 护栏（每日 07:30，错开 06:00 分配）
    from ai.optimizer.jobs import (
        scheduled_daily_optimizer_guardrail,
        scheduled_weekly_optimizer_propose,
    )

    scheduler.add_job(
        scheduled_weekly_optimizer_propose,
        CronTrigger(day_of_week="mon", hour=8, minute=0),
        id="weekly_optimizer_propose",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_daily_optimizer_guardrail,
        CronTrigger(hour=7, minute=30),
        id="daily_optimizer_guardrail",
        replace_existing=True,
    )

    from core.dashboard_incremental_snapshot import (
        SNAPSHOT_REFRESH_INTERVAL_MIN,
        scheduled_dashboard_incremental_snapshot,
    )

    # 与聊天写入、预览预热错开：启动后 +8min，再每 30min
    scheduler.add_job(
        scheduled_dashboard_incremental_snapshot,
        IntervalTrigger(
            minutes=SNAPSHOT_REFRESH_INTERVAL_MIN,
            timezone=scheduler.timezone,
        ),
        id="interval_dashboard_incremental_snapshot",
        replace_existing=True,
        next_run_time=_interval_next_run(offset_seconds=8 * 60),
    )

    # 6b. 夜间增量画像预览：每 15 分钟后台预热「今日」候选缓存，使管理端打开页面秒开。
    # 故意落后聊天增量约 6 分钟，并与 heavy_db_section 互斥，避免与 upsert 重叠
    from ai.profile_nightly_preview import warm_nightly_preview_cache

    scheduler.add_job(
        warm_nightly_preview_cache,
        IntervalTrigger(minutes=15, timezone=scheduler.timezone),
        id="interval_nightly_preview_warm",
        replace_existing=True,
        next_run_time=_interval_next_run(offset_seconds=6 * 60),
    )

    # 6c. 事件驱动画像：冷静期暂存扫尾（进程重启后仍能准时入队；正常路径靠精确定时器）
    from ai.profile_triggers import scheduled_flush_deferred_event_profiles

    scheduler.add_job(
        scheduled_flush_deferred_event_profiles,
        IntervalTrigger(minutes=2, timezone=scheduler.timezone),
        id="interval_event_profile_deferred_flush",
        replace_existing=True,
        next_run_time=_interval_next_run(offset_seconds=20),
    )

    scheduler.start()
    logger.info(
        "APScheduler 调度中心已随主程序成功启动！"
        "（周期任务已错峰：chat+10s / voice+45s / preview+6m / dashboard+8m）"
    )
