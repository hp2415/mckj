import httpx
from sqlalchemy import text
from sqlalchemy.future import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from database import AsyncSessionLocal
from models import Product, SystemConfig
from core.logger import logger

async def fetch_and_sync_832_products():
    """
    后台任务核心逻辑：自动化拉取 832 供应商所有页码的商品，支持多个供应商。
    """
    logger.info("[APScheduler] 开始定时同步 832 平台商品数据...")
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

        # ======= 2. 动态读取需要抓取的供货商 ID =======
        config_res = await db.execute(select(SystemConfig).where(SystemConfig.config_key == "supplier_ids"))
        config_obj = config_res.scalars().first()
        if config_obj and config_obj.config_value.strip():
            target_suppliers = [s.strip() for s in config_obj.config_value.split(",") if s.strip()]
        else:
            target_suppliers = ["1090698369754404144"] # 默认配置
            
        sync_error = None
        total_all_fetched = 0
        async with httpx.AsyncClient(timeout=20.0) as client:
            for supplier_id in target_suppliers:
                logger.info(f"正在同步供货商: {supplier_id} 的所有商品...")
                page = 1
                total_fetched = 0
                has_more = True

                while has_more:
                    payload = {
                        "nowPage": page,
                        "pageShow": 100,
                        "sortType": "DESC",
                        "supplierId": supplier_id,
                        "sortName": "",
                        "shopCategoryId": ""
                    }
                    
                    try:
                        response = await client.post(url, json=payload, headers=headers)
                        response.raise_for_status()
                        data = response.json()
                        
                        products = data.get("retData", {}).get("results", [])
                        if not products:
                            break
                            
                        for p in products:
                            pid = p.get("productId")
                            pname = p.get("productFullName", "未知商品")
                            price = float(p.get("basePrice", 0.0))
                            
                            # ===== 图片处理逻辑 (略) #####
                            raw_img = p.get("coverImg", "")
                            sku = p.get("skuCode", "")
                            local_img_path = ""
                            if raw_img:
                                import os
                                filename = f"{sku}.jpg" if sku else f"{pid}.jpg"
                                relative_path = f"/media/products/{filename}"
                                absolute_dir = os.path.join(os.getcwd(), "media", "products")
                                os.makedirs(absolute_dir, exist_ok=True)
                                absolute_path = os.path.join(absolute_dir, filename)
                                if not os.path.exists(absolute_path):
                                    try:
                                        img_resp = await client.get(raw_img, headers={"Referer": "https://www.fupin832.com/"})
                                        if img_resp.status_code == 200:
                                            with open(absolute_path, "wb") as f:
                                                f.write(img_resp.content)
                                    except: pass
                                local_img_path = relative_path
                            img = local_img_path
                            
                            unit = p.get("packingUnit", "件")
                            supplier = p.get("supplierName", supplier_id)
                            item_uuid = p.get("uuid", "")
                            p_url = f"https://ys.fupin832.com/pages/detail/{sku}"
                            
                            res = await db.execute(select(Product).where(Product.product_id == pid))
                            existing = res.scalars().first()
                            
                            if existing:
                                if (existing.price != price or existing.product_name != pname or 
                                    existing.cover_img != img or existing.product_url != p_url or 
                                    existing.supplier_id != supplier_id):
                                    existing.price = price
                                    existing.product_name = pname
                                    existing.cover_img = img
                                    existing.product_url = p_url
                                    existing.supplier_id = supplier_id
                                    if item_uuid: existing.uuid = item_uuid
                            else:
                                new_product = Product(
                                    uuid=item_uuid, product_id=pid, product_name=pname,
                                    price=price, cover_img=img, product_url=p_url,
                                    unit=unit, supplier_name=supplier, supplier_id=supplier_id
                                )
                                db.add(new_product)
                                
                        await db.commit()
                        total_fetched += len(products)
                        total_all_fetched += len(products)
                        
                        total_pages = int(data.get("retData", {}).get("totalPage", 0))
                        if page >= total_pages:
                            has_more = False
                        else:
                            page += 1
                            
                    except Exception as e:
                        sync_error = str(e)
                        logger.error(f"抓取 832 供货商 {supplier_id} 失败: {sync_error}")
                        has_more = False
                
                logger.info(f"供货商 {supplier_id} 同步结束！")

        # ======= 3. 标记同步结束与结果 =======
        status = "success" if not sync_error else "error"
        msg = f"已完成 {total_all_fetched} 个商品核对" if not sync_error else sync_error
        
        await db.execute(text("INSERT INTO system_configs (config_key, config_value, config_group, updated_at) VALUES ('sync_status', :v, 'sync', NOW()) ON DUPLICATE KEY UPDATE config_value=:v, updated_at=NOW()"), {"v": status})
        await db.execute(text("INSERT INTO system_configs (config_key, config_value, config_group, updated_at) VALUES ('sync_last_message', :m, 'sync', NOW()) ON DUPLICATE KEY UPDATE config_value=:m, updated_at=NOW()"), {"m": msg})
        if status == "success":
            await db.execute(text("INSERT INTO system_configs (config_key, config_value, config_group, updated_at) VALUES ('sync_last_success', NOW(), 'sync', NOW()) ON DUPLICATE KEY UPDATE config_value=NOW(), updated_at=NOW()"))
        
        await db.commit()
        logger.info(f"[APScheduler] 同步任务处理完成 [状态: {status}]")

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
