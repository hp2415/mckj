import os
import shutil
import sys
import configparser

# 服务器迁移：新环境权威地址与需替换的旧地址（临时方案，后续可改为域名）
CANONICAL_API_URL = "http://192.168.0.100:8080"
LEGACY_API_URLS = frozenset({
    "http://192.168.0.193:8000",
})


def normalize_api_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


class Config:
    """
    配置管理类：从外部 config.ini 读取设置。
    """
    def __init__(self):
        self.config = configparser.ConfigParser()
        # 确定配置文件路径：
        # - 开发模式：与源码同级
        # - 打包模式：优先 exe 同级；若不可写（如 Program Files），回退到用户可写目录（%LOCALAPPDATA%）
        self.config_path = self._resolve_config_path()
        self._load_defaults()
        
        if os.path.exists(self.config_path):
            try:
                self.config.read(self.config_path, encoding="utf-8")
            except Exception as e:
                print(f"配置文件解析错误: {e}")
            # 一次性迁移：移除已废弃的 ai_chat_model_pinned 键（改由管理后台统一下发默认）
            if self.config.has_option("Runtime", "ai_chat_model_pinned"):
                self.config.remove_option("Runtime", "ai_chat_model_pinned")
                self._save_current_config()
            self._migrate_legacy_api_url()
        else:
            # 核心改进：如果配置不存在，则自动通过默认值生成一份到磁盘
            self._save_current_config()

    def _app_name(self) -> str:
        if getattr(sys, "frozen", False):
            return os.path.splitext(os.path.basename(sys.executable))[0] or "WeChatAI_Assistant"
        return "WeChatAI_Assistant"

    def _user_config_dir(self) -> str:
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(root, self._app_name())

    def _is_path_writable(self, path: str) -> bool:
        target = path if os.path.isdir(path) else os.path.dirname(path) or path
        probe = os.path.join(target, ".write_test")
        try:
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            return True
        except OSError:
            return False

    def _resolve_config_path(self) -> str:
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(sys.executable)
            primary = os.path.join(exe_dir, "config.ini")
            user_dir = self._user_config_dir()
            os.makedirs(user_dir, exist_ok=True)
            user_config = os.path.join(user_dir, "config.ini")

            # 用户目录副本优先：迁移或不可写回退后的权威来源
            if os.path.exists(user_config):
                return user_config

            if os.path.exists(primary):
                if not self._is_path_writable(primary):
                    try:
                        shutil.copy2(primary, user_config)
                        return user_config
                    except OSError:
                        return primary
                return primary

            if self._is_path_writable(exe_dir):
                return primary
            return user_config

        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

    def _load_defaults(self):
        """设置容错默认值"""
        if not self.config.has_section("Network"):
            self.config.add_section("Network")
        self.config.set("Network", "api_url", CANONICAL_API_URL)
        self.config.set("Network", "timeout", "15")

        if not self.config.has_section("Runtime"):
            self.config.add_section("Runtime")
        self.config.set("Runtime", "log_level", "INFO")
        self.config.set("Runtime", "sync_interval_min", "10")
        self.config.set("Runtime", "theme_mode", "light")      # 新增：主题模式
        self.config.set("Runtime", "snap_enabled", "false")    # 新增：吸附开关
        self.config.set("Runtime", "snap_class", "")          # 新增：吸附类名
        self.config.set("Runtime", "snap_title", "")          # 新增：吸附标题
        self.config.set("Runtime", "ai_chat_model", "qwen3.5-plus")  # 客户对话选用的 LLM（与后台画像 llm_model 独立）
        self.config.set("Runtime", "chat_input_height", "140") # 对话输入框高度
        self.config.set("Runtime", "lite_mode", "auto")  # 轻量模式: auto / true / false
        # 注意：桌面端默认对话模型完全由管理后台 desktop_default_chat_models 决定，
        # 本机勾选仅在当前会话内生效；此处的 ai_chat_model 仅作为后端尚未下发时的兜底。

        if not self.config.has_section("CustomerLeads"):
            self.config.add_section("CustomerLeads")
        self.config.set("CustomerLeads", "claimed_sort", "assign_time")
        self.config.set("CustomerLeads", "claimed_order", "asc")
        self.config.set("CustomerLeads", "favorite_sort", "collected_time")
        self.config.set("CustomerLeads", "favorite_order", "desc")

    def _save_current_config(self):
        """将当前内存中的配置对象持久化到磁盘 config.ini，并保留/自动生成注释"""
        # 定义字段注释（中文说明）
        comments = {
            "api_url": "后端 API 接口基础地址",
            "api_url_lock": "设为 true 时锁定 api_url，启动/更新不会自动改写（测试用）",
            "timeout": "网络请求超时时间 (秒)",
            "log_level": "日志记录级别 (DEBUG, INFO, WARNING, ERROR)",
            "sync_interval_min": "云端数据自动同步间隔 (分钟)",
            "theme_mode": "主题模式 (light 为浅色，dark 为深色)",
            "snap_enabled": "窗口吸附功能开关 (true/false)",
            "snap_class": "吸附目标窗口的类名 (校准后自动填充)",
            "snap_title": "吸附目标窗口的标题 (校准后自动填充)",
            "chat_input_height": "对话输入框的高度 (像素)",
            "lite_mode": "轻量模式 (auto=自动检测低配机, true/false=强制开关)",
            "claimed_sort": "认领客资排序字段 (assign_time/operate_time)",
            "claimed_order": "认领客资排序方向 (asc/desc)",
            "favorite_sort": "收藏客资排序字段 (collected_time/operate_time)",
            "favorite_order": "收藏客资排序方向 (asc/desc)",
        }
        
        try:
            lines = []
            for section in self.config.sections():
                lines.append(f"[{section}]")
                for option in self.config.options(section):
                    val = self.config.get(section, option)
                    # 如果有对应的注释，则在其上方添加一行
                    if option in comments:
                        lines.append(f"# {comments[option]}")
                    lines.append(f"{option} = {val}")
                lines.append("") # 段落间空行
                
            try:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
            except OSError:
                # 安装目录不可写时，回退到用户目录并切换后续读写路径
                if getattr(sys, "frozen", False):
                    user_dir = self._user_config_dir()
                    os.makedirs(user_dir, exist_ok=True)
                    self.config_path = os.path.join(user_dir, "config.ini")
                    with open(self.config_path, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines))
                else:
                    raise
        except Exception as e:
            print(f"配置文件写入失败: {e}")

    def set_runtime(self, option, value):
        """通用运行时配置更新接口"""
        if not self.config.has_section("Runtime"):
            self.config.add_section("Runtime")
        self.config.set("Runtime", option, str(value))
        self._save_current_config()

    def set_customer_leads(self, option, value):
        """客资列表相关配置（排序等）。"""
        if not self.config.has_section("CustomerLeads"):
            self.config.add_section("CustomerLeads")
        self.config.set("CustomerLeads", option, str(value))
        self._save_current_config()

    @staticmethod
    def _normalize_sort_order(order: str, *, default: str = "asc") -> str:
        val = (order or "").strip().lower()
        return val if val in ("asc", "desc") else default

    @property
    def claimed_leads_sort(self) -> str:
        if not self.config.has_section("CustomerLeads"):
            return "assign_time"
        val = self.config.get("CustomerLeads", "claimed_sort", fallback="assign_time").strip()
        return val if val in ("assign_time", "operate_time") else "assign_time"

    @property
    def claimed_leads_order(self) -> str:
        if not self.config.has_section("CustomerLeads"):
            return "asc"
        return self._normalize_sort_order(
            self.config.get("CustomerLeads", "claimed_order", fallback="asc"),
            default="asc",
        )

    @property
    def favorite_leads_sort(self) -> str:
        if not self.config.has_section("CustomerLeads"):
            return "collected_time"
        val = self.config.get("CustomerLeads", "favorite_sort", fallback="collected_time").strip()
        return val if val in ("collected_time", "operate_time") else "collected_time"

    @property
    def favorite_leads_order(self) -> str:
        if not self.config.has_section("CustomerLeads"):
            return "desc"
        return self._normalize_sort_order(
            self.config.get("CustomerLeads", "favorite_order", fallback="desc"),
            default="desc",
        )

    def save_claimed_leads_sort(self, sort: str, order: str) -> None:
        sort_field = sort if sort in ("assign_time", "operate_time") else "assign_time"
        order_dir = self._normalize_sort_order(order, default="asc")
        self.set_customer_leads("claimed_sort", sort_field)
        self.set_customer_leads("claimed_order", order_dir)

    def save_favorite_leads_sort(self, sort: str, order: str) -> None:
        sort_field = sort if sort in ("collected_time", "operate_time") else "collected_time"
        order_dir = self._normalize_sort_order(order, default="desc")
        self.set_customer_leads("favorite_sort", sort_field)
        self.set_customer_leads("favorite_order", order_dir)

    def set_api_url(self, url: str) -> None:
        """持久化 API 基础地址（供服务器迁移等场景使用）。"""
        if not self.config.has_section("Network"):
            self.config.add_section("Network")
        self.config.set("Network", "api_url", normalize_api_url(url))
        self._save_current_config()

    @property
    def api_url_locked(self) -> bool:
        """为 true 时尊重 config.ini 中的 api_url，不做自动迁移。"""
        if not self.config.has_section("Network"):
            return False
        return self.config.get("Network", "api_url_lock", fallback="false").strip().lower() in (
            "1", "true", "yes", "on",
        )

    def _should_migrate_api_url(self, current: str) -> bool:
        if self.api_url_locked:
            return False
        if not current or current == CANONICAL_API_URL:
            return False
        return current in LEGACY_API_URLS

    def _migrate_legacy_api_url(self) -> None:
        """将旧服务器地址一次性迁移至当前权威地址。"""
        current = normalize_api_url(self.config.get("Network", "api_url", fallback=""))
        if self._should_migrate_api_url(current):
            self.set_api_url(CANONICAL_API_URL)
            print(f"已将 API 地址从 {current} 迁移至 {CANONICAL_API_URL}")

    @property
    def api_url(self):
        return normalize_api_url(self.config.get("Network", "api_url"))

    @property
    def timeout(self):
        return self.config.getint("Network", "timeout")

    @property
    def log_level(self):
        return self.config.get("Runtime", "log_level").upper()

    @property
    def sync_interval_min(self):
        return self.config.getint("Runtime", "sync_interval_min")

    @property
    def theme_mode(self):
        return self.config.get("Runtime", "theme_mode", fallback="light")

    @property
    def snap_enabled(self):
        return self.config.get("Runtime", "snap_enabled", fallback="false").lower() == "true"

    @property
    def snap_class(self):
        return self.config.get("Runtime", "snap_class", fallback="")

    @property
    def snap_title(self):
        return self.config.get("Runtime", "snap_title", fallback="")

    @property
    def ai_chat_model(self):
        return self.config.get("Runtime", "ai_chat_model", fallback="qwen3.5-plus")

    @property
    def chat_input_height(self):
        try:
            return self.config.getint("Runtime", "chat_input_height", fallback=140)
        except Exception:
            return 140

    @property
    def lite_mode(self) -> bool:
        """低配机优化：更小图片并发、更快缩放、关闭部分动画。"""
        raw = self.config.get("Runtime", "lite_mode", fallback="auto").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        return self._detect_low_end_machine()

    @staticmethod
    def _detect_low_end_machine() -> bool:
        import os
        try:
            cpus = int(os.cpu_count() or 2)
        except Exception:
            cpus = 2
        return cpus <= 4

# 全局单例
cfg = Config()
