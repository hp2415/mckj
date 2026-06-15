import json
import os
import sqlite3
import threading
from cryptography.fernet import Fernet
from logger_cfg import logger

CUSTOMERS_LIST_CACHE_KEY = "customers_list_v1"
TODAY_TASK_KEYS_CACHE_KEY = "today_task_keys_v1"


class SecureStorage:
    """
    负责桌面端本地数据的加密存储，基于 SQLite 实现。
    """
    def __init__(self, user_id: str):
        self.user_id = str(user_id)
        # 1. 确定存储基准目录：始终位于 .exe 同级目录下
        import sys
        if getattr(sys, 'frozen', False):
            self.base_dir = os.path.dirname(sys.executable)
        else:
            self.base_dir = os.path.dirname(os.path.abspath(__file__))

        # 数据库存放路径
        self.cache_root = os.path.join(self.base_dir, "desktop_cache")
        self.user_dir = os.path.join(self.cache_root, self.user_id)
        os.makedirs(self.user_dir, exist_ok=True)

        self.db_path = os.path.join(self.user_dir, "local_cache.db")
        self.key_file = os.path.join(self.cache_root, "secret.key")

        self._db_lock = threading.Lock()
        self._conn = None

        self._init_cipher()
        self._init_db()

    def _init_cipher(self):
        """初始化 Fernet 加密套件"""
        if not os.path.exists(self.key_file):
            os.makedirs(self.cache_root, exist_ok=True)
            key = Fernet.generate_key()
            with open(self.key_file, "wb") as f:
                f.write(key)
        else:
            with open(self.key_file, "rb") as f:
                key = f.read()
        self.fernet = Fernet(key)

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _init_db(self):
        """初始化 SQLite 数据库表架构"""
        with self._db_lock:
            conn = self._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS secure_kv (
                    key_ TEXT PRIMARY KEY,
                    value_ BLOB,
                    type_ TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def save_json(self, key: str, data: dict):
        """加密并保存 JSON 数据"""
        try:
            raw_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
            encrypted = self.fernet.encrypt(raw_bytes)
            self._write_db(key, encrypted, "json")
        except Exception as e:
            logger.error(f"写入 JSON 缓存失败: {e}")

    def load_json(self, key: str) -> dict:
        """读取并解密 JSON 数据"""
        encrypted = self._read_db(key)
        if not encrypted:
            return None
        try:
            decrypted = self.fernet.decrypt(encrypted)
            return json.loads(decrypted.decode("utf-8"))
        except Exception:
            return None

    def save_json_list(self, key: str, data: list):
        """加密并保存 JSON 列表（包装为 dict 以复用 save_json 管线）。"""
        self.save_json(key, {"items": list(data or [])})

    def load_json_list(self, key: str) -> list | None:
        """读取并解密 JSON 列表。"""
        blob = self.load_json(key)
        if not blob:
            return None
        items = blob.get("items")
        return items if isinstance(items, list) else None

    def save_data(self, key: str, data: bytes):
        """加密并保存原始二进制 (用于图片缓存)"""
        try:
            encrypted = self.fernet.encrypt(data)
            self._write_db(key, encrypted, "blob")
        except Exception as e:
            logger.error(f"写入图片缓存失败: {e}")

    def load_data(self, key: str) -> bytes:
        """读取并解密二进制原始数据"""
        encrypted = self._read_db(key)
        if not encrypted:
            return None
        try:
            return self.fernet.decrypt(encrypted)
        except Exception:
            return None

    def _write_db(self, key: str, value: bytes, category: str):
        with self._db_lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO secure_kv (key_, value_, type_, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (key, value, category),
            )
            conn.commit()

    def _read_db(self, key: str) -> bytes:
        with self._db_lock:
            conn = self._get_conn()
            cursor = conn.execute("SELECT value_ FROM secure_kv WHERE key_ = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None

    def close(self):
        """关闭持久化连接（注销时调用）。"""
        with self._db_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def clear_cache(self):
        """物理清除当前用户的数据库文件"""
        with self._db_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self._init_db()
