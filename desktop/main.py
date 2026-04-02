import sys
import asyncio
import httpx
from qasync import QEventLoop, asyncSlot
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QPixmap

# 本地模块导入
from api_client import APIClient
from ui.login_dialog import LoginDialog
from ui.main_window import MainWindow

class DesktopApp:
    """
    整合 UI 界面与异步通讯总线。
    负责控制窗口跳转、异步信号处理以及数据同步流。
    """
    def __init__(self):
        # 默认连接本地后端
        self.api = APIClient("http://localhost:8000")
        self.login_dlg = None
        self.main_win = None

    async def launch(self):
        """进入程序生命周期"""
        self.login_dlg = LoginDialog()
        self.login_dlg.login_requested.connect(self._handle_login)
        
        # 核心修复：qasync 下不要在协程内使用阻塞的 exec()，而是使用 show() 并等待结果
        self.login_dlg.show()
        
        # 创建一个 Future 用于等待登录结果
        self._login_future = asyncio.get_event_loop().create_future()
        self.login_dlg.finished.connect(self._on_login_dialog_finished)
        
        # 挂起 launch 协程，直到对话框关闭
        result_code = await self._login_future
        
        if result_code == LoginDialog.Accepted:
            # 鉴权通过，构建并展示主看板
            user_name = self.api.user_data.get("real_name", "管理员")
            self.main_win = MainWindow(user_name)
            self.main_win.search_requested.connect(self._handle_search)
            self.main_win.show()
            
            # 手动触发首屏数据的初始同步
            await self._handle_search("", 0, 20)

    def _on_login_dialog_finished(self, result_code):
        """当对话框关闭时，通知 launch 协程继续执行"""
        if not self._login_future.done():
            self._login_future.set_result(result_code)

    @asyncSlot()
    async def _handle_login(self, u, p):
        """处理来自 UI 的登录请求信号"""
        # 登录过程中禁用按钮，防止重复提交
        self.login_dlg.login_btn.setEnabled(False)
        self.login_dlg.login_btn.setText("验证中...")
        
        success, msg = await self.api.login(u, p)
        
        if success:
            self.login_dlg.accept() # 这会触发 finished 信号
        else:
            QMessageBox.warning(self.login_dlg, "登录识别失败", msg)
            self.login_dlg.login_btn.setEnabled(True)
            self.login_dlg.login_btn.setText("立即验证并登录")

    @asyncSlot()
    async def _handle_search(self, keyword, skip, limit):
        """处理来自 UI 的商品检索请求信号"""
        response_json = await self.api.search_products(keyword, skip, limit)
        if response_json and response_json.get("code") == 200:
            # 适配后端包装后的数据结构 {code, message, data: {items, total, has_more ...}}
            payload = response_json.get("data", {})
            items = payload.get("items", [])
            for item_data in items:
                card_widget = self.main_win.add_product_card(item_data)
                # 开启“无痛加载”模式：主线程渲染骨架，后台异步加载图片
                asyncio.create_task(self._async_load_image(card_widget, item_data.get("cover_img")))
            
            # 更新加载更多按钮状态
            self.main_win.update_has_more(payload.get("has_more", False))

    async def _async_load_image(self, card_widget, relative_url):
        """后台异步解析本地图库并更新至对应的卡片"""
        if not relative_url:
            return
            
        full_url = f"{self.api.base_url}{relative_url}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(full_url)
                if resp.status_code == 200:
                    pixmap = QPixmap()
                    # 将二进制流转为 Qt 纹理
                    if pixmap.loadFromData(resp.content):
                        card_widget.update_image(pixmap)
        except Exception as e:
            # 静默处理图片加载失败，UI 层会保持占位状态
            pass

if __name__ == "__main__":
    # 初始化 Qt 程序
    qt_app = QApplication(sys.argv)
    
    # 【核心修复 2】防止登录窗口关闭导致整个程序被 OS 给掐死
    # 默认情况下，Qt 发现最后一个窗口（登录框）关闭时会直接退出，
    # 导致主窗口还没来得及 show() 程序就没了。
    qt_app.setQuitOnLastWindowClosed(False)
    
    # 将 asyncio 循环与 Qt 循环融合
    event_loop = QEventLoop(qt_app)
    asyncio.set_event_loop(event_loop)
    
    desktop_app = DesktopApp()
    
    with event_loop:
        # 使用 event_loop 同时驱动 Qt 事件和异步协程
        event_loop.run_until_complete(desktop_app.launch())
        # 在主窗口成功显示后，恢复“关闭最后一个窗口即退出”的标准行为
        qt_app.setQuitOnLastWindowClosed(True)
        event_loop.run_forever()
