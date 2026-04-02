import json
import os
from cryptography.fernet import Fernet

class SecureStorage:
    """
    负责桌面端本地数据的加密存储。
    实现了基于 user_id 的物理隔离，确保切换账号后数据互不干扰。
    """
    def __init__(self, user_id: str):
        self.user_id = str(user_id)
        # 缓存存放于运行目录下的 desktop_cache 文件夹
        self.cache_dir = os.path.join("desktop_cache", self.user_id)
        os.makedirs(self.cache_dir, exist_ok=True)
        
        self.key_file = "desktop_cache/secret.key"
        self._init_cipher()

    def _init_cipher(self):
        """初始化加密套件"""
        if not os.path.exists(self.key_file):
            os.makedirs("desktop_cache", exist_ok=True)
            key = Fernet.generate_key()
            with open(self.key_file, "wb") as f:
                f.write(key)
        else:
            with open(self.key_file, "rb") as f:
                key = f.read()
        self.fernet = Fernet(key)

    def save_json(self, filename: str, data: dict):
        """加密并保存 JSON 数据"""
        try:
            raw_data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            encrypted_data = self.fernet.encrypt(raw_data)
            filepath = os.path.join(self.cache_dir, f"{filename}.bin")
            with open(filepath, "wb") as f:
                f.write(encrypted_data)
        except Exception as e:
            print(f"写入本地缓存失败: {e}")

    def load_json(self, filename: str) -> dict:
        """读取并解密本地缓存内容"""
        filepath = os.path.join(self.cache_dir, f"{filename}.bin")
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "rb") as f:
                encrypted_data = f.read()
            decrypted_data = self.fernet.decrypt(encrypted_data)
            return json.loads(decrypted_data.decode("utf-8"))
        except Exception as e:
            print(f"读取本地缓存失败 (可能密钥不匹配或文件损坏): {e}")
            return None

    def clear_cache(self):
        """清空当前账户的本地缓存，用于退出登录"""
        import shutil
        if os.path.exists(self.cache_dir):
            shutil.rmtree(self.cache_dir)
            os.makedirs(self.cache_dir, exist_ok=True)
