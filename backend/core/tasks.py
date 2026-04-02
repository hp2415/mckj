import httpx
import logging
from sqlalchemy.future import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from database import AsyncSessionLocal
from models import Product, SystemConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        # ======= 动态读取需要抓取的供货商 ID =======
        config_res = await db.execute(select(SystemConfig).where(SystemConfig.config_key == "supplier_ids"))
        config_obj = config_res.scalars().first()
        if config_obj and config_obj.config_value.strip():
            target_suppliers = [s.strip() for s in config_obj.config_value.split(",") if s.strip()]
        else:
            target_suppliers = ["1090698369754404144"] # 默认配置
            
        async with httpx.AsyncClient(timeout=20.0) as client:
            # 外层循环：遍历所有你指定的供应商店铺
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
                            
                            # ===== 图片反盗链突破与自宿主下载保护 =====
                            raw_img = p.get("coverImg", "")
                            sku = p.get("skuCode", "")
                            local_img_path = ""
                            if raw_img:
                                import os
                                # 以 sku 或 pid 为基准重塑图文名
                                filename = f"{sku}.jpg" if sku else f"{pid}.jpg"
                                relative_path = f"/media/products/{filename}"
                                
                                absolute_dir = os.path.join(os.getcwd(), "media", "products")
                                os.makedirs(absolute_dir, exist_ok=True)
                                absolute_path = os.path.join(absolute_dir, filename)
                                
                                # 图鉴只下一次即可，节约开销
                                if not os.path.exists(absolute_path):
                                    try:
                                        # 伪造官网请求头，堂堂正正直接盗源图
                                        img_resp = await client.get(raw_img, headers={"Referer": "https://www.fupin832.com/"})
                                        if img_resp.status_code == 200:
                                            with open(absolute_path, "wb") as f:
                                                f.write(img_resp.content)
                                        else:
                                            logger.warning(f"拉取原图遇阻 HTTP {img_resp.status_code}")
                                    except Exception as e:
                                        logger.warning(f"下载资源损坏: {str(e)}")
                                local_img_path = relative_path
                            img = local_img_path
                            
                            unit = p.get("packingUnit", "件")
                            supplier = p.get("supplierName", supplier_id)
                            item_uuid = p.get("uuid", "")
                            # 生成商品的外部详情页链接（基于真实的移动/PC跨平台协议路径）
                            p_url = f"https://ys.fupin832.com/pages/detail/{sku}"
                            
                            # 在本地库探查
                            res = await db.execute(select(Product).where(Product.product_id == pid))
                            existing = res.scalars().first()
                            
                            if existing:
                                # 数据比对，如果有变动则更新
                                if existing.price != price or existing.product_name != pname or existing.cover_img != img or existing.product_url != p_url or existing.uuid != item_uuid:
                                    existing.price = price
                                    existing.product_name = pname
                                    existing.cover_img = img
                                    existing.product_url = p_url
                                    if item_uuid: existing.uuid = item_uuid
                            else:
                                # 插入全新发现的商品实体
                                new_product = Product(
                                    uuid=item_uuid,
                                    product_id=pid,
                                    product_name=pname,
                                    price=price,
                                    cover_img=img,
                                    product_url=p_url,
                                    unit=unit,
                                    supplier_name=supplier
                                )
                                db.add(new_product)
                                
                        await db.commit()
                        total_fetched += len(products)
                        
                        # 翻页校验逻辑
                        total_pages = int(data.get("retData", {}).get("totalPage", 0))
                        if page >= total_pages:
                            has_more = False
                        else:
                            page += 1
                            
                    except Exception as e:
                        logger.error(f"抓取 832 供货商 {supplier_id} 第 {page} 页失败: {str(e)}")
                        has_more = False

                logger.info(f"供货商 {supplier_id} 同步结束！本次查明 {total_fetched} 个商品。")

    logger.info(f"[APScheduler] 832 平台商品全量同步结束！本次核对并洗入了 {total_fetched} 个商品。")

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
    
    # 2. 【开发测试特供】启动时立刻触发一次（正式上线后可去掉）
    scheduler.add_job(
        fetch_and_sync_832_products, 
        trigger="date",
        id="boot_sync_832"
    )
    
    scheduler.start()
    logger.info("APScheduler 调度中心已随主程序成功启动！")
