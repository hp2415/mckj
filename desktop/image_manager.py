import asyncio
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication, QListWidget
from shiboken6 import isValid
import httpx
from logger_cfg import logger
from perf_timing import async_span, enabled as _perf_enabled, log as _perf_log

if TYPE_CHECKING:
    from ui.widgets.product_card import ProductItemWidget

_IMAGE_LOAD_CONCURRENCY = 6
_IMAGE_LOAD_CONCURRENCY_LITE = 3
_VIEWPORT_BUFFER_PX = 80
_PRODUCT_DISPLAY_SIZE = (110, 120)


def _is_card_alive(card_widget) -> bool:
    """异步加载完成时卡片可能已被列表销毁，需先确认 C++ 对象仍有效。"""
    try:
        return (
            card_widget is not None
            and isValid(card_widget)
            and hasattr(card_widget, "img_label")
            and isValid(card_widget.img_label)
        )
    except RuntimeError:
        return False


def _decode_and_scale_image(
    blob: bytes,
    target_size: tuple[int, int],
    *,
    lite_mode: bool,
) -> QImage | None:
    """在工作线程中解码并缩放（QImage 线程安全；勿在此创建 QPixmap）。"""
    if not blob:
        return None
    image = QImage()
    if not image.loadFromData(blob) or image.isNull():
        return None
    tw, th = target_size
    mode = Qt.FastTransformation if lite_mode else Qt.SmoothTransformation
    scaled = image.scaled(tw, th, Qt.KeepAspectRatio, mode)
    return scaled if scaled and not scaled.isNull() else None


