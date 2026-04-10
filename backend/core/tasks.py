import httpx
from sqlalchemy import text
from sqlalchemy.future import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from database import AsyncSessionLocal
from models import Product, SystemConfig, SyncFailure
from core.logger import logger

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
        stmt_start = text("INSERT INTO system_configs (config_key, config_value, config_group, updated_at) VALUES ('sync_status', 'running', 'sync', NOW()) ON DUPLICATE KEY UPDATE config_value='running', updated_at=NOW()")
        await db.execute(stmt_start)
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
            while True:
                payload = {"nowPage": page, "pageShow": 100, "sortType": "DESC", "supplierId": supplier_id}
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                products = data.get("retData", {}).get("results", [])
                if not products: break
                
                for p in products:
                    pid = p.get("productId")
                    pname = p.get("productFullName", "未知商品")
                    price = float(p.get("basePrice", 0.0))
                    raw_img = p.get("coverImg", "")
                    sku = p.get("skuCode", "")
                    
                    # 图片持久化逻辑
                    img = ""
                    if raw_img:
                        import os
                        filename = f"{sku}.jpg" if sku else f"{pid}.jpg"
                        rel_path = f"/media/products/{filename}"
                        abs_dir = os.path.join(os.getcwd(), "media", "products")
                        os.makedirs(abs_dir, exist_ok=True)
                        abs_path = os.path.join(abs_dir, filename)
                        
                        if not os.path.exists(abs_path):
                            try:
                                # 修复 403：必须携带有效的 Referer 绕过防盗链
                                img_resp = await client.get(
                                    raw_img, 
                                    timeout=10.0, 
                                    headers={"Referer": "https://www.fupin832.com/"}
                                )
                                if img_resp.status_code == 200:
                                    with open(abs_path, "wb") as f: f.write(img_resp.content)
                                    logger.debug(f"成功保存商品图片: {filename}")
                                else:
                                    logger.warning(f"下载图片失败 ({img_resp.status_code}): {raw_img}")
                            except Exception as e:
                                logger.error(f"下载图片异常: {e}")
                        img = rel_path
                    
                    res = await db.execute(select(Product).where(Product.product_id == pid))
                    existing = res.scalars().first()
                    if existing:
                        existing.price = price
                        existing.product_name = pname
                        existing.cover_img = img
                        existing.supplier_id = supplier_id
                    else:
                        db.add(Product(
                            uuid=p.get("uuid", ""), product_id=pid, product_name=pname,
                            price=price, cover_img=img, product_url=f"https://ys.fupin832.com/pages/detail/{sku}",
                            unit=p.get("packingUnit", "件"), supplier_name=p.get("supplierName", supplier_id),
                            supplier_id=supplier_id
                        ))
                await db.commit()
                total_fetched += len(products)
                total_all_fetched += len(products)
                total_pages = int(data.get("retData", {}).get("totalPage", 0))
                if page >= total_pages: break
                page += 1
            
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

        await db.execute(text("INSERT INTO system_configs (config_key, config_value, config_group, updated_at) VALUES ('sync_status', :v, 'sync', NOW()) ON DUPLICATE KEY UPDATE config_value=:v, updated_at=NOW()"), {"v": status})
        await db.execute(text("INSERT INTO system_configs (config_key, config_value, config_group, updated_at) VALUES ('sync_failed_suppliers', :f, 'sync', NOW()) ON DUPLICATE KEY UPDATE config_value=:f, updated_at=NOW()"), {"f": failed_ids_str})
        
        # 优化：区分全量与单点消息
        if single_supplier_id:
            msg = f"单点修复成功 (本次核对 {total_all_fetched} 条商品)" if not final_errors else f"单点修复失败 (供货商 {single_supplier_id})"
        else:
            msg = f"全量核对完成 (共 {total_all_fetched} 条商品)"
            if all_failed_ids:
                msg += f" - 仍有 {len(all_failed_ids)} 个供货商待修复"
                
        await db.execute(text("INSERT INTO system_configs (config_key, config_value, config_group, updated_at) VALUES ('sync_last_message', :m, 'sync', NOW()) ON DUPLICATE KEY UPDATE config_value=:m, updated_at=NOW()"), {"m": msg})
        
        if status == "success" and not single_supplier_id:
            # 只有全量同步成功才更新“最后一次全量成功时间”
            await db.execute(text("INSERT INTO system_configs (config_key, config_value, config_group, updated_at) VALUES ('sync_last_success', NOW(), 'sync', NOW()) ON DUPLICATE KEY UPDATE config_value=NOW(), updated_at=NOW()"))
        
        await db.commit()
        logger.info(f"[APScheduler] {mode}任务结束 [状态: {status}]")

# 初始化全局异步调度器
scheduler = AsyncIOScheduler(timezone='Asia/Shanghai')

def start_scheduler():
    """
    配置任务并拉起调度引擎
    """
    # 1. 挂在一个长驻巡检任务（每天凌晨 03:00 自动巡查洗数）
    scheduler.add_job(
        fetch_and_sync_832_products, 
        CronTrigger(hour=3, minute=0),
        id="daily_sync_832",
        replace_existing=True
    )
    
    # # 2. 【开发测试特供】启动时立刻触发一次（正式上线后可去掉）
    # scheduler.add_job(
    #     fetch_and_sync_832_products, 
    #     trigger="date",
    #     id="boot_sync_832"
    # )
    
    scheduler.start()
    logger.info("APScheduler 调度中心已随主程序成功启动！")
