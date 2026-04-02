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
        self._http_session = None
        self._pixmap_cache = {}  # L1 内存缓存：url -> QPixmap

    async def launch(self):
        """进入程序生命周期"""
        # 初始化持续性的 HTTP 会话连接池，消除握手延迟
        if not self._http_session:
            self._http_session = httpx.AsyncClient(timeout=10.0)
            
        self.login_dlg = LoginDialog()
        self.login_dlg.login_requested.connect(self._handle_login)
        self.login_dlg.show()
        
        self._login_future = asyncio.get_event_loop().create_future()
        self.login_dlg.finished.connect(self._on_login_dialog_finished)
        
        result_code = await self._login_future
        
        if result_code == LoginDialog.Accepted:
            # 鉴权通过，构建主看板
            user_name = self.api.user_data.get("real_name", "管理员")
            self.main_win = MainWindow(user_name)
            self.main_win.search_requested.connect(self._handle_search)
            self.main_win.customer_selected.connect(self._handle_customer_selected)
            self.main_win.info_page.save_clicked.connect(self._handle_save_customer_relation)
            self.main_win.logout_btn.clicked.connect(self._handle_logout)
            self.main_win.show()
            
            # 【关键】主窗口显示后，恢复“最后一个窗口关闭即退出”的行为，
            # 这样手动点击 X 时，QApplication 会正常终止
            QApplication.setQuitOnLastWindowClosed(True)
            
            # 并行初始化数据加载
            asyncio.create_task(self._initial_data_fetch())
        else:
            # 登录界面被手动关闭
            if self._http_session:
                asyncio.create_task(self._http_session.aclose())
            QApplication.quit()

    async def _initial_data_fetch(self):
        """首屏数据并行拉取逻辑"""
        # 1. 加载客户列表
        customers_resp = await self.api.get_my_customers()
        if customers_resp and customers_resp.get("code") == 200:
            self.main_win.update_customer_list(customers_resp.get("data", []))

        # 2. 默认加载商品搜索
        self.main_win.stack.setCurrentIndex(2)
        await self.perform_search("", 0, 20)

    @asyncSlot()
    async def _handle_logout(self):
        """注销重启：临时接管退出信号，防止主窗口关闭导致进程被杀"""
        if self.main_win:
            # 临时关闭自动退出，确保接下来的 close() 不会干掉整个进程
            QApplication.setQuitOnLastWindowClosed(False)
            self.main_win.close()
            self.main_win = None
            
        # 清理内存 L1 缓存与会话
        self._pixmap_cache.clear()
        if self._http_session:
            await self._http_session.aclose()
            self._http_session = None
            
        self.api.logout()
        # 重启登录流程
        self._restart_task = asyncio.create_task(self.launch())

    @asyncSlot()
    async def _handle_customer_selected(self, customer_data):
        """当侧边栏选中某个客户时触发"""
        print(f"已选中客户: {customer_data.get('customer_name')}")
        # 自动切换到资料页并填充表单
        self.main_win.stack.setCurrentIndex(1)
        self.main_win.info_page.set_customer(customer_data)

    @asyncSlot()
    async def _handle_save_customer_relation(self, phone, update_data):
        """处理客户动态资料的保存提交"""
        resp = await self.api.update_customer_relation(phone, update_data)
        if resp and resp.get("code") == 200:
            QMessageBox.information(self.main_win, "同步成功", "客户动态笔记已成功更新至云端。")
            # 重新拉取一次客户列表以刷新本地数据
            customers_resp = await self.api.get_my_customers()
            if customers_resp and customers_resp.get("code") == 200:
                self.main_win.update_customer_list(customers_resp.get("data", []))
        else:
            msg = resp.get("message", "未知错误") if resp else "服务器无响应"
            QMessageBox.warning(self.main_win, "同步失败", f"更新失败: {msg}")

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

    async def perform_search(self, keyword, skip, limit):
        """核心业务：执行搜索并驱动 UI 更新"""
        response_json = await self.api.search_products(keyword, skip, limit)
        if response_json and response_json.get("code") == 200:
            payload = response_json.get("data", {})
            items = payload.get("items", [])
            
            # 如果是第一页，清空列表防止重复
            if skip == 0:
                self.main_win.product_list.clear()

            for item_data in items:
                card_widget = self.main_win.add_product_card(item_data)
                # 后台异步并发下载图片，不阻塞渲染
                asyncio.create_task(self._async_load_image(card_widget, item_data.get("cover_img")))
            
            # 更新“加载更多”按钮的可见性
            self.main_win.update_has_more(payload.get("has_more", False))

    @asyncSlot()
    async def _handle_search(self, keyword, skip, limit):
        """处理来自 UI 的搜索/翻页信号"""
        await self.perform_search(keyword, skip, limit)

    async def _async_load_image(self, card_widget, relative_url):
        """三级缓存图片加载策略：L1(内存) -> L2(SQLite) -> L3(网络)"""
        if not relative_url:
            return
            
        # 1. 检查 L1 内存缓存 (瞬时响应)
        if relative_url in self._pixmap_cache:
            card_widget.update_image(self._pixmap_cache[relative_url])
            return

        # 2. 检查 L2 磁盘持久化缓存 (SQLite)
        cache_key = self.api._generate_cache_key("img", path=relative_url)
        if self.api.storage:
            cached_blob = self.api.storage.load_data(cache_key)
            if cached_blob:
                pixmap = QPixmap()
                if pixmap.loadFromData(cached_blob):
                    # 存入 L1 方便下次使用
                    self._pixmap_cache[relative_url] = pixmap
                    card_widget.update_image(pixmap)
                    return

        # 3. 发起 L3 网络请求 (复用持久会话)
        if not self._http_session:
            return
            
        full_url = f"{self.api.base_url}{relative_url}"
        try:
            resp = await self._http_session.get(full_url)
            if resp.status_code == 200:
                # 写入 L2 磁盘
                if self.api.storage:
                    self.api.storage.save_data(cache_key, resp.content)
                
                pixmap = QPixmap()
                if pixmap.loadFromData(resp.content):
                    # 写入 L1 内存
                    self._pixmap_cache[relative_url] = pixmap
                    card_widget.update_image(pixmap)
        except Exception:
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
        # 在入口处主动创建第一个 launch 任务
        # 使用 event_loop 实例直接创建任务，避免 asyncio 的运行时检查报错
        event_loop.create_task(desktop_app.launch())
        event_loop.run_forever()
