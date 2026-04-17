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

    def _save_current_config(self):
        """将当前内存中的配置对象持久化到磁盘 config.ini"""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                self.config.write(f)
            print(f"配置文件已自动生成: {self.config_path}")
        except Exception as e:
            print(f"自动生成配置文件失败: {e}")

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

# 全局单例
cfg = Config()
