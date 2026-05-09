import os
import time
import ctypes
import psutil
import win32clipboard
import uiautomation as auto
from loguru import logger


class WeChatController:
    def __init__(self):
        self.wechat_window = None
        self._last_receiver = None
        self._bound_wxid = None      # 记录当前已校验过的 WxID
        self._bound_nickname = None  # 记录当前已校验过的昵称

    def _get_wechat_window(self):
        """查找并激活微信窗口（兼容旧版和 4.0+ 新版）。"""
        # 尝试多种可能的类名，包括 Qt 5/6 动态生成的类名
        class_names = ["mmui::MainWindow", "Qt51514QWindowIcon", "Qt6QWindowIcon", "WeChatMainWndForPC"]

        for cls in class_names:
            self.wechat_window = auto.WindowControl(searchDepth=1, ClassName=cls)
            if self.wechat_window.Exists(0):
                logger.debug(f"通过类名 {cls} 快速找到微信窗口")
                break

        # 如果通过固定类名找不到，使用正则表达式进行模糊匹配，提升 4.1.8+ 版本的检索性能
        if not self.wechat_window or not self.wechat_window.Exists(0):
            self.wechat_window = auto.WindowControl(searchDepth=1, ClassNameRegex=r"Qt\d+QWindowIcon", Name="微信")
            if self.wechat_window.Exists(0):
                logger.debug(f"通过正则模式匹配到微信窗口: {self.wechat_window.ClassName}")

        if not self.wechat_window or not self.wechat_window.Exists(0):
            # 最后尝试直接通过名称查找
            self.wechat_window = auto.WindowControl(searchDepth=1, Name="微信")

        if not self.wechat_window.Exists(0):
            logger.warning("未找到微信窗口，请确保微信已登录且窗口处于打开状态。")
            return False

        # 激活窗口
        try:
            # 检查窗口是否最小化
            try:
                pattern = self.wechat_window.GetWindowPattern()
                if pattern and pattern.WindowVisualState == auto.WindowVisualState.Minimized:
                    self.wechat_window.SetVisualState(auto.WindowVisualState.Normal)
            except Exception:
                pass  # 有些窗口可能不支持 WindowPattern

            self.wechat_window.Show()
            self.wechat_window.SetActive()
            return True
        except Exception as e:
            logger.error(f"激活微信窗口失败: {e}")
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

    def _switch_to_chat(self, receiver: str) -> bool:
        """根据版本自动适配，切换到指定联系人的聊天窗口。"""
        try:
            if not self._get_wechat_window():
                return False

            # 如果连续发送给同一个人，4.1.8+ 可能会丢失焦点
            # 我们通过“先切走，再切回”的策略强行刷新焦点
            if self._last_receiver == receiver:
                logger.debug(f"连续发送给 {receiver}，执行中转聚焦...")
                # 中转到文件传输助手
                self.wechat_window.SendKeys('{CTRL}f', waitTime=0.3)
                auto.SetClipboardText("文件传输助手")
                auto.SendKeys('{CTRL}v', waitTime=0.3)
                auto.SendKeys('{ENTER}', waitTime=0.5)

            # 1. 触发搜索
            self.wechat_window.SendKeys('{CTRL}f', waitTime=0.5)

            # 2. 输入名称并回车
            auto.SetClipboardText(receiver)
            auto.SendKeys('{CTRL}a', waitTime=0.1)
            auto.SendKeys('{CTRL}v', waitTime=0.5)
            auto.SendKeys('{ENTER}', waitTime=1.0)

            self._last_receiver = receiver

            # 3. 尝试聚焦聊天输入框 (增加位置校验，防止误认搜索框)
            chat_input = self.wechat_window.EditControl(searchDepth=15, autoId="chat_input_field")
            if not chat_input.Exists(0):
                chat_input = self.wechat_window.Control(searchDepth=15, ClassName="mmui::ChatInputField")

            # ID/类名定位 (需满足在窗口下半部分的逻辑)
            rect = self.wechat_window.BoundingRectangle
            height = rect.bottom - rect.top

            is_valid_input = False
            if chat_input.Exists(0.5):
                # 关键校验：输入框必须在窗口的中线以下
                if chat_input.BoundingRectangle.top > rect.top + height * 0.5:
                    chat_input.Click()
                    logger.debug("通过 ID/类名成功聚焦聊天输入框")
                    is_valid_input = True
                else:
                    logger.warning("检测到的输入框位置异常（疑似搜索框），将切换到比例点击方案。")

            if not is_valid_input:
                # 比例点击法 (针对 4.1.8+ 修正版)
                width = rect.right - rect.left
                # 点击位置：横向 70%，纵向 90% (稍微下移，确保避开可能的搜索干扰)
                target_x = int(rect.left + width * 0.7)
                target_y = int(rect.top + height * 0.9)

                auto.Click(target_x, target_y)
                logger.debug(f"已执行比例点击聚焦: ({target_x}, {target_y})")

            return True

        except Exception as e:
            logger.error(f"寻找联系人 {receiver} 失败: {e}")
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

    def send_message(self, receiver: str, message: str) -> bool:
        """向指定联系人发送文本消息。"""
        try:
            if not self._switch_to_chat(receiver):
                return False

            # 2. 输入并发送消息
            auto.SetClipboardText(message)
            auto.SendKeys('{CTRL}v', waitTime=0.5)
            time.sleep(3)
            auto.SendKeys('{ENTER}', waitTime=0.5)

            logger.info(f"成功发送消息给 {receiver}")
            return True
        except Exception as e:
            logger.error(f"发送消息给 {receiver} 失败: {e}")
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

