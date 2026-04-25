import os
import sys
import configparser

class Config:
    """
    配置管理类：从外部 config.ini 读取设置。
    """
    def __init__(self):
        self.config = configparser.ConfigParser()
        # 确定配置文件路径：始终在 .exe 或 main.py 同级目录
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))
            
        self.config_path = os.path.join(base_path, "config.ini")
        self._load_defaults()
        
        if os.path.exists(self.config_path):
            try:
                self.config.read(self.config_path, encoding="utf-8")
            except Exception as e:
                print(f"配置文件解析错误: {e}")
        else:
            # 核心改进：如果配置不存在，则自动通过默认值生成一份到磁盘
            self._save_current_config()

    def _load_defaults(self):
        """设置容错默认值"""
        if not self.config.has_section("Network"):
            self.config.add_section("Network")
        self.config.set("Network", "api_url", "http://192.168.0.125:8000")
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
        # 是否“固定”本机模型偏好：false 表示允许后端下发的默认值覆盖本机默认
        self.config.set("Runtime", "ai_chat_model_pinned", "false")

    def _save_current_config(self):
        """将当前内存中的配置对象持久化到磁盘 config.ini，并保留/自动生成注释"""
        # 定义字段注释（中文说明）
        comments = {
            "api_url": "后端 API 接口基础地址",
            "timeout": "网络请求超时时间 (秒)",
            "log_level": "日志记录级别 (DEBUG, INFO, WARNING, ERROR)",
            "sync_interval_min": "云端数据自动同步间隔 (分钟)",
            "theme_mode": "主题模式 (light 为浅色，dark 为深色)",
            "snap_enabled": "窗口吸附功能开关 (true/false)",
            "snap_class": "吸附目标窗口的类名 (校准后自动填充)",
            "snap_title": "吸附目标窗口的标题 (校准后自动填充)",
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
                
            with open(self.config_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:
            print(f"配置文件写入失败: {e}")

    def set_runtime(self, option, value):
        """通用运行时配置更新接口"""
        if not self.config.has_section("Runtime"):
            self.config.add_section("Runtime")
        self.config.set("Runtime", option, str(value))
        self._save_current_config()

    @property
    def api_url(self):
        return self.config.get("Network", "api_url").rstrip("/")

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
    def ai_chat_model_pinned(self):
        return self.config.get("Runtime", "ai_chat_model_pinned", fallback="false").lower() == "true"

# 全局单例
cfg = Config()
