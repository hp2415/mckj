import json
import os
import sqlite3
from cryptography.fernet import Fernet

class SecureStorage:
    """
    负责桌面端本地数据的加密存储，基于 SQLite 实现。
    """
    def __init__(self, user_id: str):
        self.user_id = str(user_id)
        # 数据库存放路径
        self.cache_root = "desktop_cache"
        self.user_dir = os.path.join(self.cache_root, self.user_id)
        os.makedirs(self.user_dir, exist_ok=True)
        
        self.db_path = os.path.join(self.user_dir, "local_cache.db")
        self.key_file = os.path.join(self.cache_root, "secret.key")
        
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

    def _init_db(self):
        """初始化 SQLite 数据库表架构"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        # key_: 键名, value_: 加密后的二进制数据, type_: 数据类型(json/blob)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS secure_kv (
                key_ TEXT PRIMARY KEY,
                value_ BLOB,
                type_ TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def save_json(self, key: str, data: dict):
        """加密并保存 JSON 数据"""
        try:
            raw_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
            encrypted = self.fernet.encrypt(raw_bytes)
            self._write_db(key, encrypted, "json")
        except Exception as e:
            print(f"写入 JSON 缓存失败: {e}")

    def load_json(self, key: str) -> dict:
        """读取并解密 JSON 数据"""
        encrypted = self._read_db(key)
        if not encrypted: return None
        try:
            decrypted = self.fernet.decrypt(encrypted)
            return json.loads(decrypted.decode("utf-8"))
        except Exception:
            return None

    def save_data(self, key: str, data: bytes):
        """加密并保存原始二进制 (用于图片缓存)"""
        try:
            encrypted = self.fernet.encrypt(data)
            self._write_db(key, encrypted, "blob")
        except Exception as e:
            print(f"写入图片缓存失败: {e}")

    def load_data(self, key: str) -> bytes:
        """读取并解密二进制原始数据"""
        encrypted = self._read_db(key)
        if not encrypted: return None
        try:
            return self.fernet.decrypt(encrypted)
        except Exception:
            return None

    def _write_db(self, key: str, value: bytes, category: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO secure_kv (key_, value_, type_, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (key, value, category)
        )
        conn.commit()
        conn.close()

    def _read_db(self, key: str) -> bytes:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT value_ FROM secure_kv WHERE key_ = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def clear_cache(self):
        """物理清除当前用户的数据库文件"""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
            self._init_db()