class ImageManager:
    """
    独立管理应用内的图片资源调度。
    维护内存 LRU 队列，并处理从内存、磁盘缓存和网络的三级读取逻辑。
    """
    def __init__(self, api_client, *, lite_mode: bool = False):
        self.api = api_client
        self._lite_mode = bool(lite_mode)
        self._pixmap_cache = OrderedDict()
        self.MAX_PIXMAP_COUNT = 100 if self._lite_mode else 150
        self._http_session = None
        concurrency = _IMAGE_LOAD_CONCURRENCY_LITE if self._lite_mode else _IMAGE_LOAD_CONCURRENCY
        self._load_semaphore = asyncio.Semaphore(concurrency)
        self._bound_product_lists: set[int] = set()

    def get_http_session(self):
        """延迟初始化 HTTP Client 以兼容 async context"""
        if not self._http_session:
            self._http_session = httpx.AsyncClient(timeout=10.0)
        return self._http_session

    def bind_product_list(self, list_widget: QListWidget):
        """绑定商品列表滚动事件，仅加载视口内卡片图片。"""
        key = id(list_widget)
        if key in self._bound_product_lists:
            return
        self._bound_product_lists.add(key)
        list_widget.verticalScrollBar().valueChanged.connect(
            lambda *_: self.schedule_product_list_images_deferred(list_widget)
        )
        list_widget.model().rowsInserted.connect(
            lambda *_: self.schedule_product_list_images_deferred(list_widget)
        )
        list_widget.model().layoutChanged.connect(
            lambda *_: self.schedule_product_list_images_deferred(list_widget)
        )

    def schedule_product_list_images_deferred(self, list_widget: QListWidget, delay_ms: int = 0):
        """布局完成后再扫描视口，避免加载更多后 visualItemRect 尚未就绪。"""
        QTimer.singleShot(delay_ms, lambda lw=list_widget: self.schedule_product_list_images(lw))

    def schedule_product_list_images(self, list_widget: QListWidget):
        """扫描视口内商品卡片，按需触发图片加载。"""
        from ui.widgets.product_card import ProductItemWidget

        if list_widget is None or list_widget.count() <= 0:
            return
        viewport = list_widget.viewport()
        view_top = -_VIEWPORT_BUFFER_PX
        view_bottom = max(1, viewport.height()) + _VIEWPORT_BUFFER_PX
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if item is None:
                continue
            rect = list_widget.visualItemRect(item)
            if rect.isNull() or rect.height() <= 0:
                continue
            item_top = rect.y()
            item_bottom = item_top + rect.height()
            if item_bottom < view_top or item_top > view_bottom:
                continue
            card = list_widget.itemWidget(item)
            if not isinstance(card, ProductItemWidget):
                continue
            url = card.product_data.get("cover_img")
            if not url or card.is_image_loaded() or getattr(card, "_image_scheduled", False):
                continue
            card.mark_image_scheduled()
            asyncio.create_task(self.async_load_image(card, url))

    async def async_load_image(self, card_widget, relative_url):
        """三级缓存图片加载策略：L1(内存LRU) -> L2(SQLite) -> L3(网络)"""
        if not relative_url:
            return
        async with self._load_semaphore:
            await self._async_load_image_impl(card_widget, relative_url)

    def _target_image_size(self) -> tuple[int, int]:
        display_w, display_h = _PRODUCT_DISPLAY_SIZE
        app = QApplication.instance()
        dpr = float(app.devicePixelRatio()) if app else 1.0
        if self._lite_mode:
            scale = 1.0
        else:
            scale = min(max(1.0, dpr), 2.0)
        return int(display_w * scale), int(display_h * scale)

    def _put_pixmap_cache(self, relative_url: str, pixmap: QPixmap):
        self._pixmap_cache[relative_url] = pixmap
        self._pixmap_cache.move_to_end(relative_url)
        if len(self._pixmap_cache) > self.MAX_PIXMAP_COUNT:
            self._pixmap_cache.popitem(last=False)

    async def _blob_to_scaled_pixmap(self, blob: bytes) -> QPixmap | None:
        """解码+缩放放到工作线程，主线程只做 QPixmap.fromImage。"""
        if not blob:
            return None
        target = self._target_image_size()
        async with async_span("img.decode_scale", bytes=len(blob)):
            image = await asyncio.to_thread(
                _decode_and_scale_image,
                blob,
                target,
                lite_mode=self._lite_mode,
            )
        if image is None or image.isNull():
            return None
        pixmap = QPixmap.fromImage(image)
        return pixmap if pixmap and not pixmap.isNull() else None

    async def _async_load_image_impl(self, card_widget, relative_url):
        from ui.widgets.product_card import ProductItemWidget

        if not isinstance(card_widget, ProductItemWidget):
            return
        if not _is_card_alive(card_widget):
            return
        if card_widget.is_image_loaded():
            return

        # 1. 检查 L1 内存缓存（主线程，无 IO）
        if relative_url in self._pixmap_cache:
            self._pixmap_cache.move_to_end(relative_url)
            if _is_card_alive(card_widget):
                card_widget.update_image(self._pixmap_cache[relative_url])
            if _perf_enabled():
                _perf_log("img.cache_hit", 0.0, force=True, layer="L1")
            return

        pixmap = None
        source = "miss"
        t0 = time.perf_counter() if _perf_enabled() else 0.0
        cache_key = self.api._generate_cache_key("img", path=relative_url)

        # 2. L2 磁盘缓存：Fernet 解密 + 解码缩放均 offload，避免阻塞 qasync/Qt 循环
        if self.api.storage:
            try:
                cached_blob = await asyncio.to_thread(self.api.storage.load_data, cache_key)
            except Exception:
                cached_blob = None
            if cached_blob:
                pixmap = await self._blob_to_scaled_pixmap(cached_blob)
                if pixmap:
                    source = "L2"

        # 3. L3 网络请求
        session = self.get_http_session()
        if not pixmap and session:
            full_url = f"{self.api.base_url}{relative_url}"
            try:
                async with async_span("img.http_get", path=relative_url[-40:]):
                    resp = await session.get(full_url)
                if resp.status_code == 200 and resp.content:
                    content = resp.content
                    if self.api.storage:
                        try:
                            await asyncio.to_thread(self.api.storage.save_data, cache_key, content)
                        except Exception:
                            pass
                    pixmap = await self._blob_to_scaled_pixmap(content)
                    if pixmap:
                        source = "L3"
            except Exception:
                pass

        # 4. 压入 L1；仅当卡片仍存活时刷新 UI（await 期间列表可能已销毁卡片）
        if pixmap and not pixmap.isNull():
            self._put_pixmap_cache(relative_url, pixmap)
            if _is_card_alive(card_widget):
                card_widget.update_image(pixmap)
            if _perf_enabled():
                _perf_log(
                    "img.load_total",
                    (time.perf_counter() - t0) * 1000.0,
                    force=True,
                    source=source,
                )
            return

        if _is_card_alive(card_widget) and hasattr(card_widget, "reset_image_schedule"):
            card_widget.reset_image_schedule()

    def handle_full_copy_image(self, relative_url):
        """处理高清原图复制请求：从系统剪贴板存入"""
        if not relative_url:
            return

        cache_key = self.api._generate_cache_key("img", path=relative_url)
        if self.api.storage:
            # 用户主动复制，偶发路径；仍尽量短阻塞，优先 L1
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
        self._bound_product_lists.clear()
        if self._http_session:
            await self._http_session.aclose()
            self._http_session = None
