import asyncio
from collections import OrderedDict
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
import httpx
from logger_cfg import logger

class ImageManager:
    """
    独立管理应用内的图片资源调度。
    维护内存 LRU 队列，并处理从内存、磁盘缓存和网络的三级读取逻辑。
    """
    def __init__(self, api_client):
        self.api = api_client
        self._pixmap_cache = OrderedDict()
        self.MAX_PIXMAP_COUNT = 150
        self._http_session = None

    def get_http_session(self):
        """延迟初始化 HTTP Client 以兼容 async context"""
        if not self._http_session:
            self._http_session = httpx.AsyncClient(timeout=10.0)
        return self._http_session

    async def async_load_image(self, card_widget, relative_url):
        """三级缓存图片加载策略：L1(内存LRU) -> L2(SQLite) -> L3(网络)"""
        if not relative_url:
            return
            
        # 1. 检查 L1 内存缓存
        if relative_url in self._pixmap_cache:
            self._pixmap_cache.move_to_end(relative_url)
            card_widget.update_image(self._pixmap_cache[relative_url])
            return

        pixmap = None

        # 2. 检查 L2 磁盘持久化缓存
        cache_key = self.api._generate_cache_key("img", path=relative_url)
        if self.api.storage:
            cached_blob = self.api.storage.load_data(cache_key)
            if cached_blob:
                pixmap = QPixmap()
                if not pixmap.loadFromData(cached_blob):
                    pixmap = None

        # 3. 发起 L3 网络请求
        session = self.get_http_session()
        if not pixmap and session:
            full_url = f"{self.api.base_url}{relative_url}"
            try:
                resp = await session.get(full_url)
                if resp.status_code == 200:
                    if self.api.storage:
                        self.api.storage.save_data(cache_key, resp.content)
                    pixmap = QPixmap()
                    if not pixmap.loadFromData(resp.content):
                        pixmap = None
            except Exception:
                pass

        # 4. 后处理：缩放并压入 L1 缓存
        if pixmap and not pixmap.isNull():
            # 由于 UI 上显示尺寸为 110x120，我们按 2 倍图存储(220x240)以保证高分屏清晰度
            scaled_pixmap = pixmap.scaled(
                220, 240, 
                Qt.KeepAspectRatio, 
                Qt.SmoothTransformation
            )
            self._pixmap_cache[relative_url] = scaled_pixmap
            self._pixmap_cache.move_to_end(relative_url)
            if len(self._pixmap_cache) > self.MAX_PIXMAP_COUNT:
                self._pixmap_cache.popitem(last=False)
            
            card_widget.update_image(scaled_pixmap)

    def handle_full_copy_image(self, relative_url):
        """处理高清原图复制请求：从系统剪贴板存入"""
        if not relative_url: return
        
        cache_key = self.api._generate_cache_key("img", path=relative_url)
        if self.api.storage:
            raw_blob = self.api.storage.load_data(cache_key)
            if raw_blob:
                pixmap = QPixmap()
                if pixmap.loadFromData(raw_blob):
                    QApplication.clipboard().setPixmap(pixmap)
                    logger.info(f"高清原图已复制至剪贴板: {relative_url}")
                    return
        
        if relative_url in self._pixmap_cache:
            QApplication.clipboard().setPixmap(self._pixmap_cache[relative_url])
            
    async def close(self):
        """释放所有缓存资源，注销时调用"""
        self._pixmap_cache.clear()
        if self._http_session:
            await self._http_session.aclose()
            self._http_session = None
