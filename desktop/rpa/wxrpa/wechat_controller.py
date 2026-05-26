import os
import re
import time
import ctypes
import threading
from ctypes import wintypes

import psutil
import win32clipboard
import uiautomation as auto
from loguru import logger


# ---------------------------------------------------------------------------
# Win32 直查微信窗口 —— 关键的性能 / 防卡死优化
#
# uiautomation 的 ``WindowControl(searchDepth=1, ...).Exists(0)`` 会枚举所有
# 顶层窗口，并通过 UIA / SendMessage 跨进程同步查询每个窗口的属性（ClassName
# 等）。当桌面端自己的 UI 线程在做较重的工作（例如新版 Markdown 气泡渲染）
# 时，UIA 查询会被 ``SendMessage`` 卡住——日志里曾经看到单次 ``Exists(0)``
# 卡了 61 秒。
#
# 这里改用 Win32 ``FindWindowW`` / ``EnumWindows``：它们直接读窗口管理器的
# 内核数据，不会向目标进程发送同步消息，毫秒级返回。
# ---------------------------------------------------------------------------
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL

_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL

_SW_RESTORE = 9


def _find_wechat_hwnd(
    cancel_event: threading.Event | None = None,
) -> int:
    """通过 Win32 API 直接定位微信主窗口 HWND。

    比 uiautomation 的顶层窗口枚举快几个数量级，且**不会**因桌面端自身 UI
    线程繁忙而 SendMessage 死锁。返回 0 表示没找到。
    """

    def _stopped() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    # 已知的微信窗口类名候选，按命中概率排序
    class_candidates = [
        "mmui::MainWindow",
        "Qt51514QWindowIcon",
        "Qt6QWindowIcon",
        "WeChatMainWndForPC",
    ]
    for cls in class_candidates:
        if _stopped():
            return 0
        hwnd = _user32.FindWindowW(cls, None)
        if hwnd:
            logger.info(f"Win32 FindWindowW 命中类名 {cls} → HWND=0x{hwnd:x}")
            return int(hwnd)

    if _stopped():
        return 0

    # 类名正则兜底：动态 Qt 类名（如 Qt621QWindowIcon）通过 EnumWindows 匹配。
    # 标题不限定为精确 "微信"，因为微信会在未读时把标题改成 "微信 (3)" 之类。
    qt_pattern = re.compile(r"^Qt\d+QWindowIcon$")
    matched: list[int] = []

    def _enum_proc(hwnd: int, _lparam: int) -> bool:
        if _stopped():
            return False  # 中止枚举
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            cls_buf = ctypes.create_unicode_buffer(256)
            n = _user32.GetClassNameW(hwnd, cls_buf, 256)
            if n <= 0:
                return True
            cls_name = cls_buf.value
            # 精确类名前面已经走过，这里只做正则匹配
            if not qt_pattern.match(cls_name):
                return True
            title_len = _user32.GetWindowTextLengthW(hwnd)
            if title_len <= 0:
                return True
            title_buf = ctypes.create_unicode_buffer(title_len + 1)
            _user32.GetWindowTextW(hwnd, title_buf, title_len + 1)
            title = title_buf.value or ""
            # 微信主窗口标题以 "微信" 开头（可能带未读数后缀）
            if title.startswith("微信"):
                matched.append(int(hwnd))
                return False  # 找到即停
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_EnumWindowsProc(_enum_proc), 0)
    except Exception as e:
        logger.warning(f"EnumWindows 异常: {e}")

    if matched:
        logger.info(f"Win32 EnumWindows 正则匹配命中 → HWND=0x{matched[0]:x}")
        return matched[0]

    if _stopped():
        return 0

    # 标题兜底：FindWindowW(None, "微信") 做精确名称匹配
    hwnd = _user32.FindWindowW(None, "微信")
    if hwnd:
        logger.info(f"Win32 FindWindowW 命中标题 '微信' → HWND=0x{hwnd:x}")
        return int(hwnd)

    return 0


class WeChatController:
    def __init__(self):
        self.wechat_window = None
        self._last_receiver = None
        self._bound_wxid = None      # 记录当前已校验过的 WxID
        self._bound_nickname = None  # 记录当前已校验过的昵称

    def _get_wechat_window(self, cancel_event: threading.Event | None = None):
        """查找并激活微信窗口（Win32 直查）。

        历史教训：旧版用 ``auto.WindowControl(searchDepth=1, ClassName=...)``
        枚举顶层窗口时，会通过 UIA / SendMessage 跨进程查询每个顶层窗口的
        ClassName。一旦桌面端自己的 UI 线程被新 Markdown 气泡渲染拖慢、
        来不及泵消息，UIA 就会在我们自己的窗口上死等，单次 ``Exists(0)``
        被实测卡过 61 秒。

        现在改用 Win32 ``FindWindowW`` / ``EnumWindows`` 直查内核窗口表，
        不会向目标进程发同步消息，毫秒级返回；然后再用 ``ControlFromHandle``
        把 HWND 转成 uiautomation 控件供后续步骤使用。
        """

        def _stopped() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        t0 = time.monotonic()
        logger.info("开始检测微信窗口（Win32 直查）…")

        if _stopped():
            logger.info("微信窗口检测被用户中断。")
            return False

        hwnd = _find_wechat_hwnd(cancel_event=cancel_event)

        if _stopped():
            logger.info("微信窗口检测被用户中断。")
            return False

        if not hwnd:
            total_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                f"未找到微信窗口（耗时 {total_ms}ms），请确认微信已登录且窗口在前台。"
            )
            return False

        # HWND → uiautomation Control（后续 SendKeys / EditControl 等还要用）。
        # ControlFromHandle 走的是 IUIAutomation::ElementFromHandle，单次调用、
        # 不做枚举，正常情况下 < 100ms。
        try:
            self.wechat_window = auto.ControlFromHandle(hwnd)
            if not self.wechat_window:
                logger.error(f"HWND 0x{hwnd:x} 转 UIA Control 返回空值。")
                return False
        except Exception as e:
            logger.exception(f"HWND 0x{hwnd:x} 转 UIA Control 失败: {e}")
            return False

        if _stopped():
            return False

        # 激活窗口直接用 Win32，避免再次触碰 uiautomation 的同步消息路径。
        try:
            if _user32.IsIconic(hwnd):
                _user32.ShowWindow(hwnd, _SW_RESTORE)
            _user32.SetForegroundWindow(hwnd)
            logger.info(
                f"微信窗口已激活 (HWND=0x{hwnd:x}), 总耗时 "
                f"{int((time.monotonic() - t0) * 1000)}ms"
            )
            return True
        except Exception as e:
            logger.error(f"激活微信窗口失败: {e}")
            # 即便 SetForegroundWindow 失败（Win10/11 会限制非前台应用激活
            # 其他窗口），仍认为窗口可用 —— uiautomation 的 SendKeys 会再
            # 尝试激活。返回 True 让上层继续尝试。
            return True

    def _safe_set_clipboard_text(
        self,
        text: str,
        cancel_event: threading.Event | None = None,
        *,
        attempts: int = 8,
        delay_s: float = 0.15,
    ) -> bool:
        """带重试的剪贴板文本写入。

        WeChat 自身也会频繁占用 Windows 剪贴板，``auto.SetClipboardText`` 在
        这种竞争下会偶发性失败甚至无声卡住。这里把重试做透明化，并在用户
        点击中断时立刻退出，避免后台线程吊死在 Win32 OpenClipboard 上。
        """
        for i in range(1, attempts + 1):
            if cancel_event is not None and cancel_event.is_set():
                logger.info("剪贴板写入被用户中断。")
                return False
            try:
                auto.SetClipboardText(text)
                return True
            except Exception as e:
                logger.warning(f"剪贴板写入第 {i}/{attempts} 次失败: {e}")
                time.sleep(delay_s)
        logger.error("剪贴板写入多次失败，可能被其它进程长时间占用。")
        return False

    def get_current_wxid(self) -> str:
        """从微信进程内存映射中提取当前登录账号的 WxID。"""
        try:
            for proc in psutil.process_iter(['name', 'cmdline']):
                if proc.info['name'] == 'Weixin.exe':
                    # 排除副进程
                    if proc.info['cmdline'] and any('--type' in arg for arg in proc.info['cmdline']):
                        continue
                    # 遍历内存映射找到包含 wxid_ 的路径
                    for mem_map in proc.memory_maps():
                        if 'wxid_' in mem_map.path:
                            import re
                            match = re.search(r'wxid_[a-zA-Z0-9]+', mem_map.path)
                            if match:
                                return match.group(0)
            return ""
        except Exception as e:
            logger.debug(f"提取 WxID 失败: {e}")
            return ""

    def verify_login(self, expected_nickname: str) -> bool:
        """
        人工确认 + 底层 WxID 绑定校验。
        """
        current_wxid = self.get_current_wxid()
        if not current_wxid:
            logger.warning("未能检测到微信登录状态，请确保微信已登录。")
            return False

        # 如果已经成功绑定了该 WxID
        if self._bound_wxid == current_wxid:
            return True

        # 如果 WxID 发生了变化（或者是第一次运行）
        if self._bound_wxid is not None and self._bound_wxid != current_wxid:
            logger.critical(f"检测到微信账号切换！当前 ID: {current_wxid}，原绑定 ID: {self._bound_wxid}")
            # 这种情况属于运行中切换账号，为了安全直接拦截
            return False

        # 首次确认：使用系统弹窗
        MB_YESNO = 0x04
        MB_ICONQUESTION = 0x20
        IDYES = 6

        msg = f"【身份确认】\n\n当前程序配置角色为: {expected_nickname}\n请确认您的微信已登录该账号。\n\n是否继续执行？"
        res = ctypes.windll.user32.MessageBoxW(0, msg, "微信群发助手 - 身份安全确认", MB_YESNO | MB_ICONQUESTION)

        if res == IDYES:
            self._bound_wxid = current_wxid
            logger.success(f"人工确认通过，已锁定微信账号: {current_wxid}")
            return True
        else:
            logger.error("人工取消发送。")
            return False

    def _switch_to_chat(
        self, receiver: str, cancel_event: threading.Event | None = None
    ) -> bool:
        """根据版本自动适配，切换到指定联系人的聊天窗口。

        关键节点都打 INFO 日志、加 cancel 检查，方便定位“一直转圈”到底卡在哪一步。
        """

        def _stopped() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        t0 = time.monotonic()
        try:
            if _stopped():
                return False
            logger.info(f"[RPA] 准备切换到联系人: {receiver}")
            if not self._get_wechat_window(cancel_event=cancel_event):
                return False

            # 如果连续发送给同一个人，4.1.8+ 可能会丢失焦点
            # 我们通过“先切走，再切回”的策略强行刷新焦点
            if self._last_receiver == receiver:
                if _stopped():
                    return False
                logger.info(f"[RPA] 连续发送给 {receiver}，执行中转聚焦…")
                self.wechat_window.SendKeys('{CTRL}f', waitTime=0.5)
                if _stopped():
                    return False
                if not self._safe_set_clipboard_text("文件传输助手", cancel_event):
                    return False
                auto.SendKeys('{CTRL}v', waitTime=0.5)
                if _stopped():
                    return False
                # 等待微信搜索列表刷新后再回车，避免过快导致未选中联系人
                time.sleep(1)
                if _stopped():
                    return False
                auto.SendKeys('{ENTER}', waitTime=0.8)

            if _stopped():
                return False
            logger.info("[RPA] 触发搜索框 (Ctrl+F)")
            self.wechat_window.SendKeys('{CTRL}f', waitTime=0.5)

            if _stopped():
                return False
            logger.info("[RPA] 写入接收方到剪贴板")
            if not self._safe_set_clipboard_text(receiver, cancel_event):
                return False
            if _stopped():
                return False
            auto.SendKeys('{CTRL}a', waitTime=0.1)
            if _stopped():
                return False
            auto.SendKeys('{CTRL}v', waitTime=0.5)
            time.sleep(1)
            if _stopped():
                return False
            auto.SendKeys('{ENTER}', waitTime=1.0)

            self._last_receiver = receiver

            if _stopped():
                return False
            logger.info("[RPA] 尝试聚焦聊天输入框")
            t_focus = time.monotonic()
            chat_input = self.wechat_window.EditControl(searchDepth=15, autoId="chat_input_field")
            if not chat_input.Exists(0):
                if _stopped():
                    return False
                chat_input = self.wechat_window.Control(searchDepth=15, ClassName="mmui::ChatInputField")

            if _stopped():
                return False
            # ID/类名定位 (需满足在窗口下半部分的逻辑)
            rect = self.wechat_window.BoundingRectangle
            height = rect.bottom - rect.top

            is_valid_input = False
            if chat_input.Exists(0.5):
                if _stopped():
                    return False
                if chat_input.BoundingRectangle.top > rect.top + height * 0.5:
                    chat_input.Click()
                    logger.info(
                        f"[RPA] 通过 ID/类名聚焦输入框成功 ({int((time.monotonic() - t_focus) * 1000)}ms)"
                    )
                    is_valid_input = True
                else:
                    logger.warning("检测到的输入框位置异常（疑似搜索框），将切换到比例点击方案。")

            if not is_valid_input:
                if _stopped():
                    return False
                width = rect.right - rect.left
                target_x = int(rect.left + width * 0.7)
                target_y = int(rect.top + height * 0.9)
                auto.Click(target_x, target_y)
                logger.info(f"[RPA] 比例点击聚焦输入框: ({target_x}, {target_y})")

            if _stopped():
                return False
            logger.info(f"[RPA] 已就绪发送，切换耗时 {int((time.monotonic() - t0) * 1000)}ms")
            return True

        except Exception as e:
            logger.exception(f"寻找联系人 {receiver} 失败: {e}")
            return False

    def _set_clipboard_files(self, file_paths: list[str]):
        """将一组文件路径放入 Windows 剪贴板 (CF_HDROP 格式)。"""

        class DROPFILES(ctypes.Structure):
            _fields_ = [
                ("pFiles", ctypes.c_uint),
                ("x", ctypes.c_long),
                ("y", ctypes.c_long),
                ("fNC", ctypes.c_int),
                ("fWide", ctypes.c_bool),
            ]

        pDropFiles = DROPFILES()
        pDropFiles.pFiles = ctypes.sizeof(DROPFILES)
        pDropFiles.fWide = True

        # 将路径转换为 Windows 格式并以 \0 分隔，结尾双 \0
        files = ("\0".join([os.path.abspath(p) for p in file_paths])).replace("/", "\\")
        data = files.encode("U16")[2:] + b"\0\0"

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, bytes(pDropFiles) + data)
        finally:
            win32clipboard.CloseClipboard()

    def send_message(
        self,
        receiver: str,
        message: str,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """向指定联系人发送文本消息。"""

        def _stopped() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        t0 = time.monotonic()
        try:
            if _stopped():
                return False
            logger.info(f"[RPA] send_message 开始: receiver={receiver}, len(msg)={len(message)}")
            if not self._switch_to_chat(receiver, cancel_event=cancel_event):
                return False

            if _stopped():
                return False

            logger.info("[RPA] 写入消息正文到剪贴板")
            if not self._safe_set_clipboard_text(message, cancel_event):
                return False
            if _stopped():
                return False
            auto.SendKeys('{CTRL}v', waitTime=0.5)

            # 等待粘贴稳定（最多 3 秒），期间持续响应中断
            for _ in range(30):
                if _stopped():
                    return False
                time.sleep(0.1)

            if _stopped():
                return False
            auto.SendKeys('{ENTER}', waitTime=0.5)

            logger.info(
                f"成功发送消息给 {receiver}，总耗时 {int((time.monotonic() - t0) * 1000)}ms"
            )
            return True
        except Exception as e:
            logger.exception(f"发送消息给 {receiver} 失败: {e}")
            return False

    def send_images(self, receiver: str, image_paths: list[str]) -> bool:
        """向指定联系人发送一组图片。"""
        try:
            # 校验文件是否存在
            valid_paths = [p for p in image_paths if os.path.exists(p)]
            if not valid_paths:
                logger.error(f"没有找到有效的图片文件: {image_paths}")
                return False

            if not self._switch_to_chat(receiver):
                return False

            # 2. 复制图片文件到剪贴板
            self._set_clipboard_files(valid_paths)

            # 3. 粘贴并发送
            auto.SendKeys('{CTRL}v', waitTime=1.5)
            auto.SendKeys('{ENTER}', waitTime=0.5)

            logger.info(f"成功发送 {len(valid_paths)} 张图片给 {receiver}")
            return True
        except Exception as e:
            logger.error(f"发送图片给 {receiver} 失败: {e}")
            return False

    def get_self_nickname(self) -> str:
        """获取当前登录账号的昵称（占位符，建议从配置获取）。"""
        return "未知"


# Singleton instance
wechat = WeChatController()

